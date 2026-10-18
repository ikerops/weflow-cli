/**
 * `search` 的**命令行面**：声明了的旗标必须真的被用上、转发路径必须分类。
 *
 * 为什么要有这条：`--no-rerank` 在 Python 侧一直存在（`scripts/semantic_search.py search
 * --no-rerank`），`docs/OPERATIONS.md` 也一直教用户敲它，而 CLI 从来没给它接通道 —— 用户会拿到
 * `unknown option`。2026-10-11 由**文档命令面**那道检查（`scripts/check_readme_commands.py`
 * 扫文档代码块）发现：它扫到 `OPERATIONS.md:557` 的 `search "..." --no-rerank` 在 CLI 里不存在。
 *
 * 同款的守卫在 `test/contact-schema-cli.test.ts`（"声明了的每个旗标都必须真的转发给脚本"）。
 * 这一条不能照抄那个判据：那边每个旗标都走 argv 字面量，而这里
 *   · `--api-key` **故意走 env**（`pythonProcessEnv(..., 'DASHSCOPE_API_KEY')`）——
 *     密钥不进 argv 是安全约束（`test/privacy-paths.test.ts`、`docs/EXTENDING.md`）；
 *   · `--json` / `--yes` / `--dry-run` 只在本进程里用。
 * 所以判据是两条：(1) 每个声明过的旗标都必须被归进 `toScript` 或 `localOnly` 之一 —— 新加的旗标
 * 必须**显式分类**，不能悄悄漏过；(2) 归进 `toScript` 的必须在 action 体里以字面量出现（真的转发），
 * 且每个旗标都要在 action 里被**读**过（`opts.<名字>`），否则它就是个死开关。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const SRC = readFileSync(join(process.cwd(), 'bin', 'weflow-cli.ts'), 'utf8')

/** `search` 那段源码：**顶层**那条（`program` 直接挂的），到下一个 `.command(` 之前。
 *
 * 先按 `.command('search')` 找是错的：别的命令下也有同名的子命令（实测会命中一个带
 * `--limit` / `--json` 的），于是这条守卫会去检查一段根本不是它的源码 —— 自己的定位错了，
 * 报出来的却像"`--no-rerank` 不见了"。所以按 `program` 那一层锚，并让调用方验证锚对了。
 */
function searchBlock(src: string): string {
  const m = /program\s*\n\s*\.command\('search'\)/.exec(src)
  if (!m) return ''
  const end = src.indexOf('.command(', m.index + m[0].length)
  return src.slice(m.index, end > m.index ? end : src.length)
}

/** 声明过的旗标（`--line <line>` → `--line`；`--no-rerank` → `--no-rerank`） */
function declaredFlags(block: string): string[] {
  return [...block.matchAll(/\.option\('([^']+)'/g)]
    .map(m => m[1].split(',').pop()!.trim().split(/\s+/)[0])
}

/** 旗标 → commander 放进 `opts` 的那个名字（`--no-rerank` → `rerank`，`--top-k` → `topK`） */
function optName(flag: string): string {
  const bare = flag.replace(/^--/, '').replace(/^no-/, '')
  return bare.replace(/-([a-z])/g, (_, c: string) => c.toUpperCase())
}

const TO_SCRIPT = ['--top-k', '--line', '--no-rerank', '--no-hybrid', '--from', '--rerank-pool', '--no-dedup', '--with-text', '--fetch-missing', '--mmr', '--phrase']   // 走 argv 转给 python
const LOCAL_ONLY = ['--api-key', '--dry-run', '--yes', '--json']  // 密钥走 env；其余本进程用

test('search：声明了的旗标都得被分类、被读到、该转发的真的转发', () => {
  const block = searchBlock(SRC)
  assert.ok(block.length > 0, '定位不到 search 的命令块（改名了？）')
  // 先证明**锚对了**：定位错了的话，下面那些断言说的是另一段源码（我自己第一版就踩了这个）
  assert.match(block, /WEFLOW_SEARCH_QUERY/, '定位到的不是顶层那条 search（锚点失效了）')
  const declared = declaredFlags(block)
  assert.ok(declared.includes('--no-rerank'),
    `这条守卫存在的理由就是它：现在声明的是 ${declared.join(' / ')}`)

  // (1) 分类：新旗标必须显式决定它走 argv 还是只在本进程用 —— 不能默认漏过
  const unclassified = declared.filter(f => !TO_SCRIPT.includes(f) && !LOCAL_ONLY.includes(f))
  assert.deepEqual(unclassified, [],
    `这些旗标没被分类（新加的？决定它走 argv 还是本进程用，再写进本文件）：${unclassified.join('、')}`)

  // 只看 `.action(` 之后：声明处自己也有那些字符串
  const action = block.slice(block.indexOf('.action('))
  assert.ok(action.length > 0, '定位不到 action 体（结构变了？）')

  // (2) 转发：走 argv 的必须以字面量出现在 action 里
  for (const flag of declared.filter(f => TO_SCRIPT.includes(f))) {
    assert.ok(action.includes(`'${flag}'`),
      `${flag} 声明了但没转发给脚本 —— 命令会静默跑成默认行为`)
  }
  // (2b) 读了没有：声明而不读，就是个死开关（`--no-rerank` 当初正是这个形状）
  for (const flag of declared) {
    assert.ok(action.includes(`opts.${optName(flag)}`),
      `${flag} 声明了却没人读 opts.${optName(flag)} —— 这个开关按下去不会有任何变化`)
  }
})

test('search：--phrase 与向量检索的旗标同时出现要报错，不能静默忽略', async () => {
  // 向量检索的旗标在 `--phrase` 那条路上不生效。以前它们被原样转发、在 Python 里被丢掉，
  // 用户以为调过参，实际跑的是默认行为 —— 这比报错更贵。这里真的起一次 CLI 验证拒绝发生在**调脚本之前**。
  const { execFile } = await import('node:child_process')
  const { promisify } = await import('node:util')
  const run = promisify(execFile)
  let stdout = ''
  try {
    const r = await run(process.execPath,
      ['--import', 'tsx', join(process.cwd(), 'bin', 'weflow-cli.ts'),
        'search', '原样短语定位', '--phrase', '--mmr', '--json'],
      { cwd: process.cwd(), timeout: 60_000, encoding: 'utf8' })
    stdout = r.stdout
  } catch (e: any) {
    stdout = String(e.stdout || '')
  }
  const at = stdout.indexOf('{')
  assert.ok(at >= 0, `没有 JSON 输出（拒绝发生得太晚，或根本没拒绝）：${stdout.slice(0, 200)}`)
  const parsed = JSON.parse(stdout.slice(at))
  assert.equal(parsed.code, 'INVALID_ARGUMENT')
  assert.match(parsed.error, /--mmr/)
})
