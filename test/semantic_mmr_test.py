"""MMR 去冗余（`--mmr`，**默认关**）：算法本身、以及"内部字段不许漏进输出"。

为什么默认关（2026-10-16 实测，同一批题）：
  · 多样性确实改善：最终 top-10 两两余弦均值 0.627 → 0.559；
  · 但**判官口径下的准确率掉了**：严格相关 40% → 30%、宽松 68% → 64%；recall@5 也掉 0.03。
  原因值得记：这条语料里的"重复"常常是**多个号转载同一件事**（多源确认），MMR 压掉的正是这些——
  所以它治的不是"邻居太多"，而是把有用的重复一起删了。默认关、留作开关。
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


def vec(*pairs):
    v = np.zeros(DIM, dtype=np.float32)
    for idx, val in pairs:
        v[idx] = val
    return v / np.linalg.norm(v)


class MmrSelectTests(unittest.TestCase):
    def test_diversity_penalty_demotes_the_near_duplicate(self):
        # A 与 B 几乎同向（近重复），C 与它们正交但相关性略低
        matrix = np.array([vec((0, 1.0)), vec((0, 0.98), (1, 0.2)), vec((1, 1.0))], dtype=np.float32)
        items = [{'id': 'A', '_row': 0, 'rerankScore': 100},
                 {'id': 'B', '_row': 1, 'rerankScore': 99},
                 {'id': 'C', '_row': 2, 'rerankScore': 80}]

        picked = ss.mmr_select(items, matrix, 2, lam=0.7)
        self.assertEqual([p['id'] for p in picked[:2]], ['A', 'C'], 'λ=0.7 时该拿"相关但不同"的那条')

        greedy = ss.mmr_select(items, matrix, 2, lam=1.0)
        self.assertEqual([p['id'] for p in greedy[:2]], ['A', 'B'], 'λ=1 退化成纯相关性')

    def test_returns_everything_when_k_covers_all(self):
        matrix = np.array([vec((0, 1.0)), vec((1, 1.0))], dtype=np.float32)
        items = [{'id': 'A', '_row': 0, 'score': 0.9}, {'id': 'B', '_row': 1, 'score': 0.1}]
        out = ss.mmr_select(items, matrix, 5)
        self.assertEqual(sorted(p['id'] for p in out), ['A', 'B'])

    def test_without_row_it_does_not_guess(self):
        items = [{'id': 'A'}, {'id': 'B'}]
        self.assertEqual(ss.mmr_select(items, np.zeros((0, DIM), dtype=np.float32), 1), items,
                         '拿不到行号就不动它 —— 不许按位置瞎猜')

    def test_rerank_score_is_the_relevance_term(self):
        # 余弦相同（同向），但重排分不同 ⇒ 应当按重排分挑
        same = vec((0, 1.0))
        matrix = np.array([same, same], dtype=np.float32)
        items = [{'id': '低', '_row': 0, 'rerankScore': 10, 'score': 0.9},
                 {'id': '高', '_row': 1, 'rerankScore': 90, 'score': 0.9}]
        self.assertEqual(ss.mmr_select(items, matrix, 1)[0]['id'], '高')


class RowFieldLeakTests(unittest.TestCase):
    """`_row` 是内部用的行号：开了关了的 MMR 都不许把它漏进给用户的结果里。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='kb-mmr-'))
        self._saved = (ss.INDEX_DIR, ss.VECTORS_FILE, ss.META_FILE, ss.get_embeddings)
        ss.INDEX_DIR = self.root
        ss.VECTORS_FILE = self.root / 'vectors.npy'
        ss.META_FILE = self.root / 'meta.json'
        meta = [{'id': 'd%d' % i, 'type': 'article', 'title': 'T%d' % i, 'text': '正文%d' % i}
                for i in range(3)]
        ss.META_FILE.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
        np.save(ss.VECTORS_FILE, np.array([vec((0, 1.0)), vec((0, 0.99), (1, 0.1)),
                                           vec((1, 1.0))], dtype=np.float32))
        ss.get_embeddings = lambda texts, key, on_batch=None: [list(vec((0, 1.0)))]

    def tearDown(self):
        (ss.INDEX_DIR, ss.VECTORS_FILE, ss.META_FILE, ss.get_embeddings) = self._saved
        shutil.rmtree(self.root, ignore_errors=True)

    def test_no_row_field_in_output(self):
        for mmr in (False, True):
            res = ss.search('q', 'key', top_k=3, rerank_results=False, line='all', mmr=mmr)
            self.assertTrue(res, '应当有结果')
            for row in res:
                self.assertNotIn('_row', row, 'mmr=%s 时 _row 漏进了输出' % mmr)


if __name__ == '__main__':
    unittest.main()
