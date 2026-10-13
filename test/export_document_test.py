"""文档本体（appmsg `type=6`）的导出：从 `msg/file/` 拷进 assets，md 里给链接。

为什么单独一条：反馈的原话是"除 html 外，其它导出格式不支持导出图片和**文档**吗"。图片那半有测试钉着，
文档这半此前根本没有 —— 消息只会渲染成 `[文件] 名字`，文件本体从不落盘。

2026-10-13 真机实测定下的两件事，都在下面钉住：
1. 匹配口径是 `(归一化名, 字节数)`：1,855 条文件消息里 1,489 条唯一命中；154 条同名多候选，靠 md5 认下；
   212 条本地确实没有（未下载/已清理）⇒ 命中 88.6%，不猜。磁盘名比消息里的 title 多一个 `(1)`/`（2）`。
2. **必须先切到第一个 `<appmsg>` 块再取字段**：引用消息（type 57）在 `<refermsg>` 里嵌着被引用的原文，
   那份原文同样带 `<title>`/`<appattach>`。用正则满文档搜会把"别人文件的名字"当成这条消息的文件 ——
   实测 7,083 条"像文件"的结果里绝大多数是引用，全部是误判。
"""

import importlib.util
import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location('export_chat_html', SCRIPTS / 'export_chat_html.py')
eh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eh)


def file_appmsg(title, size=1234, md5='a' * 32, fileext='pdf'):
    return ('<?xml version="1.0"?>\n<msg><appmsg appid="" sdkver="0">'
            f'<title>{title}</title><type>6</type>'
            f'<appattach><fileext>{fileext}</fileext><totallen>{size}</totallen>'
            f'<attachid>@cdn_x</attachid><md5>{md5}</md5></appattach>'
            '</appmsg></msg>')


def quoted_message_without_outer_type(inner_title='别人的文件.docx'):
    """引用消息：`<refermsg>` 里嵌着一份带 appattach 的原文，而**外层没有 `<type>`**。

    真机里 appmsg 缺 `<type>` 是会出现的（老消息/不完整行）。这种行满文档搜第一个 `<type>` 会取到
    `<refermsg>` 里那个 `6`，于是"别人文件的名字"成了这条消息的文件。
    """
    return ('<?xml version="1.0"?>\n<msg><appmsg appid="" sdkver="0">'
            '<title>好的</title>'
            f'<refermsg><type>6</type><title>{inner_title}</title>'
            '<appattach><fileext>docx</fileext><totallen>99</totallen></appattach></refermsg>'
            '</appmsg></msg>')


def file_message_missing_its_own_title():
    """外层确实是文件（type 6），但自己的 `<title>` 丢了 —— 不许拿引用里的标题顶上。"""
    return ('<?xml version="1.0"?>\n<msg><appmsg appid="" sdkver="0">'
            '<type>6</type>'
            '<appattach><fileext>pdf</fileext><totallen>42</totallen></appattach>'
            '<refermsg><title>别人引用过的文件.pdf</title><type>6</type></refermsg>'
            '</appmsg></msg>')


def document_message(local_id, create_time, info, path=None):
    return {
        'local_id': local_id, 'create_time': create_time, 'is_self': False, 'sender': '对方',
        'display': f'<span class="msg-file">[文件] {info["title"]}</span>', 'image_b64': None,
        'file': {'title': info['title'], 'ext': info.get('ext', ''), 'bytes': info.get('bytes', 0),
                 'md5': info.get('md5', ''), 'path': path},
    }


class ParseFileAppmsgTests(unittest.TestCase):
    def test_reads_the_top_level_file_block(self):
        info = eh.parse_file_appmsg(file_appmsg('园区五一假期温馨提示.pdf', size=284813, fileext='pdf'))
        self.assertEqual(info['title'], '园区五一假期温馨提示.pdf')
        self.assertEqual(info['ext'], 'pdf')
        self.assertEqual(info['bytes'], 284813)

    def test_a_quoted_reply_is_not_mistaken_for_the_message_s_own_file(self):
        """引用里的 `<appattach>` 是**别人的**文件，不是这条消息带的东西。"""
        self.assertIsNone(eh.parse_file_appmsg(quoted_message_without_outer_type()),
                          '在整段 XML 上取首个 <type>/<title> 会把引用原文误判成本条的文件')
        self.assertIsNone(eh.parse_file_appmsg(file_message_missing_its_own_title()),
                          '自己的标题缺失时，不许拿引用里的标题顶上')

    def test_normalize_strips_the_duplicate_copy_suffix(self):
        self.assertEqual(eh.normalize_asset_name('晚餐聚餐安排（学术研讨会后）(1).docx'),
                         eh.normalize_asset_name('晚餐聚餐安排（学术研讨会后）.docx'))
        self.assertEqual(eh.normalize_asset_name('报告.PDF'), eh.normalize_asset_name('报告.pdf'))
        # 括号里不是纯数字就不该剥（那是名字的一部分）
        self.assertNotEqual(eh.normalize_asset_name('年报(2024年度).pdf'), eh.normalize_asset_name('年报.pdf'))


