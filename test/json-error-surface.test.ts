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
 * 判据选**行为**：这些命令都在读任何本地数据**之前**返回，所以 CI 里（没有配置、没有数据库）同样成立。
 */
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
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

/**
 * 被驱动的校验点，两条来源：
 *  · `--line` 那一族 —— 同一份判断手写过四遍，其中三遍丢了 json 分支；
 *  · 手写的**取值**校验，三种写法各取一个：`![…].includes(opts.x)`（`vault sync-weread`）、
 *    `new Set([…]).has(opts.x)`（`pipeline run`）、写在 action 里的 `chat-stats` / `review`。
 *
 * 路径是问 CLI 自己拿的（`--help` 的 BFS）：`sync-weread` 在 `vault` 下、`run` 在 `pipeline` 下，
 * 而顶层另有一个同名的 `assistant run` —— 按名字猜父子会钉错命令。
 *
 * 两个**已知缺口**，别把这条测试读成"全都覆盖了"：
 *  · `export --contract` 的校验在 `resolveTalker()` **之后**，要真配置才走得到（本机验过，CI 不行）；
 *  · 手写的那几处只发 `field`、不发人类可读的 `error`（helper 两样都发）—— 所以这里只断言字段名。
 */
const VALIDATION_CASES: { name: string; args: string[]; field: string }[] = [
  { name: 'vault search', args: ['vault', 'search', 'x', '--line', 'zzz', '--json'], field: 'line' },
  { name: 'vault rag', args: ['vault', 'rag', 'q', '--line', 'zzz', '--json'], field: 'line' },
  { name: 'search', args: ['search', 'x', '--line', 'zzz', '--json'], field: 'line' },
  { name: 'chat', args: ['chat', 'q', '--line', 'zzz', '--json'], field: 'line' },
  { name: 'vault sync-weread', args: ['vault', 'sync-weread', '--type', 'zzz', '--json'], field: 'type' },
  { name: 'chat-stats', args: ['chat-stats', '--period', 'zzz', '--json'], field: 'period' },
  { name: 'review', args: ['review', '--engine', 'zzz', '--json'], field: 'engine' },
  { name: 'pipeline run', args: ['pipeline', 'run', '--engine', 'zzz', '--json'], field: 'engine' },
]

for (const { name, args, field } of VALIDATION_CASES) {
  test(`${name}：--json 模式下取值不合法时 stdout 仍是一整份 JSON`, () => {
    const r = runCli(args)
    assert.equal(r.status, 1, `${name} 应当在参数校验处退出 1`)
    assert.ok(isSingleJson(r.stdout),
      `${name} 的不合法参数错误也必须是 JSON（否则调用方拿到的是彩色散文）；`
      + `实际 stdout=${JSON.stringify(r.stdout.slice(0, 200))}`)
    const body = JSON.parse(r.stdout)
    assert.equal(body.success, false)
    assert.equal(body.code, 'INVALID_ARGUMENT')
    assert.equal(body.field, field, '要指出是哪个字段不合法')
  })
}

test('不带 --json 时，同一条校验仍然给人看散文（不是把 JSON 塞给终端用户）', () => {
  const r = runCli(['vault', 'rag', 'q', '--line', 'zzz'])
  assert.equal(r.status, 1)
  assert.ok(!isSingleJson(r.stdout),
    `人看的这一支不该是 JSON；实际 stdout=${JSON.stringify(r.stdout.slice(0, 200))}`)
  assert.match(r.stdout, /line 必须是 wiki、chat 或 all/)
})

/**
 * `weread` 的失败出口 —— 就是那个被复制了九遍的一行式所在的家族。
 *
 * 触发方式选"**没有凭据**"：把 `HOME`/`USERPROFILE` 指到空目录、并去掉 `WEREAD_API_KEY`。
 * 这样与本机有没有配过微信读书无关，也不碰网络 —— 而 CI 本来就是这个状态，所以它是确定性的。
 * （本机有凭据时另外实测过一支：`weread stats --mode bogus --json` 也回 `WEREAD_FAILED`。）
 */
test('weread --json：没有凭据时 stdout 是一整份 JSON，不是一行散文', () => {
  const home = mkdtempSync(join(tmpdir(), 'weflow-weread-'))
  try {
    const env: NodeJS.ProcessEnv = { ...process.env, HOME: home, USERPROFILE: home }
    delete env.WEREAD_API_KEY
    const r = spawnSync(process.execPath,
      ['--import', 'tsx', join(process.cwd(), 'bin', 'weflow-cli.ts'), 'weread', 'shelf', '--json'],
      { cwd: process.cwd(), encoding: 'utf8', env })
    assert.equal(r.status, 1,
      `没有凭据时应当以失败退出；stdout=${JSON.stringify(r.stdout.slice(0, 200))}`)
    const body = JSON.parse(r.stdout)          // 整份解析：多一行散文就抛
    assert.equal(body.success, false)
    assert.equal(body.code, 'WEREAD_FAILED')
    assert.ok(body.error, 'error 里要有原因（这里就是"未设置 WEREAD_API_KEY"）')
  } finally {
    rmSync(home, { recursive: true, force: true })
  }
})

/** 从 `at`（函数名开头）取出配对括号里的原文；跨行的调用也能取全。 */
function callArgs(text: string, at: number): string {
  const open = text.indexOf('(', at)
  if (open < 0) return ''
  let depth = 0
  for (let i = open; i < text.length; i++) {
    if (text[i] === '(') depth++
    else if (text[i] === ')') {
      depth--
      if (depth === 0) return text.slice(open + 1, i)
    }
  }
  return ''
}

const VALIDATION_HELPERS = ['parseCliInteger', 'requireCliDate', 'requireCliChoice']

