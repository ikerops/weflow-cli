"""向量那条腿失败时的行为：**显式降级**（把原因带出来），而且 `--mmr` 不许崩。

三件事都是在 2026-10-18 的审阅里读出来的，不是假想：

1. `search()` 原来有一个**裸 `except: pass`**：向量路径任何异常（embedding 上游失败、numpy 不可用、
   meta 坏掉、甚至 Ctrl+C）都会变成"静默只搜关键词"，而返回的 envelope 里照样写着
   `hybrid: true` —— 两种完全不同的质量在输出里长得一模一样。
2. `vectors` **只在 `try` 里赋值**，而 fallback 里的 `mmr_select(results, vectors, ...)` 要用它：
   向量在 `np.load` 之前就失败时，`--mmr` 会 **UnboundLocalError**。
3. `search()` 已经解析了 meta，`main()` 又调 `line_counts()` 再解析一遍同一个文件（实测 69+56 ms）。
   这里钉住"算一次、两边一致"，免得以后又拆回两份实现。

打桩 `keyword_search` 是必须的：它扫的是 `output/biz-daily` 下的真实文件（不是 meta），
不桩就会去扫整个语料。
"""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location('ss_fallback', SCRIPTS / 'semantic_search.py')
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)

META = [{'type': 'article', 'title': 'a1'}, {'type': 'article', 'title': 'a2'},
        {'type': 'chat', 'title': 'c1'}, {'type': 'chat', 'title': 'c2'}]
VECTORS = [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0], [0.0, 1.0]]
SENTINEL = [{'title': '关键词结果', 'score': 9.0}]


class VectorFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        np = ss.require_numpy()
        index = Path(self.tmp.name) / 'idx'
        index.mkdir(parents=True, exist_ok=True)
        self.vectors = index / 'vectors.npy'
        self.meta = index / 'meta.json'
        np.save(str(self.vectors), np.array(VECTORS, dtype=np.float32))
        self.meta.write_text(json.dumps(META, ensure_ascii=False), encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def run_search(self, embeddings, mmr=False):
        def fake_embeddings(texts, key):
            if isinstance(embeddings, Exception):
                raise embeddings
            return embeddings

        with patch.object(ss, 'VECTORS_FILE', self.vectors), \
             patch.object(ss, 'META_FILE', self.meta), \
             patch.object(ss, 'get_embeddings', fake_embeddings), \
             patch.object(ss, 'keyword_search',
                          lambda *a, **k: [dict(item) for item in SENTINEL]):
            return ss.search('任意查询', 'fake-key', top_k=10, rerank_results=False, mmr=mmr)

    def test_向量成功时_used_为真且没有降级原因(self):
        rows = self.run_search([[1.0, 0.0]])
        self.assertTrue(ss.VECTOR_STATE['used'])
        self.assertIsNone(ss.VECTOR_STATE['fallback'])
        self.assertEqual(len(rows), 4)
        self.assertEqual(ss.VECTOR_STATE['counts'], {'wiki': 2, 'chat': 2, 'all': 4})

    def test_embedding_抛错时退回关键词并记录原因(self):
        rows = self.run_search(RuntimeError('上游挂了'))
        self.assertEqual([r["title"] for r in rows], ["关键词结果"], '降级后应当返回关键词那一路的结果')
        self.assertFalse(ss.VECTOR_STATE['used'])
        self.assertIn('RuntimeError', ss.VECTOR_STATE['fallback'] or '')
        self.assertIn('上游挂了', ss.VECTOR_STATE['fallback'] or '')

    def test_embedding_返回全零也算降级(self):
        """全零是"请求成功但模型没干活"的已知形态（建索引那边同样按失败处理）。"""
        rows = self.run_search([[0.0, 0.0]])
        self.assertEqual([r["title"] for r in rows], ["关键词结果"])
        self.assertFalse(ss.VECTOR_STATE['used'])
        self.assertIn('embedding', ss.VECTOR_STATE['fallback'] or '')

    def test_mmr_在向量失败时不许崩(self):
        """`vectors` 原来只在 try 里赋值 —— 这条盯的是 embedding 之后失败。"""
        rows = self.run_search(RuntimeError('boom'), mmr=True)
        self.assertEqual([r["title"] for r in rows], ["关键词结果"])
        self.assertFalse(ss.VECTOR_STATE['used'])

    def test_numpy_不可用时_mmr_也不许崩(self):
        """**真正的 UnboundLocalError 回归测试**：异常发生在 `np.load` 之前（`require_numpy` 就炸），
        此时 `vectors` 从未被赋值，旧代码会在 fallback 的 `mmr_select(..., vectors, ...)` 处崩。

        为什么单独一条：上面那条（embedding 抛错）跑的时候 `vectors` **已经赋过了**，
        拿它做回归测试是"绿着但没盯住东西"——变异检查（把 `vectors = None` 拿掉）当场证明过这一点。
        """
        with patch.object(ss, 'VECTORS_FILE', self.vectors), \
             patch.object(ss, 'META_FILE', self.meta), \
             patch.object(ss, 'require_numpy', side_effect=ImportError('no numpy')), \
             patch.object(ss, 'keyword_search',
                          lambda *a, **k: [dict(item) for item in SENTINEL]):
            rows = ss.search('任意查询', 'fake-key', top_k=10, rerank_results=False, mmr=True)
        self.assertEqual([r["title"] for r in rows], ["关键词结果"])
        self.assertFalse(ss.VECTOR_STATE['used'])
        self.assertIn('ImportError', ss.VECTOR_STATE['fallback'] or '')

    def test_mmr_在向量成功时照常工作(self):
        rows = self.run_search([[1.0, 0.0]], mmr=True)
        self.assertTrue(ss.VECTOR_STATE['used'])
        self.assertGreaterEqual(len(rows), 1)


class CountsTests(unittest.TestCase):
    def test_从_meta_算出的条数与索引口径一致(self):
        self.assertEqual(ss._counts_from_meta(META), {'wiki': 2, 'chat': 2, 'all': 4})

    def test_all_是总条数而不是两条线相加(self):
        """多出第三种 type 时，它该被 all 带上、而不属于任何一条线。"""
        meta = META + [{'type': 'concept', 'title': 'x'}]
        self.assertEqual(ss._counts_from_meta(meta)['all'], 5)
        self.assertEqual(ss._counts_from_meta(meta)['wiki'], 2)


if __name__ == '__main__':
    unittest.main()
