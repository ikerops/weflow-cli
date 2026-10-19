/**
 * `NtCore.scan` 的**失败面貌**：特别是 Windows 的 `9009`。
 *
 * 为什么单独钉这一条：2026-10-18 一位用户发来的截图里，`init` 打印的是
 * `NT 数据库扫描: NT scan failed (exit 9009)` —— 一句既不说原因、也不说下一步的话；
 * 而它下面紧跟着"初始化完成"，于是他把"读不到会话"归因成了产品坏了。
 * `9009` 在 Windows 上是"找不到命令"，在本项目里几乎总是 PATH 上只有**微软商店的
 * python 占位程序**（`getPythonCommand()` 兜底返回裸名 `python` 时就会命中它）。
 *
 * 这类错误映射没有可移植的造假方式：真造一个 exit 9009 的子进程需要按平台的
 * 可执行文件/脚本语义来（Linux CI 与 Windows 开发机的做法不同）。所以 `scan`
 * 开了一个执行器口子，这里用假执行器直接喂 `e.code = 9009`。
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import { NtCore } from '../src/core/ntCore.js'
import type { NtExecRunner } from '../src/core/ntCore.js'

function throwing(code: number | string, message = 'Command failed'): NtExecRunner {
  return async () => {
    throw Object.assign(new Error(message), { code })
  }
}

function stdout(text: string): NtExecRunner {
  return async () => ({ stdout: text })
}

test('9009 要说清是 Python 缺失/不可用，而不是把退出码原样丢给用户', async () => {
  const r = await NtCore.scan(undefined, throwing(9009))
  assert.equal(r.success, false)
  assert.match(r.error || '', /Python 3\.10\+/)
  assert.match(r.error || '', /sqlcipher3/)
  assert.match(r.error || '', /9009/)
  assert.match(r.error || '', /商店/, '要点出 Windows 上最常见的那个原因')
  assert.doesNotMatch(r.error || '', /^NT scan failed/,
    '旧的裸报错不能回来 —— 它不包含任何下一步')
})

test('ENOENT（没装 Python）与 9009 用同一句话：说清是 Python 缺了，并给出验证动作', async () => {
  const r = await NtCore.scan(undefined, throwing('ENOENT'))
  assert.equal(r.success, false)
  // 以前这里是英文 `Python not found. Install Python and add to PATH.`，于是同一个 init 里
  // 上面已经用中文解释过"商店占位程序"，下面又冒出一句英文（2026-10-19 实跑看到）。
  assert.match(r.error || '', /没有可用的 Python/)
  assert.match(r.error || '', /python --version/, '要给出可以马上验证的动作')
  assert.doesNotMatch(r.error || '', /Python not found/, '英文那条不许回来')
})

test('正常输出：从最后一行 JSON 取值，并把 databases/keys/matched 都带出来', async () => {
  const payload = {
    keys: [{ key: 'aa', salt: 'bb' }],
    databases: [{ path: 'x', name: 'message/message_0.db', salt: 'bb', size: 1, wxid: 'wxid_x' }],
    matched: [{ path: 'x', name: 'message/message_0.db', salt: 'bb', size: 1, wxid: 'wxid_x', key: 'aa' }],
  }
  let seenArgs: string[] = []
  const runner: NtExecRunner = async (_file, args) => {
    seenArgs = args
    return { stdout: 'noise line\n' + JSON.stringify(payload) }
  }
  const r = await NtCore.scan('/data/root', runner)
  assert.equal(r.success, true)
  assert.deepEqual(r.keys, payload.keys)
  assert.deepEqual(r.databases, payload.databases)
  assert.deepEqual(r.matched, payload.matched)
  assert.ok(seenArgs.includes('scan') && seenArgs.includes('--json'), '子命令与参数要传给脚本')
})

test('脚本报了 error：失败要保留它已经发现的数据库列表（派生密钥要用）', async () => {
  const payload = { error: '需要 sqlcipher3', databases: [{ path: 'x' }] }
  const r = await NtCore.scan(undefined, stdout(JSON.stringify(payload)))
  assert.equal(r.success, false)
  assert.equal(r.error, '需要 sqlcipher3')
  assert.deepEqual(r.databases, [{ path: 'x' }])
})

test('没有任何 JSON：报"没有有效输出"，不当成成功', async () => {
  const r = await NtCore.scan(undefined, stdout('Traceback...\n只有人话，没有 JSON\n'))
  assert.equal(r.success, false)
  assert.match(r.error || '', /no valid output/i)
})
