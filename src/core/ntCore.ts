/**
 * NT Core - 4.x NT database access layer
 *
 * NT databases (xwechat_files/* /db_storage/message/message_0.db) use
 * SQLCipher 4 with plaintext_header_size=0. Decryption requires the
 * sqlcipher3 Python package. This module wraps scripts/nt_decrypt.py.
 */
import { execFile } from 'child_process'
import { promisify } from 'util'
import { existsSync, openSync, readSync, closeSync } from 'fs'
import { join } from 'path'
import crypto from 'crypto'
import { configService } from '../services/configService.js'
import { getPythonCommand } from '../utils/python.js'
import { createPythonProcessEnv, safeSubprocessError } from '../utils/pythonProcessEnv.js'
import { resolvePackageRoot } from '../utils/packageRoot.js'
import type { ChatSession, Message, Contact } from '../types.js'

const execFileAsync = promisify(execFile)
function getPackageRoot(): string {
  return resolvePackageRoot(import.meta.url)
}

/**
 * `scan` 的执行器。默认就是真的 `execFile`；测试注入假执行器来钉住**错误映射**。
 *
 * 为什么需要这个口子：Windows 上"找不到命令"的表现是子进程以 **9009** 退出，
 * 而它没有可移植的造假方式（要有真 exe / 真 shell 行为，Linux CI 上做不出来）。
 * 少了这条测试，`9009` 那段用户提示就会在以后的改动里悄悄退回
 * `NT scan failed (exit 9009)` —— 2026-10-18 那位用户卡住时看到的就是这句。
 */
export type NtExecRunner =
  (file: string, args: string[], options: Record<string, unknown>) => Promise<{ stdout: string }>

export interface NtResult {
  success: boolean
  error?: string
}

export interface NtSessionsResult extends NtResult {
  sessions?: ChatSession[]
}

/** One shard's outcome, as reported by `nt_decrypt.py --report-shards`. */
export interface ShardOutcome {
  /** basename only - the child never reports a full path */
  name: string
  opened: boolean
  /** null when the shard could not be read at all */
  hasTalkerTable: boolean | null
  rowsForTalker: number | null
  /**
   * null | KEY_REJECTED | OPEN_FAILED | READ_FAILED | SCHEMA_MISMATCH |
   * WINDOW_UNAVAILABLE
   */
  reason: string | null
  /**
   * Columns the reader needed and this shard did not have. Reported on a shard
   * that was still read, so a non-empty list with a null `reason` means the
   * read succeeded but lost fields - which a caller checking coverage has to
   * be able to see.
   */
  missingColumns?: string[]
}

export interface ShardReport {
  scanned: number
  opened: number
  failed: number
  items: ShardOutcome[]
}

export interface NtMessagesResult extends NtResult {
  messages?: Message[]
  shards?: ShardReport
}

export interface NtContactsResult extends NtResult {
  contacts?: Contact[]
}

export interface NtScanResult {
  success: boolean
  error?: string
  keys?: Array<{ key: string; salt: string }>
  databases?: Array<{ path: string; name: string; salt: string; size: number; wxid: string }>
  matched?: Array<{ path: string; name: string; salt: string; size: number; wxid: string; key: string }>
}

interface NtPythonSecrets {
  dbPath?: string
  key?: string
  salt?: string
  contactDbPath?: string | null
  contactKey?: string | null
  contactSalt?: string | null
  talker?: string
  ownWxid?: string
  keyword?: string
  usernames?: string
}

export class NtCore {
  private dbPath: string
  private keyHex: string
  private saltHex: string
  /** contact.db 路径 (用于显示备注名/昵称) */
  contactDbPath: string | null = null
  /** contact.db 解密密钥 */
  contactKey: string | null = null
  /** contact.db 盐值 */
  contactSalt: string | null = null

  constructor(dbPath: string, keyHex: string, saltHex: string) {
    this.dbPath = dbPath
    this.keyHex = keyHex
    this.saltHex = saltHex
  }

  /** 尝试自动发现并连接 contact.db */
  autoDetectContactDb(): boolean {
    // 从 message_0.db 路径推导 contact.db
    // message_0.db:  <xwechat>/<wxid>/db_storage/message/message_0.db
    // contact.db:    <xwechat>/<wxid>/db_storage/contact/contact.db
    const msgDir = this.dbPath.replace(/\\/g, '/')
    const parts = msgDir.split('/')
    // 找到 db_storage 位置
    const dbStorageIdx = parts.lastIndexOf('db_storage')
    if (dbStorageIdx < 0) return false

    const wxidDir = parts.slice(0, dbStorageIdx).join('/')
    const contactDbPath = `${wxidDir}/db_storage/contact/contact.db`

    if (existsSync(contactDbPath)) {
      this.contactDbPath = contactDbPath
      return true
    }
    return false
  }

