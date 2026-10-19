"""守卫：「不说话的失败」不许再增长。

为什么这条守卫值得存在：这个项目最贵的一类故障不是崩溃，而是**静默**——读不到分片就跳过、
配置解不开就当没配、图片映射坏了就从空表开始。用户看到的是"成功但少了一半"，于是把时间
花在怀疑自己的微信/库里（issue #3 / #9 / #10 与 2026-10-18 那封邮件，同一个根因被描述成四种现象）。

这条测试做两件事：

1. **基线**：`scripts/` 下「裸 `except:` 且 body 只有 pass/continue」的数量不许超过记录的基线。
   要加新的，必须同时改这里并写清为什么——那一步就是"想一下"的机会。
   （不禁止 `except Exception:`：它至少不会吞掉 KeyboardInterrupt/SystemExit。）
2. **行为**：`_utils` 里那几个"故意不抛"的取配置函数，失败时必须往 stderr 说一次，而不是完全无声。
"""
import ast
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
sys.path.insert(0, str(SCRIPTS))

import _utils  # noqa: E402

# 2026-10-19 清点的结果（53 处 / 17 个文件）。**只许减，不许增。**
BARE_EXCEPT_BASELINE = 53


def count_bare_silent_except(path):
    """返回该文件里「裸 except + body 全 pass/continue」的处数。"""
    try:
        tree = ast.parse(path.read_text(encoding='utf-8', errors='replace'))
    except SyntaxError:
        return 0
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or node.type is not None:
            continue
        body = [b for b in node.body
                if not (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant))]
        if body and all(isinstance(b, (ast.Pass, ast.Continue)) for b in body):
            count += 1
    return count


class SilentFailureBaselineTests(unittest.TestCase):
    def test_裸except不许增长(self):
        total = 0
        offenders = []
        for path in sorted(SCRIPTS.glob('*.py')):
            n = count_bare_silent_except(path)
            if n:
                offenders.append((path.name, n))
                total += n
        detail = '、'.join('%s=%d' % (name, n) for name, n in sorted(offenders, key=lambda kv: -kv[1]))
        self.assertLessEqual(
            total, BARE_EXCEPT_BASELINE,
            '裸 except + pass/continue 从 %d 涨到了 %d。新增的那处要么改成 `except Exception` '
            '并打印原因，要么在测试里说明为什么必须静默。\n当前分布：%s'
            % (BARE_EXCEPT_BASELINE, total, detail))


class ConfigWarningTests(unittest.TestCase):
    """取配置的函数故意不抛，但**不许完全无声**。"""

    def setUp(self):
        _utils.reset_warn_once()

    def tearDown(self):
        _utils.reset_warn_once()

    def test_解密失败会说一次且只一次(self):
        with mock.patch.object(_utils, 'decrypt_lock', side_effect=ValueError('密文坏了')):
            err = _capture_stderr(lambda: [
                _utils.get_dashscope_key({}),
                _utils.get_dashscope_key({}),
                _utils.get_dashscope_key({}),
            ])
        self.assertEqual(err.count('dashscopeApiKey'), 1, '同一原因应只说一次，实际：%r' % err)
        self.assertIn('密文坏了', err)
        self.assertIn('退成关键词', err)

    def test_仍然返回空串而不是抛(self):
        with mock.patch.object(_utils, 'decrypt_lock', side_effect=ValueError('x')):
            with mock.patch('sys.stderr', new_callable=__import__('io').StringIO):
                self.assertEqual(_utils.get_dashscope_key({}), '')
                self.assertEqual(_utils.get_typesafe_key({}), '')

    def test_读不到配置文件也会说(self):
        with mock.patch.object(_utils, 'load_config', side_effect=FileNotFoundError('no config')):
            err = _capture_stderr(lambda: (_utils.get_openrouter_key(), _utils.get_ai_engine()))
        self.assertIn('读不到配置文件', err)
        self.assertIn('FileNotFoundError', err)

    def test_没配key时不吵(self):
        # 配置里没有这个键 → decrypt_lock('') 返回 ''，不该产生任何警告
        err = _capture_stderr(lambda: _utils.get_dashscope_key({}))
        self.assertEqual(err, '')


def _capture_stderr(fn):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        fn()
    return buf.getvalue()


if __name__ == '__main__':
    unittest.main()
