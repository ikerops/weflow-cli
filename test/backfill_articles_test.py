"""`backfill_articles.py`：历史文章回填。**纯函数与落盘格式**，不查库、不联网。

这一支里唯一真正重要的是**对下游的契约**：回填写出来的 md 必须能被
`create_reading_notes` 读出 frontmatter、被 `compile_wiki` 摘出摘要。两条路读的键
不一样（前者读 title/source/url/topic/date，后者找 `## AI 摘要` 那一段），而它们读错
时**都不报错**——笔记会生成、概念页也会有，只是的字段是空的。所以这里把两边的读法
都钉住，而不是只断言"文件存在"。
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bf = load('backfill_articles')
notes = load('create_reading_notes')
cw = load('compile_wiki')


def article(**over):
    base = {
        'account': 'gh_test',
        'account_name': '测试号',
        'topic': 'AI',
        'title': '某篇讲 MCP 的文章',
        'digest': '这篇文章讲了 MCP 协议怎么把工具暴露给模型。',
        'url': 'http://mp.weixin.qq.com/s?__biz=AAA&mid=1&idx=1&sn=bbb',
        'cover': '',
        'local_text': '',
        'time': '08:15',
        'timestamp': 1,
        # 必须**长过 MIN_BODY_CHARS**：夹具太短会被闸门正确地跳过，于是断言"写入了"
        # 会失败，而失败原因看着像功能坏了。这条踩过一次——第一次改的时候我按"看着够长"
        # 估计，实际只有 79 字，还是被拦。
        'fetched_md': ('正文第一段讲的是这套工具怎么把时间线开放给 Agent。' * 6)
                      + '\n\n![图](http://x/y.png)\n',
    }
    base.update(over)
    return base


class FetchCacheTests(unittest.TestCase):
    """补历史的正文要落进**日报那条同一个缓存**。

    为什么值得钉：两条路本来就共用 `fetch_article`，但缓存只有日报在用。补完历史之后逐天
    `daily --date X` 之所以能快、且产出与平常一样完整（含摘要与配图），全靠正文已经在
    `output/.cache/fetch/` 里 —— 而"写缓存"这件事**漏了不会报错**，只会在几天后表现为
    "日报又跑了一整天"。所以这里在真链路上断言：成功写、失败不写、已有则不打网络。
    """

    def test_抓到的正文写进日报缓存_抓失败的不写(self):
        import biz_daily
        url = 'http://mp.weixin.qq.com/s?__biz=AAA&mid=9&idx=1&sn=ccc'
        body = '正文。' * 60
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(biz_daily, 'FETCH_CACHE_DIR', tmp), \
                 mock.patch.object(biz_daily, 'fetch_article', lambda _u: body):
                arts = [article(url=url)]
                bf.fetch_bodies(arts, workers=1)
            cached = Path(tmp) / (hashlib.md5(url.encode()).hexdigest() + '.md')
            self.assertTrue(cached.is_file(), '正文没进日报缓存 ⇒ 之后 daily 会重新联网抓一遍')
            self.assertEqual(cached.read_text(encoding='utf-8'), body)
            self.assertEqual(arts[0]['fetched_md'], body)

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(biz_daily, 'FETCH_CACHE_DIR', tmp), \
                 mock.patch.object(biz_daily, 'fetch_article', lambda _u: None):
                bf.fetch_bodies([article(url=url)], workers=1)
            self.assertEqual(list(Path(tmp).glob('*.md')), [], '抓失败不该落缓存：下次要重试')

    def test_已有缓存时不再打扰上游(self):
        import biz_daily
        url = 'http://mp.weixin.qq.com/s?__biz=AAA&mid=7&idx=1&sn=ddd'
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / (hashlib.md5(url.encode()).hexdigest() + '.md')).write_text(
                '早已抓过。' * 40, encoding='utf-8')
            calls = []

            def spy(u):
                calls.append(u)
                return '网络抓的。' * 40

            with mock.patch.object(biz_daily, 'FETCH_CACHE_DIR', tmp), \
                 mock.patch.object(biz_daily, 'fetch_article', spy):
                arts = [article(url=url)]
                bf.fetch_bodies(arts, workers=1)
            self.assertEqual(calls, [], '命中缓存还要联网，等于把日报那份节流白算')
            self.assertIn('早已抓过', arts[0]['fetched_md'])

    def test_强制重抓会联网并把缓存刷成新的(self):
        """`--no-fetch-cache` 只跳过"读"，不是"不写"：抓成功要把旧缓存更新掉。"""
        import biz_daily
        url = 'http://mp.weixin.qq.com/s?__biz=AAA&mid=5&idx=1&sn=eee'
        path = Path
        with tempfile.TemporaryDirectory() as tmp:
            cached = path(tmp) / (hashlib.md5(url.encode()).hexdigest() + '.md')
            cached.write_text('旧正文。' * 40, encoding='utf-8')
            calls = []
            with mock.patch.object(biz_daily, 'FETCH_CACHE_DIR', tmp), \
                 mock.patch.object(biz_daily, 'fetch_article',
                                   lambda u: (calls.append(u), '新正文。' * 40)[1]):
                arts = [article(url=url)]
                bf.fetch_bodies(arts, workers=1, use_cache=False)
            self.assertEqual(len(calls), 1, '强制重抓却没联网')
            self.assertEqual(cached.read_text(encoding='utf-8'), '新正文。' * 40,
                             '重抓成功要把缓存刷新，否则下次还在读旧的')
            self.assertEqual(arts[0]['fetched_md'], '新正文。' * 40)

    def test_强制重抓失败时不动已有的有效缓存(self):
        import biz_daily
        url = 'http://mp.weixin.qq.com/s?__biz=AAA&mid=4&idx=1&sn=fff'
        path = Path
        with tempfile.TemporaryDirectory() as tmp:
            cached = path(tmp) / (hashlib.md5(url.encode()).hexdigest() + '.md')
            cached.write_text('旧正文。' * 40, encoding='utf-8')
            with mock.patch.object(biz_daily, 'FETCH_CACHE_DIR', tmp), \
                 mock.patch.object(biz_daily, 'fetch_article', lambda _u: None):
                arts = [article(url=url, fetched_md='')]
                result = bf.fetch_bodies(arts, workers=1, use_cache=False)
            self.assertEqual(result['ok'], 0)
            self.assertEqual(cached.read_text(encoding='utf-8'), '旧正文。' * 40,
                             '抓失败把旧缓存清掉/写空 = 下次连旧的都没了')
            self.assertFalse(arts[0].get('fetched_md'))

    def test_缓存不可写时不影响这一篇(self):
        """缓存是加速器，不是依赖：写不进去也要照常拿到正文。

        否则一次目录权限/磁盘问题会把"抓到了"变成"失败"，进而让整天的产出少几篇——
        而那个后果看起来是"这几篇文章本来就没有正文"。
        """
        import biz_daily
        url = 'http://mp.weixin.qq.com/s?__biz=AAA&mid=3&idx=1&sn=ggg'
        body = '正文。' * 60
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / 'not-a-dir'
            blocker.write_text('我是个文件，不是目录', encoding='utf-8')
            with mock.patch.object(biz_daily, 'FETCH_CACHE_DIR', str(blocker / 'fetch')), \
                 mock.patch.object(biz_daily, 'fetch_article', lambda _u: body):
                arts = [article(url=url, fetched_md='')]
                result = bf.fetch_bodies(arts, workers=1)
            self.assertEqual(result['ok'], 1, '缓存写不进去不该让这一次抓取算失败')
            self.assertEqual(result['failed'], 0)
            self.assertEqual(arts[0]['fetched_md'], body)

    def test_并发抓取时结果不会串行错位(self):
        """并发是这里唯一的实现选择，而"下标错位"是它最容易出的错（本仓踩过同类）。

        5 篇、偶数篇抓得到、奇数篇抓不到：每一篇的正文必须回到**它自己**那条记录上。
        """
        import biz_daily
        urls = ['http://mp.weixin.qq.com/s?__biz=B&mid=%d&idx=1&sn=h%d' % (i, i) for i in range(5)]

        def fake(u):
            n = int(u.split('mid=')[1].split('&')[0])
            return ('第%d篇正文。' % n) * 40 if n % 2 == 0 else None

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(biz_daily, 'FETCH_CACHE_DIR', tmp), \
                 mock.patch.object(biz_daily, 'fetch_article', fake):
                arts = [article(url=u, fetched_md='') for u in urls]
                result = bf.fetch_bodies(arts, workers=3)
            self.assertEqual(result, {'attempted': 5, 'ok': 3, 'failed': 2})
            for n, item in enumerate(arts):
                if n % 2 == 0:
                    self.assertTrue(item['fetched_md'].startswith('第%d篇' % n),
                                    '第%d篇拿到了别人的正文' % n)
                else:
                    self.assertFalse(item.get('fetched_md'), '第%d篇不该有正文' % n)


class FetchFailedDayTests(unittest.TestCase):
    """抓取失败的那天**不算做完**，重跑要能补齐 —— 这条链断了的话，一次网络抖动会被
    "这天已回填"永久埋掉：文件在、索引在、md 也有，少的那几篇谁都看不出来。

    仓库里量过同类事故：84 次失败里 28 次重试就能拿到。而"正文本来就短"是另一件事，
    它不该让这天永远重做——两种情况在产物里必须能分开。
    """

    def _day(self, tmp, failed):
        arts = [article(title='第%d篇某篇文章' % i, fetched_md='正文。' * 60)
                for i in range(3)]
        return bf.write_day(arts, '2026-03-05', tmp, fetch_failed=failed)

    def test_失败数写进产物且这天判为没做完(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._day(tmp, 2)
            payload = json.loads((Path(tmp) / '2026-03-05' / '.articles.json').read_text(encoding='utf-8'))
            self.assertEqual(payload['fetchFailed'], 2)
            self.assertEqual(result['fetchFailed'], 2)
            self.assertFalse(bf.day_done(tmp, '2026-03-05'),
                             '"有篇没抓到"被当成做完了 ⇒ 以后不会再重试，那几篇永久缺失')
            readme = (Path(tmp) / '2026-03-05' / 'README.md').read_text(encoding='utf-8')
            self.assertIn('2 篇正文没抓到', readme, 'README 得说出来，否则"少了 2 篇"读起来像"这天就这些"')

    def test_补齐之后标记消失_这天恢复为完整(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._day(tmp, 2)
            # 重跑：这次全抓到了（全量跑 ⇒ 上一轮失败的篇也在这次的重试范围内）
            self._day(tmp, 0)
            payload = json.loads((Path(tmp) / '2026-03-05' / '.articles.json').read_text(encoding='utf-8'))
            self.assertNotIn('fetchFailed', payload,
                             '标记不是 sticky 的：全量跑一轮没失败就说明补齐了，否则这天永远重做')
            self.assertTrue(bf.day_done(tmp, '2026-03-05'))

    def test_局部回填不会把上一轮的全量失败抹掉(self):
        """`--topic` 只覆盖一部分文章，它没重试到的那些（别的主题）还在。"""
        with tempfile.TemporaryDirectory() as tmp:
            self._day(tmp, 3)
            arts = [article(topic='AI', title='只有 AI 的一篇', fetched_md='正文。' * 60)]
            bf.write_day(arts, '2026-03-05', tmp, topic_filter=['AI'], fetch_failed=0)
            payload = json.loads((Path(tmp) / '2026-03-05' / '.articles.json').read_text(encoding='utf-8'))
            self.assertEqual(payload.get('fetchFailed'), 3,
                             '局部跑把上一轮的全量失败抹掉了 ⇒ 那几篇再也不会被重试')
            self.assertFalse(bf.day_done(tmp, '2026-03-05'))

    def test_没失败的一天产物里就没有这个键(self):
        """全量回填写出来的文件要与以前逐字节相同（`day_done` 把缺键读作"完整"）。"""
        with tempfile.TemporaryDirectory() as tmp:
            self._day(tmp, 0)
            payload = json.loads((Path(tmp) / '2026-03-05' / '.articles.json').read_text(encoding='utf-8'))
            self.assertNotIn('fetchFailed', payload)
            self.assertTrue(bf.day_done(tmp, '2026-03-05'))


class BodyTests(unittest.TestCase):
    def test_去图去链再数字数(self):
        self.assertEqual(bf.body_without_media('a\n\n![图](http://x/y.png)\nb'), 'ab')
        self.assertEqual(bf.body_without_media('[点这](http://x/y) 正文'), '正文')

    def test_空输入不炸(self):
        for value in ('', None, '   \n\n  '):
            self.assertEqual(bf.body_without_media(value), '')

    def test_阈值与_biz_daily_是同一个数(self):
        """两条路对"哪些文章算有正文"必须给同一个答案。

        `biz_daily` 那边是**内联写死**的 `if len(body_text) < 100`（没有常量可 import），
        所以这里只能钉住这个数本身，并在改动时提醒自己去改另一边。
        """
        self.assertEqual(bf.MIN_BODY_CHARS, 100,
                         'biz_daily.py 里那个内联的 100 要跟着一起改')


class WriteTests(unittest.TestCase):
    def test_太短的正文被跳过并计入(self):
        with tempfile.TemporaryDirectory() as tmp:
            short = article(title='图片型文章', fetched_md='![图](http://x/y.png)')
            result = bf.write_day([short, article()], '2026-03-05', tmp)
            self.assertEqual(result['written'], 1)
            self.assertEqual(result['skipped'], 1)

    def test_没有正文的也算跳过_不算写入(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = bf.write_day([article(fetched_md='', local_text='')], '2026-03-05', tmp)
            self.assertEqual((result['written'], result['skipped']), (0, 1))

    def test_下游读得回_frontmatter(self):
        """**这条是这一支的核心**：写出来的 md 必须被 `create_reading_notes` 认。"""
        with tempfile.TemporaryDirectory() as tmp:
            bf.write_day([article()], '2026-03-05', tmp)
            files = list(Path(tmp).rglob('*.md'))
            files = [p for p in files if p.name != 'README.md']
            self.assertEqual(len(files), 1)
            fm, body = notes.parse_frontmatter(files[0].read_text(encoding='utf-8'))
            self.assertEqual(fm.get('title'), '某篇讲 MCP 的文章')
            self.assertEqual(fm.get('source'), '测试号')
            self.assertEqual(fm.get('topic'), 'AI')
            self.assertEqual(fm.get('date'), '2026-03-05')
            self.assertIn('mp.weixin.qq.com', str(fm.get('url')), '原文链接要落盘，笔记里要能点回去')

    def test_下游摘得出摘要(self):
        """`compile_wiki` 从 `## AI 摘要` 那一段摘——回填的摘要用的是文章自带 digest。"""
        with tempfile.TemporaryDirectory() as tmp:
            bf.write_day([article()], '2026-03-05', tmp)
            path = [p for p in Path(tmp).rglob('*.md') if p.name != 'README.md'][0]
            fm, body = notes.parse_frontmatter(path.read_text(encoding='utf-8'))
            summary = cw._extract_summary(body)
            self.assertIn('MCP 协议', summary, '摘要取的是 digest，不是正文开头')
            # 正文本身还在（知识库要的就是它）
            self.assertIn('正文第一段', body)

    def test_回填标记写进了_frontmatter_和_json(self):
        # 下游要能分辨"这一篇是历史回填的"——那张 md 没经过当天的日报流程
        with tempfile.TemporaryDirectory() as tmp:
            bf.write_day([article()], '2026-03-05', tmp)
            path = [p for p in Path(tmp).rglob('*.md') if p.name != 'README.md'][0]
            fm, _ = notes.parse_frontmatter(path.read_text(encoding='utf-8'))
            self.assertEqual(str(fm.get('backfilled')).lower(), 'true')
            payload = json.loads((Path(tmp) / '2026-03-05' / '.articles.json').read_text(encoding='utf-8'))
            self.assertTrue(payload['backfilled'])
            self.assertEqual(payload['articles'][0]['title'], '某篇讲 MCP 的文章')
            self.assertEqual(payload['articles'][0]['source'], '测试号')

    def test_主题不在分类法里时兜底并计数(self):
        with tempfile.TemporaryDirectory() as tmp:
            odd = article(topic='不存在的主题')
            result = bf.write_day([odd], '2026-03-05', tmp)
            self.assertEqual(result['written'], 1)
            self.assertEqual(result['fallbackTopics'], 1)


class TopicTests(unittest.TestCase):
    """主题必须**在收集时**就定下来，不能留给下游兜底。

    这条是真踩出来的：第一版 `collect_articles` 根本没设 `topic`，于是 `_group_by_topic`
    把整天 106 篇全部折成 `DEFAULT_TOPIC`，100% 落进 `学术/`。最坏的地方是它**不报错**——
    文件照写、笔记照生成，只有一个不起眼的"主题兜底 113 篇"混在输出里。
    """

    def test_没给主题的按关键词猜_而不是全塞兜底(self):
        item = article()
        del item['topic']
        item['title'] = '某大模型的 Agent 实践笔记'
        bf.apply_topics([item])
        self.assertEqual(item['topic'], 'AI')
        self.assertNotEqual(item['topic'], bf.DEFAULT_TOPIC)

    def test_已经定过的主题不被改写(self):
        item = article(topic='文学')
        bf.apply_topics([item])
        self.assertEqual(item['topic'], '文学')

    def test_越界的主题会被重猜(self):
        item = article(topic='不存在')
        item['title'] = '一篇讲融资与财报的文章'
        bf.apply_topics([item])
        self.assertEqual(item['topic'], '投资')

    def test_猜出来的主题落进目录名(self):
        item = article()
        del item['topic']
        item['title'] = '某大模型的 Agent 实践笔记'
        bf.apply_topics([item])
        with tempfile.TemporaryDirectory() as tmp:
            bf.write_day([item], '2026-03-05', tmp)
            dirs = sorted(p.name for p in Path(tmp, '2026-03-05').iterdir() if p.is_dir())
        self.assertEqual(dirs, ['AI'])


class VaultCopyTests(unittest.TestCase):
    """把文章 md 补进 Vault 的 `Sources/WeChat`。

    这一支盯的是**体积与不破坏**两件事，都不是正确性问题、也不会报错：
    拷了图片就是 33 GB；整目录替换就会清掉用户 Vault 里那天别的东西。
    """

    def build(self, tmp, day='2026-05-20'):
        root = Path(tmp) / 'biz-daily' / day
        (root / 'AI').mkdir(parents=True)
        (root / 'AI' / '甲-某篇.md').write_text('正文', encoding='utf-8')
        (root / 'AI' / '配图.jpg').write_bytes(b'\x89PNG' + b'x' * 500)
        (root / 'README.md').write_text('README', encoding='utf-8')
        return str(Path(tmp) / 'biz-daily')

    def test_只拷_md_不拷图片(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self.build(tmp)
            target = Path(tmp) / 'vault'
            result = bf.copy_to_vault('2026-05-20', out, str(target))
            files = sorted(p.name for p in (target / '2026-05-20').rglob('*') if p.is_file())
            self.assertEqual(files, ['README.md', '甲-某篇.md'])
            self.assertEqual(result['copied'], 2)
            self.assertEqual(len(list((target / '2026-05-20').rglob('*.jpg'))), 0, '图片一张都不该进来')

    def test_保留主题子目录(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self.build(tmp)
            target = Path(tmp) / 'vault'
            bf.copy_to_vault('2026-05-20', out, str(target))
            self.assertTrue((target / '2026-05-20' / 'AI' / '甲-某篇.md').exists())

    def test_是补缺不是整目录替换(self):
        """`copytree` 的语义是"删掉再放"。这里要的是补缺——Vault 是用户的东西。

        先放一个**不在源里**的文件，拷完它必须还在。
        """
        with tempfile.TemporaryDirectory() as tmp:
            out = self.build(tmp)
            target = Path(tmp) / 'vault' / '2026-05-20'
            target.mkdir(parents=True)
            (target / '我自己写的.md').write_text('别动我', encoding='utf-8')
            bf.copy_to_vault('2026-05-20', out, str(Path(tmp) / 'vault'))
            self.assertEqual((target / '我自己写的.md').read_text(encoding='utf-8'), '别动我',
                             '补拷不该清掉 Vault 里原有的东西')

    def build_dirty(self, tmp):
        """一篇带界面残留的原文：标题里有短语、正文里有短语、还有一段缩进的代码。"""
        root = Path(tmp) / 'biz-daily' / '2026-05-20' / 'AI'
        root.mkdir(parents=True)
        (root / '甲-某篇.md').write_text(
            '---\n'
            'title: "如何去阅读一本书"\n'
            'date: 2026-05-20\n'
            '---\n'
            '\n'
            '# 正文标题\n'
            '继续滑动看下一个 这句是界面残留。\n'
            '```html\n'
            '    <div class="a">\n'
            '        <span>x</span>\n'
            '    </div>\n'
            '```\n'
            '轻触阅读原文\n', encoding='utf-8')
        return str(Path(tmp) / 'biz-daily')

    def test_拷过去的是洗过的正文(self):
        """用户 2026-09-28 定的口径是"原文与笔记都清"。

        为什么清洗必须落在**拷贝函数**里而不是 fetch 那一步：`biz-daily` 是原始存档，
        洗了就再拿不回原文；而 `Sources/` 是给人读的那一层。落在拷贝里，任何一次补拷
        的结果才一致 —— 落别处的话，一次补拷就会把洗过的又覆盖回去（2026-09-28 实测发生过）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            out = self.build_dirty(tmp)
            target = Path(tmp) / 'vault'
            bf.copy_to_vault('2026-05-20', out, str(target))
            wrote = (target / '2026-05-20' / 'AI' / '甲-某篇.md').read_text(encoding='utf-8')
            source = (Path(out) / '2026-05-20' / 'AI' / '甲-某篇.md').read_text(encoding='utf-8')
        self.assertNotIn('继续滑动看下一个', wrote, '正文里的界面残留要洗掉')
        self.assertNotIn('轻触阅读原文', wrote)
        self.assertIn('继续滑动看下一个', source, '**原始存档一个字都不能动**')

    def test_清洗不碰标题也不碰代码块(self):
        """两处防护，改了都不会报错，所以必须钉住。

        - `去阅读` 是**短词**，标题里出现（"如何去阅读一本书"）就会被 `strip_wx_ads` 误删，
          而标题是笔记与原文之间唯一的对齐键；
        - `strip_wx_ads` 末尾的空白规整会吃掉代码块缩进（实测全库 20 篇带围栏、10 篇受损），
          而原文是素材，缩进就是内容。
        """
        with tempfile.TemporaryDirectory() as tmp:
            out = self.build_dirty(tmp)
            target = Path(tmp) / 'vault'
            bf.copy_to_vault('2026-05-20', out, str(target))
            wrote = (target / '2026-05-20' / 'AI' / '甲-某篇.md').read_text(encoding='utf-8')
        self.assertIn('如何去阅读一本书', wrote, '标题里的"去阅读"不能被当成界面残留')
        self.assertIn('    <div class="a">', wrote, '代码块缩进是内容')
        self.assertIn('        <span>x</span>', wrote)
        self.assertIn('---\n\n# 正文标题', wrote, 'frontmatter 与正文之间的空行要留着')

    def test_这一天没产出时说清原因不是静默零(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = bf.copy_to_vault('2026-01-01', str(Path(tmp) / 'nope'), str(Path(tmp) / 'v'))
            self.assertEqual(result['copied'], 0)
            self.assertTrue(result['reason'], '零要带原因，不能是个光秃秃的 0')


class IncrementalTests(unittest.TestCase):
    def test_已回填的天被跳过(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(bf.day_done(tmp, '2026-03-05'), '还没回填过 → 要做')
            bf.write_day([article()], '2026-03-05', tmp)
            self.assertTrue(bf.day_done(tmp, '2026-03-05'), '写过就跳过，免得重跑再抓一遍')

    def test_写了但一篇都没有的天不算完成(self):
        """当天全是图片型文章时 `.articles.json` 存在但列表为空——那不该被当成"做过了"。"""
        with tempfile.TemporaryDirectory() as tmp:
            bf.write_day([article(fetched_md='短')], '2026-03-05', tmp)
            self.assertFalse(bf.day_done(tmp, '2026-03-05'))


class TopicFilterTests(unittest.TestCase):
    """`--topic`：只回填一部分主题。

    这一支盯的是**"半天的产出被当成整天"**。它危险的地方在于产物**真的变少了**，而所有
    "文件在不在"式的检查都还是绿的 —— 唯一能看出区别的是 `.articles.json` 里的标记和
    `day_done` 的判据。同类的坑这一支记过一次（见 `days_with_articles` 那段注释：
    拿一个不完整的判据当"做完了"，199 天里有 12 天因此既不被补也不被报出来）。
    """

    def read_json(self, tmp, day='2026-03-05'):
        return json.loads((Path(tmp) / day / '.articles.json').read_text(encoding='utf-8'))

    def read_readme(self, tmp, day='2026-03-05'):
        return (Path(tmp) / day / 'README.md').read_text(encoding='utf-8')

    def test_只回填指定主题_其余不落盘(self):
        with tempfile.TemporaryDirectory() as tmp:
            items = [article(title='讲 MCP 的那篇'),
                     article(title='一篇散文', topic='文学', fetched_md='正文第二段。' * 30)]
            result = bf.write_day(items, '2026-03-05', tmp, topic_filter=['AI'])
            dirs = sorted(p.name for p in Path(tmp, '2026-03-05').iterdir() if p.is_dir())
        self.assertEqual(dirs, ['AI'], '没选的主题不该建目录出来')
        self.assertEqual(result['written'], 1)

    def test_筛选本身_不传就是不筛(self):
        items = [article(title='甲', topic='AI'), article(title='乙', topic='文学')]
        self.assertEqual([a['title'] for a in bf.select_topics(items, ['AI'])], ['甲'])
        self.assertEqual([a['title'] for a in bf.select_topics(items, ['AI', '文学'])], ['甲', '乙'])
        self.assertEqual(len(bf.select_topics(items, None)), 2, '不传 = 全要')
        self.assertEqual(len(bf.select_topics(items, [])), 2)

    def test_部分主题的标记写进了_json_和_README(self):
        with tempfile.TemporaryDirectory() as tmp:
            bf.write_day([article()], '2026-03-05', tmp, topic_filter=['AI'])
            payload, readme = self.read_json(tmp), self.read_readme(tmp)
        self.assertEqual(payload['topicFilter'], ['AI'])
        self.assertIn('只回填了', readme, '人读的 README 也要说清这一天是残的')
        self.assertIn('AI', readme)

    def test_全量的产出与以前逐字节同形_没有那两个键(self):
        """全量路径**不能**多出标记键。

        `day_done` 把"没有 `topicFilter`"读作"完整的一天"，所以这个键的存在与否就是判据本身；
        顺手也给旧的天保住了格式（它们在盘上本来就没有这个键）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            bf.write_day([article()], '2026-03-05', tmp)
            payload, readme = self.read_json(tmp), self.read_readme(tmp)
        self.assertNotIn('topicFilter', payload)
        self.assertNotIn('truncated', payload)
        self.assertTrue(payload['backfilled'], '原有的键一个都不能少')
        self.assertNotIn('只回填了', readme)

    def test_只回填过一部分的天_全量时必须重做(self):
        with tempfile.TemporaryDirectory() as tmp:
            bf.write_day([article()], '2026-03-05', tmp, topic_filter=['AI'])
            self.assertFalse(bf.day_done(tmp, '2026-03-05'),
                             '只回填过 AI 的一天，跑全量时必须重做，否则其余主题永久漏掉')
            self.assertTrue(bf.day_done(tmp, '2026-03-05', ['AI']),
                            '同样只要 AI 的话就不必再抓一遍')

    def test_要的比存过的多就得重做(self):
        with tempfile.TemporaryDirectory() as tmp:
            bf.write_day([article()], '2026-03-05', tmp, topic_filter=['AI'])
            self.assertFalse(bf.day_done(tmp, '2026-03-05', ['AI', '学术']))
            self.assertTrue(bf.day_done(tmp, '2026-03-05', ['AI']))

    def test_完整的一天对任何主题都算做完(self):
        with tempfile.TemporaryDirectory() as tmp:
            bf.write_day([article()], '2026-03-05', tmp)
            self.assertTrue(bf.day_done(tmp, '2026-03-05', ['AI']),
                            '完整的一天里当然已经有 AI 了，不该为它再抓一遍')
            self.assertTrue(bf.day_done(tmp, '2026-03-05'))

    def test_被截过的一天从来不算做完(self):
        """`--limit-per-day` 截掉的那些既没抓、也看不出来，所以那天永远不算完成。"""
        with tempfile.TemporaryDirectory() as tmp:
            bf.write_day([article()], '2026-03-05', tmp, truncated=True)
            self.assertTrue(self.read_json(tmp)['truncated'], '截断要留下痕迹')
            self.assertFalse(bf.day_done(tmp, '2026-03-05'))
            self.assertFalse(bf.day_done(tmp, '2026-03-05', ['AI']))

    def test_分两次回填同一天_先前那个主题不会掉出索引(self):
        """**这条迟到了**：`--topic` 第一版只验证过一次跑一个主题。

        分两次跑是它本来的用法（先补齐一个主题，later 再补别的），而第一版 `write_day` 会用
        本轮结果**重写** `.articles.json` —— 先前那个主题的条目全部消失、md 却还在盘上。
        索引少几条、盘上多几篇，不报错。所以这里钉住"索引描述的是这一天，不是这一轮"。
        """
        with tempfile.TemporaryDirectory() as tmp:
            bf.write_day([article(title='讲 MCP 的那篇')], '2026-03-05', tmp, topic_filter=['AI'])
            bf.write_day([article(title='一篇散文', topic='文学', fetched_md='正文第二段。' * 30)],
                         '2026-03-05', tmp, topic_filter=['文学'])
            payload = self.read_json(tmp)
            titles = sorted(a['title'] for a in payload['articles'])
            self.assertEqual(titles, ['一篇散文', '讲 MCP 的那篇'],
                             '第二次回填不该把第一次的条目挤掉')
            self.assertEqual(payload['topicFilter'], ['AI', '文学'], '主题范围取并集')
            # md 也要两个都还在
            mds = sorted(p.name for p in Path(tmp, '2026-03-05').rglob('*.md') if p.name != 'README.md')
            self.assertEqual(len(mds), 2)
            self.assertIn('共 2 篇', self.read_readme(tmp),
                          'README 说的是这一天有几篇，不是这一轮写了几篇')

    def test_全量重写同一天_索引里不会出现两条一样的(self):
        """**这条是变异检查逼出来的**：原来那条"重复回填不翻倍"其实碰不到去重 ——
        第二次跑时先前那条属于被筛掉的主题，压根不进 `kept`，去重有没有都一样。

        真正会重合的是**全量重写**（`--refresh`、或补完别的主题后再全量跑一次）：全量时
        `kept` 收下那天的**全部**旧条目，而 `fresh` 又把同一批生成一遍 —— 不去重就是一条
        文章在索引里出现两次。
        """
        with tempfile.TemporaryDirectory() as tmp:
            bf.write_day([article()], '2026-03-05', tmp, topic_filter=['AI'])
            bf.write_day([article()], '2026-03-05', tmp)      # 全量：同一条又被写了一次
            self.assertEqual(len(self.read_json(tmp)['articles']), 1,
                             '同一条文章在索引里只能有一条')

    def test_六个主题都补齐后标记要撤掉(self):
        """一个主题一个主题地补齐是这条路的正常用法，而它必须**能收敛**。

        不撤标记的话，"六个主题都做过"的一天仍被 `day_done` 当成没做完 —— 于是以后随便跑一次
        全量都会把整个窗口重抓一遍，永远收敛不了。判据取的是"主题并集覆盖了分类法"，
        与全量跑过等价（同一天的文章是有界的，都过了一遍就是都过了一遍）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            for index, topic in enumerate(bf.TOPICS):
                if index == len(bf.TOPICS) - 1:
                    break
                bf.write_day([article(topic=topic, title='甲' + topic, fetched_md='正文。' * 40)],
                             '2026-03-05', tmp, topic_filter=[topic])
            self.assertIn('topicFilter', self.read_json(tmp), '还差一个主题，仍算残的')
            last = bf.TOPICS[-1]
            bf.write_day([article(topic=last, title='甲' + last, fetched_md='正文。' * 40)],
                         '2026-03-05', tmp, topic_filter=[last])
            self.assertNotIn('topicFilter', self.read_json(tmp), '六个主题都做过了，标记要撤')
            self.assertTrue(bf.day_done(tmp, '2026-03-05'))
            self.assertTrue(bf.day_done(tmp, '2026-03-05', ['AI']))
            self.assertEqual(len(self.read_json(tmp)['articles']), len(bf.TOPICS), '每一篇都还在')

    def test_旧代码写的_六个主题全在的标记_也算完整(self):
        """**盘上真有这样一天**（2025-09-06），是在"收敛规则"加进去之前写的。

        `write_day` 现在不会再写出这种标记，但已经写下的那几天得能读得回来 —— 否则它们永远
        "没做完"，每次全量都把整个窗口重抓一遍。判据与写的那一头保持同一个：六个主题都在 = 完整。
        """
        with tempfile.TemporaryDirectory() as tmp:
            dirs = Path(tmp, '2026-03-05')
            dirs.mkdir(parents=True)
            (dirs / '.articles.json').write_text(json.dumps({
                'date': '2026-03-05', 'backfilled': True,
                'topicFilter': list(bf.TOPICS),      # 旧代码留下的形态：六个主题，键还在
                'articles': [{'title': '甲', 'source': '某号', 'topic': 'AI'}],
            }, ensure_ascii=False), encoding='utf-8')
            self.assertTrue(bf.day_done(tmp, '2026-03-05'), '六个主题都做过了')
            self.assertTrue(bf.day_done(tmp, '2026-03-05', ['AI']))

    def test_同一主题重复回填不会在索引里翻倍(self):
        with tempfile.TemporaryDirectory() as tmp:
            for _ in range(2):
                bf.write_day([article()], '2026-03-05', tmp, topic_filter=['AI'])
            self.assertEqual(len(self.read_json(tmp)['articles']), 1)

    def test_全量收尾后不再自称是残的(self):
        """先前只回填了 AI 的一天，被全量补完之后标记必须消失 —— 否则它永远被当成没做完。"""
        with tempfile.TemporaryDirectory() as tmp:
            bf.write_day([article()], '2026-03-05', tmp, topic_filter=['AI'])
            bf.write_day([article(title='一篇散文', topic='文学', fetched_md='正文第二段。' * 30)],
                         '2026-03-05', tmp)
            payload = self.read_json(tmp)
            self.assertNotIn('topicFilter', payload)
            self.assertTrue(bf.day_done(tmp, '2026-03-05'), '补全之后就是完整的一天了')

    def test_本来就完整的一天_重做其中一部分不会降级(self):
        with tempfile.TemporaryDirectory() as tmp:
            bf.write_day([article()], '2026-03-05', tmp)
            bf.write_day([article()], '2026-03-05', tmp, topic_filter=['AI'])   # --refresh 的一部分
            self.assertNotIn('topicFilter', self.read_json(tmp),
                             '盘上仍然是完整的一天，不该被盖上"只回填了一部分"的章')

    def test_截断标记一旦记上就不撤销(self):
        with tempfile.TemporaryDirectory() as tmp:
            bf.write_day([article()], '2026-03-05', tmp, topic_filter=['AI'], truncated=True)
            bf.write_day([article()], '2026-03-05', tmp, topic_filter=['AI'])   # 这次没截
            self.assertTrue(self.read_json(tmp).get('truncated'),
                            '这一次没截，不等于上一次截掉的那些补回来了')

    def test_索引坏掉时不静默丢条目(self):
        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp, '2026-03-05')
            day.mkdir(parents=True)
            (day / '.articles.json').write_text('{ 这不是 json', encoding='utf-8')
            bf.write_day([article()], '2026-03-05', tmp, topic_filter=['AI'])
            self.assertTrue((day / '.articles.json.bad').exists(), '读不了的原文件要留下来')
            self.assertEqual(len(self.read_json(tmp)['articles']), 1)

    def test_重复的_topic_参数两个都要读到(self):
        """`--topic ai --topic AI`（**非法值在前**）必须被拒。

        顺序是有讲究的：`--topic AI --topic ai` 判别不了任何东西 —— 只读最后一个参数的实现
        拿到 `ai` 也照样报错，看起来一样（这一版最初就是这么写的，变异检查没变红才发现）。
        把非法值放在**前面**，只读最后一个的实现就会拿到合法的 `AI`、一路跑到连库那一步，
        在测试环境里以异常而不是返回码暴露出来。同仓库 `article_notes.py` 的 `--topic` 本就
        是可重复的，照那边习惯写却静默只做一个主题，是我们要防的那类"安静地少做事"。
        """

        class Out(io.StringIO):
            def reconfigure(self, **_):
                pass

        argv = ['backfill_articles.py', '--since', '2026-03-01', '--until', '2026-03-02',
                '--topic', 'ai', '--topic', 'AI']
        buffer = Out()
        with mock.patch.object(sys, 'argv', argv), contextlib.redirect_stdout(buffer):
            rc = bf.main()
        self.assertEqual(rc, 1, '第一个 --topic 里的 ai 不合法，应当被拒')
        self.assertIn('合法值', buffer.getvalue())

    def test_主题解析_可重复与逗号两种写法(self):
        """`--topic` 的两种写法都要展开对。

        为什么不靠命令行去测：**只断言"被拒"是分不出对错的** —— 把 `--topic` 改回单值之后，
        `for chunk in 'AI'` 会逐字符迭代，每个字符都不在分类法里，于是它照样"被拒"，看着一样。
        所以直接钉解析本身，正反两个方向都钉住。
        """
        self.assertEqual(bf.parse_topics(['AI']), ['AI'])
        self.assertEqual(bf.parse_topics(['AI', '学术']), ['AI', '学术'], '可重复')
        self.assertEqual(bf.parse_topics(['AI,学术']), ['AI', '学术'], '逗号')
        self.assertEqual(bf.parse_topics(['AI, 学术', '文学']), ['AI', '学术', '文学'], '混着来')
        self.assertEqual(bf.parse_topics(['AI,,学术']), ['AI', '学术'], '空段要丢掉')
        self.assertEqual(bf.parse_topics([]), [])
        self.assertEqual(bf.parse_topics('AI,学术'), ['AI', '学术'],
                         '传字符串也当单个处理 —— 逐字符迭代会让整批看起来像"参数拼错"')
        self.assertEqual(bf.parse_topics(''), [])

    def test_拼错的主题在碰库之前就被拒(self):
        """`--topic ai`（小写）会静默筛出 0 篇，然后报一句"没有要处理的"——看着像那天本来就没文章。

        所以必须在**读配置/连库之前**就报错并列出合法值。这条测试同时也是"它没有走到连库"的
        证明：真走到那一步，测试会因为没有微信库而炸，而不是返回 1。
        """

        class Out(io.StringIO):
            def reconfigure(self, **_):   # main() 开头会调它，StringIO 没有
                pass

        argv = ['backfill_articles.py', '--since', '2026-03-01', '--until', '2026-03-02',
                '--topic', 'ai']
        buffer = Out()
        with mock.patch.object(sys, 'argv', argv), contextlib.redirect_stdout(buffer):
            rc = bf.main()
        self.assertEqual(rc, 1)
        self.assertIn('合法值', buffer.getvalue())
        self.assertIn('AI', buffer.getvalue())


class VaultSyncSelectionTests(unittest.TestCase):
    """补拷要挑哪些天。判据错了不会报错，只会**安静地少补几天**。

    2026-09-28 实测踩到：原来的判据是 `.articles.json` 里的 `backfilled` 标记，而那个键
    只有回填那条路会写。199 天里有 12 天有文章却没这个键（08-24/08-25 库里根本没有目录），
    于是它们既不会被补、也不会被报出来 —— 残留就是这么在库里活下来的 1,755 篇。
    """

    def test_有文章的天就算数_不看_backfilled_标记(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'biz-daily'
            day = root / '2026-08-24' / 'AI'
            day.mkdir(parents=True)
            (day / '甲-某篇.md').write_text('正文', encoding='utf-8')
            # `daily` 那条路写出来的 `.articles.json` 里**没有** `backfilled` 这个键
            (root / '2026-08-24' / '.articles.json').write_text(
                json.dumps({'articles': [{'title': '甲'}]}, ensure_ascii=False), encoding='utf-8')
            self.assertEqual(bf.days_with_articles(str(root)), ['2026-08-24'])

    def test_只有_README_的天不算数(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'biz-daily'
            (root / '2026-08-24').mkdir(parents=True)
            (root / '2026-08-24' / 'README.md').write_text('占位', encoding='utf-8')
            (root / '2026-08-25').mkdir(parents=True)
            self.assertEqual(bf.days_with_articles(str(root)), [])

    def test_目录不存在时给空表而不是炸(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(bf.days_with_articles(str(Path(tmp) / 'nope')), [])


if __name__ == '__main__':
    unittest.main()
