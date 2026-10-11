/**
 * **"这台机器上的微信是哪个版本"** —— 判据来自一份实测的启动器逆向分析（报告在仓库外）。
 *
 * 它纠正了两件事，两条都在这里钉住：
 *
 * 1. **别读 exe 里的版本字面量**。它硬编码着、而且恰好与本机版本目录同名，看着像"读它就够了"，
 *    实际只是**兜底**（主路径读 `update\update.data`）。⇒ 版本只能从**活进程命令行**的
 *    `--user-lib-dir` 取（那个参数在 exe 的字符串表里 0 命中，二进制里搜不到）。
 * 2. **"磁盘上有版本目录" ≠ "它能用"**。本机 `4.1.15.11\` 是升级残留、只剩一个文件，
 *    指向它的加载会失败 ⇒ 判据是**有没有 `Weixin.dll`**。
 *
 * 另外两条是"不猜"：多个进程指向不同目录时**不给**单一版本号；客户端没在跑时报**未知**
 * （不是"没问题"）—— 这一条与仓库里那条"没找到必须报 UNKNOWN，别报 CLEAR"是同一条纪律。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import {
  classifyVersionDirs, detectWechatClient, parseProcessRows, summarize, userLibDir, versionDirsUnder,
} from '../src/services/wechatClient.js'

test('命令行里取 --user-lib-dir：带引号、不带引号、带空格都要认', () => {
  assert.equal(userLibDir('"D:\\WeiXin\\Weixin.exe" --user-lib-dir="D:\\WeiXin\\4.1.15.13" --no-sandbox'),
    'D:\\WeiXin\\4.1.15.13')
  assert.equal(userLibDir('Weixin.exe --user-lib-dir=D:\\WeiXin\\4.1.15.13'), 'D:\\WeiXin\\4.1.15.13')
  assert.equal(userLibDir("Weixin.exe --user-lib-dir='C:\\a b\\4.1.2'"), 'C:\\a b\\4.1.2')
  assert.equal(userLibDir('Weixin.exe --no-sandbox --type=wxocr'), null)
  assert.equal(userLibDir(''), null)
})

test('进程表：ConvertTo-Json 给对象（单个进程）与数组都要收，坏 JSON 不炸', () => {
  const one = JSON.stringify({ ProcessId: 36248, ExecutablePath: 'D:\\WeiXin\\Weixin.exe',
                               CommandLine: 'Weixin.exe --scene=desktop' })
  const rows = parseProcessRows(one)
  assert.equal(rows.length, 1)
  assert.equal(rows[0].pid, 36248)
  assert.equal(rows[0].loadedDir, null, '主进程不带 --user-lib-dir 是实测到的形状')

  const many = JSON.stringify([
    { ProcessId: 1, ExecutablePath: 'D:\\WeiXin\\Weixin.exe', CommandLine: 'x --user-lib-dir="D:\\WeiXin\\4.1.15.13"' },
    { ProcessId: 2, ExecutablePath: 'D:\\WeiXin\\Weixin.exe', CommandLine: 'x --type=xplayer' },
  ])
  const pair = parseProcessRows(many)
  assert.deepEqual(pair.map(p => p.loadedDir), ['D:\\WeiXin\\4.1.15.13', null])
  assert.deepEqual(parseProcessRows('not json'), [])
  assert.deepEqual(parseProcessRows(''), [])
})

test('能用的版本目录 = 有 Weixin.dll 的；残留目录（只剩个别文件）要报出来', () => {
  const got = classifyVersionDirs([
    { name: '4.1.15.13', files: ['Weixin.dll', 'Weixin.exe', 'plugin_info.ini'] },
    // 实测形状：升级残留，只剩 crashpad_handler.exe 一个文件 —— 指向它的加载会失败
    { name: '4.1.15.11', files: ['crashpad_handler.exe'] },
    { name: 'locales', files: ['zh-CN.pak'] },        // 名字不是版本号 ⇒ 不参与
  ])
  assert.deepEqual(got.usable, ['4.1.15.13'])
  assert.deepEqual(got.unusable, [{ dir: '4.1.15.11', files: 1 }])
})

test('磁盘上讲一遍：安装根下哪些版本目录能用、哪些是残留', () => {
  const root = mkdtempSync(join(tmpdir(), 'weixin-install-'))
  try {
    mkdirSync(join(root, '4.1.15.13'))
    writeFileSync(join(root, '4.1.15.13', 'Weixin.dll'), 'x')
    mkdirSync(join(root, '4.1.15.11'))
    writeFileSync(join(root, '4.1.15.11', 'crashpad_handler.exe'), 'x')
    mkdirSync(join(root, 'locales'))
    const got = versionDirsUnder(root)
    assert.deepEqual(got.usable, ['4.1.15.13'])
    assert.deepEqual(got.unusable, [{ dir: '4.1.15.11', files: 1 }])
    // 读不出来（目录不存在）⇒ 空，不是"没有问题"
    assert.deepEqual(versionDirsUnder(join(root, 'nope')), { usable: [], unusable: [] })
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test('多个进程指向不同目录：列出来，但不给一个"版本号"', () => {
  const facts = summarize([
    { pid: 1, exe: 'D:\\WeiXin\\Weixin.exe', loadedDir: 'D:\\WeiXin\\4.1.15.13' },
    { pid: 2, exe: 'D:\\WeiXin\\Weixin.exe', loadedDir: 'C:\\微信\\Weixin\\4.1.13.12' },
  ], [])
  assert.equal(facts.version, null)
  assert.deepEqual(facts.loadedDirs, ['D:\\WeiXin\\4.1.15.13', 'C:\\微信\\Weixin\\4.1.13.12'])
  assert.match(facts.note, /不挑一个报/)
})

test('没在跑 / 都没带参数：版本是 null 且说清为什么（不是"没问题"）', () => {
  const idle = summarize([], [])
  assert.equal(idle.running, false)
  assert.equal(idle.version, null)
  assert.match(idle.note, /没在运行/)

  const noFlag = summarize([{ pid: 1, exe: 'D:\\WeiXin\\Weixin.exe', loadedDir: null }], [])
  assert.equal(noFlag.running, true)
  assert.equal(noFlag.version, null)
  assert.match(noFlag.note, /都没有 --user-lib-dir/)
})

test('正常一台：版本就是加载目录的名字，且重复的目录只算一次', () => {
  const facts = summarize([
    { pid: 1, exe: 'D:\\WeiXin\\Weixin.exe', loadedDir: 'D:\\WeiXin\\4.1.15.13' },
    { pid: 2, exe: 'D:\\WeiXin\\Weixin.exe', loadedDir: 'D:\\WeiXin\\4.1.15.13\\' },
  ], [])
  assert.equal(facts.version, '4.1.15.13')
  assert.equal(facts.note, '')
})

test('detectWechatClient：非 Windows 直接说平台不支持，不是"没问题"', async () => {
  const real = process.platform
  Object.defineProperty(process, 'platform', { value: 'linux', configurable: true })
  try {
    const facts = await detectWechatClient(async () => { throw new Error('不该被调用') })
    assert.equal(facts.version, null)
    assert.match(facts.note, /只做了 Windows/)
  } finally {
    Object.defineProperty(process, 'platform', { value: real, configurable: true })
  }
})

test('detectWechatClient：PowerShell 报错时回"未知"，不抛也不假装没问题', async () => {
  const facts = await detectWechatClient(async () => { throw new Error('powershell 不在') })
  assert.equal(facts.version, null)
  assert.equal(facts.running, false)
  assert.match(facts.note, /没在运行/)
})
