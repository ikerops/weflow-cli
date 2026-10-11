/**
 * `D-nnn` 交叉引用**不许指向不存在的决定** —— 除非那个号在 `DECISIONS.md` 里被明确写成"从未使用"。
 *
 * 为什么值得钉：决定日志里交叉引用极密（2026-10-11 数：**484 处**），而引用是手写的字符串 ——
 * 写错一个数字、或改了号忘了改引用，就是**指向空气**，没有任何东西会响。
 *
 * 判据是"**例外必须自己写在文件里**"：`DECISIONS.md` 头部有一段说明，写着 `D-009` 从未使用、
 * D-008 直接接 D-010（历史重编号留下的空号）。扫的时候把这类"已备案的空号"一并读出来当白名单 ——
 * 于是既不会把那个**故意**的空号报成错，也不允许任何**没备案**的号被引用。
 *
 * ⚠️ 一次性探针版踩过的坑（记在这儿）：第一版把编号 `int()` 归一化，`D-009` 于是变成 9，
 * 而"9 没有定义" ⇒ 把**文件里那句说明本身**报成了悬空引用。真相靠 PowerShell 对同一事实给出相反
 * 结论才查清（`D-9` 不是 `D-009` 的子串）。所以这里两边都按整数比、且**必须**先读出备案。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'

const ROOT = process.cwd()
const read = (...parts: string[]) => readFileSync(join(ROOT, ...parts), 'utf8')

const decisions = read('docs', 'DECISIONS.md')
const defined = new Set([...decisions.matchAll(/^## D-(\d+)/gm)].map(m => Number(m[1])))
/** 已备案的空号：文件里明确写着"从未使用"的那些 */
const documentedGaps = new Set(
  [...decisions.matchAll(/`D-(\d+)`[^\n]*从未使用/g)].map(m => Number(m[1])),
)

const TEXT_EXTENSIONS = /\.(ts|tsx|js|mjs|cjs|py|md|json|yml|yaml|txt)$/

test('每个 `D-nnn` 引用都指向一个真实存在（或已备案为空号）的决定', () => {
  assert.ok(defined.size > 50, `只解析出 ${defined.size} 条决定 —— 读文件那条路断了`)
  assert.ok(documentedGaps.size >= 1, '没读到"从未使用"那类备案（改写了？）')

  const tracked = spawnSync('git', ['-c', 'core.quotePath=false', 'ls-files'], {
    cwd: ROOT, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024,
  })
  assert.equal(tracked.status, 0, `git ls-files 失败：${tracked.stderr || tracked.stdout}`)
  const files = tracked.stdout.split('\n').filter(name => name && TEXT_EXTENSIONS.test(name))

  const dangling: string[] = []
  let references = 0
  for (const name of files) {
    let text: string
    try {
      text = readFileSync(join(ROOT, name), 'utf8')
    } catch {
      continue
    }
    for (const match of text.matchAll(/\bD-(\d+)\b/g)) {
      references += 1
      const n = Number(match[1])
      if (!defined.has(n) && !documentedGaps.has(n)) {
        const line = text.slice(0, match.index).split('\n').length
        dangling.push(`${name}:${line}  ${match[0]}`)
      }
    }
  }
  // 反空扫：引用数是这个仓库的"密集"事实（约 484 处），扫到太少说明读文件那条路断了
  assert.ok(references > 200, `只扫到 ${references} 处引用 —— 扫描范围不对`)
  assert.deepEqual(dangling, [],
    `这些引用指向不存在的决定号（要么改引用，要么那个号在 DECISIONS.md 里备案为"从未使用"）：\n  ${dangling.join('\n  ')}`)
})