  private get scriptPath(): string {
    return join(getPackageRoot(), 'scripts', 'nt_decrypt.py')
  }

  private async callPython(args: string[], secrets: NtPythonSecrets = {}): Promise<any> {
    const values: NtPythonSecrets = {
      dbPath: this.dbPath,
      key: this.keyHex,
      salt: this.saltHex,
      contactDbPath: this.contactDbPath,
      contactKey: this.contactKey,
      contactSalt: this.contactSalt,
      ...secrets,
    }
    try {
      const { stdout } = await execFileAsync(getPythonCommand(), [this.scriptPath, ...args], {
        timeout: 120_000,
        maxBuffer: 50 * 1024 * 1024,
        env: createPythonProcessEnv({
          WEFLOW_DB_PATH: values.dbPath,
          WEFLOW_NT_KEY: values.key,
          WEFLOW_NT_SALT: values.salt,
          // WeChat rolls conversations into new message_*.db shards, each with
          // a key PBKDF2-derived from this shared passphrase. Without it only
          // shard 0 opens, and every read silently stops at its last write.
          WEFLOW_NT_PASSPHRASE: (configService.get('favPassphrase') || configService.get('decryptKey') || '') || undefined,
          WEFLOW_CONTACT_DB: values.contactDbPath || undefined,
          WEFLOW_CONTACT_KEY: values.contactKey || undefined,
          WEFLOW_CONTACT_SALT: values.contactSalt || undefined,
          WEFLOW_TALKER: values.talker,
          WEFLOW_OWN_WXID: values.ownWxid,
          WEFLOW_QUERY_KEYWORD: values.keyword,
          WEFLOW_QUERY_USERNAMES: values.usernames,
        }),
        encoding: 'utf-8',
      })

      const lines = stdout.split('\n').filter((l: string) => l.trim())
      for (let i = lines.length - 1; i >= 0; i--) {
        const trimmed = lines[i].trim()
        if (trimmed.startsWith('{"')) {
          try {
            return JSON.parse(trimmed)
          } catch {
            // Try next line
          }
        }
      }
      return { error: 'No valid JSON output from Python script' }
    } catch (e: any) {
      if (e?.code === 'ENOENT') {
        return { error: 'Python not found. Install Python and sqlcipher3: pip install sqlcipher3' }
      }
      if (e?.code === 'ETIMEDOUT' || e?.killed || e?.signal) {
        return { error: 'NT database query timed out' }
      }
      return { error: safeSubprocessError(e, 'NT database query failed') }
    }
  }

  /**
   * Scan memory for NT keys and match to databases.
   * Requires WeChat to be running.
   */
  static async scan(root?: string, run: NtExecRunner = execFileAsync as unknown as NtExecRunner): Promise<NtScanResult> {
    try {
      const scriptPath = join(getPackageRoot(), 'scripts', 'nt_decrypt.py')
      const scanArgs = ['scan', '--json']
      const { stdout } = await run(getPythonCommand(), [scriptPath, ...scanArgs], {
        timeout: 300_000,
        maxBuffer: 10 * 1024 * 1024,
        env: createPythonProcessEnv(root ? { WEFLOW_SCAN_ROOT: root } : {}),
        encoding: 'utf-8',
      })

      const lines = stdout.split('\n').filter((l: string) => l.trim())
      for (let i = lines.length - 1; i >= 0; i--) {
        const trimmed = lines[i].trim()
        if (trimmed.startsWith('{"')) {
          let result: any
          try { result = JSON.parse(trimmed) } catch { continue }
          if (result.error) {
            // Keep whatever the scan discovered alongside the failure. The
            // database file list is independent of the process, and callers
            // derive keys from the passphrase when the memory scan finds
            // nothing - which needs exactly that list. Dropping it here left
            // the derivation step with nothing to work on, so a run that had
            // already captured the passphrase still ended with no keys.
            return {
              success: false,
              error: result.error,
              keys: result.keys || [],
              databases: result.databases || [],
              matched: result.matched || [],
            }
          }
          return {
            success: true,
            keys: result.keys || [],
            databases: result.databases || [],
            matched: result.matched || [],
          }
        }
      }
      return { success: false, error: 'Scan produced no valid output' }
    } catch (e: any) {
      if (e?.code === 'ENOENT') {
        return { success: false, error: 'Python not found. Install Python and add to PATH.' }
      }
      if (e?.code === 9009) {
        // Windows 的 9009 是"找不到命令"。最常见的原因是 PATH 上只有**微软商店的 python
        // 占位程序**：`getPythonCommand()` 在所有候选都不过（没有真 Python / 没有 sqlcipher3）
        // 时兜底返回裸名 `python`，正好交给那个占位程序，它一跑就以 9009 退出。
        // 不把这条翻译出来，用户看到的是"NT scan failed (exit 9009)"，而下一步该做什么完全没有线索
        // （2026-10-18 一位用户就是这么卡住的）。
        return {
          success: false,
          error: '没有可用的 Python：NT 扫描需要 Python 3.10+ 且能 `import sqlcipher3`。'
            + 'exit 9009 在 Windows 上通常意味着只装了商店的 python 占位程序，装一个真正的 Python 后重试',
        }
      }
      return { success: false, error: safeSubprocessError(e, 'NT scan failed') }
    }
  }

