import { join, basename, dirname } from 'path'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'fs'
import { execFile } from 'child_process'
import { promisify } from 'util'
import { chatService } from './chatService.js'
import { configService } from './configService.js'
import { getPythonCommand } from '../utils/python.js'
import { createPythonProcessEnv, safeSubprocessError } from '../utils/pythonProcessEnv.js'
import type { Message } from '../types.js'
import { createWeFlowEnvelope } from './messageContract.js'
import { parseLocalDateOrIso } from '../utils/dateRange.js'
import { resolvePackageRoot } from '../utils/packageRoot.js'

export type ExportContract = 'raw' | 'weflow-v1'
export interface ExportResult { success: boolean; path?: string; count?: number; error?: string }

const execFileAsync = promisify(execFile)

/**
 * md 导出落盘的媒体清单：`<output>/<talker>_assets/manifest.json`（`local_id` → 相对路径）。
 *
 * 这是"其余格式也能带图"的那一半：**md 导出抽取媒体，json/txt/excel 消费同一份清单** —— 匹配逻辑
 * （缓存缩略图、资源 md5、原图那档）只存在于 Python 那条路上，在这里重写一遍必然与它漂移。
 * 读不到就返回空表：那说明还没跑过 md 导出，行为与从前一致。
 */
/**
 * 从清单里取这条消息的媒体相对路径。
 *
 * 键是 `local_id:create_time`：**local_id 会在分片之间重复**（同一会话的表可能存在于多个分片，
 * 各片自己编号），只按 local_id 查会让 json/txt/excel 引用到别的消息的图。两条消息硬要同 id 同秒
 * 才算撞上，那个概率可以忽略；回退查裸 local_id 是为了兼容更早写出的清单。
 */
export function mediaFor(manifest: Record<string, string>, message: { localId?: unknown; createTime?: unknown }): string | undefined {
  const id = String(message.localId)
  return manifest[`${id}:${message.createTime}`] ?? manifest[id]
}

export function readMediaManifest(outputDir: string, talker: string): Record<string, string> {
  try {
    const file = join(outputDir, `${talker}_assets`, 'manifest.json')
    if (!existsSync(file)) return {}
    const data = JSON.parse(readFileSync(file, 'utf8'))
    const map = data && data.messages
    return map && typeof map === 'object' ? map : {}
  } catch {
    return {}
  }
}
function getPackageRoot(): string {
  return resolvePackageRoot(import.meta.url)
}

export class ExportService {
  async exportJson(talker: string, outputDir: string, limit = 0, from?: number, to?: number, contract: ExportContract = 'raw'): Promise<ExportResult> {
    try {
      const messages = await chatService.getMessagesInRange(talker, limit, from, to)
      if (messages.length === 0) {
        return { success: false, error: '未找到消息' }
      }

      const dir = this.ensureOutputDir(outputDir)
      const filePath = join(dir, `${talker}_messages.json`)
      // 有清单就带上媒体相对路径（清单由 markdown 导出产生，见 readMediaManifest）
      const mediaMap = readMediaManifest(dir, talker)
      const withMedia = Object.keys(mediaMap).length
        ? messages.map(m => {
          const media = mediaFor(mediaMap, m)
          return media ? { ...m, media } : m
        })
        : messages
      const payload = contract === 'weflow-v1'
        ? createWeFlowEnvelope(withMedia, new Date().toISOString(), {
          requestedFrom: from,
          requestedTo: to,
          requestedLimit: limit,
        })
        : withMedia
      writeFileSync(filePath, JSON.stringify(payload, null, 2), 'utf8')
      return { success: true, path: filePath, count: messages.length }
    } catch {
      return { success: false, error: 'JSON 导出失败' }
    }
  }

