/**
 * 媒体清单的读取与查表：json/txt/excel 就是靠它带上图片路径的。
 *
 * 清单由 **markdown 导出**写出来（`<output>/<talker>_assets/manifest.json`）—— 匹配逻辑（缓存缩略图、
 * 资源 md5、原图那档）只存在于 Python 那条路上，在 TS 里重写一遍必然与它漂移，所以这里是"消费"而不是
 * "再实现"。
 *
 * 键为什么带时间戳：**同一个 local_id 会在分片之间重复**（同一会话的表可能存在于多个分片，各片自己
 * 编号）。2026-10-13 真机实测过只按 local_id 的后果：1,914 条里 763 条对上 522 个条目，非 md 格式会
 * 引用到别的消息的图；键改成 `local_id:create_time` 后四种格式都落在 556（= 有图的消息数）。
 */
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { mediaFor, readMediaManifest } from '../src/services/exportService.js'

function withManifest(data: unknown, run: (dir: string, talker: string) => void): void {
  const dir = mkdtempSync(join(tmpdir(), 'weflow-media-'))
  const talker = 'wxid_test'
  try {
    const assets = join(dir, `${talker}_assets`)
    mkdirSync(assets, { recursive: true })
    writeFileSync(join(assets, 'manifest.json'), JSON.stringify(data), 'utf8')
    run(dir, talker)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

test('清单能读出来；没有清单就是空表（不是报错）', () => {
  withManifest({ schema: 'weflow-media-manifest/v1', messages: { '2:1700000060': 'wxid_test_assets/a.jpg' } },
    (dir, talker) => {
      assert.deepEqual(readMediaManifest(dir, talker), { '2:1700000060': 'wxid_test_assets/a.jpg' })
    })
  const empty = mkdtempSync(join(tmpdir(), 'weflow-media-'))
  try {
    assert.deepEqual(readMediaManifest(empty, 'wxid_test'), {}, '没跑过 md 导出时行为要与从前一致')
  } finally {
    rmSync(empty, { recursive: true, force: true })
  }
})

test('查表按 local_id:create_time，两个分片里同 id 的两条各拿各的图', () => {
  const manifest = { '7:1700000000': 'assets/old.jpg', '7:1700009999': 'assets/new.jpg' }
  assert.equal(mediaFor(manifest, { localId: 7, createTime: 1700009999 }), 'assets/new.jpg')
  assert.equal(mediaFor(manifest, { localId: 7, createTime: 1700000000 }), 'assets/old.jpg')
  // 旧清单（键只有 id）仍认得出来，不至于因为一次升级就什么都不带
  assert.equal(mediaFor({ 7: 'assets/legacy.jpg' }, { localId: 7, createTime: 1 }), 'assets/legacy.jpg')
  assert.equal(mediaFor({}, { localId: 7, createTime: 1 }), undefined)
})