  /**
   * 验证 key+salt 能否真实打开数据库 (读取 sqlite_master 触发解密)。
   * cannotVerify=true 表示环境问题 (Python/sqlcipher3 缺失) 而非密钥错误。
   */
  static async verifyDbKey(dbPath: string, keyHex: string, saltHex: string): Promise<{
    success: boolean
    error?: string
    tables?: number
    cannotVerify?: boolean
  }> {
    const scriptPath = join(getPackageRoot(), 'scripts', 'nt_decrypt.py')
    let stdout = ''
    try {
      const r = await execFileAsync(getPythonCommand(), [scriptPath, 'verify'], {
        timeout: 60_000,
        maxBuffer: 1024 * 1024,
        env: createPythonProcessEnv({
          WEFLOW_DB_PATH: dbPath,
          WEFLOW_NT_KEY: keyHex,
          WEFLOW_NT_SALT: saltHex,
        }),
        encoding: 'utf-8',
      })
      stdout = r.stdout
    } catch (e: any) {
      // 缺 sqlcipher3 等场景: 脚本打印 JSON 后 exit 1
      stdout = e?.stdout || ''
      if (!stdout.trim()) {
        return { success: false, cannotVerify: true, error: safeSubprocessError(e, 'Python 调用失败') }
      }
    }
    const lines = stdout.split('\n').filter((l: string) => l.trim())
    for (let i = lines.length - 1; i >= 0; i--) {
      const trimmed = lines[i].trim()
      if (trimmed.startsWith('{"')) {
        try {
          const r = JSON.parse(trimmed)
          if (r.success === undefined) {
            // 只有 error 字段 = 环境问题 (如 "需要 sqlcipher3")
            return { success: false, cannotVerify: true, error: r.error }
          }
          return { success: !!r.success, tables: r.tables, error: r.error }
        } catch { /* try next line */ }
      }
    }
    return { success: false, cannotVerify: true, error: 'verify 无有效输出' }
  }

  async getSessions(keyword?: string): Promise<NtSessionsResult> {
    const args: string[] = ['sessions']
    const result = await this.callPython(args, { keyword })
    if (result.error) {
      return { success: false, error: result.error }
    }
    return { success: true, sessions: result.sessions || [] }
  }

  async getMessages(talker: string, limit = 100, offset = 0): Promise<NtMessagesResult> {
    const args: string[] = [
      'messages',
      '--limit', String(limit),
      '--offset', String(offset),
    ]
    // Pass own wxid from config for self-message detection
    const ownWxid = configService.get('wxid')
    const result = await this.callPython(args, { talker, ownWxid })
    if (result.error) {
      return { success: false, error: result.error }
    }
    return { success: true, messages: result.messages || [] }
  }

  /**
   * `getMessages` plus the per-shard read report.
   *
   * Separate method rather than a flag on `getMessages`: the extra argv token
   * changes what the child returns, and every existing caller is better off
   * with the smaller payload it already parses.
   *
   * `fromTime` is pushed into SQL so a resumed read does not fetch the whole
   * conversation only to discard it. It is an optimisation and nothing more:
   * callers must still filter by window themselves, because the child refuses
   * the window outright (WINDOW_UNAVAILABLE) on a shard that cannot express
   * it rather than approximating it. Only the lower bound is worth pushing -
   * the upper bound is normally "now", above which nothing exists to skip.
   */
  async getMessagesWithShards(
    talker: string,
    limit = 100,
    offset = 0,
    fromTime?: number | null
  ): Promise<NtMessagesResult> {
    const args: string[] = [
      'messages',
      '--limit', String(limit),
      '--offset', String(offset),
      '--report-shards',
    ]
    if (typeof fromTime === 'number' && Number.isFinite(fromTime)) {
      args.push('--from', String(fromTime))
    }
    const ownWxid = configService.get('wxid')
    const result = await this.callPython(args, { talker, ownWxid })
    if (result.error) {
      return { success: false, error: result.error }
    }
    return { success: true, messages: result.messages || [], shards: result.shards }
  }