  async exportTxt(talker: string, outputDir: string, limit = 0, from?: number, to?: number): Promise<ExportResult> {
    try {
      const messages = await chatService.getMessagesInRange(talker, limit, from, to)
      if (messages.length === 0) {
        return { success: false, error: '未找到消息' }
      }

      const mediaMap = readMediaManifest(this.ensureOutputDir(outputDir), talker)
      const lines = messages.map(m => {
        const time = new Date(m.createTime * 1000).toLocaleString('zh-CN')
        const sender = m.isSend ? '我' : (m.senderUsername || talker)
        // 有清单就把 `[图片]` 换成 `[图片: <相对路径>]`，没有就保持原样
        const media = mediaFor(mediaMap, m)
        const content = media ? `[图片: ${media}]` : (m.parsedContent || m.rawContent || '')
        return `[${time}] ${sender}: ${content}`
      })

      const dir = this.ensureOutputDir(outputDir)
      const filePath = join(dir, `${talker}_messages.txt`)
      writeFileSync(filePath, lines.join('\n'), 'utf8')
      return { success: true, path: filePath, count: messages.length }
    } catch {
      return { success: false, error: '文本导出失败' }
    }
  }

  async exportHtml(talker: string, outputDir: string, limit = 0, date = '', from?: number, to?: number, quiet = false, options: { fullImages?: boolean; format?: 'html' | 'md'; assetsOnly?: boolean } = {}): Promise<ExportResult> {
    const format = options.format ?? 'html'
    if (format === 'md' && (from !== undefined || to !== undefined)) {
      // 不静默降级成「导出全部」：范围是调用方明确要的，做不到就说出来。
      return { success: false, error: 'markdown 导出暂不支持 --from/--to：请用 --date，或整段导出' }
    }
    try {
      if (from !== undefined || to !== undefined) {
        return this.exportHtmlBasic(talker, outputDir, limit, from, to)
      }
      // Use Python export_chat_html.py for rich HTML with images
      const cfg = configService.getAll()
      const db = cfg.ntDbPath
      const key = cfg.ntKey
      const salt = cfg.ntSalt
      // Derives each message shard's key (WeChat 4.1.12.26+). favPassphrase is
      // the dedicated field, but decryptKey holds the same value and is what
      // `init` writes - without this fallback the shard merge silently stays
      // dormant and exports stop at the first shard's last message.
      const passphrase = configService.get('favPassphrase') || cfg.decryptKey || ''

      if (!db || !key || !salt) {
        if (format === 'md') {
          return { success: false, error: 'markdown 导出需要已配置的数据库与密钥（这条路没有简版回退）' }
        }
        // Fallback: basic HTML
        let fallbackFrom: number | undefined = from
        let fallbackTo: number | undefined = to
        if (date && fallbackFrom === undefined && fallbackTo === undefined) {
          fallbackFrom = parseLocalDateOrIso(date)
          fallbackTo = parseLocalDateOrIso(date, true)
        }
        return this.exportHtmlBasic(talker, outputDir, limit, fallbackFrom, fallbackTo)
      }

      // Resolve Python script path (dist/src/services → package root)
      const script = join(getPackageRoot(), 'scripts', 'export_chat_html.py')

      // Cache dir for image thumbnails
      const cacheDir = cfg.ntDbPath
        ? join(dirname(dirname(dirname(cfg.ntDbPath))), 'cache')
        : ''
      const accountDir = cfg.ntDbPath
        ? dirname(dirname(dirname(cfg.ntDbPath)))
        : ''

      // Resolve display name for filename (prefer remark/nickname over wxid)
      let displayName = ''
      try {
        const sessions = await chatService.listSessions()
        const match = sessions.find(s => s.username === talker)
        if (match?.displayName && match.displayName !== talker) {
          displayName = match.displayName
        }
      } catch {}

      const args: string[] = [script]
      if (format === 'md') {
        // 图片落盘 + 清单。资产目录按 **talker** 命名，好让 json/txt/excel 导出能确定性找到它
        // （它们的文件前缀是 talker，而 md 文件名用的是显示名，不显式指定就对不上）。
        args.push('--format', 'md', '--assets-dir', join(outputDir || './output', `${talker}_assets`))
        if (options.assetsOnly) args.push('--assets-only')
      } else {
        // Paginate: a whole year of chat in one file makes the browser crawl.
        args.push('--per-page', '100')
      }
      const ownWxid = String(cfg.wxid || '').trim()
      const exportCacheDir = cacheDir && existsSync(cacheDir) ? cacheDir : undefined
      const exportAccountDir = accountDir && existsSync(accountDir) ? accountDir : undefined
      if (!quiet) console.log(`  Exporting HTML via Python...`)
      const { stdout } = await execFileAsync(getPythonCommand(), args, {
        // Headroom for the opt-in --full-images path, which resizes hundreds
        // of multi-MB originals and can take several minutes.
        timeout: 900_000,
        maxBuffer: 50 * 1024 * 1024,
        env: createPythonProcessEnv({
          WEFLOW_DB_PATH: db,
          WEFLOW_NT_KEY: key,
          WEFLOW_NT_SALT: salt,
          WEFLOW_TALKER: talker,
          WEFLOW_OWN_WXID: ownWxid || undefined,
          WEFLOW_NT_PASSPHRASE: passphrase || undefined,
          WEFLOW_EXPORT_NAME: displayName || undefined,
          WEFLOW_EXPORT_OUTPUT: outputDir || './output',
          WEFLOW_EXPORT_CACHE_DIR: exportCacheDir,
          WEFLOW_EXPORT_ACCOUNT_DIR: exportAccountDir,
          // Lets custom stickers be decrypted from WeChat's local cache.
          WEFLOW_EMOTICON_SEED: String(cfg.emoticonSeed || '') || undefined,
          // Group rows identify their sender only by wxid; the contact
          // database is what turns that into a name.
          WEFLOW_CONTACT_DB_PATH: cfg.contactDbPath || undefined,
          WEFLOW_CONTACT_KEY: cfg.contactKey || undefined,
          WEFLOW_CONTACT_SALT: cfg.contactSalt || undefined,
          // Off by default: originals are 5-24MB each and downscaling a few
          // hundred of them costs minutes in PIL.
          WEFLOW_FULL_IMAGES: options.fullImages ? '1' : undefined,
          WEFLOW_EXPORT_DATE: date || undefined,
          // Without this the Python path exported the entire history: `--limit`
          // only ever reached the fallback renderer, so a capped export of a
          // busy group silently produced hundreds of files.
          WEFLOW_EXPORT_LIMIT: limit > 0 ? String(limit) : undefined,
        }),
      })

      // Surface Python's progress and diagnostics. Only the trailing JSON
      // summary is machine-read; everything else used to be discarded, so a
      // multi-minute export showed nothing between "Exporting..." and the
      // result - and the sticker seed hint never reached the user at all.
      if (!quiet) {
        for (const raw of stdout.split('\n')) {
          const line = raw.trim()
          if (!line || line.startsWith('{') || line.startsWith('Total messages:')) continue
          if (/^Fetched \d+\/\d+/.test(line)) continue
          console.log(`  ${line}`)
        }
      }

      // Parse JSON result from Python output
      const lines = stdout.split('\n').filter((l: string) => l.trim())
      for (let i = lines.length - 1; i >= 0; i--) {
        try {
          const result = JSON.parse(lines[i])
          if (result.success && result.files?.length > 0) {
            const total = Number(result.total)
            return { success: true, path: result.files[0], count: Number.isFinite(total) ? total : undefined }
          }
        } catch {}
      }

      return { success: false, error: 'Python export succeeded but no output found' }
    } catch (e: any) {
      const failure = safeSubprocessError(e, 'Python export failed')
      if (!quiet) console.error(`  ${failure}`)
      if (date) {
        return { success: false, error: `指定日期导出失败: ${failure}` }
      }
      if (format === 'md') {
        // markdown 没有「简版」：回退成 HTML 会让调用方以为拿到了 .md。失败就是失败。
        return { success: false, error: failure }
      }
      // Fallback to basic HTML：**这条回退不带内嵌图片**，调用方得知道 ——
      // 2026-10-13 实测：Python 因一个 AttributeError 退出后，这里静默回退，
      // 而 CLI 照样打印「✓ 导出成功」，人只会以为导出正常。
      if (!quiet) console.error('  ⚠ 已回退到简版 HTML（不含内嵌图片）')
      return this.exportHtmlBasic(talker, outputDir, limit, from, to)
    }
  }

