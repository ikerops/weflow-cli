"""混合检索（默认开）的零件与接线：分词、BM25 的 idf、**门控**、RRF 的确定性、以及"关键词救回向量丢掉的那条"。

为什么这些要有测试：这条路**默认开着**，所以它一旦退化（门控漏了、掩码漏了、默认被改回去），
症状都只是"排序变了"——没有任何东西会红。五条各钉住一个"安静地错"的方式：

  · 分词：中文二元组 + ASCII 词，去重且顺序稳定（同样的输入给同样的词表）
  · idf：稀有词命中的那篇必须压过只命中常见词的那些
  · 门控（1）**弃权**：问句里一个稀有词都没有（"怎么用/有没有"）⇒ 关键词侧不投票
  · 门控（2）**只留头部**：分数远低于最高分的那些不投票
  · RRF：并列的次序按下标定死（同一批数据两次跑必须同序）
  · 掩码：`--line wiki` 时聊天记录不许从关键词这一侧混进来
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
        self.assertEqual(ss.query_terms('PM2.5 遥感'), ['遥感', 'pm2'])
        self.assertEqual(ss.query_terms('好'), ['好'])
        self.assertEqual(ss.query_terms('   '), [])

    def test_terms_are_deduped_in_order(self):
        self.assertEqual(ss.query_terms('空气 空气'), ['空气'])


class KeywordRankTests(unittest.TestCase):
    def meta(self):
        return [
            {'id': 'a', 'type': 'chat', 'text': '稀有词在这里'},
            {'id': 'b', 'type': 'chat', 'text': '常见词常见词常见词'},
            {'id': 'c', 'type': 'chat', 'text': '常见词'},
            {'id': 'd', 'type': 'article', 'text': '常见词'},
        ]

    def test_idf_prefers_the_rare_term(self):
        ranked = ss.keyword_rank('稀有词 常见词', self.meta())
        self.assertEqual(ranked[0][0], 0, ranked)
        self.assertGreater(ranked[0][1], ranked[1][1])

    def test_no_match_returns_empty(self):
        self.assertEqual(ss.keyword_rank('一点都沾不上', self.meta()), [])

    def test_mask_keeps_the_other_line_out(self):
        meta = self.meta()
        mask = np.array([m['type'] == 'article' for m in meta])
        self.assertEqual(ss.keyword_rank('稀有词', meta, mask), [])

    def test_ties_break_by_index(self):
        meta = [{'id': 'x', 'type': 'chat', 'text': '同名同分'},
                {'id': 'y', 'type': 'chat', 'text': '同名同分'}]
        self.assertEqual([i for i, _s in ss.keyword_rank('同名', meta)], [0, 1])


class GateTests(unittest.TestCase):
    """门控：40 篇语料里出现 1 次 = 2.5% ≤ 5% ⇒ 算"稀有"；只命中常见词的不投票。"""

    def corpus(self):
        meta = [{'id': 'common%d' % i, 'type': 'chat', 'text': '常见词' * 3} for i in range(38)]
        meta.append({'id': 'rare', 'type': 'chat', 'text': '常见词 里夹着一个稀罕记号'})
        meta.append({'id': 'noise', 'type': 'chat', 'text': '另一个孤立的记号'})
        return meta

    def test_abstains_when_no_rare_term(self):
        # 全部 40 篇都含"常见词" ⇒ 它不是稀有词；问句里没有稀有词 ⇒ 关键词侧弃权
        self.assertEqual(ss.keyword_rank('常见词', self.corpus(),
                                         rare_ratio=ss.HYBRID_RARE_RATIO,
                                         score_ratio=ss.HYBRID_SCORE_RATIO), [])

    def test_keeps_only_rare_matching_docs(self):
        ranked = ss.keyword_rank('稀罕记号', self.corpus(), rare_ratio=ss.HYBRID_RARE_RATIO,
                                 score_ratio=ss.HYBRID_SCORE_RATIO)
        self.assertEqual([i for i, _s in ranked], [38], ranked)

    def test_score_ratio_drops_the_long_tail(self):
        # 两篇都命中稀有词，但一篇命中两个、另一篇只命中一个 ⇒ 紧的相对阈只留头部
        meta = [{'id': 'c%d' % i, 'type': 'chat', 'text': '常见词' * 3} for i in range(38)]
        meta.append({'id': 'both', 'type': 'chat', 'text': '甲记号 乙记号 都在这里'})
        meta.append({'id': 'one', 'type': 'chat', 'text': '甲记号 但只有这一个'})
        loose = ss.keyword_rank('甲记号 乙记号', meta, rare_ratio=ss.HYBRID_RARE_RATIO,
                                score_ratio=None)
        tight = ss.keyword_rank('甲记号 乙记号', meta, rare_ratio=ss.HYBRID_RARE_RATIO,
                                score_ratio=0.9)
        self.assertIn(38, [i for i, _s in loose])
        self.assertEqual([i for i, _s in tight], [38], tight)


class RrfTests(unittest.TestCase):
    def test_both_lists_agreeing_wins(self):
        fused = dict(ss.rrf_fuse([[2, 0, 1], [2, 1, 0]]))
        self.assertGreater(fused[2], fused[0])
        self.assertGreater(fused[2], fused[1])

    def test_order_is_deterministic_on_ties(self):
        self.assertEqual([i for i, _s in ss.rrf_fuse([[5, 3], [3, 5]])], [3, 5])

    def test_single_list_still_scores(self):
        self.assertEqual([i for i, _s in ss.rrf_fuse([[7, 8]])], [7, 8])


class HybridRescueTests(unittest.TestCase):
    """端到端：向量把正确的那条排到最后，关键词把它救回来 —— 而且是**默认**行为。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='kb-hyb-'))
        self._saved = (ss.INDEX_DIR, ss.VECTORS_FILE, ss.META_FILE, ss.get_embeddings,
                       ss.HYBRID_RARE_RATIO)
        ss.INDEX_DIR = self.root
        ss.VECTORS_FILE = self.root / 'vectors.npy'
        ss.META_FILE = self.root / 'meta.json'
        # 小语料里 df/N 天然偏高，会让门控一律弃权；这几条测的是**接线**，所以把阈值放到 1.0
        ss.HYBRID_RARE_RATIO = 1.0

        meta = [{'id': 'other1', 'type': 'article', 'text': '无关的一篇', 'title': '无关一'},
                {'id': 'other2', 'type': 'article', 'text': '也无关', 'title': '无关二'},
                {'id': 'target', 'type': 'article', 'text': '这条里有只在字面上出现的稀罕词',
                 'title': '目标'}]
        ss.META_FILE.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
        ss.VECTORS_FILE.parent.mkdir(parents=True, exist_ok=True)
        np.save(ss.VECTORS_FILE, np.array([onehot(0), onehot(0, 0.9), onehot(1)], dtype=np.float32))
        ss.get_embeddings = lambda texts, key, on_batch=None: [onehot(0)]

    def tearDown(self):
        (ss.INDEX_DIR, ss.VECTORS_FILE, ss.META_FILE, ss.get_embeddings,
         ss.HYBRID_RARE_RATIO) = self._saved
        shutil.rmtree(self.root, ignore_errors=True)

    def test_default_is_on_and_rescues(self):
        # top_k=2 才判得出"救没救"：取 3 条的话三条都会返回，向量把目标排第几都看不出来
        res = ss.search('稀罕词', 'key', top_k=2, rerank_results=False, line='all')
        self.assertIn('target', [r['id'] for r in res],
                      '默认必须融合关键词名次（2026-10-15 实测门控后只增不减）—— 这条红了说明默认被改了')

    def test_no_hybrid_escape_restores_plain_vector_order(self):
        res = ss.search('稀罕词', 'key', top_k=2, rerank_results=False, line='all', hybrid=False)
        self.assertEqual([r['id'] for r in res], ['other1', 'other2'])

    def test_line_mask_applies_to_the_keyword_side_too(self):
        """`--line wiki` 时，即使聊天记录完美命中关键词，也不许从这条路上混进来。"""
        meta = json.loads(ss.META_FILE.read_text(encoding='utf-8'))
        meta.append({'id': 'chat-hit', 'type': 'chat', 'text': '稀罕词', 'talker': '某人'})
        ss.META_FILE.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
        np.save(ss.VECTORS_FILE,
                np.array([onehot(0), onehot(0, 0.9), onehot(1), onehot(1, 0.5)], dtype=np.float32))

        res = ss.search('稀罕词', 'key', top_k=5, rerank_results=False, line='wiki')
        ids = [r['id'] for r in res]
        self.assertNotIn('chat-hit', ids, '关键词侧没有跟着掩码，另一条线的记录漏进来了')
        self.assertIn('target', ids)


if __name__ == '__main__':
    unittest.main()
