"""`--from <公众号>`：按号筛是**属性查询**，它必须筛得干净、且在筛不到时说出来。

为什么要有这条：号名不在嵌入文本里（2026-10-15 修正文提取时 `source:` 随 YAML 头一起走了），
只住在 `id`（文件名 `<公众号>-<标题>.md`）。实测窗口 2026-09-12..10-13：红星新闻 908 篇，
语义检索前 10 只回 3 条。筛子本身的三种失效方式各有断言：漏筛（把别的号带进来）、
空筛（不报"这个号没有"）、以及把另一条线（聊天，根本没有"公众号"这个属性）带进来。
"""

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location('semantic_search', SCRIPTS / 'semantic_search.py')
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)

import numpy as np  # noqa: E402

DIM = ss.EMBEDDING_DIM


class FromFilterTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='kb-from-'))
        self._saved = (ss.INDEX_DIR, ss.VECTORS_FILE, ss.META_FILE, ss.get_embeddings)
        ss.INDEX_DIR = self.root
        ss.VECTORS_FILE = self.root / 'vectors.npy'
        ss.META_FILE = self.root / 'meta.json'

        meta = [
            {'id': 'article:2026-10-01\\新闻\\红星新闻-暗访助贷公司.md', 'type': 'article',
             'title': '暗访助贷公司', 'text': '暗访助贷公司黑幕，多部门赶赴现场'},
            {'id': 'article:2026-10-01\\新闻\\新京报-暗访医保套现.md', 'type': 'article',
             'title': '暗访医保套现', 'text': '暗访医保套现乱象后续'},
            {'id': 'chat:某人:123:4', 'type': 'chat', 'talker': '某人',
             'text': '红星新闻那篇暗访写得挺好'},
        ]
        ss.META_FILE.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
        v = [0.0] * DIM
        v[0] = 1.0
        np.save(ss.VECTORS_FILE, np.array([v, v, v], dtype=np.float32))
        ss.get_embeddings = lambda texts, key, on_batch=None: [v]

    def tearDown(self):
        ss.INDEX_DIR, ss.VECTORS_FILE, ss.META_FILE, ss.get_embeddings = self._saved
        shutil.rmtree(self.root, ignore_errors=True)

    def ids(self, **kw):
        res = ss.search('暗访', 'key', rerank_results=False, **kw)
        return [r['id'] for r in res]

    def test_keeps_only_that_account(self):
        got = self.ids(line='all', from_source='红星新闻')
        self.assertEqual(got, ['article:2026-10-01\\新闻\\红星新闻-暗访助贷公司.md'])

    def test_chat_line_cannot_match_a_public_account(self):
        """聊天消息没有"公众号"这个属性（id 里不含文件名），所以 --from 下不该冒出来 ——
        即使那条消息的正文里提到了号名。"""
        self.assertEqual(self.ids(line='all', from_source='红星新闻'),
                         ['article:2026-10-01\\新闻\\红星新闻-暗访助贷公司.md'])

    def test_empty_is_empty_not_a_silent_fallback(self):
        self.assertEqual(self.ids(line='all', from_source='不存在的号'), [])

    def test_matches_by_source_field_too(self):
        """将来若把 `source` 写进记录，这一条要跟着认（前向兼容）。"""
        meta = json.loads(ss.META_FILE.read_text(encoding='utf-8'))
        meta.append({'id': 'article:2026-10-02\\新闻\\某文件.md', 'type': 'article',
                     'source': '大气污染模拟', 'text': '土地利用数据'})
        ss.META_FILE.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
        v = [0.0] * DIM
        v[0] = 1.0
        np.save(ss.VECTORS_FILE, np.array([v] * 4, dtype=np.float32))

        self.assertEqual(self.ids(line='wiki', from_source='大气污染模拟'),
                         ['article:2026-10-02\\新闻\\某文件.md'])

    def test_combines_with_hybrid_keyword_side(self):
        """开了 `--hybrid` 时，关键词那一侧也必须跟着 --from 筛，否则它会从别的号里拉人进来。"""
        got = self.ids(line='all', from_source='红星新闻', hybrid=True)
        self.assertEqual(got, ['article:2026-10-01\\新闻\\红星新闻-暗访助贷公司.md'])


if __name__ == '__main__':
    unittest.main()
