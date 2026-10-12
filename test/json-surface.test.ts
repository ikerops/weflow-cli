/**
 * 每个**叶子命令**（有 `.action(` 的）都要声明 `--json`。
 *
 * 为什么要有这条：`docs/EXTENDING.md` 写着"脚本或 AI 会驱动的命令都有 `--json` 模式"，但在
 * 2026-10-11 之前**没有任何东西在检查它**。那天实测：`vault search` 是唯一输出就是 JSON、而
 * `--json` 本身不存在的命令（已修，见 `test/vault-search-cli.test.ts`）—— 这条守卫把它那一类
 * 变成会红的：新加一个命令却忘了 `--json`，CI 会点名它。
 *
 * 判据读**源码**而不是 `--help`：help 的 `Commands:` 段会折行，多行描述被读成命令名
 * （第一版就这么量出 62 个"命令"，里面混着散文，见 `D:\tmp` 的教训）。分支边界按
 * `.command('x')` / `new Command('x')` 切，块里有 `.action(` 才算叶子。
 *
 * 已知看不见的：用循环或变量名**动态注册**的命令（这个文件里没有这种写法 —— 靠下面的下限控制兜底）、
 * 以及选项由 helper 统一补的命令（也没有；真出现时，症状是"某个命令被报缺 --json"，那时把
 * helper 也解析掉，而不是往白名单里塞）。
 *
 * 白名单必须带**理由**，和 `MCP_EXCLUDED`（值就是理由）同一个写法。
 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import test from 'node:test'

const ALLOWED_WITHOUT_JSON: Record<string, string> = {
  whitelist: '不传参数的那个 action 只是给人看的一张表；机器路径是 `whitelist list --json`',
  blacklist: '同上：机器路径是 `blacklist list --json`',
}

const SOURCE = join(process.cwd(), 'bin', 'weflow-cli.ts')

/**
 * 块里有没有**真的**声明机器可读那一档的旗标（行内注释里提到的不算）。
 *
 * 认全名，不认前缀：原来写 `.option('--json`，于是 `--json-result` 会**冒充**通过（2026-10-12 用
 * 驱动扫描时偶然发现：`fav export --json` 报 `unknown option` 而守卫是绿的）。只允许两种写法：
 *  · `--json` —— 约定里的那个；
 *  · `--json-result` —— `fav export` 与 `mcp-config` 用：这两个命令另有一个表示**文件格式**的
 *    `json` 参数（`-o json` / `<markdown|json>`），叫 `--json` 会分不清是格式还是输出形状。
 *    两个名字都写进了 `capabilities --json`（`favorites.export`、`mcpConfiguration`）。
 * 再出现第三种写法，得先在这里加一行并说明理由。
 */
function declaresJson(block: string): boolean {
  for (const m of block.matchAll(/\.option\('([^']+)'/g)) {
    const at = m.index!
    const lineStart = block.lastIndexOf('\n', at) + 1
    if (block.slice(lineStart, at).includes('//')) continue
    const flag = m[1].split(',').pop()!.trim().split(/\s+/)[0]
    if (flag === '--json' || flag === '--json-result') return true
  }
  return false
}

function leafCommands(src: string): { name: string; line: number; hasJson: boolean }[] {
  const marks = [...src.matchAll(/\.command\('([^']+)'\)|new Command\('([^']+)'\)/g)]
    .map(m => ({ at: m.index!, name: m[1] ?? m[2] }))
  marks.push({ at: src.length, name: '' })
  const leaves: { name: string; line: number; hasJson: boolean }[] = []
  for (let i = 0; i < marks.length - 1; i++) {
    const block = src.slice(marks[i].at, marks[i + 1].at)
    if (!block.includes('.action(')) continue
    // 名字**不唯一**：`list` / `clear` / `stats` 这些在不同父命令下各有一个（实测 13 个重名）。
    // 所以报出来时必须带行号，否则"`list` 缺 --json"这句话指不回任何地方。
    const line = src.slice(0, marks[i].at).split('\n').length
    leaves.push({ name: marks[i].name, line, hasJson: declaresJson(block) })
  }
  return leaves
}

test('每个叶子命令都声明了 --json（例外要写进白名单并给理由）', () => {
  const src = readFileSync(SOURCE, 'utf8')
  const leaves = leafCommands(src)

  // 反空扫：判据看漏了命令（切块规则被改、注册方式变了）不能让这条静默变绿。
  assert.ok(leaves.length >= 100,
    `只看出 ${leaves.length} 个叶子命令 —— 判据自己失效了，不是命令变少了`)

  const missing = leaves.filter(l => !l.hasJson)
  const unexpected = missing.filter(l => !(l.name in ALLOWED_WITHOUT_JSON))
  assert.deepEqual(unexpected.map(l => `${l.name}（第 ${l.line} 行）`), [],
    `这些命令没有 --json：${unexpected.map(l => `${l.name}（第 ${l.line} 行）`).join('、')}\n`
    + '要么给它声明一个，要么写进本文件的 ALLOWED_WITHOUT_JSON 并写清理由'
    + '（机器路径在别的子命令上、不打算让脚本驱动，都算理由）。')

  // 白名单也得是活的：例外被补上 --json 之后还留着，下次就没人看它了。
  const names = missing.map(l => l.name)
  const stale = Object.keys(ALLOWED_WITHOUT_JSON).filter(n => !names.includes(n))
  assert.deepEqual(stale, [],
    `白名单里这些已经有 --json 了，请删掉：${stale.join('、')}`)
})
