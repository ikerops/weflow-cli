/**
 * 「可粘贴的诊断块」：结论要说人话、脱敏要真的脱、缺什么就指什么。
 *
 * 这几条都得钉住，因为它们失效时**看着都正常**：
 *  - 结论退化成"没发现明显问题"→ 用户拿着报告还是不知道错哪（同 issue #3/#9/#10 的老路）；
 *  - 脱敏漏一条 → 用户名/ wxid / 密钥被贴进公开 issue（AGENTS.md 明令禁止）；
 *  - 把"缺 sqlcipher3"报成"Python 不可用" → 用户去重装 Python，问题不变。
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import { diagnose, redactForReport, renderDiagnosticReport, type DiagnosticInput } from '../src/services/diagnosticReport.js'

const BASE: DiagnosticInput = {
  cliVersion: '9.9.9',
  generatedAt: '2026-10-19 00:00:00 +08:00',
  platform: 'win32',
  arch: 'x64',
  nodeVersion: '24.11.1',
  python: { command: 'C:/Users/Example/Documents/python.exe', available: true, version: 'Python 3.11.9' },
  dependencies: {
    required: { sqlcipher3: true, html2text: true, zstandard: true, cryptography: true },
    optional: { scrapling: true },
  },
  configuration: {
    initialized: true,
    messageDatabase: true,
    momentsDatabase: true,
    favoritesDatabase: true,
    favoritesReady: true,
  },
  keys: { messageKeyConfigured: true, passphraseConfigured: true },
  wechatClient: {
    running: true,
    version: '4.1.15.53',
    dataDirMatchesConfig: true,
    dataDirs: ['C:/Users/Example/Documents/xwechat_files'],
    unusableVersionDirs: [],
  },
}

const withPatch = (patch: Partial<DiagnosticInput>): DiagnosticInput => ({ ...BASE, ...patch })

test('一切正常时也要说清"这不是保证能用"', () => {
  const findings = diagnose(BASE)
  assert.equal(findings.length, 1)
  assert.match(findings[0].title, /没发现明显问题/)
  assert.match(findings[0].hint, /不是"保证能用"/)
})

test('Python 不可用 → 指向商店占位程序那条，而不是 sqlcipher3', () => {
  const findings = diagnose(withPatch({
    python: { command: 'python', available: false, version: '' },
    dependencies: { required: { ...BASE.dependencies.required, sqlcipher3: false } },
  }))
  const blocker = findings[0]
  assert.equal(blocker.level, 'blocker')
  assert.match(blocker.title, /Python 不可用/)
  assert.match(blocker.hint, /商店/)
  // Python 都没有时不该再让用户去 pip install sqlcipher3（那是第二步）
  assert.ok(!findings.some(f => /缺少 sqlcipher3/.test(f.title)))
})

test('Python 在、sqlcipher3 缺 → 只报 sqlcipher3', () => {
  const findings = diagnose(withPatch({
    dependencies: { required: { ...BASE.dependencies.required, sqlcipher3: false } },
  }))
  assert.equal(findings[0].level, 'blocker')
  assert.match(findings[0].title, /sqlcipher3/)
  assert.match(findings[0].hint, /pip install sqlcipher3/)
})

test('没密钥是一条 blocker，并且给出 forget-keys 的下一步', () => {
  const findings = diagnose(withPatch({ keys: { messageKeyConfigured: false, passphraseConfigured: false } }))
  const keyFinding = findings.find(f => /没有任何数据库密钥/.test(f.title))
  assert.ok(keyFinding, JSON.stringify(findings))
  assert.equal(keyFinding!.level, 'blocker')
  assert.match(keyFinding!.hint, /forget-keys/)
})

test('客户端没在跑只是 warning，不冒充 blocker', () => {
  const findings = diagnose(withPatch({
    wechatClient: { running: false, version: null, dataDirMatchesConfig: null },
  }))
  const running = findings.find(f => /没有在运行/.test(f.title))
  assert.ok(running)
  assert.equal(running!.level, 'warning')
})

test('多份数据目录不是同一份 → 警告，且说清"你可能在读另一个库"', () => {
  const findings = diagnose(withPatch({
    wechatClient: { ...BASE.wechatClient!, dataDirMatchesConfig: false },
  }))
  const mismatch = findings.find(f => /不是同一份/.test(f.title))
  assert.ok(mismatch)
  assert.match(mismatch!.hint, /另一个账号/)
})

test('结论按严重度排序：blocker 在 warning 之前', () => {
  const findings = diagnose(withPatch({
    keys: { messageKeyConfigured: false, passphraseConfigured: false },
    wechatClient: { running: false, version: null, dataDirMatchesConfig: null },
  }))
  assert.equal(findings[0].level, 'blocker')
  assert.equal(findings[findings.length - 1].level, 'warning')
})

test('脱敏：Windows 用户名、wxid、密钥形态、三十多位 hex 都要换掉', () => {
  const raw = 'C:\\Users\\Alice\\Documents\\xwechat_files\\wxid_abc123def\\db '
    + 'key sk-abcdefghijklmnopqrstuvwxyz012345 口 '
    + '0f1e2d3c4b5a69788796a5b4c3d2e1f0 邮箱 alice@example.com 电话 13800138000'
  const safe = redactForReport(raw)
  for (const leaked of ['Alice', 'wxid_abc123def', 'sk-abcdefghijklmnopqrstuvwxyz012345',
    '0f1e2d3c4b5a69788796a5b4c3d2e1f0', 'alice@example.com', '13800138000']) {
    assert.ok(!safe.includes(leaked), `没脱掉: ${leaked} → ${safe}`)
  }
  // 结构要留下：报告的价值在于"路径长什么样"
  assert.match(safe, /C:\\Users\\<用户>\\Documents\\xwechat_files/)
})

test('脱敏不能吃掉正常的版本号与路径（过度打码会让线索消失）', () => {
  const safe = redactForReport('加载版本 4.1.15.53 / 路径 D:\\WeiXin\\4.1.15.53\\Weixin.dll')
  assert.match(safe, /4\.1\.15\.53/)
  assert.match(safe, /D:\\WeiXin\\4\.1\.15\.53\\Weixin\.dll/)
})

test('整份报告：含分段标题、结论、可复制说明，且不含原始密钥与用户名', () => {
  const input = withPatch({
    wechatClient: { ...BASE.wechatClient!, dataDirs: ['C:\\Users\\Alice\\Documents\\xwechat_files'] },
    lastError: 'NT scan failed (exit 9009)\nC:\\Users\\Alice\\AppData\\secret path',
  })
  const report = renderDiagnosticReport(input)
  for (const section of ['【环境】', '【微信客户端】', '【配置】', '【结论】', '【最近一次报错原文】']) {
    assert.ok(report.includes(section), `缺分段 ${section}`)
  }
  assert.match(report, /可整段复制给 AI/)
  assert.ok(!report.includes('Alice'), '用户名漏了')
  assert.ok(report.includes('<用户>'), '应该有占位符')
})

test('报告里不出现"是/否"以外的东西冒充密钥状态', () => {
  const report = renderDiagnosticReport(withPatch({ keys: { messageKeyConfigured: true, passphraseConfigured: false } }))
  assert.match(report, /数据库密钥已配置: 是/)
  assert.match(report, /全库口令已配置: 否/)
})
