/**
 * 2026-09-29 三个界面对齐时新加的那批工具。
 *
 * 为什么单独一个文件：这批工具是"本来就暴露给 MCP、但聊天这条路上没有"的能力
 * （`fetch_article` / `search_public` / `format_article` / `list_themes` / `get_review` /
 * `get_concepts` / `list_contacts` / `lint_wiki` / `check_skills`）加上一条低风险的写操作
 * （`set_todo_status`）。它们的失败方式与老工具不同：
 * - **网络类**：白名单/逐跳复检写错 → 抓到不该抓的东西（SSRF），而且不报错；
 * - **只读类**：把本机标识（wxid）或本地路径漏进模型上下文；
 * - **写操作**：指错条目 —— 用户以为改的是 A，实际改的是 B。
 *
 * HOME 与几个目录都指到临时夹具（`WEFLOW_ASSISTANT_*` 注入点），所以断言不依赖这台机器。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const HOME = mkdtempSync(join(tmpdir(), 'weflow-tools-surface-'))
process.env.HOME = HOME
process.env.USERPROFILE = HOME

const BIZ_DAILY = join(HOME, 'biz-daily')
const REVIEWS = join(HOME, 'reviews')
const SKILLS = join(HOME, 'skills')
mkdirSync(join(BIZ_DAILY, '2026-01-01'), { recursive: true })
writeFileSync(join(BIZ_DAILY, '2026-01-01', '.articles.json'), JSON.stringify([
  { title: '扩散模型综述', source: '某号', topic: 'AI', summary: '一篇综述' },
  { title: '城市更新观察', source: '另一个号', topic: '新闻', summary: '城市更新' },
]), 'utf8')
writeFileSync(join(BIZ_DAILY, '2026-01-01', 'README.md'), '# 2026-01-01 日报\n\n第一篇讲扩散模型。\n', 'utf8')
mkdirSync(join(BIZ_DAILY, '2026-01-02'), { recursive: true })
writeFileSync(join(BIZ_DAILY, '2026-01-02', '.articles.json'), JSON.stringify([
  { title: '扩散模型的训练技巧', source: '某号', topic: 'AI', summary: '训练相关' },
]), 'utf8')
mkdirSync(REVIEWS, { recursive: true })
writeFileSync(join(REVIEWS, 'Daily-2026-01-02.md'), '# 学习回顾\n\n今天读了扩散模型那篇。\n', 'utf8')
mkdirSync(join(SKILLS, 'good-skill'), { recursive: true })
writeFileSync(join(SKILLS, 'good-skill', 'SKILL.md'),
  '---\nname: 好技能\ndescription: 干点事\n---\n\n# 正文\n', 'utf8')

process.env.WEFLOW_ASSISTANT_BIZ_DAILY_DIR = BIZ_DAILY
process.env.WEFLOW_ASSISTANT_REVIEWS_DIR = REVIEWS
process.env.WEFLOW_ASSISTANT_SKILL_DIRS = SKILLS

// 知识库那两条线也要指到夹具（`get_concepts` / `lint_wiki` 都从 `WEFLOW_ASSISTANT_VAULT_DIR` 取）。
// 不注入的话它们读的是开发机上真实的 `output/wechat-vault` —— 本机有、CI 上没有，于是
// `lint_wiki` 在 CI 上直接"目录不存在，跳过"（2026-10-11 报红 2 条；顺带暴露出那一段的假绿：
// 一条线都没体检过，它却说"两个知识库都没有断链/孤儿/空页/同名"，那句假话已改）。
const VAULT = join(HOME, 'wechat-vault')
const VAULT_LINES: Array<[string, string[]]> = [['Wiki', ['夹具一', '夹具二']], ['Chat', ['夹具聊天']]]
for (const [lineDir, pages] of VAULT_LINES) {
  mkdirSync(join(VAULT, lineDir, 'Concepts'), { recursive: true })
  for (const page of pages) {
    writeFileSync(join(VAULT, lineDir, 'Concepts', `${page}.md`), `# ${page}\n\n夹具概念页。\n`, 'utf8')
  }
  writeFileSync(join(VAULT, lineDir, '00-Overview.md'), `# 总览\n\n共 ${pages.length} 个概念。\n`, 'utf8')
}
process.env.WEFLOW_ASSISTANT_VAULT_DIR = VAULT

const { executeTool, TOOL_DEFS, MCP_TOOL_DEFS, wikiIndexPath } = await import('../src/services/assistantTools.js')
const { AssistantMemory } = await import('../src/services/assistantMemory.js')
const { chatService } = await import('../src/services/chatService.js')
const { setScriptRunner } = await import('../src/services/pythonBridge.js')

const svc = chatService as any
const realFetch = globalThis.fetch
let fetchCalls: Array<{ url: string; init?: any }> = []
/** 默认：任何真实请求都炸——测网络工具时必须显式给响应 */
function stubFetch(handler?: (url: string, init?: any) => Response): void {
  fetchCalls = []
  globalThis.fetch = (async (url: any, init?: any) => {
    fetchCalls.push({ url: String(url), init })
    if (handler) return handler(String(url), init)
    throw new Error('这条用例不该真的发请求')
  }) as any
}
test.beforeEach(() => stubFetch())
test.after(() => { globalThis.fetch = realFetch })