  async getContacts(keyword?: string, limit = 200): Promise<NtContactsResult> {
    const args: string[] = [
      'contacts',
      '--limit', String(limit),
    ]
    const result = await this.callPython(args, { keyword })
    if (result.error) {
      return { success: false, error: result.error }
    }
    return { success: true, contacts: result.contacts || [] }
  }

  // ====== SNS (朋友圈) ======

  /** 查询 SNS 朋友圈时间线 */
  async getSnsTimeline(snsDbPath: string, snsKey: string, snsSalt: string, opts: {
    limit?: number; offset?: number; usernames?: string[];
    keyword?: string; startTime?: number; endTime?: number;
  } = {}): Promise<{ success: boolean; timeline?: any[]; error?: string }> {
    const args: string[] = [
      'sns-timeline',
      '--limit', String(opts.limit ?? 20),
      '--offset', String(opts.offset ?? 0),
    ]
    if (opts.startTime) args.push('--start-time', String(opts.startTime))
    if (opts.endTime) args.push('--end-time', String(opts.endTime))
    const result = await this.callPython(args, {
      dbPath: snsDbPath,
      key: snsKey,
      salt: snsSalt,
      keyword: opts.keyword,
      usernames: opts.usernames?.length ? JSON.stringify(opts.usernames) : undefined,
    })
    if (result.error) return { success: false, error: result.error }
    return { success: true, timeline: result.timeline || [] }
  }

  /** 获取朋友圈中有动态的用户列表 */
  async getSnsUsernames(snsDbPath: string, snsKey: string, snsSalt: string): Promise<{ success: boolean; usernames?: string[]; error?: string }> {
    const result = await this.callPython(['sns-usernames'], { dbPath: snsDbPath, key: snsKey, salt: snsSalt })
    if (result.error) return { success: false, error: result.error }
    return { success: true, usernames: result.usernames || [] }
  }

  /** 获取朋友圈统计信息 */
  async getSnsExportStats(snsDbPath: string, snsKey: string, snsSalt: string, myWxid?: string): Promise<{ success: boolean; data?: { totalPosts: number; totalFriends: number; myPosts: number | null }; error?: string }> {
    const result = await this.callPython(['sns-stats'], { dbPath: snsDbPath, key: snsKey, salt: snsSalt, ownWxid: myWxid })
    if (result.error) return { success: false, error: result.error }
    return { success: true, data: result.data }
  }

  // ====== 收藏 (favorite.db) ======

  /**
   * 从全库共用 passphrase 派生某库的 raw key。
   * 微信 4.x 各库密钥 = PBKDF2-HMAC-SHA512(passphrase, 库salt, 256000轮, 32字节)
   */
  static deriveRawKey(passphraseHex: string, saltHex: string): string {
    const passphrase = Buffer.from(passphraseHex, 'hex')
    const salt = Buffer.from(saltHex, 'hex')
    return crypto.pbkdf2Sync(passphrase, salt, 256000, 32, 'sha512').toString('hex')
  }

  /** 读取数据库文件前 16 字节盐值 */
  static readDbSalt(dbPath: string): string | null {
    let fd: number
    try {
      fd = openSync(dbPath, 'r')
    } catch {
      return null
    }
    try {
      const buf = Buffer.alloc(16)
      const n = readSync(fd, buf, 0, 16, 0)
      return n === 16 ? buf.toString('hex') : null
    } catch {
      return null
    } finally {
      closeSync(fd)
    }
  }

  /** 查询收藏列表 (keyHex 为派生后的 raw key) */
  async getFavorites(favDbPath: string, keyHex: string, opts: {
    limit?: number; offset?: number; keyword?: string; favType?: number;
  } = {}): Promise<{ success: boolean; favorites?: any[]; total?: number; error?: string }> {
    const args: string[] = [
      'fav-list',
      '--limit', String(opts.limit ?? 100),
      '--offset', String(opts.offset ?? 0),
    ]
    if (opts.favType != null) args.push('--type', String(opts.favType))
    const result = await this.callPython(args, { dbPath: favDbPath, key: keyHex, salt: undefined, keyword: opts.keyword })
    if (result.error) return { success: false, error: result.error }
    return { success: true, favorites: result.favorites || [], total: result.total ?? 0 }
  }
}