/**
 * "一行式、只打散文"的失败分支：同一行里既打彩色散文又 `exit`，且没有 json 支。
 *
 * `weread` 的 9 个调用点就是这么被复制粘贴出来的（shelf / stats / notes×2 / search / book / review /
 * discover / profile），命令的 `if (opts.json)` 支在它后面 ⇒ JSON 模式下 stdout 只有一行 `✗ …`。
 *
 * **这条只认这一种形状** —— 同样的缺陷写成多行就躲过去了，所以它不是"运行时错误都合规"的证明，
 * 只是"这个被复制了九遍的写法别再回来"的绊线。运行时错误路径这一族**没有**审计过（要逐条读，
 * 见 docs/EXTENDING.md 那段）。
 */
function isProseOnlyFailureBranch(line: string): boolean {
  const trimmed = line.trimStart()
  // 注释行不算：注释里引用旧代码是常事（这条判据自己的说明里就写了一个例子）。
  if (trimmed.startsWith('//') || trimmed.startsWith('*') || trimmed.startsWith('/*')) return false
  if (!line.includes('console.log(chalk.')) return false
  if (!line.includes('process.exit(1)')) return false
  return !line.includes('opts.json')
}

test('不再有"一行式、只打散文"的失败分支', () => {
  // 自对照：判据认不认这个形状 —— 否则"扫不到"与"文件里没有"完全一样。
  assert.equal(isProseOnlyFailureBranch('  if (!r.ok) { console.log(chalk.red(`x`)); process.exit(1) }'), true)
  assert.equal(isProseOnlyFailureBranch('  if (!r.ok) { if (opts.json) console.log(JSON.stringify({})); process.exit(1) }'), false)
  assert.equal(isProseOnlyFailureBranch('    // 例：{ console.log(chalk.red(x)); process.exit(1) }'), false)
  assert.equal(isProseOnlyFailureBranch("  console.log(chalk.red('只是一条日志'))"), false)

  const src = readFileSync(join(process.cwd(), 'bin', 'weflow-cli.ts'), 'utf8')
  const bad = src.split('\n')
    .map((line, i) => ({ line, no: i + 1 }))
    .filter(x => isProseOnlyFailureBranch(x.line))
    .map(x => `第 ${x.no} 行：${x.line.trim().slice(0, 90)}`)
  assert.deepEqual(bad, [],
    `这些失败分支只有散文、没有 json 支：机器调用方在 stdout 上拿到的就是这行字。${bad.join(' / ')}`)
})

/**
 * 上面四条是**行为**判据，只覆盖 `--line` 那一族。这一条把整族的**静态**规则钉住：
 * 声明了 `--json` 的命令里，每个校验 helper 调用都必须把 `opts.json` 传进去 —— 少传一个是**安静**
 * 的（helper 的默认值是 `json = false`），症状与下面这段历史一模一样：`--line` 的校验被手写四遍，
 * 三遍漏了 json 分支。
 *
 * 2026-10-11 手审过这一族的全部 74 处（60 处 parseCliInteger + 5 处 requireCliDate +
 * 5 处 requireCliChoice + 4 处手写 `!['…'].includes(opts.X)`），**0 处真缺陷**：唯一少传的
 * `dbkey --timeout` 在 `--json` 下不可达（命令在更上面就 `INTERACTIVE_REQUIRED` 退出了），
 * 现在也统一传上，好让这条规则没有例外。4 处手写 `includes` 各自都带着 json 分支 ——
 * 它们不受这条判据管，新写的应当改用 `requireCliChoice`（见 docs/EXTENDING.md）。
 */
test('声明了 --json 的命令里，校验 helper 一律带上 opts.json', () => {
  const src = readFileSync(join(process.cwd(), 'bin', 'weflow-cli.ts'), 'utf8')
  const marks = [...src.matchAll(/\.command\('([^']+)'\)|new Command\('([^']+)'\)/g)]
    .map(m => ({ at: m.index!, name: m[1] ?? m[2] }))
  marks.push({ at: src.length, name: '' })

  const bad: string[] = []
  let seen = 0
  for (let i = 0; i < marks.length - 1; i++) {
    const block = src.slice(marks[i].at, marks[i + 1].at)
    if (!block.includes('.action(')) continue
    // 行内注释里提到 `--json` 不算声明 —— 判法同 `test/json-surface.test.ts` 的 `declaresJson`。
    // ⚠️ `lastIndexOf` 只有两个参数：写成 `lastIndexOf('\n', 0, at)` 会用 `'0'` 当搜索串
    // （实测：整个 `run` 块因此被判成"没声明 --json"而跳过，变异没响，看着像判据有洞而其实是判据自己错）。
    const declares = [...block.matchAll(/\.option\('--json/g)].some(m => {
      const lineStart = block.lastIndexOf('\n', m.index!) + 1
      return !block.slice(lineStart, m.index!).includes('//')
    })
    if (!declares) continue

    for (const helper of VALIDATION_HELPERS) {
      for (const m of block.matchAll(new RegExp(`\\b${helper}\\(opts\\.\\w+`, 'g'))) {
        seen++
        if (callArgs(block, m.index!).includes('opts.json')) continue
        const line = src.slice(0, marks[i].at + m.index!).split('\n').length
        bad.push(`${marks[i].name}（第 ${line} 行）: ${helper}`)
      }
    }
  }

  // 反空扫：这一族有约 70 处，看到几十处以下就说明切块或正则失效了。
  assert.ok(seen >= 50, `只看到 ${seen} 处校验调用 —— 判据自己失效了，不是命令变少了`)
  assert.deepEqual(bad, [],
    `这些校验忘了把 opts.json 传进去（helper 默认 json=false），JSON 模式下会往 stdout 写彩色散文：`
    + bad.join('、'))
})