function ctx() { return { userId: 'u-surface', memory: new AssistantMemory() } }
const run = (name: string, args: Record<string, any> = {}) => executeTool(name, args, ctx() as any)

function stubScript(stdout: string, code = 0, stderr = '') {
  const calls: { script: string; args: string[] }[] = []
  setScriptRunner(async (script: string, args: string[]) => {
    calls.push({ script, args })
    return { stdout, stderr, code }
  })
  return calls
}

const resetScripts = () => setScriptRunner(null)

// --------------------------------------------------------------- list_contacts

test('list_contacts：只给名字，绝不给 wxid 或头像地址', async () => {
  svc.listContacts = async () => ([
    { username: 'wxid_secret_a', displayName: '老王', remark: '客户-王总', nickname: '老王', alias: 'wang', avatarUrl: 'https://x/a.png' },
    { username: 'wxid_secret_b', displayName: '小李', nickname: '小李子' },
  ])
  const out = await run('list_contacts', {})
  assert.match(out, /客户-王总/, '有备注就用备注（那是用户自己起的名字）')
  assert.match(out, /小李/)
  assert.doesNotMatch(out, /wxid_secret/, 'wxid 是本机标识，不许进模型上下文')
  assert.doesNotMatch(out, /avatar|\.png/, '头像地址也不给')
  assert.match(out, /2 个名字/)
})

test('list_contacts：通讯录为空与"搜不到这个名字"是两句不同的话', async () => {
  svc.listContacts = async () => ([])
  assert.match(await run('list_contacts', {}), /空的或读不到/)
  assert.match(await run('list_contacts', { keyword: '老王' }), /没有匹配「老王」/)
})

// --------------------------------------------------------------- get_review

test('get_review：默认给最近一份，指定日期给那一份，日期格式错要说清', async () => {
  const out = await run('get_review', {})
  assert.match(out, /2026-01-02 学习回顾/)
  assert.match(out, /扩散模型那篇/)
  assert.match(await run('get_review', { date: '2026-01-02' }), /扩散模型/)
  assert.match(await run('get_review', { date: '2026-01-09' }), /没有学习回顾/)
  assert.match(await run('get_review', { date: '今天' }), /日期格式不对/)
})

