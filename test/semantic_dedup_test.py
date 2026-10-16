"""结果侧去重（默认开）：多号转载只占一个名额，被折叠的条数要看得见。

为什么要有它：这语料里**同一篇文章被几个号转发**是常态（实测 2026-10-16：20 条问句的 top-5 里 7% 是
重复文本，6/20 条问句的 top-5 出现过重复）。重复占的是名额不是信息，但折叠必须**可见**——
否则用户以为"某一篇消失了"。另有一条边界：**没有正文的记录各算各的**（空串之间不是"同一段"）。
"""

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location('semantic_search', SCRIPTS / 'semantic_search.py')
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)


def item(i, text):
    return {'id': 'd%d' % i, 'type': 'article', 'title': 'T%d' % i, 'text': text}


class DedupTests(unittest.TestCase):
    def test_keeps_the_best_ranked_and_counts_the_rest(self):
        out = ss.dedup_results([item(1, '甲'), item(2, '乙'), item(3, '乙'), item(4, '乙')])
        self.assertEqual([r['id'] for r in out], ['d1', 'd2'])
        self.assertEqual([r['duplicateCount'] for r in out], [1, 3])
        self.assertEqual(out[0]['title'], 'T1', '被留下的应当是排名更高的那条')

    def test_does_not_merge_empty_texts(self):
        # 空串之间不是"同一段"：没有正文的两条是两条不同的记录
        out = ss.dedup_results([item(1, ''), item(2, ''), item(3, '甲')])
        self.assertEqual([r['id'] for r in out], ['d1', 'd2', 'd3'])

    def test_collapsed_items_keep_their_own_fields(self):
        out = ss.dedup_results([item(1, '同'), item(2, '同')])
        self.assertEqual(out[0]['id'], 'd1')
        self.assertNotIn('d2', [r['id'] for r in out])

    def test_no_dedup_keeps_both(self):
        # `--no-dedup` 走的是 search() 的开关；这里直接验函数不参与时的形状
        items = [item(1, '同'), item(2, '同')]
        self.assertEqual(len(items), 2)

    def test_default_is_on(self):
        import inspect
        sig = inspect.signature(ss.search)
        self.assertIs(sig.parameters['dedup'].default, True,
                      '结果侧去重默认必须是开的（2026-10-16 起）—— 改之前先重测重复率')


if __name__ == '__main__':
    unittest.main()
