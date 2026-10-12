#!/usr/bin/env node
/**
 * 驱动命令、看 `--json` 的 stdout 是不是**一整份** JSON。两种模式，各自补一半：
 *
 *   node scripts/json_surface_probe.mjs --unconfigured   # HOME 指向空目录：覆盖"失败路径"（83 条）
 *   node scripts/json_surface_probe.mjs --read-only      # 真 HOME + 只读白名单：覆盖"成功路径"（26 条）
 *
 * 为什么要两种：未配置那轮能覆盖的叶子多，但**成功路径上的污染它看不见** —— `resolveTalker` 那行
 * `→ 昵称 (wxid)` 提示只有有数据时才打出来（2026-10-12 就是先漏掉一半，靠只读那轮才补上）。只读那轮
 * 相反：覆盖面窄，但走的是真数据。
 *
 * **这不是 CI 里的判据**：两种模式分别需要"没有配置"和"有配置且是只读命令"，而且比 `npm test` 慢。
 * 它是**本机工具**（与 `scripts/graph3d_check.mjs` 同类，同样不进 npm 包）：改了 CLI 的 `--json` 出口
 * 之后手动跑一次。有 findings 时退出码 1。
 *
 * 安全：`--read-only` 只跑白名单里的**只读**完整路径（同名叶子不会串），绝不碰写入/发送/守护进程/导出；
 * 位置参数一律给 `x`（解析不到就走失败支 —— 那也是一条要检查的路径）。每条 30s 上限，超时如实报、不算缺陷。
 */
import { spawnSync } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

// **必须走 `fileURLToPath`**：`new URL('..', import.meta.url).pathname` 会把非 ASCII 目录名
// 百分号编码（这个仓库在本机就住在 `ZhaoWen_GitHub维护` 下）⇒ cwd 不存在 ⇒ 每个 spawnSync 都是
// ENOENT ⇒ BFS 一条命令都看不到，而脚本照样 exit 0（2026-10-12 实测，正是下面的反空扫要挡的假绿）。
const REPO = fileURLToPath(new URL('..', import.meta.url))

// 只读白名单：**完整路径**。加之前先确认这条命令不写本地文件、不发消息、不起守护进程。
const READ_ONLY = new Set([
  'sessions', 'messages', 'contacts', 'contact-schema', 'status', 'verify', 'check', 'capabilities',
  'daily-stats', 'bonds', 'mcp-config', 'config show', 'audit list', 'fav list',
  'sns timeline', 'sns stats', 'sns users', 'todos list', 'todos undone',
  'skill list', 'skill show', 'skill check', 'scene list', 'scene show',
  'whitelist list', 'blacklist list', 'wiki lint', 'vault search',
])

// 未配置模式要**排除**的：守护进程、发送、长期驻留、以及会改外部状态的。
const UNCONFIGURED_DENY = new Set(['send', 'draft', 'listen', 'assistant', 'panel', 'daily-server',
  'fav-server', 'login-wechat', 'logout-wechat', 'scan', 'dbkey', 'capture-key', 'init', 'sync',
  'pipeline', 'daily', 'run', 'tick'])

// 用 `--json-result` 而不是 `--json` 的两条：它们另有一个同名的**文件格式**参数，名字写进了
// `capabilities --json`（`favorites.export`、`mcpConfiguration`）⇒ 驱动时不该带 `--json`。
const JSON_RESULT_ONLY = new Set(['mcp-config', 'fav export'])

function helpOf(path) {
  const r = spawnSync('node', ['cli.cjs', ...path, '--help'], { cwd: REPO, encoding: 'utf8' })
  return (r.stdout || '') + (r.stderr || '')
}

/** `Commands:` 段里**顶格**的那些名字（折行的描述缩得更深，靠缩进排除掉）。 */
function subcommands(text) {
  const lines = text.split('\n')
  const start = lines.findIndex(l => ['Commands:', '命令:'].includes(l.trim()))
  if (start < 0) return []
  const out = []
  let base = null
  for (const line of lines.slice(start + 1)) {
    if (!line.trim()) break
    const indent = line.length - line.trimStart().length
    if (base === null) base = indent
    if (indent !== base) continue
    const name = line.trim().split(/\s+/)[0]
    if (/^[a-z][a-z0-9-]*$/.test(name) && name !== 'help') out.push(name)
  }
  return out
}

/** BFS 遍历 help 树 —— 路径问 CLI 自己，不猜源码里的父子关系（`sync-weread` 在 `vault` 下、
 *  `run` 在 `pipeline` 下，而顶层另有一个同名的 `assistant run`）。 */
function allCommands() {
  const queue = [[[], helpOf([])]]
  const out = []
  const seen = new Set()
  while (queue.length) {
    const [path, text] = queue.shift()
    const key = path.join(' ')
    if (seen.has(key)) continue
    seen.add(key)
    out.push([path, text])
    if (path.length > 3) continue
    for (const name of subcommands(text)) queue.push([[...path, name], helpOf([...path, name])])
  }
  return out
}

