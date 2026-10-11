#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""README 里写的命令，必须在 CLI 里真实存在（`docs/RELEASING.md` 第 3 步的一部分）。

为什么要有它：`1.5.0` 与主线之间曾拉开 128 个提交、两个多月，用户照着 README 敲
`init --path` 得到 `unknown option` —— **文档漂移是用户先发现的那一类故障**，而仓库里
原本没有任何东西盯着它（`test/pack-contents.test.ts` 管的是 npm 包里**有什么文件**，
不是**命令面**）。

判据取自真实 CLI，不复刻命令表：逐级问 `--help`，用输出里的 `Usage:` 行判断 commander
实际解析到了哪一级。**不能看退出码** —— `--help` 会短路，`wiki <不存在的子命令> --help`
照样打印 wiki 的帮助并 exit 0。第一版就是这么错的：`bad_path` 永远是 0，看着"全绿"，
其实是那条判据从来没机会执行（同 `AGENTS.md` 的"负结论要配正对照"）。

只扫**围栏代码块**里以 `weflow-cli` 开头的行。散文与表格里的提及不扫 —— 英文 README 里
「Manage with `weflow-cli scene`, or bind…」的 `or` 会被当成子命令，那是假阳性。

跑法：

    npm run build                      # cli.cjs 依赖 dist/
    python scripts/check_readme_commands.py
    python scripts/check_readme_commands.py --self-test    # 正对照：证明判据是活的
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys

# 输出里会有中文与命令原文；手跑/重定向时 stdout 走本机编码（GBK）会 UnicodeEncodeError。
# 这是 `test/script_stdout_encoding_test.py` 盯着的规矩，见 `docs/EXTENDING.md` Recipe E。
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FENCE = '`' * 3
TOKEN_OK = re.compile(r'^[a-z][a-z0-9-]*$')
FLAG = re.compile(r'^--?[A-Za-z][A-Za-z0-9-]*$')


def usage_path(help_text):
    """这条帮助属于哪个命令 —— 从 `Usage:` 行取，直到第一个选项/占位符。"""
    if not help_text:
        return None
    m = re.search(r'^Usage: weflow-cli\s*([^\n]*)', help_text, re.M)
    if not m:
        return None
    out = []
    for tok in m.group(1).split():
        if tok.startswith(('-', '<', '[')):
            break
        out.append(tok)
    return out


def has_subcommands(help_text):
    return bool(help_text) and bool(re.search(r'^\s*Commands:', help_text, re.M))


def command_lines(text):
    """围栏代码块里的命令：(行号, 去掉 `$ ` 与行尾注释后的正文)。"""
    out = []
    fence = False
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith(FENCE):
            fence = not fence
            continue
        if not fence:
            continue
        body = stripped[2:].strip() if stripped.startswith('$ ') else stripped
        if not body.startswith('weflow-cli '):
            continue
        out.append((lineno, re.split(r'\s+#', body)[0].strip()))
    return out


def audit_line(body, help_of):
    """→ (命令路径, [问题])。问题里 kind 是 'unknown-command' 或 'unknown-flag'。"""
    tokens = shlex.split(body, posix=False)[1:]      # posix=False：Windows 路径里的 \ 不被吃掉
    path, problems = [], []
    for tok in tokens:
        clean = tok.strip('`,')
        if not TOKEN_OK.match(clean):
            break                                    # 参数/占位符，路径到此为止
        if usage_path(help_of(path + [clean])) == path + [clean]:
            path.append(clean)
        else:
            # 走不动了。父命令**有子命令**，说明这个裸词是想当子命令用的 ⇒ 它不存在。
            if has_subcommands(help_of(path)):
                problems.append(('unknown-command', ' '.join(path + [clean])))
            break
    help_text = help_of(path)
    for tok in tokens:
        clean = tok.strip('`,')
        if FLAG.match(clean) and clean not in (help_text or ''):
            problems.append(('unknown-flag', '%s %s' % (' '.join(path), clean)))
    return path, problems


# ---------------------------------------------------------------- 真实 CLI / 正对照

