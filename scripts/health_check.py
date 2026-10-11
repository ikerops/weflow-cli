#!/usr/bin/env python3
"""Periodic health check for weflow-cli.

A pure script - no model, no tokens. Every check here exists because that exact
thing has silently broken at least once, and the failure was invisible:

  版本一致        1.6.0 自报 1.5.1, so "which build am I running" was unanswerable
  读取路径一致    只读 message_0.db 时, export json 有 31 条而 export html 有 1145 条
  会话新鲜度      同上, 会话列表停在 18 天前而命令仍然"成功"
  导出格式         excel 全程从未成功过, 报错只有"导出失败"四个字
  Python 依赖     缺 sqlcipher3 时命令报的是"数据库连接失败"
  计划任务        提醒脚本静默停跑, 没人会发现
  微信客户端      从没报过客户端版本; 而"读 exe 里的版本字面量"是错的判据(实测那只是兜底),
                  磁盘上还可能留着一个不能用的版本目录(指向它的加载会失败)

Run it ad hoc, or schedule it and read the summary line:

    py scripts/health_check.py
    py scripts/health_check.py --json

Exit code is 0 only when every check passes, so a scheduler can act on it.
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / 'cli.cjs'

# Windows consoles default to a legacy codepage that cannot encode the report.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

# Each check shells out to the CLI, which takes seconds; keep the sample small.
SAMPLE_LIMIT = 5
TIMEOUT = 300

# A session list that has not moved in this long means reads are stale. WeChat
# is not chatty for everyone, so this is deliberately generous.
STALE_SESSION_DAYS = 7


def run(args, timeout=TIMEOUT):
    """(returncode, stdout, stderr) with Node's experimental warning suppressed."""
    env = {**os.environ, 'NODE_NO_WARNINGS': '1', 'PYTHONIOENCODING': 'utf-8'}
    try:
        proc = subprocess.run(
            ['node', str(CLI), *args], capture_output=True, timeout=timeout,
            encoding='utf-8', errors='replace', env=env, cwd=str(ROOT))
    except (OSError, subprocess.SubprocessError) as error:
        return 1, '', f'{type(error).__name__}: {error}'
    return proc.returncode, proc.stdout or '', proc.stderr or ''


def run_json(args):
    """Parsed JSON payload, or None.

    The CLI pretty-prints, so the payload spans many lines - parse from the
    first brace rather than looking for a single-line blob.
    """
    code, out, _ = run([*args, '--json'])
    start = out.find('{')
    if start < 0:
        return code, None
    try:
        return code, json.JSONDecoder().raw_decode(out[start:])[0]
    except ValueError:
        return code, None


class Report:
    def __init__(self):
        self.rows = []

    def add(self, name, ok, detail, hint=''):
        self.rows.append({'name': name, 'ok': bool(ok), 'detail': detail, 'hint': hint})

    @property
    def failed(self):
        return [r for r in self.rows if not r['ok']]

    def render(self):
        stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
        lines = [f'WeFlow 健康检查 · {stamp}', '']
        width = max(len(r['name']) for r in self.rows) + 2
        for row in self.rows:
            mark = 'OK  ' if row['ok'] else 'FAIL'
            lines.append(f'[{mark}] {row["name"]:<{width}}{row["detail"]}')
            if not row['ok'] and row['hint']:
                lines.append(f'{"":<7}{"":<{width}}↳ {row["hint"]}')
        lines.append('')
        if self.failed:
            lines.append(f'{len(self.rows) - len(self.failed)} 项通过 / {len(self.failed)} 项失败')
        else:
            lines.append(f'全部通过（{len(self.rows)} 项）')
        return '\n'.join(lines)


def check_version(report):
    """The CLI must report the version it actually is."""
    try:
        declared = json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))['version']
    except (OSError, ValueError, KeyError) as error:
        report.add('版本一致', False, f'读不到 package.json ({type(error).__name__})')
        return
    code, out, _ = run(['--version'])
    reported = out.strip().split('\n')[-1].strip() if out.strip() else ''
    report.add('版本一致', code == 0 and reported == declared,
               f'CLI {reported or "?"} / package {declared}',
               '两者不一致说明 --version 又写死了，或 dist 未重新构建')


def check_environment(report):
    """Environment self-check: config present, Python deps importable."""
    _, data = run_json(['check'])
    if not data:
        report.add('环境自检', False, 'check 未返回 JSON', '先跑 weflow-cli check 看原始输出')
        return
    config = data.get('configuration') or {}
    deps = ((data.get('dependencies') or {}).get('required') or {})
    missing = [name for name, status in deps.items() if not status]
    ok = bool(config.get('initialized')) and bool(config.get('messageDatabase')) and not missing
    detail = f'已初始化={config.get("initialized")} 消息库={config.get("messageDatabase")}'
    if missing:
        detail += f' 缺依赖={",".join(missing)}'
    report.add('环境自检', ok, detail,
               '缺依赖: pip install -r requirements.txt；未初始化: weflow-cli init')