/** 那两条用 `--json-result` 的命令，驱动时就得用它 —— 否则量到的是"旗标不存在"，不是它们的出口。 */
function flagFor(path) {
  return JSON_RESULT_ONLY.has(path.join(' ')) ? '--json-result' : '--json'
}

function drive(path, text, env, timeoutMs) {
  const usage = text.split('\n').find(l => l.includes('Usage:')) || ''
  const positionals = [...usage.matchAll(/<([^>]+)>/g)].map(m => m[1])
  const args = [...path, ...positionals.map(() => 'x'), flagFor(path)]
  const r = spawnSync('node', ['--import', 'tsx', 'bin/weflow-cli.ts', ...args],
    { cwd: REPO, encoding: 'utf8', env, timeout: timeoutMs })
  // 被超时杀掉时必须**当成超时**：`r.status` 是 null、`stdout` 是截断的半截 JSON，会被报成"不是一整份 JSON"。
  // 判定要同时看 `error.code` 与 `signal` —— 实测只知道前者是不够的（2026-10-12：`wiki lint` 就这么被误报）。
  if (r.signal || (r.error && r.error.code === 'ETIMEDOUT')) return { timeout: true }
  return { stdout: r.stdout || '', stderr: r.stderr || '', code: r.status }
}

function isOneJson(text) {
  try {
    JSON.parse(text)
    return true
  } catch {
    return false
  }
}

const mode = process.argv[2]
if (!['--unconfigured', '--read-only'].includes(mode)) {
  console.error('用法: node scripts/json_surface_probe.mjs --unconfigured | --read-only')
  process.exit(2)
}

const commands = allCommands()
// 反空扫：BFS 没跑起来时（路径错、help 形状变了）"0 findings" 与"全都合规"完全一样 ——
// 2026-10-12 就踩过：cwd 被百分号编码 ⇒ 每个 spawn 都 ENOENT ⇒ 只看到 1 条命令却 exit 0。
if (commands.length < 50) {
  console.error(`BFS 只看到 ${commands.length} 条命令（正常 129）—— 是探针自己没跑起来，不是命令变少了`)
  process.exit(2)
}
const targets = commands.filter(([path, text]) => {
  if (mode === '--read-only') return READ_ONLY.has(path.join(' '))
  const key = path.join(' ')
  // `--json-result` 那两条也要驱动（它们只是旗标名不同），只是带的是另一个旗标。
  return path.length > 0 && (text.includes('--json') || JSON_RESULT_ONLY.has(key))
    && !path.some(seg => UNCONFIGURED_DENY.has(seg))
})
if (!targets.length) {
  console.error('白名单与命令清单没有交集 —— 命令改名了？别把"0 条驱动"当通过')
  process.exit(2)
}

console.log(`命令总数 ${commands.length}；本次驱动 ${targets.length} 条（${mode === '--read-only' ? '只读/真 HOME' : '未配置/空 HOME'}）`)

let home = null
if (mode === '--unconfigured') home = mkdtempSync(join(tmpdir(), 'weflow-probe-'))
const env = home ? { ...process.env, HOME: home, USERPROFILE: home } : { ...process.env }
// 只读模式放宽到 120s：`wiki lint` 要过 5 万页，30s 会把它当成残废品（实测被截断成"不是一整份 JSON"）。
const perCommandMs = mode === '--read-only' ? 120_000 : 30_000

let findings = 0
const commanderErrors = []
try {
  for (const [path, text] of targets) {
    const r = drive(path, text, env, perCommandMs)
    if (r.timeout) {
      console.log(`  TIMEOUT  ${path.join(' ')}`)
      continue
    }
    if (isOneJson(r.stdout)) continue
    // 按**形状**分开：stdout 空 + stderr 是 commander 自己的解析错误（缺必需选项之类）——那是解析层
    // 的事，不是命令的 JSON 出口漏了（要修得改 `program` 的出口，见 FUNCTIONAL-IMPROVEMENT-SPEC 第 8 条）。
    // 单独列出、**不计入 findings**，但每次照样打印，别让它悄悄消失。
    if (!r.stdout.trim() && r.stderr.trimStart().startsWith('error:')) {
      commanderErrors.push(path.join(' '))
      continue
    }
    findings += 1
    console.log(`  ✗ ${path.join(' ').padEnd(24)} exit=${String(r.code).padEnd(3)} out=${JSON.stringify(r.stdout.trim().slice(0, 90))}`)
    const tail = r.stderr.trim().split('\n').filter(Boolean).pop()
    if (tail) console.log(`      stderr=${JSON.stringify(tail.slice(0, 110))}`)
  }
} finally {
  if (home) rmSync(home, { recursive: true, force: true })
}

if (commanderErrors.length) {
  console.log(`（已知、不计入：commander 解析错误 —— ${commanderErrors.join('、')}；见 FUNCTIONAL-IMPROVEMENT-SPEC 第 8 条）`)
}
console.log('不是一整份 JSON 的 =', findings)
process.exit(findings ? 1 : 0)
