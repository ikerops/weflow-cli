"""混合检索（`--hybrid`）的零件与接线：分词、BM25 的 idf 与掩码、RRF 的确定性、以及"关键词救回向量丢掉的那条"。

为什么这些要有测试：这套东西是**默认关的可选路径**（2026-10-15 实测它只在"问句含稀有词"时更好），
所以它一旦被误当成默认、或者掩码漏了（`--line wiki` 时把聊天记录带进来），症状都会很安静：
结果看着正常，只是排序变了。四条各钉住一个"安静地错"的方式。
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


def onehot(i, val=1.0):
    v = [0.0] * DIM
    v[i] = val
    return v


class TokenizerTests(unittest.TestCase):
    def test_cjk_bigrams_and_ascii_words(self):
        self.assertEqual(ss.query_terms('空气质量'), ['空气', '气质', '质量'])
        # `PM2.5` 里 `pm2` 是唯一的 ≥2 字符 ASCII 段：`.` 是分隔符，`5` 单字符被丢掉
        # （单字符 ASCII 在中文语料里只会添噪声；而文本里的 `pm2.5` 同样落在 `pm2` 上）
        self.assertEqual(ss.query_terms('PM2.5 遥感'), ['遥感', 'pm2'])
        self.assertEqual(ss.query_terms('好'), ['好'])
        self.assertEqual(ss.query_terms('   '), [])

    def test_terms_are_deduped_in_order(self):
        # 「空气」出现两次只留一个，且顺序稳定（同样的输入给同样的词表 —— 缓存/复现都靠它）
        self.assertEqual(ss.query_terms('空气 空气'), ['空气'])


class KeywordRankTests(unittest.TestCase):
    def meta(self):
        return [
            {'id': 'a', 'type': 'chat', 'text': '稀有词在这里'},          # 只有 a 含"稀有"
            {'id': 'b', 'type': 'chat', 'text': '常见词常见词常见词'},
            {'id': 'c', 'type': 'chat', 'text': '常见词'},
            {'id': 'd', 'type': 'article', 'text': '常见词'},
        ]

    def test_idf_prefers_the_rare_term(self):
        # 同一个查询里"稀有"只在一篇出现、"常见"在三篇出现 —— 命中稀有的那篇必须排第一
        ranked = ss.keyword_rank('稀有词 常见词', self.meta())
        self.assertEqual(ranked[0][0], 0, ranked)
        self.assertGreater(ranked[0][1], ranked[1][1])

    def test_no_match_returns_empty(self):
        self.assertEqual(ss.keyword_rank('一点都沾不上', self.meta()), [])

    def test_mask_keeps_the_other_line_out(self):
        meta = self.meta()
        mask = np.array([m['type'] == 'article' for m in meta])
        ranked = ss.keyword_rank('稀有词', meta, mask)
        self.assertEqual(ranked, [], '掩码挡掉的记录不许从关键词这一侧冒出来')

    def test_ties_break_by_index(self):
        meta = [{'id': 'x', 'type': 'chat', 'text': '同名同分'},
                {'id': 'y', 'type': 'chat', 'text': '同名同分'}]
        self.assertEqual([i for i, _s in ss.keyword_rank('同名', meta)], [0, 1])


class RrfTests(unittest.TestCase):
    def test_both_lists_agreeing_wins(self):
        fused = dict(ss.rrf_fuse([[2, 0, 1], [2, 1, 0]]))
        self.assertGreater(fused[2], fused[0])
        self.assertGreater(fused[2], fused[1])

    def test_order_is_deterministic_on_ties(self):
        # 分数完全并列时按下标升序 —— 不能让结果取决于 dict 的插入顺序
        self.assertEqual([i for i, _s in ss.rrf_fuse([[5, 3], [3, 5]])], [3, 5])

    def test_single_list_still_scores(self):
        self.assertEqual([i for i, _s in ss.rrf_fuse([[7, 8]])], [7, 8])


class HybridRescueTests(unittest.TestCase):
    """端到端：向量把正确的那条排到最后，关键词把它救回来 —— 且只有 `--hybrid` 打开时才救。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='kb-hyb-'))
        self._saved = (ss.INDEX_DIR, ss.VECTORS_FILE, ss.META_FILE, ss.get_embeddings)
        ss.INDEX_DIR = self.root
        ss.VECTORS_FILE = self.root / 'vectors.npy'
        ss.META_FILE = self.root / 'meta.json'

        meta = [{'id': 'other1', 'type': 'article', 'text': '无关的一篇', 'title': '无关一'},
                {'id': 'other2', 'type': 'article', 'text': '也无关', 'title': '无关二'},
                {'id': 'target', 'type': 'article', 'text': '这条里有只在字面上出现的稀罕词',
                 'title': '目标'}]
        ss.META_FILE.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
        # 前两条与查询向量同向，目标那条正交 ⇒ 向量侧把目标排到最后
        ss.VECTORS_FILE.parent.mkdir(parents=True, exist_ok=True)
        np.save(ss.VECTORS_FILE, np.array([onehot(0), onehot(0, 0.9), onehot(1)], dtype=np.float32))
        ss.get_embeddings = lambda texts, key, on_batch=None: [onehot(0)]

    def tearDown(self):
        ss.INDEX_DIR, ss.VECTORS_FILE, ss.META_FILE, ss.get_embeddings = self._saved
        shutil.rmtree(self.root, ignore_errors=True)

    def test_keyword_rescues_when_enabled(self):
        # top_k=2 才判得出"救没救"：取 3 条的话三条都会返回，向量把目标排第几都看不出来
        res = ss.search('稀罕词', 'key', top_k=2, rerank_results=False, line='all', hybrid=True)
        self.assertIn('target', [r['id'] for r in res], '开启了混合检索，关键词侧应当把目标救回来')

    def test_default_is_off(self):
        res = ss.search('稀罕词', 'key', top_k=2, rerank_results=False, line='all')
        self.assertEqual([r['id'] for r in res], ['other1', 'other2'],
                         '默认必须是关的（实测真实问句上开了不优）—— 这条红了说明默认被改了')

    def test_line_mask_applies_to_the_keyword_side_too(self):
        """`--line wiki` 时，即使聊天记录完美命中关键词，也不许从这条路上混进来。"""
        meta = json.loads(ss.META_FILE.read_text(encoding='utf-8'))
        meta.append({'id': 'chat-hit', 'type': 'chat', 'text': '稀罕词', 'talker': '某人'})
        ss.META_FILE.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
        np.save(ss.VECTORS_FILE,
                np.array([onehot(0), onehot(0, 0.9), onehot(1), onehot(1, 0.5)], dtype=np.float32))

        res = ss.search('稀罕词', 'key', top_k=5, rerank_results=False, line='wiki', hybrid=True)
        ids = [r['id'] for r in res]
        self.assertNotIn('chat-hit', ids, '关键词侧没有跟着掩码，另一条线的记录漏进来了')
        self.assertIn('target', ids)


if __name__ == '__main__':
    unittest.main()