def cli_help(path, cli=None):
    """问真实 CLI。取不到就返回 None —— 由调用方报错，不许当成"没问题"。"""
    cli = cli or os.path.join(ROOT, 'cli.cjs')
    if not os.path.exists(cli):
        raise SystemExit('找不到 %s —— 先跑 `npm run build`（cli.cjs 依赖 dist/）' % cli)
    proc = subprocess.run(['node', cli, *path, '--help'], capture_output=True, cwd=ROOT)
    return (proc.stdout + proc.stderr).decode('utf-8', 'replace')


CANNED = {
    (): ('Usage: weflow-cli [options] [command]\n\nOptions:\n  -V, --version\n\n'
         'Commands:\n  sessions [options]  会话列表\n  wiki [options]      概念图谱管理\n'),
    ('sessions',): 'Usage: weflow-cli sessions [options]\n\nOptions:\n  -n, --limit <n>\n',
    ('wiki',): ('Usage: weflow-cli wiki [options] [command]\n\nOptions:\n  -h, --help\n\n'
                'Commands:\n  graph [options]\n  lint [options]\n'),
    ('wiki', 'graph'): 'Usage: weflow-cli wiki graph [options]\n\nOptions:\n  --flat\n  --open\n',
}

# (说明, 命令行, 期望报出的问题数)
CONTROLS = [
    ('该过的过：存在的命令 + 存在的旗标', 'weflow-cli sessions -n 5', 0),
    ('该报的报：不存在的旗标', 'weflow-cli sessions --bogus', 1),
    ('该报的报：不存在的子命令', 'weflow-cli wiki bogus', 1),
    ('该报的报：不存在的顶层命令', 'weflow-cli bogus', 1),
    ('不该报的不报：参数里的裸词（父命令没有子命令）', 'weflow-cli sessions 张三', 0),
]


def self_test():
    """判据本身的正对照。扫描器失灵时它会安静地全绿，所以先证明它活着。"""
    bad = []
    for label, line, expect in CONTROLS:
        _, problems = audit_line(line, lambda p: CANNED.get(tuple(p)))
        if len(problems) != expect:
            bad.append('%s\n    %s\n    期望 %d 个问题，实得 %d：%s'
                       % (label, line, expect, len(problems), problems))
    # 反"空扫"：一条命令都没解析出来，说明扫描器坏了，那时的"没问题"不作数
    sample = FENCE + 'powershell\ndir\nweflow-cli sessions -n 5\n' + FENCE + '\n'
    if len(command_lines(sample)) != 1:
        bad.append('反空扫失败：围栏代码块里的命令没被识别出来')
    print('正对照：%d 条，通过 %d 条' % (len(CONTROLS) + 1, len(CONTROLS) + 1 - len(bad)))
    for item in bad:
        print('  ✗ ' + item)
    return 1 if bad else 0


def main():
    parser = argparse.ArgumentParser(description='校验 README 里的命令在 CLI 里存在')
    parser.add_argument('--files', nargs='*', default=['README.md', 'README.en.md'])
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--self-test', action='store_true', help='只跑判据的正对照')
    args = parser.parse_args()

    if args.self_test:
        raise SystemExit(self_test())

    checked, findings = 0, []
    for name in args.files:
        path = os.path.join(ROOT, name)
        if not os.path.exists(path):
            raise SystemExit('找不到 %s' % path)
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        for lineno, body in command_lines(text):
            checked += 1
            _, problems = audit_line(body, cli_help)
            findings += [{'file': name, 'line': lineno, 'command': body,
                          'kind': kind, 'detail': detail} for kind, detail in problems]

    if args.json:
        print(json.dumps({'checked': checked, 'findings': findings}, ensure_ascii=False, indent=2))
    else:
        for f in findings:
            print('[FAIL] %s:%d  %s\n       %s —— README 里有，CLI 里没有'
                  % (f['file'], f['line'], f['command'], f['detail']))
        if findings:
            print('\n%d 条命令里有 %d 处对不上（共检查 %d 条）。'
                  % (len(set(f['command'] for f in findings)), len(findings), checked))
        else:
            print('README 命令面与 CLI 一致：检查了 %d 条命令，0 处对不上。' % checked)
    if checked == 0:
        print('反空扫失败：一条命令都没扫到，这次"通过"不作数。')
        return 2
    return 1 if findings else 0


if __name__ == '__main__':
    raise SystemExit(main())