def pick_talkers(report):
    """The most recently active sessions - the likeliest to span shards."""
    _, data = run_json(['sessions', '--limit', str(SAMPLE_LIMIT)])
    sessions = ((data or {}).get('sessions') or [])
    if not sessions:
        report.add('选择样本会话', False, '会话列表为空', '先确认微信已登录且 init 成功')
        return []
    ordered = sorted(sessions, key=lambda s: s.get('sortTimestamp') or 0, reverse=True)
    return [s['username'] for s in ordered[:2] if s.get('username')]


def read_newest_json(out_dir):
    """那个目录里 json 导出的最新消息时间（秒）；读不出来给 None。"""
    newest = None
    for path in Path(out_dir).glob('*.json'):
        try:
            rows = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        times = [int(r.get('createTime') or 0) for r in rows]
        if times:
            newest = max(times)
            break
    return newest


def check_read_paths(report, talker):
    """`export json` and `export html` read the same chat by different code.

    They disagreed by 1114 messages when the JSON path read only the first
    shard, so comparing their newest message is the cheapest way to notice the
    read paths drifting apart again.

    **但"最新消息"是会动的**：json 先导、html 后导，中间隔着一两分钟 —— 这期间聊天里来了
    一条新消息，两条路径就会"不一致"，而那是**聊天活着**，不是读取断了。原来判据是精确到分钟
    的相等（2026-10-11 实测撞到过：`json 13:38 / html 13:40:14` 判红，紧接着同一条聊天
    `13:40 vs 13:40:14` 判绿）。放宽容差解决不了：繁忙的群里分片轮转本来就在分钟级，
    放宽就会把这个检查唯一要抓的东西放过去。

    所以不一致时**再读一次 json 做仲裁**：那一读能看到 html 那条 ⇒ 差别来自"消息是新的"；
    看不到 ⇒ 两条路径真的不一致（这才是要报的）。仲裁只在**本来就不一致**时才花这一次导出。
    """
    with tempfile.TemporaryDirectory(prefix='wf-health-') as tmp:
        out_json = Path(tmp) / 'json'
        code_j, err_j = run(['export', talker, 'json', '--limit', '5',
                             '--output', str(out_json), '--non-interactive'])[0::2]
        newest_json = read_newest_json(out_json)

        out_html = Path(tmp) / 'html'
        code_h, out_h, _ = run(['export', talker, 'html', '--limit', '5',
                                '--output', str(out_html), '--non-interactive'])
        total_html = None
        for line in out_h.split('\n'):
            line = line.strip()
            if line.startswith('Total:') and 'messages' in line:
                try:
                    total_html = int(line.split()[1])
                except (IndexError, ValueError):
                    pass
        # Read the pages while the temp directory still exists.
        newest_html = html_newest_stamp(out_html)

    label = f'读取路径一致 ({talker[:14]}…)'
    if newest_json is None:
        report.add(label, False, 'json 导出没有产出可比对的消息',
                   'export json 失败时先单独跑一次看报错')
        return
    if total_html is None:
        report.add(label, False, 'html 导出没有报告条数',
                   'export html 失败时先单独跑一次看报错')
        return

    newest_txt = datetime.datetime.fromtimestamp(newest_json).strftime('%Y-%m-%d %H:%M')
    if not newest_html:
        # No timestamp in the page (empty chat); the count comparison still holds.
        report.add(label, total_html > 0, f'json 最新 {newest_txt} / html 共 {total_html} 条')
        return

    if newest_html[:16] == newest_txt:
        report.add(label, True, f'json 最新 {newest_txt} / html 最新 {newest_html}')
        return

    # 不一致 —— **仲裁**：再读一次 json。它能看到 html 那条，说明差别只是"消息是新的"。
    with tempfile.TemporaryDirectory(prefix='wf-health-arbiter-') as tmp:
        again = Path(tmp) / 'json'
        run(['export', talker, 'json', '--limit', '5',
             '--output', str(again), '--non-interactive'])
        arbiter = read_newest_json(again)
    if arbiter is not None and arbiter >= newest_json:
        arbiter_txt = datetime.datetime.fromtimestamp(arbiter).strftime('%Y-%m-%d %H:%M')
        if arbiter_txt == newest_html[:16]:
            report.add(label, True,
                       f'json 最新 {newest_txt} → 复查 {arbiter_txt} / html 最新 {newest_html}',
                       '差别来自检查期间来了新消息（这一项防的是"只剩一个分片被读了"）')
            return

    # 仲裁没说通（复查也没读到 html 那条，或者根本没读出来）⇒ 这是真的不一致
    if arbiter is None:
        reason = '复查 json 也没读出来'
    else:
        reason = '复查 json 仍是 %s，没有更近的消息' % \
            datetime.datetime.fromtimestamp(arbiter).strftime('%Y-%m-%d %H:%M')
    report.add(label, False, f'json 最新 {newest_txt} / html 最新 {newest_html}',
               '两条路径读到不同的最新消息，通常又只剩一个分片被读了（%s）' % reason)


