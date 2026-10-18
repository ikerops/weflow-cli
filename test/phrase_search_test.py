"""精确短语定位（D-121）：原样匹配、标点当字面量、两条不许静默、分组口径。

为什么这几条要钉死：这条腿的价值全在"原样"两个字，而它有三种**看起来正常**的失效方式：

1. 把查询当 FTS 语法 → `PM2.5` 直接语法报错、`GPT-4` 报 `no such column: 4`（实测）；
2. 短查询（trigram 的 3 字下限）→ 返回 0 命中，把"查不了"说成"没有"（这是最坏的一种）；
3. 索引没建 → 同样回报 0 命中，用户会以为"知识库里没有这句话"。

前两条有硬判据（引号 + 显式 `tooShort`），第三条有 `error` + 建索引命令。全部离线。
"""
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location('ss_phrase', SCRIPTS / 'semantic_search.py')
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)

ACCOUNT = '某号'
REL = '2026-10-15/AI/%s-标题一.md' % ACCOUNT
BODY = ('# 标题一\n\n'
        '腾讯混元团队在真实互联网环境测试 Agent，出现越权与沙箱逃逸，并用 PM2.5 做对照实验。\n'
        '第二段提到 std::vector 与 C++ 的性能差异，以及 GPT-4 的输出。\n')


class EscapeTests(unittest.TestCase):
    def test_整串当字面量_标点不再是语法(self):
        """`PM2.5` / `GPT-4` 不加引号会直接语法报错（实测），加引号才当"原样找这个串"。"""
        self.assertEqual(ss.escape_phrase('PM2.5'), '"PM2.5"')
        self.assertEqual(ss.escape_phrase('GPT-4'), '"GPT-4"')

    def test_查询里的引号要翻倍(self):
        """FTS5 里 `"` 是字面量引号的分隔符：不翻倍就会被当语法，症状是解析错误或截断匹配。"""
        self.assertEqual(ss.escape_phrase('他说"你好"'), '"他说""你好"""')

    def test_空查询(self):
        self.assertEqual(ss.escape_phrase(''), '""')
        self.assertEqual(ss.escape_phrase(None), '""')

    def test_有效字符数不算空白(self):
        self.assertEqual(ss.phrase_chars('PM 2.5'), 5)
        self.assertEqual(ss.phrase_chars('  光 伏  '), 2)


class LabelTests(unittest.TestCase):
    """「哪个号」的取法必须与 `--from` 同源（那条路也是靠文件名找号）。"""

    def test_文章取文件名里第一个短横前那段(self):
        item = {'type': 'article', 'id': 'article:2026-10-15/AI/某号-标题-带短横.md'}
        self.assertEqual(ss._phrase_label(item), '某号')

    def test_聊天取会话名(self):
        self.assertEqual(ss._phrase_label({'type': 'chat', 'talker': '老表亲戚群'}), '老表亲戚群')

    def test_文件名没有短横时整段用(self):
        self.assertEqual(ss._phrase_label({'type': 'article', 'id': 'article:2026-10-15/AI/无横标题.md'}),
                         '无横标题')


class PhraseIndexTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='phrase-'))
        self._saved = (ss.OUTPUT_ROOT, ss.PHRASE_DB, ss.META_FILE)
        ss.OUTPUT_ROOT = str(self.root)
        ss.PHRASE_DB = self.root / '.phrase_index' / 'phrase.sqlite'
        ss.META_FILE = self.root / '.semantic_index' / 'meta.json'
        md = self.root / 'biz-daily' / REL
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(BODY, encoding='utf-8')
        md2 = self.root / 'biz-daily' / ('2026-10-15/AI/%s-标题二.md' % ACCOUNT)
        md2.write_text('# 标题二\n\n另一篇也提到沙箱逃逸，但没提序号。\n', encoding='utf-8')
        meta = [
            {'id': 'article:' + REL, 'type': 'article', 'title': '标题一',
             'date': '2026-10-15', 'topic': 'AI', 'text': '标题一\n摘录：沙箱逃逸'},
            {'id': 'article:2026-10-15/AI/%s-标题二.md' % ACCOUNT, 'type': 'article',
             'title': '标题二', 'date': '2026-10-15', 'topic': 'AI', 'text': '标题二\n摘录：沙箱'},
            {'id': 'chat:123@chatroom:9:1', 'type': 'chat', 'talker': '老表亲戚群',
             'sender': 3, 'time': '2026-08-30 15:03', 'text': 'wxid_x:\n等下去学校门口买 PM2.5 口罩'},
        ]
        ss.META_FILE.parent.mkdir(parents=True, exist_ok=True)
        ss.META_FILE.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
        self.stats = ss.build_phrase_index()

    def tearDown(self):
        ss.OUTPUT_ROOT, ss.PHRASE_DB, ss.META_FILE = self._saved
        shutil.rmtree(self.root, ignore_errors=True)

    def test_建索引把两条线都收进来(self):
        self.assertEqual(self.stats['records'], 3)
        self.assertEqual(self.stats['byLine'], {'article': 2, 'chat': 1})
        self.assertEqual(self.stats['textMissing'], 0)

    def test_正文取读者看到的那份而不是摘录(self):
        """摘录里没有 PM2.5，md 正文里有 —— 命中说明用的是 md 那份（这是"原样定位"的意义）。"""
        result = ss.phrase_search('PM2.5')
        self.assertEqual(result['total'], 2)          # 文章一 + 聊天
        self.assertTrue(any('PM2.5' in h['context'] for h in result['hits']))

    def test_标点查询不报错(self):
        for query in ('PM2.5', 'C++', 'GPT-4', 'std::vector'):
            with self.subTest(query=query):
                self.assertGreaterEqual(ss.phrase_search(query)['total'], 1)

    def test_原样才命中_隔着字就不命中(self):
        """这条是"精确"的定义：`沙箱与逃逸` 在原样语料里不存在，就该 0 命中。"""
        self.assertGreaterEqual(ss.phrase_search('沙箱逃逸')['total'], 2)
        self.assertEqual(ss.phrase_search('沙箱与逃逸')['total'], 0)

    def test_短查询明说查不了而不是0命中(self):
        result = ss.phrase_search('光伏')
        self.assertTrue(result['tooShort'])
        self.assertEqual(result['chars'], 2)
        self.assertIn('查不了', result['note'])
        self.assertEqual(result['total'], 0)

    def test_分组给出哪个号在哪天(self):
        result = ss.phrase_search('沙箱逃逸')
        self.assertTrue(result['groups'])
        self.assertEqual(result['groups'][0]['label'], ACCOUNT)
        self.assertEqual(result['groups'][0]['date'], '2026-10-15')
        self.assertEqual(result['groups'][0]['count'], 2)

    def test_按线过滤(self):
        self.assertEqual(ss.phrase_search('PM2.5', line='wiki')['total'], 1)
        self.assertEqual(ss.phrase_search('PM2.5', line='chat')['total'], 1)

    def test_按号过滤(self):
        self.assertEqual(ss.phrase_search('沙箱逃逸', from_source=ACCOUNT)['total'], 2)
        self.assertEqual(ss.phrase_search('沙箱逃逸', from_source='别的号')['total'], 0)

    def test_命中带上下文且高亮原样的串(self):
        hit = ss.phrase_search('std::vector')['hits'][0]
        self.assertIn('【std::vector】', hit['context'])
        self.assertEqual(hit['label'], ACCOUNT)
        self.assertEqual(hit['title'], '标题一')

    def test_索引没建要报错并给命令_不能回报0命中(self):
        ss.PHRASE_DB = self.root / '不存在.sqlite'
        result = ss.phrase_search('沙箱逃逸')
        self.assertFalse(result['total'])
        self.assertIn('还没建', result['error'])
        self.assertIn('search-index --phrase', result['hint'])
        self.assertNotIn('tooShort', result)          # 两种"查不了"不许混

    def test_没有语义索引时拒绝建(self):
        ss.META_FILE = self.root / '没有.json'
        self.assertIn('error', ss.build_phrase_index())

    def test_正文读不到时退回摘录并计数(self):
        """md 被删/挪走时：用索引里那段摘录兜底，**并把这个数报出来**，不让这篇静默消失。"""
        (self.root / 'biz-daily' / REL).unlink()
        stats = ss.build_phrase_index()
        self.assertEqual(stats['records'], 3)
        self.assertEqual(stats['textMissing'], 1)
        self.assertGreaterEqual(ss.phrase_search('沙箱逃逸')['total'], 1)


class PhraseFlagWarningTests(unittest.TestCase):
    """向量检索的旗标在 `--phrase` 上不生效：直接调脚本时要**出声**（CLI 那条路会直接拒绝）。

    为什么值得钉：静默忽略会让人把**默认行为**当成"我调过参数的结果"——这比报错贵得多。
    """

    class _NullOut:
        def write(self, _s):
            pass

        def flush(self):
            pass

        def reconfigure(self, **_kw):
            pass

    def _run_main(self, argv):
        from unittest import mock
        calls = {}

        def fake_phrase(query, **_kw):
            calls['query'] = query
            return {'query': query, 'total': 0, 'hits': [], 'groups': []}

        import io
        err = io.StringIO()
        old_argv, old_stdout, old_stderr = sys.argv, sys.stdout, sys.stderr
        sys.argv = ['semantic_search.py'] + argv
        sys.stdout, sys.stderr = self._NullOut(), err
        try:
            # **不读本机的 `~/.weflow-cli/config.json`**：CI 上没有那个文件，之前这条测试
            # 只在"作者机器上恰好有配置"时才是绿的（CI 红、本地绿，2026-10-19 实测）。
            with mock.patch.object(ss, 'load_config', lambda *a, **k: {}), \
                 mock.patch.object(ss, 'phrase_search', fake_phrase):
                ss.main()
        finally:
            sys.argv, sys.stdout, sys.stderr = old_argv, old_stdout, old_stderr
        return calls, err.getvalue()

    def test_带向量旗标时打警告且照常定位(self):
        calls, err = self._run_main(['search', '原样短语定位', '--phrase', '--mmr', '--no-rerank'])
        self.assertEqual(calls.get('query'), '原样短语定位')
        self.assertIn('--mmr', err)
        self.assertIn('--no-rerank', err)

    def test_不带向量旗标时不出声(self):
        calls, err = self._run_main(['search', '原样短语定位', '--phrase'])
        self.assertEqual(calls.get('query'), '原样短语定位')
        self.assertNotIn('ignored', err)


if __name__ == '__main__':
    unittest.main()