test('get_review：目录不存在时说"还没跑过 review"，不是"今天没有"', async () => {
  // 注入点是惰性读的（每次调用才看环境变量），所以这里换得动。
  // 这条**必须是真断言**：占位式的 assert.ok(true) 看着像覆盖，其实什么也没钉住。
  const saved = process.env.WEFLOW_ASSISTANT_REVIEWS_DIR
  try {
    process.env.WEFLOW_ASSISTANT_REVIEWS_DIR = join(HOME, '根本没有这个目录')
    const out = await run('get_review', {})
    assert.match(out, /还没有学习回顾/)
    assert.match(out, /review/, '要说清是哪个命令还没跑过')
  } finally {
    process.env.WEFLOW_ASSISTANT_REVIEWS_DIR = saved
  }
})

test('wikiIndexPath：总览索引在概念目录的上一层（这段路径写错过一次）', () => {
  // 出错的形式是 join(dir, '00-Overview.md')：那会指到 `Wiki/Concepts/00-Overview.md`，
  // 而真实文件在 `Wiki/00-Overview.md` —— 读了半天读不到，且没有任何报错。
  const p = wikiIndexPath(join('/', 'vault', 'Wiki', 'Concepts'))
  assert.equal(p.replace(/\\/g, '/'), '/vault/Wiki/00-Overview.md')
  const c = wikiIndexPath(join('/', 'vault', 'Chat', 'Concepts'))
  assert.equal(c.replace(/\\/g, '/'), '/vault/Chat/00-Overview.md')
  assert.ok(!p.includes('Concepts'), '索引不在概念目录**里面**')
})

// --------------------------------------------------------------- get_concepts

test('get_concepts：两线都要读，读不到的那条要说出来', async () => {
  const out = await run('get_concepts', {})
  // 总览在概念目录的**上一层**（这里断言的就是这条：写错路径会一直读不到）
  // **两条线必须各自标对**。之前这里写成「两条线之一」就行，松到抓不到一个真 bug：
  // 标签判据里 `/[\/]/` 的反斜杠被吃掉一层，Windows 上两条线都被标成「文章线」，
  // 而这条断言照样绿。
  assert.match(out, /【文章线】/, '文章线要标出来')
  assert.match(out, /【聊天线】/, '聊天线也要标出来，而且不能标错')
  // 内容本身取决于这个检出里有没有编译过知识库（CI 上没有），所以只断言"两线各自标对"；
  // **索引路径本身由纯函数 wikiIndexPath 直接测**——那条是确定性的，而且正是出错的那段逻辑。
})

// --------------------------------------------------------------- lint_wiki

test('lint_wiki：两个知识库都体检，数字来自脚本而不是编的', async () => {
  const calls = stubScript(JSON.stringify({
    pages: 12, broken: [{ name: 'x' }], orphans: [], empty: [], duplicateTitles: {},
  }))
  try {
    const out = await run('lint_wiki', {})
    assert.match(out, /断链 1/)
    assert.match(out, /12 张页/)
    assert.equal(calls.filter(c => c.script.includes('wiki_lint')).length, 2, '文章线与聊天线各查一次')
    assert.ok(calls.every(c => c.args.includes('--json')), '要机器可读')
  } finally { resetScripts() }
})

test('lint_wiki：脚本失败时不许说"体检通过"', async () => {
  stubScript('', 1, 'python 炸了')
  try {
    const out = await run('lint_wiki', {})
    assert.match(out, /体检失败/)
    assert.doesNotMatch(out, /都没有断链/)
  } finally { resetScripts() }
})

test('lint_wiki：两条线都不存在时也不许说"都干净"（跳过 ≠ 通过）', async () => {
  // CI 上的真实处境：检出里没有 `output/wechat-vault`。原来那一支会先加一句
  // "两个知识库都没有断链/孤儿/空页/同名"，紧接着两行"目录不存在，跳过"——**没看过却说干净**。
  // 用户没编译过知识库时撞的是同一句话。
  const realVault = process.env.WEFLOW_ASSISTANT_VAULT_DIR
  process.env.WEFLOW_ASSISTANT_VAULT_DIR = join(HOME, 'no-such-vault')
  stubScript(JSON.stringify({ pages: 0, broken: [], orphans: [], empty: [], duplicateTitles: {} }))
  try {
    const out = await run('lint_wiki', {})
    assert.doesNotMatch(out, /都没有断链/, '一条线都没体检过，不能说"都干净"')
    assert.match(out, /【文章线】目录不存在，跳过/)
    assert.match(out, /【聊天线】目录不存在，跳过/)
  } finally {
    process.env.WEFLOW_ASSISTANT_VAULT_DIR = realVault
    resetScripts()
  }
})