class ResolveAccountFileTests(unittest.TestCase):
    def test_unique_name_and_size_hit(self):
        index = {('报告.pdf', 10): [r'C:\x\报告.pdf']}
        self.assertEqual(eh.resolve_account_file({'title': '报告.pdf', 'bytes': 10, 'md5': ''}, index),
                         r'C:\x\报告.pdf')

    def test_multi_candidate_is_disambiguated_by_md5(self):
        with tempfile.TemporaryDirectory() as tmp:
            keep = os.path.join(tmp, '同名(1).pdf')
            other = os.path.join(tmp, '同名.pdf')
            Path(keep).write_bytes(b'right')
            Path(other).write_bytes(b'wrong')
            want = hashlib.md5(b'right').hexdigest()
            index = {('同名.pdf', 5): [other, keep]}
            self.assertEqual(eh.resolve_account_file({'title': '同名.pdf', 'bytes': 5, 'md5': want}, index),
                             keep, '同名两份时不许随便挑一个')

    def test_missing_file_stays_unresolved(self):
        """本地没有就不猜（D-014）：212/1855 条属于这一档。"""
        self.assertIsNone(eh.resolve_account_file({'title': '没有.pdf', 'bytes': 1, 'md5': 'b' * 32},
                                                  {('别的.pdf', 1): ['x']}))
        self.assertIsNone(eh.resolve_account_file({'title': '没有.pdf', 'bytes': 1, 'md5': 'b' * 32}, {}))


