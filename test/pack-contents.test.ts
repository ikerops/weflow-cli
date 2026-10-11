/**
 * npm 包**该有的东西在不在**。
 *
 * CI 的 `release-consistency` 只查"不该有的"（`output/`、`models/`、密钥、`.db`），
 * `test/panel-packaging.test.ts` 的头注释把这件事写成了"只查不该有的东西，不查该有的东西"。
 * 这一条补的就是那半 —— 它防的故障 2026-10-11 在**真实发布物**上量到了：
 * registry 上 `weflow-cli@1.9.0` 的 501 个文件里没有 `scripts/data/wechat_emoji.json`，
 * 而 `scripts/wechat_emoji.py` 读它、读失败被 `except OSError` 吞成 `_FACES = {}`。
 * 后果是 npm 装出来的用户，HTML 导出里 37 个中文表情码（`[乒乓]` `[冷汗]` `[开红包]`…）
 * 连同 67 个英文别名一起**原样显示成文本**，而 git clone 的用户看到的是表情 ——
 * 两边都不报错。根因是白名单只匹配 `scripts/` 下的 `.py`（一条 `**` 通配的 glob）——
 * `scripts/data/` 整个不在里面。
 *
 * 判据取自**真实产物**：`npm pack --dry-run --json`（干跑，不落 .tgz）。
 * 不去复刻 npm 的 glob 语义 —— 那种复刻一旦和 npm 不一致，测试就变成自说自话。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'

const ROOT = process.cwd()

function run(command: string, args: string[]): string {
  // Windows 上的 `npm` 是 `.cmd` 包装：Node 20 起不经解释器起不来。`shell: true` 能跑，
  // 但每条子进程都会带一条 DEP0190 警告（参数只拼接、不转义）。这里显式走 `cmd /c`：
  // 参数仍是数组，由 CreateProcess 负责转义，安全边界与 `shell: true` 相同，但没有那条噪声。
  // `git` 是 `.exe`，不需要绕。
  const [file, argv] = process.platform === 'win32' && command === 'npm'
    ? ['cmd', ['/c', command, ...args]]
    : [command, args]
  const result = spawnSync(file, argv, { cwd: ROOT, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 })
  assert.equal(result.status, 0, `${command} 失败：${result.stderr || result.stdout}`)
  return result.stdout
}

let cached: Set<string> | null = null

/** 干跑出来的包内路径。只跑一次（约 2 秒），两条测试共用。 */
function packedPaths(): Set<string> {
  if (!cached) {
    const parsed = JSON.parse(run('npm', ['pack', '--dry-run', '--json'])) as Array<{ files: Array<{ path: string }> }>
    cached = new Set(parsed[0].files.map(file => file.path))
  }
  return cached
}

test('scripts/ 下每个跟踪文件都进包（.ts/.mjs 开发入口除外）', () => {
  const packed = packedPaths()
  const tracked = run('git', ['-c', 'core.quotePath=false', 'ls-files', '-z', '--', 'scripts'])
    .split('\0').filter(Boolean)
  // 先挡住"扫到 0 个文件"这种**假绿**：路径写错、或 git 换了输出形状时，
  // 下面那条 deepEqual 会因为"没有缺的"而通过 —— 绿色，但什么都没看。
  assert.ok(tracked.length > 20, `只列出 ${tracked.length} 个 scripts/ 文件，扫描范围不对`)
  // `.ts` 不进包是结构性的：它们由 `tsx` 跑（`npm run eval:assistant` 等），而 tsx 是
  // devDependency —— 装好的包里根本没有让它们跑起来的前提，所以它们不需要进包。
  // `.ts` 不进包是结构性的：它们由 `tsx` 跑（`npm run eval:assistant` 等），而 tsx 是 devDependency
  // ⇒ 装好的包里没有让它们跑起来的前提。
  // `.mjs` 是另一类：`scripts/graph3d_check.mjs` 是**验证工具**（要一个装好 GPU 浏览器、且已经
  // 生成过页面的环境才用得上），不是用户会用到的东西 —— 不进包是有意的，不是漏了。
  const missing = tracked.filter(file => !packed.has(file)
    && !file.endsWith('.ts') && !file.endsWith('.mjs'))
  assert.deepEqual(missing, [], `scripts/ 下有跟踪文件没进包，装出来就缺它：${missing.join('、')}`)
})

test('README 引用的本地图片都进包', () => {
  const packed = packedPaths()
  const imageRef = /!\[[^\]]*\]\(([^)]+)\)|<img[^>]*?src=["']([^"']+)["']/gi
  const broken: string[] = []
  let found = 0
  for (const name of ['README.md', 'README.en.md']) {
    // 行内代码与围栏代码块里的 `<img src=…>` 是**举例**，不是引用（CHANGELOG 里就有一处
    // 在描述微信自己的标记），先把它们摘掉再扫。
    const text = readFileSync(join(ROOT, name), 'utf8')
      .replace(/```[\s\S]*?```/g, '')
      .replace(/`[^`\n]*`/g, '')
    for (const match of text.matchAll(imageRef)) {
      const target = (match[1] ?? match[2] ?? '').split(' ')[0].trim()
      if (!target || /^(https?:|data:|#|\/\/)/.test(target)) continue
      found += 1
      const local = target.split('#')[0].replace(/^\.?\//, '')
      if (!packed.has(local)) broken.push(`${name} -> ${target}`)
    }
  }
  // 同样先挡假绿：正则或文件名一旦对不上，`broken` 会是空，而"没有坏的"看着像通过。
  assert.ok(found > 0, '两份 README 里的本地图片一个都没扫到，判据本身坏了')
  assert.deepEqual(broken, [], `README 引用的图片不在包里，npm 页面上是裂图：${broken.join('、')}`)
})
