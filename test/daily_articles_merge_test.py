"""`.articles.json` 必须是**合并写**，否则"一天跑两次"会让结构化数据少一半。

背景：午报 13:00 与日终 24:00 会各跑一次同一天。第二次进来时 `.run_state.json` 已经
把上午处理过的文章跳过，所以这一批只有下午新到达的那些 —— 若覆盖写，上午那批的
标题/摘要/主题/relevance 就从 `.articles.json` 里消失。而 README 与 index.html 是
**从磁盘上的 md 重建**的，于是页面是全天、结构化数据只有半天，下游静默少一半且不报错。

这条测试钉四件事：并集不丢、同 key 以新为准、坏文件不拖垮今天、`decisionModel`
不被 `None` 覆盖。
"""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location('biz_merge', SCRIPTS / 'biz_daily.py')
biz = importlib.util.module_from_spec(spec)
spec.loader.exec_module(biz)


def entry(title, time, url='', topic='AI', relevance='中'):
    return {'title': title, 'source': '某号', 'date': '2026-10-19', 'time': time,
            'topic': topic, 'relevance': relevance, 'tags': ['AI'],
            'summary': '', 'url': url}


class MergeArticlesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / '.articles.json'

    def tearDown(self):
        self.tmp.cleanup()

    def test_第一次写入(self):
        out = biz.merge_articles_json(self.path, '2026-10-19', [entry('甲', '08:00', 'u1')])
        self.assertEqual(len(out), 1)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['articles'][0]['title'], '甲')

    def test_并集不丢_同key以新为准(self):
        biz.merge_articles_json(self.path, '2026-10-19',
                                [entry('上午甲', '08:00', 'u1'), entry('上午乙', '09:00', 'u2')])
        out = biz.merge_articles_json(self.path, '2026-10-19', [
            entry('上午甲（补了摘要）', '08:00', 'u1'),   # 同一 url：更新
            entry('下午丙', '15:00', 'u3'),              # 新增
        ])
        titles = [e['title'] for e in out]
        self.assertEqual(sorted(titles), sorted(['上午甲（补了摘要）', '上午乙', '下午丙']))
        self.assertEqual(len(out), 3, '上午乙不许被第二次的写入抹掉')

    def test_没有url时按标题加时间合并(self):
        biz.merge_articles_json(self.path, '2026-10-19', [entry('同题', '08:00')])
        out = biz.merge_articles_json(self.path, '2026-10-19', [entry('同题', '08:00'), entry('同题', '09:00')])
        self.assertEqual(len(out), 2, '标题相同但时间不同 = 两篇不同推送')

    def test_按时间排序(self):
        out = biz.merge_articles_json(self.path, '2026-10-19', [
            entry('晚', '18:00', 'u3'), entry('早', '07:00', 'u1'), entry('中', '12:00', 'u2')])
        self.assertEqual([e['time'] for e in out], ['07:00', '12:00', '18:00'])

    def test_旧文件坏了不拖垮今天(self):
        self.path.write_text('{ 这不是 json', encoding='utf-8')
        out = biz.merge_articles_json(self.path, '2026-10-19', [entry('甲', '08:00', 'u1')])
        self.assertEqual(len(out), 1)

    def test_decisionModel不被None覆盖(self):
        biz.merge_articles_json(self.path, '2026-10-19', [entry('甲', '08:00', 'u1')], decision_model='deepseek-flash')
        # 第二次是 `--no-ai` 那种没有判断的跑：不许把上一次记下的模型名抹成 null
        biz.merge_articles_json(self.path, '2026-10-19', [entry('乙', '09:00', 'u2')], decision_model=None)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['decisionModel'], 'deepseek-flash')

    def test_generated_at会更新到最后一次(self):
        import re
        biz.merge_articles_json(self.path, '2026-10-19', [entry('甲', '08:00', 'u1')])
        first = json.loads(self.path.read_text(encoding='utf-8'))['generated_at']
        biz.merge_articles_json(self.path, '2026-10-19', [entry('乙', '09:00', 'u2')])
        second = json.loads(self.path.read_text(encoding='utf-8'))['generated_at']
        self.assertRegex(second, r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')
        self.assertGreaterEqual(second, first, 'generated_at 记录的应是最后一次写入')


if __name__ == '__main__':
    unittest.main()
