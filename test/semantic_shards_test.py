"""聊天收集必须**遍历所有分片**、不截断会话、且同一条不重复计数。

为什么专门有一条：2026-10-13 实测本机四片消息库，近期消息全在 `message_3.db`，而配置指向的
`message_0.db` 最新一条是 44 天前的 —— `collect_chat_messages` 当时只开 `ntDbPath` 一个文件，
于是**索引的聊天半边会静默收进旧数据**（症状与 issue #10 的"只读到半个月前"同形）。索引是要花钱
嵌入的，收错数据比报错更贵，所以这三条各钉住一个"安静地少收"的方式：

  · 只开一个分片 ⇒ 第 2 片的会话整段看不见；
  · `sessions[:100]` ⇒ 无排序取前 100，第 101 个会话静默消失；
  · id 只按 `talker:ts` ⇒ 同一秒的多条互相顶掉（实测一张表里 12,106 条只落在 12,058 秒上）。

夹具是**假的连接对象**（只认收集段真正发出的那三种查询），所以不需要 sqlcipher、也不碰真库。
"""

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location('semantic_search', SCRIPTS / 'semantic_search.py')
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)


def ago_minutes(minutes):
    return int((datetime.now(ss.TZ) - timedelta(minutes=minutes)).timestamp())


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.result = []

    def execute(self, sql, params=()):
        s = ' '.join(sql.split())
        if 'FROM Name2Id' in s:
            self.result = [(name,) for name in self.conn.sessions]
        elif 'COUNT(*) FROM sqlite_master' in s:
            self.result = [(1 if params[0] in self.conn.tables else 0,)]
        elif 'FROM "Msg_' in s:
            table = s.split('FROM "')[1].split('"')[0]
            cutoff, limit = params
            rows = [r for r in self.conn.tables.get(table, []) if r[1] >= cutoff]
            self.result = sorted(rows, key=lambda r: -r[1])[:limit]
        else:
            raise AssertionError('未预期的查询: %s' % s[:80])

    def fetchall(self):
        return self.result

    def fetchone(self):
        return self.result[0] if self.result else None


class FakeConn:
    """一个分片：`sessions` 是它 Name2Id 里的会话，`tables` 是 {表名: [(rowid, ts, sender, text)]}。"""

    def __init__(self, sessions=(), tables=None):
        self.sessions = list(sessions)
        self.tables = tables or {}

    def cursor(self):
        return FakeCursor(self)


def table_of(talker):
    import hashlib
    return 'Msg_' + hashlib.md5(talker.encode()).hexdigest()


class MultiShardTests(unittest.TestCase):
    def test_消息只存在于第_2_片时也要收到(self):
        old, new = '张三', '李四'
        shard_a = FakeConn(sessions=[old], tables={table_of(old): [(1, ago_minutes(60 * 24 * 40), 'x', '很久以前说过的一句够长的话')]})
        shard_b = FakeConn(sessions=[new], tables={table_of(new): [(7, ago_minutes(30), 'y', '刚刚说的一句话，长度也够了')]})
        items = ss.collect_chat_messages([shard_a, shard_b], {}, days=90)
        texts = [i['text'] for i in items]
        self.assertIn('刚刚说的一句话，长度也够了', texts, '第 2 片里的消息没收进来 —— 只开了一个分片？')

    def test_同一个会话跨两片时同一条不重复(self):
        talker = '王五'
        row = (3, ago_minutes(20), 'z', '这句话两个分片里都有')
        conns = [FakeConn(sessions=[talker], tables={table_of(talker): [row]}),
                 FakeConn(tables={table_of(talker): [row]})]
        items = ss.collect_chat_messages(conns, {}, days=90)
        self.assertEqual(len(items), 1, '同一条消息被数了两次（id 里要带 rowid 去重）')

    def test_同一秒的多条不会被顶掉(self):
        talker = '赵六'
        ts = ago_minutes(10)
        rows = [(1, ts, 'a', '第一句写在同一个秒里的话'), (2, ts, 'b', '第二句也写在同一个秒里')]
        items = ss.collect_chat_messages([FakeConn(sessions=[talker], tables={table_of(talker): rows})], {}, days=90)
        self.assertEqual(len(items), 2, '同一秒的两条只留下一条 —— id 里没带 rowid？')

    def test_第一_100_个之外的会话也要收(self):
        conn = FakeConn(sessions=['s%03d' % i for i in range(101)], tables={})
        last = 's100'
        conn.tables[table_of(last)] = [(1, ago_minutes(15), 'p', '第一百零一个会话里说出的一句够长的话')]
        items = ss.collect_chat_messages([conn], {}, days=90)
        self.assertEqual([i['text'] for i in items], ['第一百零一个会话里说出的一句够长的话'],
                         '第 101 个会话被 `[:100]` 截掉了（而且那 100 个是无排序取的）')

    def test_太短的和非文本仍然不收(self):
        talker = '钱七'
        rows = [(1, ago_minutes(5), 'a', '太短'), (2, ago_minutes(5), 'a', '')]
        self.assertEqual(ss.collect_chat_messages(
            [FakeConn(sessions=[talker], tables={table_of(talker): rows})], {}, days=90), [])


if __name__ == '__main__':
    unittest.main()
