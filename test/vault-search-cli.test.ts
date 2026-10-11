/**
 * `vault search` 的**命令行面**：`--json` 必须被接受，而且**默认不是 JSON**。
 *
 * 为什么要有这条（2026-10-11 实测）：这条命令以前是**唯一输出就是 JSON、而 `--json` 本身不存在** ——
 * action 的参数表里硬编码着 `'--json'`，脚本里写着的人类模式（`vault_search.py` 不带 `--json` 时
 * 打印带相关度的可读列表）**没有任何入口能到达**，用户显式敲 `--json` 还会拿到
 * `error: unknown option '--json'`（exit 1、stdout 0 字节）。这与 `docs/EXTENDING.md` 那条
 * "脚本或 AI 会驱动的命令都有 `--json` 模式"正相反 —— 同一类还有 `--no-rerank`
 * （Python 侧有、CLI 没接线，见 `test/search-cli.test.ts`），但那条**文档里写着**，被
 * `scripts/check_readme_commands.py` 扫了出来；这一条文档里没写，只有真敲一次才知道。
 *
 * 判据选**行为**而不是源码文本：`--json` 有没有真的转过去、默认到底吐什么，只有端到端跑一次才有定论。
 * 静态写法在这个仓库里有先例（`test/chat-notes-cli.test.ts` 的 `opts.transcribeVoice ? [...]`），
 * 但一条端到端断言已经蕴含它 —— 把 `...(opts.json ? ['--json'] : [])` 改回硬编码 `'--json'`，
 * 下面第二条会红；声明了却不转发，第一条会红。两条一起覆盖，就不必再对同一件事写一遍形状断言。
 *
 * **不需要本机数据**：`DEFAULT_VAULT` 是按脚本位置算的绝对路径，CI 里 `output/` 不存在 ⇒
 * 0 条命中、退出 0，人类模式照样打印"找到 0 条"。已实测过空目录与不存在的目录两种情形
 * （都在调脚本**之前**或**没有 Vault 时**成立，所以本机与 CI 的结论一致）。
 */
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { join } from 'node:path'
import test from 'node:test'

function runVaultSearch(args: string[]) {
  return spawnSync(process.execPath, [
    '--import', 'tsx', join(process.cwd(), 'bin', 'weflow-cli.ts'), 'vault', 'search', ...args,
  ], { cwd: process.cwd(), encoding: 'utf8' })
}

function isSingleJson(text: string): boolean {
  try {
    JSON.parse(text)
    return true
  } catch {
    return false
  }
}

test('vault search --json 被接受，且 stdout 是一整份 JSON', () => {
  const r = runVaultSearch(['x', '--json'])
  assert.ok(isSingleJson(r.stdout),
    `--json 必须给出可解析的一整份 JSON；实际 stdout=${JSON.stringify(r.stdout.slice(0, 200))}`
    + `（若这里是 "unknown option"，说明这条通道又断了）`)
  const body = JSON.parse(r.stdout)
  assert.equal(body.line, 'wiki', '不带 --line 时默认搜文章线')
  assert.ok(Array.isArray(body.results), 'JSON 里应有 results 数组')
})

test('vault search 默认是人类可读，不是 JSON', () => {
  const r = runVaultSearch(['x'])
  assert.ok(!isSingleJson(r.stdout),
    `默认输出不该是 JSON —— 把 '--json' 重新硬编码进参数表就会让这条红；`
    + `实际 stdout=${JSON.stringify(r.stdout.slice(0, 200))}`)
  assert.match(r.stdout, /找到/, '脚本人类模式那句"找到 N 条"不见了（脚本的默认输出被改了？）')
})

test('vault search 的校验分支认 --json：JSON 模式下 stdout 仍是一整份 JSON', () => {
  const r = runVaultSearch(['x', '--json', '--type', 'bogus'])
  assert.equal(r.status, 1, '不合法的 --type 应当退出 1')
  assert.ok(isSingleJson(r.stdout),
    `JSON 模式下就算参数不合法，stdout 也得是一份 JSON（否则调用方拿到的是彩色散文）；`
    + `实际 stdout=${JSON.stringify(r.stdout.slice(0, 200))}`)
  const body = JSON.parse(r.stdout)
  assert.equal(body.success, false)
  assert.equal(body.code, 'INVALID_ARGUMENT')
  assert.equal(body.field, 'type', '要指出是哪个字段不合法')
})
