/**
 * **这台机器上的微信客户端，运行时到底加载的是哪一份。**
 *
 * 为什么需要它：`check` / `health_check` 一直在报"数据版本 4.x"（那是**数据库格式**），
 * 而"客户端是哪个版本"从来没人报过 —— 出问题时（数据读不出来、客户端起不来）这恰恰是
 * 第一个要问的。
 *
 * **判据来自一份实测的启动器逆向分析**（报告在仓库外），它纠正了一个很自然的错法：
 *
 * - `Weixin.exe` 里**确实**硬编码着一个版本字面量，而且**恰好与本机的版本目录同名** ——
 *   看起来"读它就能知道版本"。但控制流显示它只是**兜底**：主路径读
 *   `%APPDATA%\Tencent\xwechat\update\update.data` 里记的目录，只有那次失败才会用到它。
 *   ⇒ **读字面量判版本是错的**，哪怕它在这台机器上碰巧对。（报告里那条"2/2 实测"就是先
 *   写错、后被控制流推翻的。）
 * - 正确的判据是**活进程命令行里的 `--user-lib-dir`** —— 那是真的被加载的那个目录。
 * - 而"磁盘上有个版本目录"**不代表它能用**：本机 `D:\WeiXin\4.1.15.11\` 是升级残留，
 *   只剩 `crashpad_handler.exe` 一个文件（实测 1 个文件 / 2.1 MB），指向它的加载会失败。
 *
 * 所以这个模块只做两件事，且都**不猜**：从进程命令行取实际目录；把一个安装根下的版本目录
 * 分成"能用/不能用"（判据是**有没有 `Weixin.dll`** —— 启动器加载的就是它）。
 * 取不到就是 `null` + 一句 `note`，不拿别的来源顶替。
 */
import { existsSync, readdirSync, statSync } from 'node:fs'
import { basename, dirname, join } from 'node:path'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'

const execFileAsync = promisify(execFile)

/** 一个 `Weixin.exe` 进程的画像（只留判版本要用的两个字段）。 */
export interface WechatClientProcess {
  pid: number
  exe: string
  /** 命令行里的 `--user-lib-dir`；进程没带就是 null（实测 `--type=xplayer` 那个不带）。 */
  loadedDir: string | null
}

export interface WechatClientFacts {
  /** 本机有没有在跑的 `Weixin.exe`（没跑 ⇒ 版本未知，不是"没问题"）。 */
  running: boolean
  /** 去重后的实际加载目录，按出现顺序。 */
  loadedDirs: string[]
  /** 版本号 = 加载目录的名字；**恰好一个**目录时才给，否则 null。 */
  version: string | null
  /** `version` 为 null 时说清为什么。 */
  note: string
  /** 安装根下**不能用的**版本目录（没有 `Weixin.dll`）。 */
  unusableVersionDirs: Array<{ dir: string; files: number }>
}

/** 版本目录名：`4.1.15.13` 这种。别的一律不当版本目录（不猜）。 */
const VERSION_DIR = /^\d+(\.\d+)+$/

/**
 * PowerShell 一行取所有 `Weixin.exe` 的 pid / 可执行路径 / 命令行。
 *
 * 与 `keyService.isProcessRunning` 用同一种调法（`-NoProfile -NonInteractive`）：这台机器上
 * 那是仓库既有做法，不引第二种。`ConvertTo-Json` 在只有一个结果时会给对象而不是数组，
 * 所以下面 `parseProcessRows` 两种都收。
 */
const PS_LIST = "$p = Get-CimInstance Win32_Process -Filter \"Name='Weixin.exe'\" | "
  + 'Select-Object ProcessId, ExecutablePath, CommandLine; '
  + 'if ($p) { $p | ConvertTo-Json -Compress }'

export function parseProcessRows(raw: string): WechatClientProcess[] {
  const text = String(raw || '').trim()
  if (!text) return []
  let data: unknown
  try {
    data = JSON.parse(text)
  } catch {
    return []
  }
  const rows = Array.isArray(data) ? data : [data]
  const out: WechatClientProcess[] = []
  for (const row of rows) {
    if (!row || typeof row !== 'object') continue
    const r = row as Record<string, unknown>
    const pid = Number(r.ProcessId)
    if (!Number.isFinite(pid)) continue
    out.push({
      pid,
      exe: String(r.ExecutablePath || ''),
      loadedDir: userLibDir(String(r.CommandLine || '')),
    })
  }
  return out
}

/**
 * 从一条命令行里取 `--user-lib-dir` 的值。**两种写法都认**（`=` 与空格、带引号与不带），
 * 取不到返回 null。
 *
 * 注意这个参数**不在 `Weixin.exe` 的字符串表里**（实测 0 命中）—— 它是被加载的 DLL 自己
 * 解析的。也就是说：只有**进程**能告诉我们它，二进制里搜不到。
 */
