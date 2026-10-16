"""`--with-text`：把命中还原成**本地那份正文**，而不是只给"用来匹配的摘录"。

为什么要有它：索引里存的 `text` 是「标题 + 前 500 字摘录」，拿它当"内容"交给人是没有意义的
（2026-10-16 用户的原话）。正文其实就在本地 —— `id` 带着日报 md 的相对路径，而正文缓存
（`output/.cache/fetch/<md5(url)>.md`）里是**完整正文**，两条取更长的那份。
四条断言各钉一个"安静地错"的方式：读到的是内容不是标记、缓存优先、读不到要说出来、聊天不假装有文件。
"""

import hashlib
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

URL = 'http://mp.weixin.qq.com/s?__biz=TEST&mid=1&idx=1&sn=abc'
MD = '''---
title: "测试文章"
source: "某公众号"
date: 2026-10-02
url: "%s"
---

# 测试文章

> 来源：某公众号

---

**点击蓝字**

在小说阅读器读本章

去阅读

**亮点**

融合物理约束的模型把平流-扩散方程写进损失函数，兼顾物理一致性。这里是一段足够长的正文句子。

![](https://mmbiz.qpic.cn/x/640?wx_fmt=png)

引用格式：某某等, 2026[J]. 测试学报, 35(9):1423-1435.

[某篇相关文章](http://example.com/a) 讲的是别的东西。
''' % URL


class FullTextTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='kb-full-'))
        self._saved = ss.OUTPUT_ROOT
        ss.OUTPUT_ROOT = str(self.root)
        self.rel = '2026-10-02/新闻/某号-测试文章.md'
        p = self.root / 'biz-daily' / self.rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(MD, encoding='utf-8')
        self.item = {'id': 'article:' + self.rel.replace('/', '\\'), 'type': 'article',
                     'title': '测试文章', 'text': '测试文章\n摘录只有这么多'}

    def tearDown(self):
        ss.OUTPUT_ROOT = self._saved
        shutil.rmtree(self.root, ignore_errors=True)

    def test_returns_readable_content_not_markup(self):
        text, path, url = ss.load_record_text(self.item)
        self.assertIn('平流-扩散方程', text)
        self.assertNotIn('**', text, '加粗标记不该留给读者')
        self.assertNotIn('mmbiz.qpic.cn', text, '图片 URL 不该糊进来')
        self.assertNotIn('点击蓝字', text)
        self.assertNotIn('在小说阅读器读本章', text)
        self.assertIn('某篇相关文章', text, '链接要留文字、去 URL')
        self.assertNotIn('example.com', text)
        self.assertEqual(url, URL)
        self.assertTrue(path.endswith('某号-测试文章.md'))

    def test_prefers_the_fetch_cache_when_it_is_longer(self):
        cache = self.root / '.cache' / 'fetch'
        cache.mkdir(parents=True, exist_ok=True)
        (cache / (hashlib.md5(URL.encode()).hexdigest() + '.md')).write_text(
            '这是正文缓存里的**完整正文**，比日报 md 里的那份长得多。' * 20, encoding='utf-8')
        text, _p, _u = ss.load_record_text(self.item)
        self.assertIn('完整正文', text)
        self.assertNotIn('平流-扩散方程', text, '缓存更长时应当用缓存那份')

    def test_missing_file_is_reported_not_silently_empty(self):
        item = {'id': 'article:2020-01-01/新闻/不存在.md', 'type': 'article', 'text': 'x'}
        text, path, url = ss.load_record_text(item)
        self.assertIsNone(text)
        self.assertIn('不存在.md', path)
        self.assertIsNone(url)

    def test_attach_counts_what_it_could_not_read(self):
        results = [{**self.item}, {'id': 'article:2020-01-01/新闻/不存在.md', 'type': 'article'}]
        out, missing, fetched = ss.attach_full_text(results)
        self.assertEqual(fetched, 0, '没开 --fetch-missing 时不该有任何抓取')
        self.assertEqual(missing, 1)
        self.assertIn('平流-扩散方程', out[0]['fullText'])
        self.assertIsNone(out[1]['fullText'])

    def test_respects_the_length_limit(self):
        text, _p, _u = ss.load_record_text(self.item, 30)
        self.assertLessEqual(len(text), 30)

    def test_chat_record_is_the_message_itself(self):
        item = {'id': 'chat:某人:123:4', 'type': 'chat', 'text': '我直接把那个pdf导出成图的'}
        text, path, url = ss.load_record_text(item)
        self.assertEqual(text, '我直接把那个pdf导出成图的')
        self.assertIsNone(path)
        self.assertIsNone(url)


