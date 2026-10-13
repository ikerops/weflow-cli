"""markdown 导出：图片要**落盘**并按相对路径引用，同时写一份清单给其余格式用。

为什么单独一条：md 的用法是喂给编辑器/别的工具，那里 `![](data:image/…)` 多数不认 —— 图片必须是文件。
而 json/txt/excel 要能跟着带上图片，就得有一份**两边都认的键**。2026-10-13 真机实测踩到的坑：
清单原本按 `local_id` 做键，而**同一个 local_id 会在分片之间重复**（同一会话的表可能存在于多个分片，
各片自己编号）⇒ 763 条消息只对上 522 条，非 md 格式会引用到别人的图。键改成 `local_id:create_time` 后，
四种格式都落在 556（= 有图的消息数）。下面钉住这两件事。
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location('export_chat_html', SCRIPTS / 'export_chat_html.py')
eh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eh)

# 一个 1x1 的 PNG，够小够真（b64decode 要能解出来）
PNG_B64 = ('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')


def msg(local_id, create_time, text='', image=None):
    return {
        'local_id': local_id,
        'create_time': create_time,
        'is_self': False,
        'sender': '对方',
        'display': text,
        'image_b64': image,
    }


class MarkdownExportTests(unittest.TestCase):
    def test_images_are_written_to_disk_and_referenced_relatively(self):
        with tempfile.TemporaryDirectory() as tmp:
            md_path, asset_total, written, assets_name = eh.write_markdown(
                tmp, '测试会话', [msg(1, 1700000000, '你好'), msg(2, 1700000060, image=PNG_B64)])
            md = Path(md_path).read_text(encoding='utf-8')
            self.assertEqual(asset_total, 1)
            self.assertEqual(written, 1)
            self.assertIn('![图片](', md, '有图的消息要按相对路径引用')
            self.assertNotIn('data:image', md, 'md 里不许内嵌 base64（编辑器多数不认）')
            # 图片真的落盘了，且路径与正文里写的一致
            import re
            rel = re.search(r'!\[图片\]\(([^)]+)\)', md).group(1)
            self.assertTrue((Path(tmp) / rel).is_file(), rel)
            self.assertTrue(rel.startswith(assets_name + '/'), rel)
            # 清单也写了
            manifest = json.loads((Path(tmp) / assets_name / 'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['schema'], 'weflow-media-manifest/v1')
            self.assertEqual(list(manifest['messages']), ['2:1700000060'])

    def test_manifest_key_carries_the_timestamp_so_ids_can_repeat_across_shards(self):
        """同一个 local_id 出现在两个分片里（时间不同）时，两条都要有自己的条目。"""
        with tempfile.TemporaryDirectory() as tmp:
            _md, asset_total, _written, assets_name = eh.write_markdown(
                tmp, '会话', [msg(7, 1700000000, image=PNG_B64), msg(7, 1700009999, image=PNG_B64)])
            keys = sorted(json.loads((Path(tmp) / assets_name / 'manifest.json')
                                     .read_text(encoding='utf-8'))['messages'])
            self.assertEqual(keys, ['7:1700000000', '7:1700009999'],
                             '只按 local_id 做键会让后一条覆盖前一条（非 md 格式于是引用到别人的图）')
            self.assertEqual(asset_total, 2)

    def test_text_messages_keep_their_text_and_placeholders_are_replaced_by_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            md_path, _t, _w, _n = eh.write_markdown(
                tmp, '会话', [msg(1, 1700000000, '这是一句正常的话'),
                              msg(2, 1700000060, '[图片]', image=PNG_B64)])
            md = Path(md_path).read_text(encoding='utf-8')
            self.assertIn('这是一句正常的话', md)
            # `[图片]` 这种纯占位被图片替换掉，而不是并排出现。
            # 注意判**独立的一行**：`![图片](…)` 里也含 `[图片]` 这几个字符，用子串判会误报。
            self.assertNotIn('\n[图片]\n', md, '占位符该被图片替换，而不是与图片并排')
            self.assertIn('![图片](', md)


if __name__ == '__main__':
    unittest.main()
