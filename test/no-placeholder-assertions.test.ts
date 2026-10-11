/**
 * 占位式断言：**两边都是同一个字面量**的断言什么也没钉住，但看起来像覆盖。
 *
 * `test/assistant-tools-surface.test.ts` 里有一句注释专门警告这个形状（"占位式的 assert.ok(true)
 * 看着像覆盖，其实什么也没钉住"）—— 这条守卫把那句话变成一条会红的线。立论同 `no-console-windows.test.ts`：
 * 没有运行时行为可测，就把文件当文本读，钉住一条不该松的线。
 *
 * 判据故意**收窄**到只有字面量对字面量的形态（`assert.ok(true)`、`assert.equal(1, 1)`、
 * `assert.deepEqual([], [])` …，且两侧**逐字相同**）：`assert.equal(body.ok, true)` 那一侧是变量、
 * 是真断言，不能报。
 *
 * ⚠️ 这里**不做**"每个 test 块里有没有断言"的检查：那需要真解析器。试过两版静态做法都被 TS 骗过 ——
 * 一次是**标题里的花括号**（`test('别的 {{...}} 原样保留', …)`）让括号配对切进标题内部，
 * 一次是**正则字面量里的引号**（`/['"]/`）让剥字符串的扫描器失步、把后面整段吞掉。
 * 两次的输出都和"真有问题"长得一样，所以那个问题到此为止。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

const FILES = readdirSync(join(process.cwd(), 'test')).filter(name => name.endsWith('.test.ts'))

/** 去掉块注释与行注释 —— 只在"**缩小**扫描范围"这个方向出错，不会凭空造出命中。 */
function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '')
}

/** 字面量（数字 / true / false / 引号字符串），用于 backreference 两侧逐字相同 */
const LITERAL = String.raw`(?:'[^']*'|"[^"]*"|\d+(?:\.\d+)?|true|false)`

const DEAD_FORMS: Array<[string, RegExp]> = [
  // 说明文字里**不能**写出被查的那个形态（写成 `assert.ok(true)` 的话，这条守卫会命中自己 —— 第一版就是）
  ['assert.ok(…) 恒真', /assert\.ok\(\s*true\s*[,)]/],
  ['assert.<equal>(X, X) —— 两侧逐字相同', new RegExp(String.raw`assert\.(?:equal|strictEqual|deepEqual|notEqual)\(\s*${LITERAL}\s*,\s*\1\s*[,)]`)],
]

test('没有"两边都是同一个字面量"的占位式断言', () => {
  const hits: string[] = []
  let scanned = 0
  for (const name of FILES) {
    const source = stripComments(readFileSync(join(process.cwd(), 'test', name), 'utf8'))
    scanned += source.length
    for (const [label, pattern] of DEAD_FORMS) {
      for (const match of source.matchAll(new RegExp(pattern.source, 'g'))) {
        const line = source.slice(0, match.index).split('\n').length
        hits.push(`${name}:${line}  ${match[0].trim()}   (${label})`)
      }
    }
  }
  // 反空扫：扫到的字符数太少说明读文件的这条路断了，那时的"没有命中"不作数
  assert.ok(scanned > 200_000, `只扫到 ${scanned} 个字符 —— 测试目录没读到？`)
  assert.deepEqual(hits, [], `占位式断言不会让任何东西红，但它会让人以为已经覆盖了：\n  ${hits.join('\n  ')}`)
})