  /**
   * 只抽媒体（图片落盘 + 清单），不产出正文 —— `export … json|txt|excel --media` 先跑这一步。
   * 与 markdown 导出共用同一条 Python 通路与同一个资产目录，所以清单只有一份。
   */
  async extractMedia(talker: string, outputDir: string, quiet = false, options: { fullImages?: boolean } = {}): Promise<ExportResult> {
    return this.exportHtml(talker, outputDir, 0, '', undefined, undefined, quiet,
      { ...options, format: 'md', assetsOnly: true })
  }

  /** markdown + 图片落盘到 `<output>/<talker>_assets/`（同一条 Python 通路，换个输出形态）。 */
  async exportMarkdown(talker: string, outputDir: string, limit = 0, date = '', from?: number, to?: number, quiet = false, options: { fullImages?: boolean } = {}): Promise<ExportResult> {
    return this.exportHtml(talker, outputDir, limit, date, from, to, quiet, { ...options, format: 'md' })
  }

  private async exportHtmlBasic(talker: string, outputDir: string, limit = 0, from?: number, to?: number): Promise<ExportResult> {
    const messages = await chatService.getMessagesInRange(talker, limit, from, to)
    if (messages.length === 0) {
      return { success: false, error: '未找到消息' }
    }

    const html = this.buildHtml(talker, messages)
    const dir = this.ensureOutputDir(outputDir)
    const filePath = join(dir, `${talker}_messages.html`)
    writeFileSync(filePath, html, 'utf8')
    return { success: true, path: filePath, count: messages.length }
  }

