# -*- coding: utf-8 -*-
"""`health_check` 的"微信客户端"那一项 —— 尤其**"少了一项不算通过"**。

判据来自一份实测的启动器逆向分析（报告在仓库外）：
- 版本只能从**活进程命令行的 `--user-lib-dir`** 取；exe 里的版本字面量只是**兜底**，
  而这台机器上它恰好与目录同名 ⇒ "读字面量"是个看着会对的错判据。
- 磁盘上**有**版本目录 ≠ 它能用：没有 `Weixin.dll` 的残留目录，指向它的加载会失败。
- `check --json` 里整个 `wechatClient` 字段不在 = CLI 是**没重建**的旧版本 ⇒ 必须红。
  这一条**真发生过**：加完这段代码第一次跑 `health_check` 就是红的（"check 没报这一项"），
  因为 `dist` 还没重建 —— 也就是说，少了这一项却一片绿是现实里会发生的事。
"""
import datetime
import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location('health_check', SCRIPTS / 'health_check.py')
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


def client(**kwargs):
    base = {'running': True, 'version': None, 'loadedDirs': [], 'note': '', 'unusableVersionDirs': []}
    base.update(kwargs)
    return {'wechatClient': base}


class WechatClientCheckTests(unittest.TestCase):
    def run_check(self, payload):
        report = hc.Report()
        with patch.object(hc, 'run_json', lambda args: (0, payload)):
            hc.check_wechat_client(report)
        self.assertEqual(len(report.rows), 1)
        return report.rows[0]

    def test_字段整个不在时不算通过(self):
        """旧 CLI / 没重建的 dist：少一项**不能**当成"没问题"。"""
        row = self.run_check({'configuration': {}})
        self.assertFalse(row['ok'])
        self.assertIn('没报这一项', row['detail'])
        self.assertIn('构建', row['hint'])

    def test_check_没返回_不算通过(self):
        self.assertFalse(self.run_check(None)['ok'])

    def test_有版本时报版本与目录(self):
        row = self.run_check(client(version='4.1.15.13', loadedDirs=['D:\\WeiXin\\4.1.15.13']))
        self.assertTrue(row['ok'])
        self.assertIn('4.1.15.13', row['detail'])
        self.assertIn('D:\\WeiXin\\4.1.15.13', row['detail'])

    def test_客户端没跑时说未知_而不是没问题(self):
        row = self.run_check(client(running=False, note='客户端没在运行 ⇒ 版本未知'))
        self.assertTrue(row['ok'], '客户端没开不是故障')
        self.assertIn('未知', row['detail'])

    def test_残留版本目录要在明细里点出来(self):
        row = self.run_check(client(version='4.1.15.13', loadedDirs=['D:\\WeiXin\\4.1.15.13'],
                                    unusableVersionDirs=[{'dir': '4.1.15.11', 'files': 1}]))
        self.assertIn('4.1.15.11', row['detail'])
        self.assertIn('1 个文件', row['detail'])

    def test_数据目录不是同一份时要明说(self):
        row = self.run_check(client(version='4.1.15.13', loadedDirs=['D:\\WeiXin\\4.1.15.13'],
                                    dataDirMatchesConfig=False))
        self.assertIn('不是同一份', row['detail'])
        # 一致 / 不知道（客户端没跑）时不该出现这句话
        self.assertNotIn('不是同一份', self.run_check(client(dataDirMatchesConfig=True))['detail'])
        self.assertNotIn('不是同一份', self.run_check(client(dataDirMatchesConfig=None))['detail'])

    def test_崩溃记录与_update_data_的不确定要带出来(self):
        row = self.run_check(client(version='4.1.15.13', loadedDirs=['D:\\WeiXin\\4.1.15.13'],
                                    crashRecords={'dir': 'x', 'reports': 2, 'records': 1,
                                                  'newest': '2026-10-10T22:00:00.000Z'},
                                    caveats=['update\\update.data 存在：加载路径可能由它决定']))
        self.assertIn('崩溃记录 3 份', row['detail'])
        self.assertIn('2026-10-10', row['detail'])
        self.assertIn('update.data', row['detail'])


class SessionFreshnessTests(unittest.TestCase):
    """"会话很旧"有两种解释，提示里必须**两种都写**，并点出判别器在隔壁那一项。"""

    def run_check(self, sessions):
        report = hc.Report()
        with patch.object(hc, 'run_json', lambda args: (0, {'sessions': sessions})):
            hc.check_session_freshness(report)
        return report.rows[0]

    def test_很久没消息时两种解释都要说(self):
        old = time.time() - 30 * 86400
        row = self.run_check([{'username': 'a', 'sortTimestamp': old}])
        self.assertFalse(row['ok'])
        self.assertIn('没聊天', row['hint'], '必须给出"只是没聊天"这个最可能的解释')
        self.assertIn('读取路径一致', row['hint'], '并且要点出判别器在隔壁那一项')
        self.assertIn('部分分片', row['hint'])

    def test_新消息时绿且没有那串提示(self):
        row = self.run_check([{'username': 'a', 'sortTimestamp': time.time() - 60}])
        self.assertTrue(row['ok'])

    def test_没有时间戳时说实话(self):
        row = self.run_check([{'username': 'a'}])
        self.assertFalse(row['ok'])
        self.assertIn('没有时间戳', row['detail'])


