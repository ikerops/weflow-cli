/**
 * 3D 图谱页（`wiki graph --open` 的产物）**真的画出来了吗** —— 在**真 GPU** 的浏览器里跑一遍，
 * 读它自己暴露的自检钩子。关掉的是 `docs/PROJECT_STATE.md` 里"3D 图谱的 JS 只钉了源码形状"
 * 那一行：2D 版能在 jsdom 里真跑（`test/graph-2d-cli.test.ts`），3D 版要 WebGL，jsdom 没有。
 *
 * 为什么不用像素、也不用帧率：
 *   · **像素**：WebGL canvas 在帧外永远黑（帧缓冲被换走了），截图证明不了"画出来了"；
 *   · **帧率**：页面是**按需渲染**的，空转的 rAF 量出来是 144 fps 那种假数字 ——
 *     生成器自己在 `window.__viz` 的注释里就写着这句。
 * 所以判据是 `renderer.info.render` 的**计数器**（实际提交了多少次 draw call / 多少个点 / 多少条线），
 * 那是"GPU 真的收到了几何"的直接证据。⚠️ 别拿 `triangles` 当判据：球与线是 `Points` /
 * `LineSegments`，**triangles 恒为 0**（我第一次就是这么误判的）。
 *
 * 跑法（需要本机装了 Edge 或 Chrome —— 这台机器是
 * `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`，可用 `GRAPH3D_EDGE` 覆盖）：
 *
 *     node scripts/graph3d_check.mjs output/knowledge-graph-3d-core.html
 *     node scripts/graph3d_check.mjs output/knowledge-graph-3d.html 9340   # 不筛点的整页
 *
 * 依赖：无（CDP 走 Node 自带的 WebSocket + fetch）。退出码 0 = 画出来了，非 0 = 没画出来或拿不到钩子。
 */
import { spawn } from 'node:child_process'
import { pathToFileURL } from 'node:url'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

const PAGE = process.argv[2]
const PORT = Number(process.argv[3] || 9333)
const EDGE = process.env.GRAPH3D_EDGE
  || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
if (!PAGE) {
  console.error('用法: node scripts/graph3d_check.mjs <已生成的 3d 页面.html> [端口]')
  process.exit(2)
}

const profile = mkdtempSync(join(tmpdir(), 'g3d-'))
const edge = spawn(EDGE, [
  '--headless=new', '--use-angle=d3d11', '--enable-gpu', '--disable-gpu-sandbox',
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  '--no-first-run', '--no-default-browser-check', '--window-size=1600,1000',
  pathToFileURL(resolve(PAGE)).href,
], { stdio: 'ignore' })
const sleep = ms => new Promise(r => setTimeout(r, ms))

async function findTarget() {
  for (let i = 0; i < 60; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()
      const page = list.find(t => t.type === 'page' && t.url.startsWith('file:'))
      if (page) return page
    } catch {}
    await sleep(500)
  }
  throw new Error('拿不到可调试的页面目标（Edge 起不来？路径见 GRAPH3D_EDGE）')
}

let edgeKilled = false
const done = code => { if (!edgeKilled) { edgeKilled = true; edge.kill() } process.exit(code) }

try {
  const target = await findTarget()
  const ws = new WebSocket(target.webSocketDebuggerUrl)
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej })
  let id = 0
  const pending = new Map()
  ws.onmessage = ev => {
    const msg = JSON.parse(ev.data)
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id) }
  }
  const send = (method, params = {}) => new Promise(res => {
    const n = ++id
    pending.set(n, res)
    ws.send(JSON.stringify({ id: n, method, params }))
  })
  const evaluate = async (expression, awaitPromise = false) => {
    const r = await send('Runtime.evaluate', { expression, awaitPromise, returnByValue: true })
    if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0, 300))
    return r.result?.result?.value
  }
  await send('Runtime.enable')

  // 等页面自己的钩子：布局要几秒（`window.__layoutMs` 就是它自己的计时）
  let ready = false
  for (let i = 0; i < 120 && !ready; i++) {
    ready = await evaluate('!!(window.__viz && window.__viz.renderer)')
    if (!ready) await sleep(1000)
  }
  if (!ready) {
    console.error('页面没有暴露 window.__viz —— 脚本没跑到装钩子的那一步')
    console.error('页面自述：', await evaluate('document.title + " | " + (document.body.innerText||"").slice(0,200)'))
    done(2)
  }

  const facts = {
    nodes: await evaluate('window.__viz.nodes.length'),
    links: await evaluate('window.__DATA__.links.length'),
    layoutMs: await evaluate('window.__layoutMs'),
    drawCalls: await evaluate('window.__viz.renderer.info.render.calls'),
    points: await evaluate('window.__viz.renderer.info.render.points'),
    lines: await evaluate('window.__viz.renderer.info.render.lines'),
    triangles: await evaluate('window.__viz.renderer.info.render.triangles'),
    hidden: await evaluate(`(() => { const out = []
      window.__viz.scene.traverse(o => { if (o.isPoints || o.isLineSegments || o.isMesh) out.push(o.type + (o.visible ? '' : ' (visible=false)')) })
      return out })()`),
    programCount: await evaluate('window.__viz.renderer.info.programs?.length'),
    glRenderer: await evaluate(`(() => { const g = window.__viz.renderer.getContext()
      const d = g.getExtension('WEBGL_debug_renderer_info')
      return d ? g.getParameter(d.UNMASKED_RENDERER_WEBGL) : 'n/a' })()`),
    // **GL 提交代价**（render + gl.finish()），不是页面的帧时间 —— 页面的每一帧还包含
    // HUD、拾取射线与力导 tick，那些都在这条路径之外（`graph_3d.py` 自己记的 144 fps
    // 是页面整帧的实测，别把这两个数当同一个）。
    glSubmitMs: await evaluate(`(async () => {
      const { renderer, scene, camera } = window.__viz
      const gl = renderer.getContext()
      const t = []
      for (let i = 0; i < 30; i++) {
        const a = performance.now()
        renderer.render(scene, camera)
        gl.finish()
        t.push(performance.now() - a)
        await new Promise(r => requestAnimationFrame(r))
      }
      t.sort((x, y) => x - y)
      return { median: +t[15].toFixed(2), min: +t[0].toFixed(2), max: +t[29].toFixed(2) }
    })()`, true),
  }
  ws.close()

  const drawn = facts.points > 0 && facts.lines > 0
  console.log(JSON.stringify(facts, null, 2))
  console.log(drawn
    ? `\nOK：GPU 收到了几何 —— ${facts.points} 个点、${facts.lines} 条线、${facts.drawCalls} 次 draw call`
      + `（渲染器：${facts.glRenderer}）`
    : `\nFAIL：一次几何都没提交（points=${facts.points} lines=${facts.lines}）—— 页面没画出来`)
  done(drawn ? 0 : 1)
} catch (error) {
  console.error('检查没能跑完：', error.message)
  done(2)
}
