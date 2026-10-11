#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**手写的命令面**必须在 CLI 里真实存在：README 的代码块 + `capabilities --json` 里的 `cli:` 串。

`capabilities --json` 也在这一支里，因为它是**学这个项目的入口**（比 600 KB 的文档好读），而它的
`cli:` 字段是**手写**的 —— 同一个输出里的 MCP 那半从真实注册表派生（`safety.mcpSurface.derivedFrom`），
手写的那半同样会漂移。2026-10-11 量过：21 条 `cli:` 串、0 处对不上（准的），但准不等于有人盯着。


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

第三类**手写的命令面**：文档代码块里的 `python|py scripts/x.py --flag`。判据**不跑脚本**，
用 AST 取 `add_argument('--x')` 当声明表 —— 仓库对 Python 脚本的既定纪律就是不 import 也不运行
（`test/script_stdout_encoding_test.py` 头部写着原因：CI 的 python job 只装两个依赖，import 会假失败）。
2026-10-11 量过：24 处文档调用、脚本全在、旗标全认。

跑法：

    npm run build                      # cli.cjs 依赖 dist/
    python scripts/check_readme_commands.py
    python scripts/check_readme_commands.py --self-test    # 正对照：证明判据是活的
"""
import argparse
import ast
import glob
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
# 文档里的 Python 调用（`py -3 scripts/x.py --flag` 这种也认）。
# 脚本名里**允许连字符**：第一版写成 `[\w.]+\.py`，于是 `scripts/no-such-script.py` 整条被静默跳过 ——
# 变异检查里夹具写了 3 条、提取到 2 条才发现（"负结论要先排除假阴性"）。`--` 前缀的自由参数也认。
PY_CALL = re.compile(r'\b(?:python|py)\s+(?:--?[\w-]+\s+)*scripts/([\w.-]+\.py)(.*)$')
PY_FLAG = re.compile(r'(?<!\S)(--?[A-Za-z][A-Za-z0-9-]*)')


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


def fenced_lines(text):
    """围栏代码块里的每一行（围栏标记本身不算）：(行号, 行内容)。"""
    out = []
    fence = False
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith(FENCE):
            fence = not fence
            continue
        if fence:
            out.append((lineno, stripped))
    return out


def _body(stripped):
    """去掉 `$ ` 提示符与行尾注释。"""
    body = stripped[2:].strip() if stripped.startswith('$ ') else stripped
    return re.split(r'\s+#', body)[0].strip()


def command_lines(text):
    """围栏代码块里的 `weflow-cli` 命令：(行号, 正文)。"""
    return [(lineno, body) for lineno, stripped in fenced_lines(text)
            if (body := _body(stripped)).startswith('weflow-cli ')]


def python_invocations(text):
    """围栏代码块里的 Python 调用：(行号, 脚本名, 剩余参数)。"""
    out = []
    for lineno, stripped in fenced_lines(text):
        m = PY_CALL.search(_body(stripped))
        if m:
            out.append((lineno, m.group(1), m.group(2).strip()))
    return out


def declared_flags(source):
    """一个脚本用 `add_argument` 声明过的旗标 —— **纯 AST：不 import、不运行**。

    仓库对 Python 脚本的既定纪律就是不在检查里跑它们（`test/script_stdout_encoding_test.py`
    头部写了原因：CI 的 python job 只装两个依赖，import 会假失败），所以这里也只读源码。
    """
    flags = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and getattr(node.func, 'attr', None) == 'add_argument':
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                        and arg.value.startswith('-'):
                    flags.add(arg.value)
    return flags


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


def default_files():
    """默认扫的**用户文档**。

    排除两份按性质记录过去的：`CHANGELOG.md`（发布日志）与 `docs/DECISIONS.md`（决策日志）——
    它们会成段引用**当时**的命令，拿今天的 CLI 去校验必然假阳性。其余 `docs/*.md` 都扫：
    2026-10-11 就是这份集合里的 `OPERATIONS.md` 抓到了"文档教用户敲一个 CLI 没有的旗标"
    （`search --no-rerank`），而只扫两份 README 时它一条也扫不到。
    """
    out = ['README.md', 'README.en.md', 'OPERATIONS.md', 'ARCHITECTURE.md',
           'CONTRIBUTING.md', 'SECURITY.md']
    out += ['docs/' + os.path.basename(p)
            for p in sorted(glob.glob(os.path.join(ROOT, 'docs', '*.md')))
            if os.path.basename(p) != 'DECISIONS.md']
    return out


def check_python_call(doc, lineno, script, rest):
    """文档里的一条 `py scripts/x.py …` → [问题]。"""
    path = os.path.join(ROOT, 'scripts', script)
    if not os.path.exists(path):
        return [{'file': doc, 'line': lineno, 'command': 'scripts/' + script,
                 'kind': 'missing-script', 'detail': 'scripts/%s 不存在' % script}]
    try:
        with open(path, encoding='utf-8') as fh:
            have = declared_flags(fh.read())
    except (OSError, SyntaxError) as exc:
        # 读不出来就说读不出来（这一面**没有被检查**），不当成通过
        return [{'file': doc, 'line': lineno, 'command': 'scripts/' + script,
                 'kind': 'unreadable-script', 'detail': '读不出这个脚本的声明：%s' % exc}]
    out = []
    for flag in PY_FLAG.findall(rest):
        # argparse 允许前缀缩写，所以 `--dry` 也算认；文档里写全名最清楚，这里按前缀判
        long_ok = flag.startswith('--') and any(h.startswith(flag) for h in have if h.startswith('--'))
        if flag not in have and not long_ok:
            out.append({'file': doc, 'line': lineno, 'command': 'scripts/%s %s' % (script, flag),
                        'kind': 'unknown-script-flag', 'detail': '%s 没声明 %s' % (script, flag)})
    return out


def capability_commands(cli=None):
    """`capabilities --json` 里那几个**手写**的 `cli:` 命令串 → [(字段路径, 命令行)]。

    只收 `cli` 字段：这个对象的其余部分要么是布尔/契约名，要么（`safety.mcpSurface`）是从真实
    注册表派生的 —— 派生那半不用这儿管。
    """
    cli = cli or os.path.join(ROOT, 'cli.cjs')
    if not os.path.exists(cli):
        raise SystemExit('找不到 %s —— 先跑 `npm run build`' % cli)
    proc = subprocess.run(['node', cli, 'capabilities', '--json'], capture_output=True, cwd=ROOT)
    text = (proc.stdout or b'').decode('utf-8', 'replace')
    try:
        data = json.loads(text)
    except ValueError:
        raise SystemExit('`capabilities --json` 没给出可解析的 JSON：%s' % text[:200])
    out = []

    def walk(node, where):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == 'cli' and isinstance(value, str):
                    out.append((where + '.' + key, value))
                else:
                    walk(value, where + '.' + key)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, '%s[%d]' % (where, i))

    walk(data, 'capabilities')
    return out


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
    # Python 那一半：**围栏里**的调用要认得出，**散文里**的提及不许认（那正是假阳性来源）
    fenced = FENCE + 'powershell\npy scripts/health_check.py --json\n' + FENCE + '\n'
    if len(python_invocations(fenced)) != 1:
        bad.append('反空扫失败：围栏里的 `py scripts/x.py` 没被识别出来')
    if python_invocations('跑 `py scripts/health_check.py --json` 就行（散文里的一句）\n'):
        bad.append('散文里的调用被当成命令了 —— 那会造出假阳性')
    flags = declared_flags('import argparse\np = argparse.ArgumentParser()\n'
                           "p.add_argument('--dry-run')\np.add_argument('-n', '--limit')\n")
    if flags != {'--dry-run', '-n', '--limit'}:
        bad.append('AST 取声明旗标取错了：%s' % sorted(flags))
    # 脚本名里的连字符：第一版的正则漏了它，整条调用被静默跳过（夹具 3 条只提取到 2 条才发现）。
    # 注意围栏**必须单独一行** —— 第一版把命令写在 ``` 同一行上，那行被当成围栏标记，内容根本不产出
    # （控制项自己错了，却报成"连字符没被认出来"）。
    hyphen = FENCE + '\npy scripts/some-hyphenated-script.py --flag\n' + FENCE + '\n'
    if len(python_invocations(hyphen)) != 1:
        bad.append('脚本名里的连字符没被认出来 —— 那会让整条调用静默漏检')
    controls = len(CONTROLS) + 5
    print('正对照：%d 条，通过 %d 条' % (controls, controls - len(bad)))
    for item in bad:
        print('  ✗ ' + item)
    return 1 if bad else 0


def main():
    parser = argparse.ArgumentParser(description='校验文档与 capabilities 里的命令在 CLI 里存在')
    parser.add_argument('--files', nargs='*', default=None,
                        help='要扫的 markdown（缺省：README + 仓库根那几份 + docs/*.md，'
                             '排除按性质记录过去的 CHANGELOG 与 DECISIONS）')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--self-test', action='store_true', help='只跑判据的正对照')
    args = parser.parse_args()

    if args.self_test:
        raise SystemExit(self_test())

    checked, findings, doc_checked = 0, [], 0
    for name in (args.files if args.files else default_files()):
        path = os.path.join(ROOT, name)
        if not os.path.exists(path):
            raise SystemExit('找不到 %s' % path)
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        for lineno, body in command_lines(text):
            checked += 1
            doc_checked += 1
            _, problems = audit_line(body, cli_help)
            findings += [{'file': name, 'line': lineno, 'command': body,
                          'kind': kind, 'detail': detail} for kind, detail in problems]
        # 同一批文档里的第三类手写面：`py scripts/x.py --flag`
        for lineno, script, rest in python_invocations(text):
            checked += 1
            findings += check_python_call(name, lineno, script, rest)

    # 第二个手写面：`capabilities --json` 的 `cli:` 串（学这个项目的入口，也是手写的）。
    caps = capability_commands()
    for where, command in caps:
        checked += 1
        _, problems = audit_line('weflow-cli ' + command, cli_help)
        findings += [{'file': 'capabilities --json', 'line': 0, 'command': command, 'where': where,
                      'kind': kind, 'detail': detail} for kind, detail in problems]
    script_checked = checked - doc_checked - len(caps)

    if args.json:
        print(json.dumps({'checked': checked, 'docChecked': doc_checked,
                          'scriptChecked': script_checked, 'capabilitiesChecked': len(caps),
                          'findings': findings}, ensure_ascii=False, indent=2))
    else:
        for f in findings:
            source = ('%s（%s）' % (f['file'], f['where'])) if f.get('where') else ('%s:%d' % (f['file'], f['line']))
            where_it_lives = ('文档里写了它' if f['kind'] == 'unknown-script-flag' else '写的地方有它')
            missing_side = ('那个脚本没声明' if f['kind'] == 'unknown-script-flag' else 'CLI 里没有')
            print('[FAIL] %s  %s\n       %s —— %s%s' % (source, f['command'], f['detail'], where_it_lives, missing_side))
        if findings:
            print('\n%d 处对不上（共检查 %d 条：文档命令 %d + 脚本调用 %d + capabilities %d）。'
                  % (len(findings), checked, doc_checked, script_checked, len(caps)))
        else:
            print('命令面与实现一致：文档命令 %d 条 + `py scripts/…` 调用 %d 条 + capabilities 的 %d 条 `cli:` 串，0 处对不上。'
                  % (doc_checked, script_checked, len(caps)))
    # 反空扫只对**缺省语料**生效：那批文档里一定有命令，扫到 0 条说明判据/提取器坏了，不该报通过。
    # 显式 `--files <夹具>` 时不算失败 —— 夹具本来就可能只含某一类。
    # （"提取器本身还活着"另由 `--self-test` 的三条对照负责；脚本那一面**不做** entry 级反空扫：
    #  文档里一条 `py scripts/…` 都没有是可能发生的。）
    if not args.files and doc_checked == 0:
        print('反空扫失败：缺省文档里一条命令都没扫到，这次"通过"不作数。')
        return 2
    if not caps:
        print('反空扫失败：`capabilities --json` 里一条 `cli:` 串都没读到，这一半没有被检查。')
        return 2
    return 1 if findings else 0


if __name__ == '__main__':
    raise SystemExit(main())