def html_newest_stamp(out_dir):
    """Newest 'YYYY-MM-DD HH:MM:SS' stamp rendered into the exported pages."""
    import re
    newest = ''
    try:
        for page in sorted(Path(out_dir).glob('*.html')):
            text = page.read_text(encoding='utf-8', errors='replace')
            stamps = re.findall(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', text)
            if stamps and stamps[-1] > newest:
                newest = stamps[-1]
    except OSError:
        pass
    return newest


def check_session_freshness(report):
    """A read path that silently stops at an old shard shows up here first."""
    _, data = run_json(['sessions', '--limit', '5'])
    sessions = ((data or {}).get('sessions') or [])
    stamps = [s.get('sortTimestamp') or 0 for s in sessions]
    if not stamps or not max(stamps):
        report.add('会话新鲜度', False, '会话列表没有时间戳', 'init 可能未完成')
        return
    newest = max(stamps)
    age = (time.time() - newest) / 86400
    when = datetime.datetime.fromtimestamp(newest).strftime('%Y-%m-%d %H:%M')
    report.add('会话新鲜度', age <= STALE_SESSION_DAYS,
               f'最新会话 {when}（{age:.1f} 天前）',
               f'超过 {STALE_SESSION_DAYS} 天未更新，通常意味着读取只覆盖了部分分片')


def check_export_formats(report, talker):
    """json / txt / excel must each produce a file. html is covered above."""
    with tempfile.TemporaryDirectory(prefix='wf-health-') as tmp:
        for fmt, pattern in (('json', '*.json'), ('txt', '*.txt'), ('excel', '*.xlsx')):
            out = Path(tmp) / fmt
            code, stdout, stderr = run(['export', talker, fmt, '--limit', '3',
                                        '--output', str(out), '--non-interactive'])
            produced = list(out.glob(pattern)) if out.exists() else []
            size = produced[0].stat().st_size if produced else 0
            detail = f'{size} 字节' if produced else (stderr.strip().split('\n')[-1][:60] or f'exit {code}')
            report.add(f'导出格式 {fmt}', code == 0 and size > 0, detail)


def check_python_deps(report):
    """The NT reader is a Python subprocess; a missing module looks like a DB error."""
    required = {
        'sqlcipher3': 'sqlcipher3',
        'html2text': 'html2text',
        'zstandard': 'zstandard',
        'cryptography': 'cryptography',
    }
    missing = []
    for module, package in required.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    report.add('Python 依赖', not missing,
               '齐全' if not missing else f'缺 {", ".join(missing)}',
               'pip install -r requirements.txt')


def check_scheduled_tasks(report):
    """The watcher tasks must exist and their last run must have succeeded.

    **"读不出来"必须与"上次成功"分开**：这一项的判据是从 `schtasks` 的文本输出里找
    `Last Result` / `上次运行结果` 那一行 —— 系统语言或输出格式一变，那一行就找不到了，
    而**两个清单都会是空的**，于是它落到 `else`、报"均在位且上次成功"。
    那正是把"没验证"说成"通过"（同一类：`docs/DECISIONS.md` D-107 里那条"取不到就说未知"）。
    """
    if os.name != 'nt':
        report.add('计划任务', True, '非 Windows，跳过')
        return
    if not shutil.which('schtasks'):
        report.add('计划任务', True, '找不到 schtasks，跳过')
        return
    names = ['WeFlow Issue Watch', 'WeFlow Mail Watch']
    broken, absent, unreadable = [], [], []
    for name in names:
        try:
            proc = subprocess.run(['schtasks', '/query', '/tn', name, '/fo', 'LIST', '/v'],
                                  capture_output=True, timeout=60, encoding='utf-8', errors='replace')
        except (OSError, subprocess.SubprocessError):
            absent.append(name)
            continue
        if proc.returncode != 0:
            absent.append(name)
            continue
        text = proc.stdout or ''
        result = None
        for line in text.split('\n'):
            if 'Last Result' in line or '上次运行结果' in line:
                result = line.split(':', 1)[-1].strip()
                break
        if result is None:
            # 输出里**没有那个字段**：要么语言/格式变了，要么它没在汇报这一项。
            # 这时该说的是"读不出来"，不是"上次成功"。
            unreadable.append(name)
        elif result not in ('0', '0x0'):
            broken.append(f'{name}={result}')
    if broken:
        report.add('计划任务', False, f'上次运行失败: {", ".join(broken)}',
                   '脚本可能抛异常了，手动跑一次看输出')
    elif absent:
        report.add('计划任务', False, f'不存在: {", ".join(absent)}',
                   '提醒不会触发；见 docs/HEALTH-CHECK.md 的注册命令')
    elif unreadable:
        # **不算通过**：这一项没被验证过（不是"上次成功"）
        report.add('计划任务', False, f'读不出上次运行结果: {", ".join(unreadable)}',
                   'schtasks 的输出格式或系统语言变了 —— 这一项**没有验证**，不代表成功；'
                   '手动跑 `schtasks /query /tn "WeFlow Issue Watch" /fo LIST /v` 看一眼')
    else:
        report.add('计划任务', True, '均在位且上次成功')


def check_wechat_client(report):
    """**微信客户端版本**，以及磁盘上有没有"不能用的版本目录"。

    判据在 CLI 那一侧（`src/services/wechatClient.ts`）：版本来自**活进程命令行的
    `--user-lib-dir`** —— 不是 exe 里的版本字面量（实测那只是兜底），也不是"磁盘上有没有
    这个版本目录"（本机就有一个只剩 1 个文件的残留目录，指向它的加载会失败）。

    取不到就写"未知"，**不写"没问题"**：与仓库里那条"没找到必须报 UNKNOWN，别报 CLEAR"同一条纪律。
    字段整个不在 = CLI 是该重建的旧版本 ⇒ **不算通过**（这正是"打开成功≠能用"那一类：
    少了一项却一片绿）。
    """
    _, data = run_json(['check'])
    if not data:
        report.add('微信客户端', False, 'check 未返回 JSON', '先跑 weflow-cli check 看原始输出')
        return
    client = data.get('wechatClient')
    if not isinstance(client, dict):
        report.add('微信客户端', False, 'check 没报这一项',
                   'CLI 可能没重新构建（npm run build），或 dist 是旧的')
        return
    version = client.get('version')
    loaded = client.get('loadedDirs') or []
    if version:
        detail = str(version) + (f'（{loaded[0]}）' if loaded else '')
    else:
        detail = '未知 —— ' + str(client.get('note') or '')
    dead = client.get('unusableVersionDirs') or []
    if dead:
        shown = '、'.join(f'{d.get("dir")}({d.get("files")} 个文件)' for d in dead)
        detail += f'　⚠ 残留版本目录（没有 Weixin.dll，加载会失败）: {shown}'
    # 客户端自报的数据目录 vs 配置推出来的根：不一致 = 两边可能不是同一份库（CLI 那边已按真身比过，
    # 本机数据目录有 D 盘软链，所以这里只报结论）
    if client.get('dataDirMatchesConfig') is False:
        detail += '　⚠ 客户端自报的数据目录与配置里那个数据库推出来的根**不是同一份**'
    crashes = client.get('crashRecords') or {}
    count = int(crashes.get('reports') or 0) + int(crashes.get('records') or 0)
    if count:
        newest = str(crashes.get('newest') or '时间读不到')[:16]
        detail += f'　· 客户端崩溃记录 {count} 份（最近 {newest}）—— 崩过时数据读不出来先看这条'
    # `update.data` 在场 ⇒ 上面那个版本**未必**是加载的那个：这句必须跟着版本一起出现
    for caveat in client.get('caveats') or []:
        detail += f'　⚠ {caveat}'
    report.add('微信客户端', True, detail)


def main():
    parser = argparse.ArgumentParser(description='weflow-cli periodic health check')
    parser.add_argument('--json', action='store_true', help='机器可读输出')
    args = parser.parse_args()

    if not CLI.exists():
        print(f'找不到 {CLI}', file=sys.stderr)
        return 2

    report = Report()
    check_version(report)
    check_environment(report)
    check_wechat_client(report)
    check_python_deps(report)
    check_session_freshness(report)

    talkers = pick_talkers(report)
    if talkers:
        check_read_paths(report, talkers[0])
        check_export_formats(report, talkers[0])

    check_scheduled_tasks(report)

    if args.json:
        print(json.dumps({'success': not report.failed, 'checks': report.rows},
                         ensure_ascii=False, indent=2))
    else:
        print(report.render())
    return 1 if report.failed else 0


if __name__ == '__main__':
    sys.exit(main())