// --------------------------------------------------------------- check_skills

test('check_skills：报出问题清单，干净时说干净', async () => {
  const out = await run('check_skills', {})
  assert.match(out, /技能目录/)
  assert.match(out, /good-skill|1 个技能/)
  assert.match(out, /没有读不出来或撞名的技能/, '干净时要明确说干净，而不是沉默')
})

// --------------------------------------------------------------- format / themes

test('list_themes 与 format_article：主题名来自真实现，转换是纯本地的', async () => {
  const themes = await run('list_themes', {})
  assert.match(themes, /可用主题/)
  const out = await run('format_article', { content: '# 标题\n\n正文一段。' })
  assert.match(out, /主题: default/)
  assert.match(out, /<h1|<section|<div/, '要真的产出 HTML')
  assert.match(out, /正文一段/)
  assert.equal(fetchCalls.length, 0, '排版不许联网')
})

test('format_article：content 为空时是参数错误，不是产出空 HTML', async () => {
  assert.match(await run('format_article', {}), /参数错误/)
})

// --------------------------------------------------------------- fetch_article

const ARTICLE_HTML = '<html><head><title>标题</title><meta property="og:title" content="扩散模型综述">'
  + '</head><body><div id="js_content"><p>' + '正文内容。'.repeat(20) + '</p>'
  + '<img data-src="https://mmbiz.qpic.cn/a.png"></div><script></script></body></html>'

test('fetch_article：只接受 mp.weixin.qq.com，伪装域名一个请求都不发', async () => {
  for (const bad of ['https://mp.weixin.qq.com.evil.test/s/x', 'http://mp.weixin.qq.com/s/x',
                     'https://evil.test/x', 'file:///etc/passwd', '']) {
    const out = await run('fetch_article', { url: bad })
    assert.match(out, /只接受 https 的 mp\.weixin\.qq\.com/, `「${bad}」应当被拒`)
  }
  assert.equal(fetchCalls.length, 0, '被拒的 URL 连请求都不该发出去')
})

test('fetch_article：正常正文能取出来，并带上标题与配图', async () => {
  stubFetch(() => new Response(ARTICLE_HTML, { status: 200 }))
  const out = await run('fetch_article', { url: 'https://mp.weixin.qq.com/s/abc' })
  assert.match(out, /扩散模型综述/, '标题要带出来')
  assert.match(out, /正文内容/)
  assert.match(out, /mmbiz\.qpic\.cn/, '配图链接要列出来')
})

test('fetch_article：重定向离开白名单就放弃（默认 follow 会把这层绕过去）', async () => {
  // 入口合法、302 跳到内网地址：逐跳复检要拦住它
  stubFetch((url) => url.includes('mp.weixin.qq.com')
    ? new Response('', { status: 302, headers: { location: 'http://127.0.0.1:8766/api/status' } })
    : new Response('内部接口', { status: 200 }))
  const out = await run('fetch_article', { url: 'https://mp.weixin.qq.com/s/abc' })
  assert.match(out, /抓取失败|取不出正文/, '跟到白名单外就该停下')
  assert.match(out, /HTTP 302/, '而且要说清是被跳转挡下的')
  // **这一条才是真正盯着逐跳复检的**：必须自己跟跳转（redirect: 'manual'），
  // 让 fetch 自动 follow 就等于跳转后的地址完全不检查。踩过：只用「不许请求内网地址」
  // 断言时，把逐跳复检整段删掉测试照样绿——因为 follow 是 undici 内部做的，
  // 打桩的 fetch 根本看不到第二跳，那条断言永远是绿的。
  assert.equal(fetchCalls.length, 1, '自己跟跳转：跳转由我们处理，不该再发第二次请求')
  assert.equal(fetchCalls[0].init?.redirect, 'manual', '必须显式 manual，否则白名单只挡了入口')
  assert.ok(!fetchCalls.some(c => c.url.includes('127.0.0.1')), '内网地址一个请求都不许发')
})