class ReadPathArbiterTests(unittest.TestCase):
    """两条读取路径"最新消息"不一致时，**再读一次 json 做仲裁**。

    判据原来精确到分钟、零容差，而 json 先导 html 后导（中间一两分钟）⇒ 检查期间来一条新消息
    就必然假红（2026-10-11 实测撞到）。放宽容差会把这一项唯一要抓的东西（分片半读）放过去，
    所以改成仲裁：复查能看到 html 那条 ⇒ 差别来自消息是新的；看不到 ⇒ 真的不一致。
    """

    @staticmethod
    def ts(hour, minute, second=0):
        return datetime.datetime(2026, 10, 11, hour, minute, second).timestamp()

    def run_check(self, json_reads, html_stamp, total=5):
        """`json_reads` 按调用顺序给出每次 json 导出的时间戳列表。"""
        calls = []
        queue = [list(x) for x in json_reads]

        def fake_run(args):
            calls.append(args)
            fmt = args[2]
            out = Path(args[args.index('--output') + 1])
            out.mkdir(parents=True, exist_ok=True)
            if fmt == 'json':
                stamps = queue.pop(0) if queue else json_reads[-1]
                (out / 'a.json').write_text(
                    json.dumps([{'createTime': int(t)} for t in stamps]), encoding='utf-8')
                return (0, '', '')
            (out / 'p.html').write_text(
                '导出时间 %s\nTotal: %d messages\n' % (html_stamp, total), encoding='utf-8')
            return (0, 'Total: %d messages' % total, '')

        report = hc.Report()
        with patch.object(hc, 'run', fake_run):
            hc.check_read_paths(report, 'wxid_x')
        self.assertEqual(len(report.rows), 1)
        return report.rows[0], calls

    def test_一致时不多花那一次导出(self):
        row, calls = self.run_check([[self.ts(13, 40)]], '2026-10-11 13:40:14')
        self.assertTrue(row['ok'], row['detail'])
        self.assertEqual(len(calls), 2, '一致时只该导出两次（json + html）')

    def test_不一致但复查能看到_判绿并说明原因(self):
        row, calls = self.run_check([[self.ts(13, 38)], [self.ts(13, 40)]], '2026-10-11 13:40:14')
        self.assertTrue(row['ok'], row['detail'])
        self.assertIn('复查', row['detail'])
        self.assertIn('新消息', row['hint'])
        self.assertEqual(len(calls), 3, '不一致时才花第三次导出')

    def test_复查也没看到更近的消息_判红(self):
        row, calls = self.run_check([[self.ts(13, 38)], [self.ts(13, 38)]], '2026-10-11 13:40:14')
        self.assertFalse(row['ok'])
        self.assertIn('复查 json 仍是', row['hint'])
        self.assertIn('没有更近的消息', row['hint'],
                      '仲裁没说通时要写清"复查也没看到更新的"，而不是只说"不一致"')
        self.assertEqual(len(calls), 3)


class ScheduledTasksCheckTests(unittest.TestCase):
    """`schtasks` 的输出**读不出来**时，必须报"没验证"，不能报"上次成功"。

    这一项原来会假绿：判据是在输出里找 `Last Result` / `上次运行结果` 那一行，找不到时
    两个清单都是空的 ⇒ 落到 else 报"均在位且上次成功"。系统语言或输出格式一变就会走到那条路。
    与 `docs/DECISIONS.md` D-107 那条"取不到就说未知"是同一条纪律。
    """

    @staticmethod
    def _proc(stdout, returncode):
        class P:
            pass
        p = P()
        p.stdout, p.returncode = stdout, returncode
        return p

    def run_check(self, stdout, returncode=0):
        report = hc.Report()
        with patch.object(hc.shutil, 'which', lambda name: 'schtasks'), \
             patch.object(hc.subprocess, 'run', lambda *a, **k: self._proc(stdout, returncode)):
            hc.check_scheduled_tasks(report)
        self.assertEqual(len(report.rows), 1)
        return report.rows[0]

    @unittest.skipIf(os.name != 'nt', '这一项只在 Windows 上做')
    def test_上次成功(self):
        row = self.run_check('TaskName:   WeFlow Issue Watch\nLast Result: 0\n')
        self.assertTrue(row['ok'])
        self.assertIn('均在位且上次成功', row['detail'])

    @unittest.skipIf(os.name != 'nt', '这一项只在 Windows 上做')
    def test_上次失败(self):
        row = self.run_check('TaskName:   WeFlow Issue Watch\nLast Result: 1\n')
        self.assertFalse(row['ok'])
        self.assertIn('上次运行失败', row['detail'])

    @unittest.skipIf(os.name != 'nt', '这一项只在 Windows 上做')
    def test_读不出上次结果时不算通过(self):
        """**这条钉的是假绿**：输出里没有那一行（语言/格式变了）⇒ 说"读不出"，不说"成功"。"""
        row = self.run_check('TaskName:  WeFlow Issue Watch\nStatus:  Ready\n')
        self.assertFalse(row['ok'])
        self.assertIn('读不出', row['detail'])
        self.assertNotIn('上次成功', row['detail'])

    @unittest.skipIf(os.name != 'nt', '这一项只在 Windows 上做')
    def test_任务不存在(self):
        row = self.run_check('', returncode=1)
        self.assertFalse(row['ok'])
        self.assertIn('不存在', row['detail'])


if __name__ == '__main__':
    unittest.main()