export function userLibDir(commandLine: string): string | null {
  const m = /--user-lib-dir\s*=\s*(?:"([^"]+)"|'([^']+)'|(\S+))/.exec(String(commandLine || ''))
  if (!m) return null
  const value = (m[1] || m[2] || m[3] || '').trim().replace(/["']/g, '')
  return value || null
}

/** 一个版本目录能不能用：**有没有 `Weixin.dll`**（启动器加载的就是它）。 */
export function versionDirFiles(dir: string): string[] | null {
  try {
    if (!statSync(dir).isDirectory()) return null
    return readdirSync(dir)
  } catch {
    return null
  }
}

/** 把一堆目录名分成"能用的版本目录 / 不能用的 / 与版本无关的"。纯函数，便于测。 */
export function classifyVersionDirs(entries: Array<{ name: string; files: string[] }>): {
  usable: string[]
  unusable: Array<{ dir: string; files: number }>
} {
  const usable: string[] = []
  const unusable: Array<{ dir: string; files: number }> = []
  for (const entry of entries) {
    if (!VERSION_DIR.test(entry.name)) continue
    if (entry.files.includes('Weixin.dll')) usable.push(entry.name)
    else unusable.push({ dir: entry.name, files: entry.files.length })
  }
  return { usable, unusable }
}

/** 安装根（`D:\WeiXin`）→ 它的版本目录分类。读不出来就当空（不猜）。 */
export function versionDirsUnder(installRoot: string): ReturnType<typeof classifyVersionDirs> {
  let names: string[]
  try {
    names = readdirSync(installRoot)
  } catch {
    return { usable: [], unusable: [] }
  }
  const entries = names.map(name => ({ name, files: versionDirFiles(join(installRoot, name)) || [] }))
  return classifyVersionDirs(entries)
}

export function summarize(processes: WechatClientProcess[], unusable: Array<{ dir: string; files: number }>): WechatClientFacts {
  // 去重按**规范形**（去掉尾部分隔符、大小写折叠 —— Windows 路径大小写不敏感）：
  // 同一个目录写成 `D:\WeiXin\4.1.15.13` 与 `…\4.1.15.13\` 是**一个**目录，
  // 不折算的话会被当成"两个进程指向不同目录"从而拒绝给出版本号。
  const seen = new Map<string, string>()
  for (const p of processes) {
    if (!p.loadedDir) continue
    const shown = p.loadedDir.replace(/[\\/]+$/, '')
    if (!shown) continue
    const key = shown.toLowerCase()
    if (!seen.has(key)) seen.set(key, shown)
  }
  const loadedDirs = [...seen.values()]
  let version: string | null = null
  let note = ''
  if (!processes.length) {
    note = '客户端没在运行 ⇒ 版本未知（`--user-lib-dir` 只有进程知道）'
  } else if (!loadedDirs.length) {
    note = `在跑 ${processes.length} 个进程，但命令行里都没有 --user-lib-dir`
  } else if (loadedDirs.length > 1) {
    // **不猜**：多个目录各有各的来源（子进程可以指到不同的库），挑一个报出去就是编。
    // 目录本身照旧列出来（那是实测到的），只是不给"版本号"这个单一结论。
    note = `有 ${loadedDirs.length} 个不同的目录在使用，不挑一个报版本`
  } else {
    version = basename(loadedDirs[0])
  }
  return {
    running: processes.length > 0,
    loadedDirs,
    version,
    note,
    unusableVersionDirs: unusable,
  }
}

/** 默认的执行器：`Get-CimInstance`。测试里换掉它，别真去问系统。 */
export type PowershellRunner = (command: string) => Promise<string>

const defaultRunner: PowershellRunner = async (command) => {
  const { stdout } = await execFileAsync('powershell.exe',
    ['-NoProfile', '-NonInteractive', '-Command', command], { timeout: 15_000 })
  return String(stdout || '')
}

/**
 * 汇总：进程画像 + 每个安装根下"不能用的版本目录"。
 *
 * 非 Windows 直接返回"平台不支持"（`Get-CimInstance` 是 Windows 的），**不是**返回"没问题"。
 */
export async function detectWechatClient(run: PowershellRunner = defaultRunner): Promise<WechatClientFacts> {
  if (process.platform !== 'win32') {
    return { running: false, loadedDirs: [], version: null,
             note: `判版本只做了 Windows（当前平台 ${process.platform}）`, unusableVersionDirs: [] }
  }
  let raw = ''
  try {
    raw = await run(PS_LIST)
  } catch {
    raw = ''
  }
  const processes = parseProcessRows(raw)
  // 安装根从进程的可执行路径推（**别去猜**：注册表/常见路径都不可靠，报告实测注册表 InstallPath 是空的）
  const roots = [...new Set(processes.map(p => dirname(p.exe)).filter(dir => dir && existsSync(dir)))]
  const unusable = roots.flatMap(root => versionDirsUnder(root).unusable)
  return summarize(processes, unusable)
}