  async exportExcel(talker: string, outputDir: string, limit = 0, from?: number, to?: number): Promise<ExportResult> {
    try {
      // exceljs is CJS-only, so a dynamic import hands back a namespace whose
      // only member is `default`. `new ExcelJS.Workbook()` on the namespace
      // itself is a TypeError, which the catch below used to swallow into a
      // bare "Excel 导出失败".
      const mod: any = await import('exceljs')
      const ExcelJS = mod.default ?? mod
      const messages = await chatService.getMessagesInRange(talker, limit, from, to)
      if (messages.length === 0) {
        return { success: false, error: '未找到消息' }
      }

      const workbook = new ExcelJS.Workbook()
      const sheet = workbook.addWorksheet('聊天记录')

      sheet.columns = [
        { header: '时间', key: 'time', width: 20 },
        { header: '发送者', key: 'sender', width: 15 },
        { header: '类型', key: 'type', width: 10 },
        { header: '内容', key: 'content', width: 80 },
        { header: '消息ID', key: 'msgId', width: 15 },
        // 有清单时填相对路径（清单由 markdown 导出产生）
        { header: '媒体', key: 'media', width: 40 }
      ]

      const mediaMap = readMediaManifest(this.ensureOutputDir(outputDir), talker)
      for (const m of messages) {
        sheet.addRow({
          time: new Date(m.createTime * 1000).toLocaleString('zh-CN'),
          sender: m.isSend ? '我' : (m.senderUsername || talker),
          type: this.getMessageTypeName(m.localType),
          content: m.parsedContent || m.rawContent || '',
          msgId: m.localId,
          media: mediaFor(mediaMap, m) || ''
        })
      }

      const dir = this.ensureOutputDir(outputDir)
      const filePath = join(dir, `${talker}_messages.xlsx`)
      await workbook.xlsx.writeFile(filePath)
      return { success: true, path: filePath, count: messages.length }
    } catch (e: any) {
      // Keep the reason: a bare label turned a one-line library error into an
      // undiagnosable "export failed" for both users and agents.
      const reason = e?.message ? `: ${String(e.message).slice(0, 200)}` : ''
      return { success: false, error: `Excel 导出失败${reason}` }
    }
  }

