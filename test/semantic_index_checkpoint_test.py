"""建索引必须**边嵌边落盘**、失败批次不许写进索引、meta 与向量行数不一致时不许越界。

为什么有这三条（2026-10-15 实测）：
· 全量重建这份索引要跑二十分钟以上，而 `search-index` 给子进程的超时是**五分钟**（`bin/weflow-cli.ts`）——
  原来那个"攒到最后才写"的写法，实际结果是"CLI 里 100% 跑不完，而且每次失败都把这一次花的钱和时间全丢"。
· `get_embeddings` 对失败的批次返回**全零**向量。照旧写进索引的话，那些 id 会永久躺在 meta 里、
  再也不会被重嵌（增量只嵌新 id）—— 索引被安静地投毒，而且没有任何东西会红。
· meta 比 vectors **长**时，`np.dot` 出来的下标会越界，`search()` 里那个 `except: pass` 会把它变成
  "静默退回关键词检索"：一次真检索看起来像"没命中"。所以按少的那个截断，宁可少几条。

夹具全是假的：假的 urlopen、假的 embedding 函数、临时索引目录，不联网、不碰真库。
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


def vec(first=0.5, **rest):
    v = [0.0] * DIM
    v[0] = first
    for k, val in rest.items():
        v[int(k[1:])] = val
    return v


class FakeResp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode('utf-8')

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class IndexFilesMixin(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='kb-idx-'))
        self._saved = (ss.INDEX_DIR, ss.VECTORS_FILE, ss.META_FILE)
        ss.INDEX_DIR = self.root
        ss.VECTORS_FILE = self.root / 'vectors.npy'
        ss.META_FILE = self.root / 'meta.json'

    def tearDown(self):
        ss.INDEX_DIR, ss.VECTORS_FILE, ss.META_FILE = self._saved
        shutil.rmtree(self.root, ignore_errors=True)

    def read_index(self):
        meta = json.loads(ss.META_FILE.read_text(encoding='utf-8'))
        vectors = np.load(ss.VECTORS_FILE)
        return meta, vectors


class SaveIndexTests(IndexFilesMixin):
    def test_rows_match_and_are_normalized(self):
        base = np.zeros((1, DIM), dtype=np.float32)
        base[0, 0] = 2.0                      # 故意不归一：本函数负责归一化
        pairs = [({'id': 'a', 'type': 'chat', 'text': 'x'}, vec(3.0)),
                 ({'id': 'b', 'type': 'chat', 'text': 'y'}, vec(0.0, _1=4.0))]

        n = ss._save_index(base, [{'id': 'base', 'type': 'chat'}], pairs)

        meta, vectors = self.read_index()
        self.assertEqual(n, 3)
        self.assertEqual([m['id'] for m in meta], ['base', 'a', 'b'])
        self.assertEqual(vectors.shape, (3, DIM), 'meta 行数与向量行数必须一致')
        np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)
        leftovers = list(self.root.glob('*.tmp*'))
        self.assertEqual(leftovers, [], '临时文件必须被 replace 掉，不能留在索引目录里')

    def test_flushing_twice_keeps_earlier_records(self):
        """中途落盘两次：第二次的盘上内容必须包含第一次那批（否则"续跑"就丢记录了）。"""
        base = np.empty((0, DIM), dtype=np.float32)
        ss._save_index(base, [], [({'id': 'a', 'type': 'chat'}, vec(1.0))])
        ss._save_index(base, [], [({'id': 'a', 'type': 'chat'}, vec(1.0)),
                                  ({'id': 'b', 'type': 'chat'}, vec(1.0))])

        meta, vectors = self.read_index()
        self.assertEqual([m['id'] for m in meta], ['a', 'b'])
        self.assertEqual(vectors.shape[0], 2)


class EmbeddingProgressTests(unittest.TestCase):
    """`on_batch(done, total, batch_embeddings)`：进度与"哪批全零"都要如实报出来。"""

    def setUp(self):
        self._saved = ss.urllib.request.urlopen
        self.calls = []

    def tearDown(self):
        ss.urllib.request.urlopen = self._saved

    def fake_open(self, fail_on=()):
        state = {'n': 0}

        def _open(req, timeout=None):
            state['n'] += 1
            batch = json.loads(req.data.decode('utf-8'))['input']
            if state['n'] in fail_on:
                raise OSError('boom')
            return FakeResp({'data': [{'embedding': vec(1.0)} for _ in batch]})
        return _open

    def test_reports_every_batch(self):
        ss.urllib.request.urlopen = self.fake_open()
        seen = []
        ss.get_embeddings(['t%d' % i for i in range(25)], 'key',
                          on_batch=lambda done, total, embs: seen.append((done, total, len(embs))))
        self.assertEqual(seen, [(10, 25, 10), (20, 25, 10), (25, 25, 5)])

    def test_failed_batch_is_reported_as_zeros(self):
        ss.urllib.request.urlopen = self.fake_open(fail_on=(2,))
        batches = []
        ss.get_embeddings(['t%d' % i for i in range(25)], 'key',
                          on_batch=lambda done, total, embs: batches.append(embs))
        self.assertEqual(len(batches), 3)
        self.assertTrue(all(any(v) for v in batches[0]), '第 1 批应当是正常向量')
        self.assertTrue(all(not any(v) for v in batches[1]),
                        '失败的那批必须是全零 —— 上层靠这个判据把它排除出索引')


class SearchRowGuardTests(IndexFilesMixin):
    """meta 比 vectors **短**时（中途被杀、两份文件新旧不一），不许越界后静默退回关键词。"""

    def setUp(self):
        super().setUp()
        self._saved_fns = (ss.get_embeddings, ss.keyword_search)

    def tearDown(self):
        ss.get_embeddings, ss.keyword_search = self._saved_fns
        super().tearDown()

    def test_short_meta_is_truncated_not_fatal(self):
        np.save(ss.VECTORS_FILE, np.array([vec(1.0), vec(1.0)], dtype=np.float32))
        ss.META_FILE.write_text(json.dumps([{'id': 'a', 'type': 'chat', 'text': 'x'}]),
                                encoding='utf-8')
        ss.get_embeddings = lambda texts, key, on_batch=None: [vec(1.0)]
        ss.keyword_search = lambda *a, **k: [{'id': 'FALLBACK', 'type': 'chat', 'score': 1}]

        res = ss.search('q', 'key', top_k=5, rerank_results=False, line='all')

        ids = [r['id'] for r in res]
        self.assertEqual(ids, ['a'], '按少的那个截断后应当只剩 a，且必须是**向量**那条路的结果')
        self.assertNotIn('FALLBACK', ids, '越界会被 except 吞掉并退回关键词 —— 那是最难看出来的一种')


if __name__ == '__main__':
    unittest.main()