class WriteDocumentAssetTests(unittest.TestCase):
    def test_body_is_copied_with_its_own_extension_and_referenced_from_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, '源文件目录')
            os.makedirs(source)
            body = os.path.join(source, '园区五一假期温馨提示(1).pdf')
            Path(body).write_bytes(b'%PDF-1.4 fake')

            info = {'title': '园区五一假期温馨提示.pdf', 'ext': 'pdf', 'bytes': 13, 'md5': 'c' * 32}
            item = document_message(1, 1700000000, info, path=body)
            md_path, total, _new, assets_name = eh.write_markdown(tmp, '会话', [item])

            assets = Path(tmp) / assets_name
            copied = list((assets / 'files').iterdir())
            self.assertEqual(len(copied), 1)
            # 扩展名是**原件自己的**：.pdf 不许被改写成图片那套的 .jpg
            self.assertEqual(copied[0].suffix, '.pdf')
            self.assertEqual(copied[0].read_bytes(), b'%PDF-1.4 fake')
            self.assertEqual(total, 1)

            manifest = json.loads((assets / 'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['schema'], 'weflow-media-manifest/v2')
            record = manifest['messages']['1:1700000000']
            self.assertEqual(record['kind'], 'file')
            self.assertEqual(record['name'], '园区五一假期温馨提示.pdf')
            self.assertEqual(manifest['documents'], {'exported': 1, 'copied': 1, 'unavailable': 0})

            md = Path(md_path).read_text(encoding='utf-8')
            # 文档要写成**链接**：`![](…)` 会被编辑器当成坏图
            self.assertIn('](%s)' % record['path'].replace('/', '/'), md)
            self.assertNotIn('![图片]', md)
            self.assertIn('园区五一假期温馨提示', md)

    def test_unresolved_and_oversized_documents_are_counted_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = os.path.join(tmp, 'huge.zip')
            with open(big, 'wb') as fh:
                fh.write(b'0' * (eh.MAX_ASSET_FILE_BYTES + 1))
            info = {'title': 'huge.zip', 'ext': 'zip', 'bytes': 1, 'md5': 'd' * 32}
            items = [document_message(1, 1, info, path=big),
                     document_message(2, 2, info, path=None)]
            _md, _total, _new, assets_name = eh.write_markdown(tmp, '会话', items)
            manifest = json.loads((Path(tmp) / assets_name / 'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['documents'], {'exported': 0, 'copied': 0, 'unavailable': 2})
            self.assertFalse((Path(tmp) / assets_name / 'files').exists(), '没拷成就不该建目录')

    def test_second_run_still_reports_the_documents_it_did_not_have_to_copy(self):
        """同一个输出目录跑第二遍时新拷贝是 0，但文档**都在**。

        2026-10-13 真机踩到：`export … txt --media` 第二次跑打印「0 document(s)」，而 67 份文档就躺在
        assets 里 —— 只数"新拷贝"就会给自己制造"什么都没导出"的假象。`exported` 与 `written` 要分开。
        """
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, 'src')
            os.makedirs(source)
            body = os.path.join(source, '方案.docx')
            Path(body).write_bytes(b'PK\x03\x04fake')
            info = {'title': '方案.docx', 'ext': 'docx', 'bytes': 8, 'md5': 'f' * 32}
            item = document_message(1, 5, info, path=body)

            eh.write_assets(tmp, '会话', [item])
            self.assertEqual((eh.FILE_ASSET_STATE['exported'], eh.FILE_ASSET_STATE['written']), (1, 1))
            _n, _mf, _new = eh.write_assets(tmp, '会话', [item])
            self.assertEqual((eh.FILE_ASSET_STATE['exported'], eh.FILE_ASSET_STATE['written']), (1, 0),
                             '第二遍没有新拷贝，但"这份文档在不在"必须还是 1')

    def test_link_target_is_encoded_so_parentheses_and_spaces_cannot_break_it(self):
        """真机踩到：`…开题报告(2).pdf` 写成 `[名](…报告(2).pdf)`，渲染器在**第一个半角 `)`** 处截断，
        点开是空的；文件名带**空格**同理（目标在空白处结束）。

        这两个是真实文件名里最常见的字符（重导咸鱼梦想家时，29 份文档里就有一份带 `(2)`、一份带空格）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, 'src')
            os.makedirs(src)
            cases = [
                ('202421047946_研究生学位（毕业）论文开题报告(2).pdf', 'a.pdf'),
                ('Chang 等 - 2022 - Environmental Justice Assessment.pdf', 'b.pdf'),
            ]
            items = []
            for i, (title, body) in enumerate(cases, start=1):
                path = os.path.join(src, body)
                Path(path).write_bytes(b'%PDF-1.4 fake')
                items.append(document_message(i, i, {'title': title, 'ext': 'pdf', 'bytes': 13,
                                                     'md5': chr(96 + i) * 32}, path=path))
            md_path, _t, _n, _assets = eh.write_markdown(tmp, '会话', items)
            md = Path(md_path).read_text(encoding='utf-8')
            targets = re.findall(r'\]\(([^)]*)\)', md)
            self.assertEqual(len(targets), 2)
            for target in targets:
                self.assertNotIn(' ', target, '目标里留空格会让渲染器在空白处截断')
                self.assertNotIn('(', target, '目标里留半角括号会让渲染器在 `)` 处截断')
                self.assertTrue((Path(tmp) / urllib.parse.unquote(target)).is_file(), target)
            self.assertIn('%282%29.pdf', md)
            self.assertIn('%20', md)

    def test_a_long_title_keeps_its_extension(self):
        """80 字符上限**不能把扩展名一起切掉**：真机落过一个没有扩展名的文件，
        系统与编辑器都不知道该用什么打开它。"""
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, 'src')
            os.makedirs(src)
            title = 'Chang 等 - 2022 - Environmental Justice Assessment of Fine Particles, Ozone, and More ' * 2 + '.pdf'
            self.assertGreater(len(title), 80)
            path = os.path.join(src, 'long.pdf')
            Path(path).write_bytes(b'%PDF-1.4 fake')
            md_path, _t, _n, assets_name = eh.write_markdown(
                tmp, '会话', [document_message(1, 1, {'title': title, 'ext': 'pdf', 'bytes': 13,
                                                     'md5': 'c' * 32}, path=path)])
            names = [p.name for p in (Path(tmp) / assets_name / 'files').iterdir()]
            self.assertEqual(len(names), 1)
            self.assertTrue(names[0].endswith('.pdf'), names[0])
            self.assertLessEqual(len(names[0]), 80, names[0])
            self.assertIn('](', Path(md_path).read_text(encoding='utf-8'))

    def test_hostile_filenames_neither_break_the_link_nor_the_whole_export(self):
        """一批真实会遇到的恶劣文件名。判据是两条：**没有一条链接解不回来**、**没有一个名字能把导出搞崩**。

        这里每一条都对应一次实测（2026-10-13）：
        - 半角括号 / 空格 → 链接在第一个 `)` 或空白处被截断，点开是空的（真机上踩到过）
        - 制表符 → `open()` 直接 `OSError: Invalid argument`，而它没被接住，
          **整次导出会在最后一步崩掉**（前面几分钟的解析与图片抽取全废）—— 这条是最严重的
        - `CON.pdf` / `NUL.txt` / `aux.docx` → Windows 保留设备名，建出来也打不开
        - 200 字符长名 → 截断到 80，且**扩展名要留下**
        - `[草稿] 方案` → 方括号会打断链接自身的结构
        """
        titles = [
            '普通报告.pdf', '开题报告(2).pdf', '开题报告（2）.pdf',
            'Chang et al 2022 assessment.pdf', 'a#b%c&d+e.pdf', '[草稿] 方案 v2.docx',
            'tab\tinside.csv', '  前后空格  .docx', '结尾点...docx', 'emoji🎀报告.pdf',
            'CON.pdf', 'NUL.txt', 'aux.docx', '无扩展名文件', 'x' * 200 + '.pdf',
            '竖线|星号*问号?.pdf', 'back\\slash.pdf',
        ]
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, 'src')
            os.makedirs(src)
            items = []
            for index, title in enumerate(titles, start=1):
                body = os.path.join(src, 'body_%02d.bin' % index)
                Path(body).write_bytes(b'%%PDF-1.4 body %02d' % index)
                items.append(document_message(index, 1700000000 + index,
                                              {'title': title, 'ext': '', 'bytes': 18,
                                               'md5': '%032x' % index}, path=body))
            out = os.path.join(tmp, 'out')
            os.makedirs(out)
            md_path, _total, _new, assets = eh.write_markdown(out, '会话', items)
            md = Path(md_path).read_text(encoding='utf-8')

            targets = re.findall(r'\]\(([^)\n]*)\)', md)
            self.assertEqual(len(targets), len(titles))
            for target in targets:
                self.assertTrue((Path(out) / urllib.parse.unquote(target)).is_file(), target)

            names = [p.name for p in (Path(out) / assets / 'files').iterdir()]
            self.assertEqual(len(names), len(titles), '一个都不该被跳过')
            for name in names:
                self.assertLessEqual(len(name), 80, name)
                self.assertFalse(any(ord(ch) < 32 for ch in name), repr(name))
                self.assertNotIn('\\', name)
            self.assertIn('_CON.pdf', names, 'Windows 保留设备名要加前缀，否则打不开')
            self.assertTrue(any(n.endswith('.pdf') and len(n) == 80 for n in names),
                            '被截断的那个也要留住扩展名')

    def test_a_narrower_later_export_does_not_erase_the_wider_manifest(self):
        """清单住在共享的 `<会话>_assets/` 里，而导出可以分批做。

        2026-10-13 真机撞上：先整段导（585 条 / 29 份文档），再 `json --media --limit 600`（150 条 / 7 份）
        导进同一个目录 —— 后者把清单**整个覆盖**了，宽窗口那批文档的条目凭空消失，而 md 正文里的链接还在。
        两边都不报错，是核对产物才看出来的。
        """
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, 'src')
            os.makedirs(src)
            bodies = {}
            for index, name in enumerate(('宽窗口.pdf', '窄窗口.docx'), start=1):
                bodies[name] = os.path.join(src, 'b%d' % index)
                Path(bodies[name]).write_bytes(b'body %d' % index)

            info_a = {'title': '宽窗口.pdf', 'ext': 'pdf', 'bytes': 6, 'md5': '1' * 32}
            info_b = {'title': '窄窗口.docx', 'ext': 'docx', 'bytes': 6, 'md5': '2' * 32}
            eh.write_assets(tmp, '会话', [document_message(1, 100, info_a, path=bodies['宽窗口.pdf'])])
            _n, manifest, _new = eh.write_assets(
                tmp, '会话', [document_message(2, 200, info_b, path=bodies['窄窗口.docx'])])

            self.assertIn('1:100', manifest, '窄窗口那次不该把宽窗口的条目删掉')
            self.assertIn('2:200', manifest)
            on_disk = json.loads((Path(tmp) / '会话_assets' / 'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(on_disk['documents']['exported'], 2)
            self.assertEqual(on_disk['documents']['copied'], 1, '这一轮只新拷了 1 个')

    def test_two_documents_with_the_same_name_do_not_overwrite_each_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = os.path.join(tmp, 'a'); b = os.path.join(tmp, 'b')
            os.makedirs(a); os.makedirs(b)
            Path(os.path.join(a, '同名.docx')).write_bytes(b'AAA')
            Path(os.path.join(b, '同名.docx')).write_bytes(b'BBB')
            info = {'title': '同名.docx', 'ext': 'docx', 'bytes': 3, 'md5': 'e' * 32}
            items = [document_message(1, 1, info, path=os.path.join(a, '同名.docx')),
                     document_message(2, 2, info, path=os.path.join(b, '同名.docx'))]
            _md, _t, _n, assets_name = eh.write_markdown(tmp, '会话', items)
            bodies = sorted(p.read_bytes() for p in (Path(tmp) / assets_name / 'files').iterdir())
            self.assertEqual(bodies, [b'AAA', b'BBB'], '同名不同源必须都留下，否则清单会指错文件')


if __name__ == '__main__':
    unittest.main()