  private buildHtml(talker: string, messages: Message[]): string {
    const rows = messages.map(m => {
      const time = new Date(m.createTime * 1000).toLocaleString('zh-CN')
      const sender = m.isSend ? '我' : this.escapeHtml(m.senderUsername || talker)
      const contentType = m.localType
      const contentHtml = this.renderMessageContent(m)
      const align = m.isSend ? 'right' : 'left'
      const bgColor = m.isSend ? '#95ec69' : '#ffffff'

      return `<div style="text-align:${align};margin:8px 0;">
        <div style="display:inline-block;background:${bgColor};padding:8px 12px;border-radius:8px;max-width:80%;">
          <div style="font-size:12px;color:#999;">${sender} · ${time}</div>
          <div style="margin-top:4px;white-space:pre-wrap;word-break:break-all;">${contentHtml}</div>
        </div>
      </div>`
    }).join('\n')

    return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>聊天记录 - ${this.escapeHtml(talker)}</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f0f0f0; padding: 20px; }
    .container { max-width: 820px; margin: 0 auto; background: #f5f5f5; padding: 20px; border-radius: 12px; }
    .msg-image { color:#888; }
    .msg-image img { max-width:240px; max-height:240px; border-radius:4px; margin-top:6px; display:block; }
    .msg-app { margin:0; }
    .msg-app .app-title { font-size:14px; font-weight:600; color:#333; }
    .msg-app .app-desc { font-size:12px; color:#999; margin-top:2px; }
    .msg-app .app-url { display:block; margin-top:6px; padding:6px 10px; background:#f0f0f0; border-left:3px solid #07c160; text-decoration:none; color:#576b95; border-radius:0 4px 4px 0; font-size:13px; }
    .msg-app .app-file { color:#07c160; font-weight:600; }
    .msg-sys { color:#bbb; font-size:13px; }
    .msg-media { color:#888; }
  </style>
</head>
<body>
  <div class="container">
    <h2 style="text-align:center;">聊天记录 - ${this.escapeHtml(talker)}</h2>
    <p style="text-align:center;color:#999;">共 ${messages.length} 条消息</p>
    ${rows}
  </div>
</body>
</html>`
  }

  private renderMessageContent(m: Message): string {
    switch (m.localType) {
      case 1:
        return this.escapeHtml(m.parsedContent || m.rawContent || '')

      case 3: {
        let html = `<span class="msg-media">[图片]</span>`
        if (m.imageFileName) {
          html = `<span class="msg-media">[图片: ${this.escapeHtml(m.imageFileName)}]</span>`
        }
        if (m.imageBase64) {
          html += `<br><img src="data:image/jpeg;base64,${m.imageBase64}" />`
        }
        return html
      }

      case 34:
        return '<span class="msg-media">[语音]</span>'

      case 43:
        return '<span class="msg-media">[视频]</span>'

      case 47:
        return this.escapeHtml(m.parsedContent || '[表情]')

      case 49: {
        let html = '<div class="msg-app">'
        if (m.appTitle) {
          html += `<div class="app-title">${this.escapeHtml(m.appTitle)}</div>`
        }
        if (m.appDescription) {
          html += `<div class="app-desc">${this.escapeHtml(m.appDescription)}</div>`
        }
        if (m.appUrl) {
          html += `<a class="app-url" href="${this.escapeHtml(m.appUrl)}" target="_blank">${this.escapeHtml(m.appUrl)}</a>`
        }
        html += '</div>'
        if (!m.appTitle && !m.appDescription && !m.appUrl) {
          html = this.escapeHtml(m.parsedContent || '[链接/文件]')
        }
        return html
      }

      case 50:
        return '<span class="msg-media">[语音通话]</span>'

      case 10000:
        return `<span class="msg-sys">${this.escapeHtml(m.parsedContent || '')}</span>`

      default:
        return this.escapeHtml(m.parsedContent || m.rawContent || '')
    }
  }

  private escapeHtml(str: string): string {
    return str
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
  }

  private getMessageTypeName(type: number): string {
    const map: Record<number, string> = {
      1: '文本', 3: '图片', 34: '语音', 42: '名片',
      43: '视频', 47: '表情', 48: '位置', 49: '链接/文件',
      50: '语音通话', 10000: '系统', 10002: '引用'
    }
    return map[type] || `类型${type}`
  }

  private ensureOutputDir(dir: string): string {
    const resolved = dir || './output'
    if (!existsSync(resolved)) {
      mkdirSync(resolved, { recursive: true })
    }
    return resolved
  }
}

export const exportService = new ExportService()
