/**
 * `--json` 模式下**校验失败也要是一份 JSON**。
 *
 * 为什么要有这条：`--json` 的契约是"stdout 上是一整份机器可读的结果"。而校验分支原来只打彩色
 * 散文（`console.log(chalk.red(...))`），于是 `vault rag q --line zzz --json` 的调用方在 stdout 上
 * 什么都解析不出来 —— 除了 exit 1，它连"哪个参数不合法"都拿不到。2026-10-11 实测：`--line` 的
 * 校验**在四个命令里各写了一遍**，只有一处带 `--json` 分支，另外三处（`vault rag`、顶层 `search`、
 * `chat`）没有。同一份判断写四遍就有三遍漏 —— 现在四处都走 `requireCliChoice`，与
 * `parseCliInteger` / `requireCliDate` 同一条约定（错误也走 JSON）。
 *
 * 判据选**行为**：这四条都在读任何本地数据**之前**返回，所以 CI 里（没有配置、没有数据库）同样成立。
 */
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { join } from 'node:path'
import test from 'node:test'

function runCli(args: string[]) {
  return spawnSync(process.execPath, [
    '--import', 'tsx', join(process.cwd(), 'bin', 'weflow-cli.ts'), ...args,
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

const COMMANDS_WITH_LINE: [string, string[]][] = [
  ['vault search', ['vault', 'search', 'x', '--line', 'zzz', '--json']],
  ['vault rag', ['vault', 'rag', 'q', '--line', 'zzz', '--json']],
  ['search', ['search', 'x', '--line', 'zzz', '--json']],
  ['chat', ['chat', 'q', '--line', 'zzz', '--json']],
]

for (const [name, args] of COMMANDS_WITH_LINE) {
  test(`${name}：--json 模式下 --line 不合法时 stdout 仍是一整份 JSON`, () => {
    const r = runCli(args)
    assert.equal(r.status, 1, `${name} 应当在参数校验处退出 1`)
    assert.ok(isSingleJson(r.stdout),
      `${name} 的不合法参数错误也必须是 JSON（否则调用方拿到的是彩色散文）；`
      + `实际 stdout=${JSON.stringify(r.stdout.slice(0, 200))}`)
    const body = JSON.parse(r.stdout)
    assert.equal(body.success, false)
    assert.equal(body.code, 'INVALID_ARGUMENT')
    assert.equal(body.field, 'line', '要指出是哪个字段不合法')
    assert.match(body.error, /wiki/, '错误里要写清合法取值')
  })
}

test('不带 --json 时，同一条校验仍然给人看散文（不是把 JSON 塞给终端用户）', () => {
  const r = runCli(['vault', 'rag', 'q', '--line', 'zzz'])
  assert.equal(r.status, 1)
  assert.ok(!isSingleJson(r.stdout),
    `人看的这一支不该是 JSON；实际 stdout=${JSON.stringify(r.stdout.slice(0, 200))}`)
  assert.match(r.stdout, /line 必须是 wiki、chat 或 all/)
})