test('fetch_article：已删除的文章给明确说法', async () => {
  stubFetch(() => new Response('<div id="js_content">已被发布者删除</div>', { status: 200 }))
  assert.match(await run('fetch_article', { url: 'https://mp.weixin.qq.com/s/x' }), /已被发布者删除/)
})

// --------------------------------------------------------------- search_public

const SOGOU_HTML = '<div class="news-list"><ul><li>'
  + '<h3><a href="/link?url=1">扩散模型<em>综述</em></a></h3>'
  + '<span class="all-time-y2">某号</span>'
  + '<p class="txt-info">这是一段足够长的摘要，用来验证解析。</p>'
  + '</li><li>'
  + '<h3><a href="/link?url=2">第二个标题</a></h3>'
  + '<span class="all-time-y2">另一个号</span>'
  + '<p class="txt-info">第二段足够长的摘要文字，也要被解析出来。</p>'
  + '</li></ul></div>'

test('search_public：解析出标题/公众号/摘要，并说清这不是全文', async () => {
  stubFetch(() => new Response(SOGOU_HTML, { status: 200 }))
  const out = await run('search_public', { keyword: '扩散模型' })
  assert.match(out, /扩散模型综述/, 'em 标签要被去掉、文字留下')
  assert.match(out, /某号/)
  assert.match(out, /这是一段足够长的摘要/)
  assert.match(out, /不是全文/, '要说清给的是搜索页摘要')
  assert.match(out, /fetch_article/, '并指出要全文该用哪个工具')
})

test('search_public：一条都没解析出来时说"没搜到"，而不是回空', async () => {
  stubFetch(() => new Response('<html>没有结果</html>', { status: 200 }))
  assert.match(await run('search_public', { keyword: '不存在的东西' }), /没搜到/)
})

test('search_public：缺关键词是参数错误；网络失败要说成失败', async () => {
  assert.match(await run('search_public', {}), /参数错误/)
  stubFetch(() => { throw new Error('断网') })
  assert.match(await run('search_public', { keyword: 'x' }), /搜索失败/)
})

// --------------------------------------------------------------- set_todo_status

const TODOS = JSON.stringify({
  items: [
    { id: 'todo_1', task: '交季度报表', urgency: '高', deadline: '本周五', status: 'pending' },
    { id: 'todo_2', task: '回老王的邮件', urgency: '中', deadline: '未提及', status: 'pending' },
    { id: 'todo_3', task: '回老王的邮件', urgency: '低', deadline: '未提及', status: 'pending' },
  ],
  extracted: true, count: 3,
})

test('set_todo_status：按 id 改状态，命令与 id 都对', async () => {
  const calls = stubScript(TODOS)
  try {
    assert.match(await run('set_todo_status', { id: 'todo_1', status: 'done' }), /已标记完成: todo_1/)
    assert.deepEqual(calls[0].args, ['done', 'todo_1', '--json'])
    assert.match(await run('set_todo_status', { id: 'todo_2', status: 'pending' }), /已取消完成标记/)
    assert.deepEqual(calls[1].args, ['undone', 'todo_2', '--json'])
  } finally { resetScripts() }
})

