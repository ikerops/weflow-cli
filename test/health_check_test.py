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
import importlib.util
import os
import sys
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
