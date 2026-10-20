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


if __name__ == '__main__':
    unittest.main()