test('set_todo_status：按文字匹配时，唯一才动；0 条或重复都拒绝', async () => {
  stubScript(TODOS)
  try {
    // 唯一命中
    assert.match(await run('set_todo_status', { task: '交季度报表', status: 'done' }), /todo_1/)
    // 两条同名 → 拒绝，并列出候选，**不许挑一个**
    const dup = await run('set_todo_status', { task: '回老王的邮件', status: 'done' })
    assert.match(dup, /匹配到 2 条/)
    assert.match(dup, /请用 id 指定/)
    // 没有 → 拒绝
    assert.match(await run('set_todo_status', { task: '不存在的事', status: 'done' }), /没有任务文字包含/)
  } finally { resetScripts() }
})

test('set_todo_status：参数校验（状态、以及必须给出指哪一条）', async () => {
  stubScript(TODOS)
  try {
    assert.match(await run('set_todo_status', { id: 'todo_1', status: '删掉' }), /status 只能是/)
    assert.match(await run('set_todo_status', { status: 'done' }), /要给 id 或 task/)
  } finally { resetScripts() }
})

test('工具表里没有任何删除待办的工具（低风险子集就是子集）', () => {
  const names = TOOL_DEFS.map(t => t.function.name)
  assert.equal(names.includes('remove_todo'), false)
  assert.equal(names.includes('delete_todo'), false)
  assert.equal(names.includes('create_todo'), false)
  // 写操作只有一个，而且只翻转状态
  assert.equal(names.filter(n => /todo/.test(n)).sort().join(','), 'get_todos,set_todo_status')
})

// --------------------------------------------------------------- get_daily_report（此前从没被执行过）

test('get_daily_report：给了 date 就是那一天；不给 date 只给关键词就跨日期搜', async () => {
  const one = await run('get_daily_report', { date: '2026-01-01' })
  assert.match(one, /2026-01-01 日报共 2 篇/)
  assert.match(one, /扩散模型综述/)
  assert.doesNotMatch(one, /训练技巧/, '指定日期就只给那一天')

  const cross = await run('get_daily_report', { keyword: '扩散模型' })
  // 窗口是 min(days, 有数据的天数)：夹具只有两天，所以它如实报「最近 2 天」
  assert.match(cross, /最近 \d+ 天里匹配 2 篇/, '跨日期应当两天都命中')
  assert.match(cross, /共扫 \d+ 篇/, '要说清扫了多少篇')
  assert.match(cross, /2026-01-01/)
  assert.match(cross, /2026-01-02/)

  const topic = await run('get_daily_report', { topic: '新闻' })
  assert.match(topic, /城市更新观察/)
  assert.doesNotMatch(topic, /扩散模型综述/)
})

test('get_daily_report：full 给的是人读的那份正文，不是条目清单', async () => {
  const out = await run('get_daily_report', { date: '2026-01-01', full: true })
  assert.match(out, /日报正文/)
  assert.match(out, /第一篇讲扩散模型/, '这是 README.md 里的句子')
  assert.doesNotMatch(out, /· \[AI\]/, '不是条目清单')
})

test('get_daily_report：日期格式错、没有数据的日期，都说清是哪一种', async () => {
  assert.match(await run('get_daily_report', { date: '2026-1-1' }), /日期格式不对/)
  assert.match(await run('get_daily_report', { date: '2026-01-09' }), /没有日报。可用日期/)
  assert.match(await run('get_daily_report', { keyword: '根本没有的词' }), /没有匹配/)
})

// --------------------------------------------------------------- MCP 边界

test('新工具在 MCP 表里的取舍：只读的进、改待办状态的不进', () => {
  const mcp = MCP_TOOL_DEFS.map(t => t.function.name)
  for (const expected of ['list_contacts', 'check_skills', 'lint_wiki', 'get_review',
                          'get_concepts', 'fetch_article', 'search_public',
                          'format_article', 'list_themes']) {
    assert.ok(mcp.includes(expected), `${expected} 是只读的，应当也在 MCP 表里`)
  }
  assert.equal(mcp.includes('set_todo_status'), false, '待办修改不在 MCP 的能力范围内')
})
