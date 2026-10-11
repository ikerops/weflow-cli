/**
 * 文档里的**相对链接**必须指向存在的文件 —— 与 `test/decision-refs.test.ts` 同类：
 * 指针指向空气，没有任何东西会响，而这些都是发给用户看的文档。
 *
 * 2026-10-11 量过：30 份 markdown、**93 条相对链接、0 死链**。放进守卫是为了那份"今天准"不会
 * 因为某次改文档名而静默变成假的。
 *
 * ⚠️ 不查链接里的 `#锚点`：那要把标题转成 slug 才算得准，而各渲染器的规则不完全一样 ——
 * 与其写一条会误报的判据，不如只查"文件在不在"这一半（这一半是确定的）。
 * ⚠️ 只看**已跟踪**的文件（`git ls-files`）：新写、还没 `git add` 的文件看不见 ——
 * 上一轮做变异时就栽在这上面（种在未跟踪的新文件里，"没命中"看着像"守卫没生效"）。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { join, posix } from 'node:path'
import { spawnSync } from 'node:child_process'

const ROOT = process.cwd()
const LINK = /!?\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g

test('文档里的相对链接都指向存在的文件', () => {
  const tracked = spawnSync('git', ['-c', 'core.quotePath=false', 'ls-files'], {
    cwd: ROOT, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024,
  })
  assert.equal(tracked.status, 0, `git ls-files 失败：${tracked.stderr || tracked.stdout}`)
  const docs = tracked.stdout.split('\n').filter(name => name.endsWith('.md'))
  assert.ok(docs.length > 10, `只列出 ${docs.length} 份 markdown —— 扫描范围不对`)

  const broken: string[] = []
  let checked = 0
  for (const name of docs) {
    const text = readFileSync(join(ROOT, name), 'utf8')
    for (const match of text.matchAll(LINK)) {
      const target = match[1].split('#')[0].trim()
      if (!target || /^(https?:|mailto:|data:|#)/.test(target)) continue
      checked += 1
      const rel = posix.normalize(posix.join(posix.dirname(name), target))
      if (!existsSync(join(ROOT, ...rel.split('/')))) {
        const line = text.slice(0, match.index).split('\n').length
        broken.push(`${name}:${line}  ->  ${target}`)
      }
    }
  }
  // 反空扫：这个仓库的文档互链很密（约 93 条），扫到太少说明读文件那条路断了
  assert.ok(checked > 50, `只扫到 ${checked} 条相对链接 —— 扫描范围不对`)
  assert.deepEqual(broken, [], `这些链接指向不存在的文件（改名/移目录后忘了改）：\n  ${broken.join('\n  ')}`)
})
