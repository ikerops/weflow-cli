"""文章记录里嵌进去的必须是**正文**，不是 YAML 头。

为什么专门有一条：2026-10-15 实测最近 30 个日期目录的 10,759 篇日报 md，含 `## 正文` 的有
**0 篇**（真实结构是 `frontmatter → # 标题 → > 来源 → --- → ## AI 摘要 → --- → 重复标题 → 正文`），
而 `collect_articles` 找正文的判据只有 `line.startswith('## 正文')`，找不到就退回 `content[:1000]`。
后果是那 10,759 篇送进 embedding 的全是 YAML 头（抽样 300 篇：100% 含 `title:`，汉字占比中位
**0.16**），正文一个字都没进索引 —— 症状是"文章线排序只能靠标题"，不报错、不报警、也没有东西会红。

夹具**照真实结构**写（含那段几百字的 url 与公众号套话），并把要断言的那句话放在**第 1000 字之后**：
退回 `content[:1000]` 的老写法必然拿不到它，所以这条测试对"改成老样子"是会红的。
`## 正文` 那条老分支保留为**优先**路径，另有一条用例钉住它没有被新提取器顶掉。
"""

import importlib.util
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

PROSE = '沙尘气溶胶中的铁元素在光照下会催化有机物的降解，这条链路此前只在实验室里被观察到。'
SUMMARY = '模型能力够了，要办事还得看现有生态愿不愿意让你接入。'
LONG_URL = 'http://mp.weixin.qq.com/s?__biz=' + 'Mzk5NzY0NDc4Nw==' * 12 + '&scene=0&xtrack=1#rd'


def article_md(title='某号写的一篇文章', body_prose=PROSE, marker=None):
    """真实结构：frontmatter → # 标题 → > 来源 → --- → ## AI 摘要 → --- → 重复标题 → 正文。

    `marker` 传一组行时会插在正文之前，用来造"带 `## 正文` 的老形态"。
    """
    parts = [
        '---',
        'title: "%s"' % title,
        'source: "某公众号"',
        'date: 2026-10-13',
        'topic: AI',
        'relevance: 中',
        'tags: [AI]',
        'created: 2026-10-13',
        'backfilled: true',
        'url: "%s"' % LONG_URL,
        '---',
        '',
        '# %s' % title,
        '',
        '> 来源：某公众号  ',
        '> 时间：2026-10-13 23:27  ',
        '> 原文：[阅读原文](%s)' % LONG_URL,
        '',
        '---',
        '',
        '## AI 摘要',
        '',
        SUMMARY,
        '',
        '---',
        '',
        '#  %s' % title,
        '',
        '原创 一直在路上的Max 一直在路上的Max 某公众号',
        '',
        '______',
        '',
        '在小说阅读器读本章',
        '',
        '去阅读',
        '',
        '在公众号小说中沉浸阅读',
        '',
        '![Image](https://mmbiz.qpic.cn/mmbiz_jpg/xxxx/640?wx_fmt=jpeg)',
        '',
        '内容｜Max',
        '',
        '编辑｜Max',
        '',
        body_prose,
        '',
        '接下来是第二段正文，用来确保攒够 500 字之后还能取到连贯的句子。',
    ]
    if marker is not None:
        at = parts.index(body_prose)
        parts[at:at] = list(marker)
    return '\n'.join(parts)


def cjk_ratio(text):
    cjk = sum(1 for ch in text if '一' <= ch <= '鿿')
    return cjk / max(1, len(text))


class ArticleBodyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='kb-body-'))
        self._saved = ss.OUTPUT_ROOT
        ss.OUTPUT_ROOT = str(self.root)

    def tearDown(self):
        ss.OUTPUT_ROOT = self._saved
        shutil.rmtree(self.root, ignore_errors=True)

    def write_day(self, date, topic, filename, content):
        d = self.root / 'biz-daily' / date / topic
        d.mkdir(parents=True, exist_ok=True)
        (d / filename).write_text(content, encoding='utf-8')

    def test_body_is_prose_not_frontmatter(self):
        self.write_day('2026-10-13', 'AI', '某号-一篇讲沙尘的文章.md', article_md())
        items = ss.collect_articles(article_days=5)

        self.assertEqual(len(items), 1, items)
        text = items[0]['text']
        self.assertTrue(text.startswith('某号写的一篇文章'), text[:60])

        # 正文那句话在第 1000 字之后：退回 content[:1000] 的老写法必然拿不到
        self.assertIn(PROSE, text)
        self.assertIn(SUMMARY, text, 'AI 摘要那句话也应当收进来（它就是一句话概括）')

        self.assertNotIn('title:', text, 'YAML 头不许进 embedding')
        self.assertNotIn('backfilled:', text)
        self.assertNotIn('mp.weixin.qq.com', text, 'url 不许进 embedding')
        self.assertGreater(cjk_ratio(text), 0.5,
                           '取到正文的话汉字占比应当明显过半，实测老写法是 0.16：%r' % text[:80])

    def test_explicit_marker_branch_still_wins(self):
        """有 `## 正文` 的（老形态）仍然优先用它，别被新提取器顶掉。

        判据是**只有老分支会收的一行**：提取器会把引用行丢掉，老分支（标记后 50 行）不会。
        """
        content = article_md(marker=['## 正文', '',
                                     '> 只有老分支会收的一行（引用行会被提取器丢掉）', ''])
        self.write_day('2026-10-13', 'AI', '有标记的一篇.md', content)
        items = ss.collect_articles(article_days=5)

        self.assertEqual(len(items), 1, items)
        self.assertIn('只有老分支会收的一行', items[0]['text'])
        self.assertIn(PROSE, items[0]['text'])

    def test_file_without_frontmatter_does_not_crash(self):
        self.write_day('2026-10-13', 'AI', '没有头的一篇.md',
                       'title: "没有 frontmatter 的一篇"\n\n这是正文第一句，讲的是空气质量模型的评估。\n')
        items = ss.collect_articles(article_days=5)

        self.assertEqual(len(items), 1, items)
        self.assertIn('空气质量模型的评估', items[0]['text'])


if __name__ == '__main__':
    unittest.main()
