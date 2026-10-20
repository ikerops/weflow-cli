"""日报邮件的两个"读起来累"的修复 + 正文里的 GitHub 链接。

这三条都是用户当场提的：
  1. `【核心观点】…【关键细节】1.…；2.…` 这种给机器分段的结构，在手机上就是噪音；
  2. 这类 AI 项目文章正文里常有 GitHub 地址，读者最想直接点，不该埋在正文里；
  3. 没有标签的老格式不能被清洗逻辑吃掉。
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))

import daily_mail as dm


class CleanSummaryTests(unittest.TestCase):
    def test_只保留核心观点并去掉标签(self):
        raw = '【核心观点】Caldera 把仓库整理成主页。【关键细节】1. 前 Myspace CEO；2. 城市可视化。'
        self.assertEqual(dm.clean_summary(raw), 'Caldera 把仓库整理成主页。')

    def test_没有标签时整段保留(self):
        self.assertEqual(dm.clean_summary('普通摘要，没有标签。'), '普通摘要，没有标签。')

    def test_空输入不炸(self):
        self.assertEqual(dm.clean_summary(''), '')
        self.assertEqual(dm.clean_summary(None), '')

    def test_超长才截断并加省略号(self):
        long_text = '【核心观点】' + ('啊' * 300)
        out = dm.clean_summary(long_text, limit=50)
        self.assertEqual(len(out), 51)      # 50 + 省略号
        self.assertTrue(out.endswith('…'))

    def test_截断不留下悬空的标点(self):
        text = '【核心观点】' + ('词句，' * 40)
        out = dm.clean_summary(text, limit=21)
        self.assertFalse(out.rstrip('…')[-1] in '，；、')


class GithubLinkTests(unittest.TestCase):
    def test_提取并按仓库去重(self):
        body = '见 https://github.com/foo/bar 与 https://github.com/foo/bar/ ，还有 https://github.com/baz/qux?tab=readme'
        self.assertEqual(dm.github_links(body),
                         ['https://github.com/foo/bar', 'https://github.com/baz/qux'])

    def test_去掉句末标点(self):
        self.assertEqual(dm.github_links('地址 https://github.com/a/b.'), ['https://github.com/a/b'])

    def test_没有链接时返回空(self):
        self.assertEqual(dm.github_links('这里没有仓库地址'), [])

    def test_最多三个(self):
        body = ' '.join(f'https://github.com/o/r{i}' for i in range(6))
        self.assertEqual(len(dm.github_links(body)), 3)


class RenderSectionsTests(unittest.TestCase):
    """**每个主题都必须真的出现在邮件里。**

    2026-10-20 出过一次：`render` 里「主题分区」那一段缩进错位（掉到 `for topic` 循环外），
    结果只有最后一个主题被渲染——邮件里只有学术、AI 整段消失，而且**不报错、篇数也对**
    （因为 total 是另一个变量）。用户报的"新邮件只有学术没有 AI"就是这个。
    """

    def _article(self, title, topic_source='某号'):
        return {'title': title, 'source': topic_source, 'time': '10:00', 'relevance': '中',
                'url': 'https://mp.weixin.qq.com/s/x', 'summary': '【核心观点】摘要。', 'tags': []}

    def test_two_topics_both_render(self):
        groups = {
            'AI': [self._article('AI 的文章甲'), self._article('AI 的文章乙')],
            '学术': [self._article('学术的文章丙')],
        }
        body, page, total = dm.render('2026-10-18', groups, {}, {}, {})
        self.assertEqual(total, 3)
        self.assertIn('>AI<', page, 'AI 主题头必须在')
        self.assertIn('>学术<', page, '学术主题头必须在')
        for title in ('AI 的文章甲', 'AI 的文章乙', '学术的文章丙'):
            self.assertIn(title, page, f'{title} 丢了')

    def test_every_article_becomes_one_card(self):
        groups = {'AI': [self._article(f'甲{i}') for i in range(5)],
                  '学术': [self._article(f'乙{i}') for i in range(4)]}
        body, page, total = dm.render('2026-10-18', groups, {}, {}, {})
        self.assertEqual(page.count('border:1px solid #eef0f3'), 9,
                         '卡片数必须等于篇数——少一张就说明有一篇没渲染')

    def test_empty_topic_is_skipped_not_rendered(self):
        groups = {'AI': [self._article('甲')], '学术': []}
        body, page, total = dm.render('2026-10-18', groups, {}, {}, {})
        self.assertIn('>AI<', page)
        self.assertNotIn('>学术<', page)
        self.assertEqual(total, 1)


if __name__ == '__main__':
    unittest.main()
