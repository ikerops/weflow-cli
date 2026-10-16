"""重排这条路：段落清洗、合同校验、以及"降级必须看得见"。

为什么要有它：重排在**本机一直是空转的**（Typesafe 451 地区封锁），而失败只往 stderr 打一行 WARN，
CLI 又不转发子进程 stderr —— 于是"没重排"看起来像"重排过了"。这一组把三件事钉住：
  · 清洗：送进模型的摘录里不该有 `**加粗**`、公众号套话、署名行、图片语法；
  · 合同：返回的候选编号不齐 ⇒ **保持原序**并标降级（这是重排最危险的失效方式：模型把编号搞混）；
  · 可见：失败/无 key 都写进 `RERANK_STATE`，而成功要写清楚是哪一家在排。
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


class FakeResp:
    def __init__(self, content):
        self._body = json.dumps({'choices': [{'message': {'content': content}}]}).encode('utf-8')

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def items(n=4):
    return [{'id': 'd%d' % i, 'type': 'article', 'title': '标题%d' % i, 'text': '正文%d' % i}
            for i in range(n)]


class CleanseTests(unittest.TestCase):
    def test_strips_markup_and_boilerplate(self):
        raw = ('**点击蓝字** 关注我们   ![Image](https://mmbiz.qpic.cn/x/640)\n'
               '原创 某某某 某某某 某公众号\n真正的正文在这里，讲的是空气质量模型。')
        out = ss.cleanse_passage(raw)
        self.assertNotIn('**', out)
        self.assertNotIn('点击蓝字', out)
        self.assertNotIn('mmbiz.qpic.cn', out)
        self.assertNotIn('原创', out)
        self.assertIn('空气质量模型', out)

    def test_keeps_link_text_and_truncates(self):
        # 链接留文字：`[某篇论文](url)` → 某篇论文；但"阅读原文"这类**套话**即便写成链接也要去掉
        self.assertIn('某篇论文', ss.cleanse_passage('[某篇论文](http://x/y) 讲的是模型评估'))
        self.assertNotIn('阅读原文', ss.cleanse_passage('[阅读原文](http://x/y) 后面的话'))
        self.assertLessEqual(len(ss.cleanse_passage('很长' * 500)), ss.RERANK_EXCERPT_CHARS)


class LlmRerankTests(unittest.TestCase):
    def setUp(self):
        self._saved = (ss.urllib.request.urlopen, dict(ss.RERANK_STATE),
                       ss.get_api_key, ss.load_config)
        self.calls = []
        # 自足：不依赖本机 ~/.weflow-cli/config.json（CI 上没有它 —— 第一版因此
        # 在本机绿、在 CI 红）
        ss.load_config = lambda: {'aiModel': 'fake-model'}
        ss.get_api_key = lambda config=None: 'fake-key'

    def tearDown(self):
        (ss.urllib.request.urlopen, _state, ss.get_api_key, ss.load_config) = self._saved
        ss.RERANK_STATE.update(self._saved[1])

    def fake_open(self, content):
        def _open(req, timeout=None):
            self.calls.append(json.loads(req.data.decode('utf-8')))
            return FakeResp(content)
        return _open

    def test_reorders_and_reports_provider(self):
        ss.urllib.request.urlopen = self.fake_open(
            json.dumps({'scores': [{'i': 1, 'score': 10}, {'i': 2, 'score': 90},
                                   {'i': 3, 'score': 50}, {'i': 4, 'score': 0}]}))
        out = ss.llm_rerank('q', items(), key='k', model='m')
        self.assertEqual([it['id'] for it in out], ['d1', 'd2', 'd0', 'd3'])
        self.assertEqual(out[0]['rerankScore'], 90)
        self.assertEqual(ss.RERANK_STATE['provider'], 'llm')
        self.assertFalse(ss.RERANK_STATE['degraded'])
        # 送出去的提示里必须是**清洗过**的摘录，且带打分判据
        sent = self.calls[0]['messages'][0]['content']
        self.assertIn('候选1', sent)
        self.assertIn('0-100', sent)

    def test_incomplete_scores_keeps_original_order_and_says_so(self):
        ss.urllib.request.urlopen = self.fake_open(
            json.dumps({'scores': [{'i': 1, 'score': 90}]}))
        out = ss.llm_rerank('q', items(), key='k', model='m')
        self.assertEqual([it['id'] for it in out], ['d0', 'd1', 'd2', 'd3'])
        self.assertTrue(ss.RERANK_STATE['degraded'])
        self.assertEqual(ss.RERANK_STATE['error'], 'incomplete-scores')

    def test_broken_json_keeps_original_order(self):
        ss.urllib.request.urlopen = self.fake_open('这不是 JSON')
        out = ss.llm_rerank('q', items(), key='k', model='m')
        self.assertEqual(len(out), 4)
        self.assertTrue(ss.RERANK_STATE['degraded'])
    def test_unreadable_config_keeps_order_and_is_visible(self):
        # 配置读不出来时不许抛：一抛就会被 search() 的 except 吞成"静默退回关键词"
        def boom():
            raise FileNotFoundError('no config')
        ss.load_config = boom
        ss.urllib.request.urlopen = self.fake_open('{}')
        out = ss.llm_rerank('q', items(), key='k', model='m')
        self.assertEqual(len(out), 4)
        self.assertEqual(self.calls, [], '配置读不到就不该发请求')
        self.assertEqual(ss.RERANK_STATE['error'], 'config-unreadable')


    def test_no_key_makes_no_request(self):
        # 生产语义是"显式 key 优先、否则读配置"——所以要验"完全没有 key"，
        # 得把配置来源也堵上（第一版只传 key=''，于是它照旧读了配置里的 key，测试假绿）
        ss.load_config = lambda: {}
        ss.get_api_key = lambda config=None: ''
        ss.urllib.request.urlopen = self.fake_open('{}')
        out = ss.llm_rerank('q', items(), key='', model='m')
        self.assertEqual(len(out), 4)
        self.assertEqual(self.calls, [], '没有 key 就不该发请求（也不该把空 key 送出去）')
        self.assertEqual(ss.RERANK_STATE['error'], 'no-chat-key')


class DispatchTests(unittest.TestCase):
    """先试 Jev 契约、失败再退到对话模型 —— 而"退过"这件事必须留在状态里。"""

    def setUp(self):
        self._saved = (ss.rerank, ss.llm_rerank, dict(ss.RERANK_STATE))

    def tearDown(self):
        ss.rerank, ss.llm_rerank = self._saved[0], self._saved[1]
        ss.RERANK_STATE.update(self._saved[2])

    def test_falls_back_when_contract_client_fails(self):
        called = {}

        def fake_rerank(query, results, client=None, pool_size=20):
            ss.RERANK_STATE.update(provider='typesafe', degraded=True, error='HTTPError', scored=0)
            return results

        def fake_llm(query, results, **kw):
            called['yes'] = True
            ss.RERANK_STATE.update(provider='llm', degraded=False, error=None, scored=len(results))
            return [dict(r, rerankScore=1) for r in results]

        ss.rerank, ss.llm_rerank = fake_rerank, fake_llm
        out = ss.rerank_dispatch('q', items(), client=object())
        self.assertTrue(called.get('yes'), '契约侧失败后必须退到对话模型')
        self.assertTrue(all('rerankScore' in r for r in out))
        self.assertEqual(ss.RERANK_STATE['provider'], 'llm')

    def test_contract_success_is_not_overridden(self):
        def fake_rerank(query, results, client=None, pool_size=20):
            ss.RERANK_STATE.update(provider='typesafe', degraded=False, error=None, scored=4)
            return [dict(r, rerankScore=0.9) for r in results]
        ss.rerank = fake_rerank
        ss.llm_rerank = lambda *a, **kw: (_ for _ in ()).throw(AssertionError('不该退到 llm'))
        out = ss.rerank_dispatch('q', items(), client=object())
        self.assertEqual(ss.RERANK_STATE['provider'], 'typesafe')
        self.assertEqual(out[0]['rerankScore'], 0.9)


class PoolTests(unittest.TestCase):
    """重排候选池：默认值 + 它有没有真的传进 search()。

    池子是 2026-10-16 实测出来的最大杠杆（20 → 100 让 recall@10 从 0.82 到 0.95）——
    所以"默认被谁改回去"这件事得有东西盯着；而"传了没生效"是同一类静默失效。
    """

    def test_default_pool_is_100(self):
        self.assertEqual(ss.RERANK_CANDIDATES, 100,
                         '默认池被改了？改之前先重跑评测台（recall_eval --rerank-pool 20/50/100/200）')

    def test_search_forwards_pool_size(self):
        root = Path(tempfile.mkdtemp(prefix='kb-pool-'))
        saved = (ss.INDEX_DIR, ss.VECTORS_FILE, ss.META_FILE, ss.get_embeddings, ss.rerank_dispatch)
        seen = {}
        try:
            ss.INDEX_DIR = root
            ss.VECTORS_FILE = root / 'vectors.npy'
            ss.META_FILE = root / 'meta.json'
            meta = [{'id': 'd%d' % i, 'type': 'article', 'text': '正文%d' % i} for i in range(3)]
            ss.META_FILE.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
            v = [0.0] * ss.EMBEDDING_DIM
            v[0] = 1.0
            np.save(ss.VECTORS_FILE, np.array([v, v, v], dtype=np.float32))
            ss.get_embeddings = lambda texts, key, on_batch=None: [v]

            def fake_dispatch(query, results, client=None, pool_size=None):
                seen['pool'] = pool_size
                return results

            ss.rerank_dispatch = fake_dispatch
            ss.search('q', 'key', top_k=2, line='all', rerank_pool=7)
        finally:
            (ss.INDEX_DIR, ss.VECTORS_FILE, ss.META_FILE, ss.get_embeddings,
             ss.rerank_dispatch) = saved
            shutil.rmtree(root, ignore_errors=True)
        self.assertEqual(seen.get('pool'), 7, 'rerank_pool 没有传到重排（参数被吞了）')


if __name__ == '__main__':
    unittest.main()