class FetchMissingTests(unittest.TestCase):
    """`--fetch-missing`：本地正文太薄时按 url 抓一次（复用日报的缓存），**默认关**、有上限。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='kb-fetch-'))
        self._saved = (ss.OUTPUT_ROOT, ss._fetch_body)
        ss.OUTPUT_ROOT = str(self.root)
        # 一份"很薄的日报 md"（正文只有几十字）
        self.rel = '2026-10-13/学术/某号-好文回顾.md'
        p = self.root / 'biz-daily' / self.rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('---\ntitle: "好文回顾"\nurl: "%s"\n---\n\n# 好文回顾\n\n摘要很短，但也够六个字了。\n' % URL,
                     encoding='utf-8')
        self.item = {'id': 'article:' + self.rel.replace('/', '\\'), 'type': 'article',
                     'title': '好文回顾', 'text': '好文回顾\n摘要很短，但也够六个字了。'}
        self.calls = []

    def tearDown(self):
        (ss.OUTPUT_ROOT, ss._fetch_body) = self._saved
        shutil.rmtree(self.root, ignore_errors=True)

    def fetch(self, body):
        def _f(url):
            self.calls.append(url)
            return body
        return _f

    def test_off_by_default_no_request(self):
        ss._fetch_body = self.fetch('抓来的完整正文' * 60)
        out, _m, fetched = ss.attach_full_text([{**self.item}])
        self.assertEqual(self.calls, [], '默认不许对上游发请求')
        self.assertEqual(fetched, 0)
        self.assertNotIn('fetchedFromUrl', out[0])

    def test_fetches_only_short_bodies(self):
        ss._fetch_body = self.fetch('抓来的完整正文' * 60)
        long_item = dict(self.item, id='article:2026-10-02/新闻/长文.md')
        p = self.root / 'biz-daily' / '2026-10-02' / '新闻' / '长文.md'
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('---\nurl: "%s"\n---\n\n# 长文\n\n%s\n' % (URL, '本来就很长的正文。' * 60),
                     encoding='utf-8')
        out, _m, fetched = ss.attach_full_text([long_item, {**self.item}], fetch_missing=3)
        self.assertEqual(len(self.calls), 1, '只有薄的那条该被抓')
        self.assertEqual(fetched, 1)
        self.assertTrue(out[1].get('fetchedFromUrl'))
        self.assertNotIn('fetchedFromUrl', out[0])

    def test_respects_the_cap(self):
        ss._fetch_body = self.fetch('抓来的完整正文' * 60)
        items = []
        for n in range(5):
            rel = '2026-10-1%d/学术/薄%d.md' % (n, n)
            f = self.root / 'biz-daily' / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text('---\nurl: "%s"\n---\n\n# 薄\n\n短。\n' % URL, encoding='utf-8')
            items.append({'id': 'article:' + rel.replace('/', '\\'), 'type': 'article', 'text': '正文很短，先记一笔。'})
        _out, _m, fetched = ss.attach_full_text(items, fetch_missing=2)
        self.assertEqual(len(self.calls), 2, '上限就是上限')
        self.assertEqual(fetched, 2)

    def test_failure_keeps_the_local_text(self):
        ss._fetch_body = self.fetch(None)
        out, _m, fetched = ss.attach_full_text([{**self.item}], fetch_missing=3)
        self.assertEqual(fetched, 0)
        self.assertFalse(out[0].get('fetchedFromUrl'))
        self.assertIn('够六个字', out[0]['fullText'], '抓不到就保留本地那份，别清空')


if __name__ == '__main__':
    unittest.main()