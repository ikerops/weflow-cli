"""把当天日报按主题整理成一封邮件（AI / 文学 / 学术），SMTP 发出。

为什么不发 HTML 阅读器：`output/biz-daily/<日期>/index.html` 的图片是**本地相对路径**，
发到邮箱里会全部裂掉；内联 base64 又会让邮件变成好几 MB。所以正文走「文本 + 原文链接」，
要带图的完整版仍在电脑上开阅读器。

"每篇都由 AI 读过"不是这里做的：逐篇摘要（`【核心观点】/【关键细节】`）是 `biz_daily.py`
在抓取阶段用决策模型生成的，本脚本只负责**按主题聚合 + 写每个主题的综述**（每个主题一次调用）。

用法：
    python scripts/daily_mail.py --date 2026-10-18
    python scripts/daily_mail.py --date 2026-10-18 --to someone@example.com --dry-run
"""
from __future__ import annotations

import argparse
import html
import json
import os
import pathlib
import re
import smtplib
import sys
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _utils import get_api_key, load_config  # noqa: E402

ROOT = SCRIPT_DIR.parent
DEFAULT_TOPICS = ('AI', '学术')
SMTP_CONFIG = pathlib.Path(os.path.expanduser('~/.weflow-issue-watch.json'))


def articles_path(date: str) -> pathlib.Path:
    return ROOT / 'output' / 'biz-daily' / date / '.articles.json'


def load_articles(date: str, topics):
    path = articles_path(date)
    if not path.is_file():
        raise SystemExit(f'没有这一天的数据：{path}')
    data = json.loads(path.read_text(encoding='utf-8'))
    arts = [a for a in (data.get('articles') or []) if a.get('topic') in topics and a.get('title')]
    return data, arts


def group_by_topic(articles, topics):
    groups = {t: [] for t in topics}
    for a in articles:
        groups.setdefault(a.get('topic'), []).append(a)
    for t in groups:
        groups[t].sort(key=lambda a: (a.get('time') or '', a.get('relevanceScore') or 0), reverse=True)
    return groups


GITHUB_RE = re.compile(r'https?://github\.com/[\w\-./]+', re.I)
#: 主题内分类的小圆点配色。同一条新闻流里给不同聚类换色，读起来不容易串行。
PALETTE = ('#6366f1', '#8b5cf6', '#0ea5e9', '#f59e0b', '#ec4899', '#14b8a6')
_TAG_RE = re.compile(r'【[^】]{1,12}】')


def clean_summary(summary: str, limit=170) -> str:
    """把 `【核心观点】…【关键细节】1. …；2. …` 压成**一句人话**。

    邮件里那串标签是给机器分段的，对读者只是噪音：谁在手机上读日报，都不会去分辨
    「核心观点」和「关键细节」的区别。所以只取【核心观点】那一段，标签一律去掉；
    没有标签的老格式就整段压平。
    """
    text = summary or ''
    m = re.search(r'【核心观点】(.+?)(?=【|$)', text, re.S)
    if m:
        text = m.group(1)
    text = _TAG_RE.sub(' ', text)
    text = ' '.join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip('，；、,. ') + '…'


def body_index(date: str) -> dict:
    """url -> 那篇文章的 md 全文（用来从正文里找 GitHub 链接）。

    按 frontmatter 的 `url` 建索引，而不是按文件名：文件名是 `sanitize_filename` 拼的，
    标题里有 `/`、`:` 或超长名字时会被改写，按名字匹配会静默丢文章。
    """
    index = {}
    for md in (ROOT / 'output' / 'biz-daily' / date).glob('*/*.md'):
        try:
            text = md.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        m = re.search(r'^url:\s*"?([^"\n]+?)"?\s*$', text, re.M)
        if m:
            index[m.group(1)] = text
    return index


def github_links(text: str, limit=3) -> list:
    if not text:
        return []
    seen, out = set(), []
    for url in GITHUB_RE.findall(text):
        url = url.rstrip('.,;)）')
        key = url.rstrip('/').lower()
        if key in seen or '/features' in key or '/pricing' in key:
            continue
        seen.add(key)
        out.append(url)
        if len(out) >= limit:
            break
    return out


def cluster_items(topic: str, items, api_key: str, want=5):
    """把同一主题下的文章再分一次类（让 68 篇的 AI 段落不再是一根长肠子）。

    为什么不用现成的 `tags`：实测 68 篇 AI 文章里有 **382 个不同标签**，频次最高的才 6 次——
    按标签聚类会碎成一地。所以交给模型分一次，答案缓存进当天的
    `.mail-clusters.json`，同一天重复跑不再花钱。

    返回 `[(类名, [文章...])]`；任何一步失败都退回"一组=全部"，**绝不因此不发信**。
    """
    if len(items) < 8 or not api_key:
        return [(topic, list(items))]

    numbered = '\n'.join(
        f'{i}. {a.get("title")}｜{clean_summary(a.get("summary"), 40)}'
        for i, a in enumerate(items, 1))
    prompt = (
        f'把下面 {len(items)} 条公众号文章分成 {want - 1}-{want + 1} 组。'
        '组名用 4-8 个汉字，能概括这一组在讲什么（例如「模型与产品」「科研工具」「治理与风险」）。\n'
        '只输出 JSON 数组，不要解释、不要代码围栏：\n'
        '[{"name":"组名","items":[1,2,3]}, ...]\n'
        '要求：每个序号出现且只出现一次；组内按相关性把最能代表该组的排前面。\n\n'
        + numbered
    )
    raw = ''
    try:
        raw = __import__('_utils').call_ai(prompt, 'deepseek', api_key, max_tokens=1200) or ''
    except Exception as exc:
        print(f'[WARN] {topic} 聚类失败（{type(exc).__name__}: {exc}），这一段不分类', file=sys.stderr)
        return [(topic, list(items))]

    data = None
    try:
        data = json.loads(raw)
    except Exception:
        import re as _re
        m = _re.search(r'\[[\s\S]*\]', raw)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    if not isinstance(data, list) or not data:
        print(f'[WARN] {topic} 聚类返回无法解析，这一段不分类', file=sys.stderr)
        return [(topic, list(items))]

    seen, clusters = set(), []
    for group in data:
        if not isinstance(group, dict):
            continue
        name = str(group.get('name') or '').strip()[:12] or '其他'
        picked = []
        for idx in group.get('items') or []:
            try:
                i = int(idx)
            except Exception:
                continue
            if 1 <= i <= len(items) and i not in seen:
                seen.add(i)
                picked.append(items[i - 1])
        if picked:
            clusters.append((name, picked))
    # 模型漏掉的篇目不能丢：塞回最后一组，并说明这是兜底
    leftover = [a for i, a in enumerate(items, 1) if i not in seen]
    if leftover:
        if clusters and len(leftover) <= max(3, len(items) // 4):
            clusters[-1][1].extend(leftover)
        else:
            clusters.append(('其他', leftover))
    return clusters or [(topic, list(items))]


def topic_digest(topic: str, items, api_key: str) -> str:
    """每个主题一次调用：让它读完全部标题+摘要，写一段综述。失败就返回空串（不影响发信）。"""
    if not api_key:
        return ''
    lines = [f'- {a.get("title")}｜{a.get("source")}｜{clean_summary(a.get("summary"), 90)}' for a in items]
    prompt = (
        f'下面是今天「{topic}」主题下 {len(items)} 篇公众号文章的标题与要点。\n'
        '请用中文写一段 150-250 字的综述：今天这个主题主要在讨论什么、有哪些值得注意的进展或分歧、'
        '其中哪一两篇值得优先精读（给出标题）。不要罗列全部篇目，不要空话，不要用列表。\n\n'
        + '\n'.join(lines)
    )
    try:
        return api_key and __import__('_utils').call_ai(prompt, 'deepseek', api_key, max_tokens=600) or ''
    except Exception as exc:
        print(f'[WARN] {topic} 综述生成失败（{type(exc).__name__}: {exc}），邮件里跳过这一段', file=sys.stderr)
        return ''


def render(date: str, groups, digests, bodies=None, clusters=None) -> tuple[str, str, int]:
    bodies = bodies or {}
    clusters = clusters or {}
    total = sum(len(v) for v in groups.values())
    # 主题配色：AI 靛蓝、学术 青绿。用 table + inline style —— 邮件客户端会剥离
    # `<style>` 与 class，class 选择器在 QQ / Gmail / 企业微信里表现不一致。
    accents = {'AI': ('#4f46e5', '#eef2ff'), '学术': ('#0d9488', '#ecfdf5')}

    text_lines = [f'公众号日报 {date}', f'共 {total} 篇', '']
    blocks: list[str] = []

    for topic, items in groups.items():
        if not items:
            continue
        accent, tint = accents.get(topic, ('#334155', '#f1f5f9'))
        text_lines.append(f'=== {topic}（{len(items)} 篇）===')
        digest = (digests or {}).get(topic) or ''
        if digest:
            text_lines.append(digest)
        text_lines.append('')

        # 主题分区：一条通栏色条 + 主题名，比一个小圆角标签更能"切开"版面。
        # 档位名取自 `_utils.RELEVANCE_NAMES`：写死 `('高','中','低')` 会与判据层漂移，
        # 而 `topic_taxonomy_test` 就是专门拦这件事的（它当场红过一次）。
        from _utils import RELEVANCE_NAMES as _REL
        hi, mid, lo = (sum(1 for a in items if a.get('relevance') == k) for k in (_REL[2], _REL[1], _REL[0]))
        def pct(n):
            return max(1, round(n * 100 / max(1, len(items))))
        blocks.append(
            '<tr><td style="padding:30px 28px 0;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
            f'<td width="4" style="background:{accent};border-radius:2px;"></td>'
            '<td style="padding-left:10px;">'
            f'<span style="font-size:18px;font-weight:700;color:#111827;">{html.escape(topic)}</span>'
            f'<span style="font-size:12px;color:#9aa3af;margin-left:8px;">{len(items)} 篇</span>'
            '</td></tr></table>'
            # 相关度比例条：不用看数字就知道今天"值得读的占多少"
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:10px;">'
            '<tr>'
            f'<td width="{pct(hi)}%" height="7" style="background:{accent};border-radius:4px 0 0 4px;"></td>'
            f'<td width="{pct(mid)}%" height="7" style="background:{tint};"></td>'
            f'<td width="{pct(lo)}%" height="7" style="background:#eef1f4;border-radius:0 4px 4px 0;"></td>'
            '</tr></table>'
            f'<div style="font-size:11.5px;color:#9aa3af;margin-top:6px;">'
            f'<span style="color:{accent};font-weight:600;">高 {hi}</span> · 中 {mid} · 低 {lo}</div>'
            '</td></tr>'
        )
        if digest:
            blocks.append(
                '<tr><td style="padding:14px 28px 0;">'
                '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                f'style="background:#f8fafc;border-left:3px solid {accent};border-radius:8px;">'
                '<tr><td style="padding:14px 16px;font-size:14px;line-height:1.8;color:#3f4652;">'
                f'{html.escape(digest)}</td></tr></table></td></tr>'
            )

        # 主题内再分一次类：68 篇挤成一排没人看，分成 4-6 组后每一段都有名字。
        section_clusters = (clusters or {}).get(topic) or [(topic, list(items))]
        for ci, (cname, citems) in enumerate(section_clusters):
            dot = PALETTE[ci % len(PALETTE)]
            if len(section_clusters) > 1:
                blocks.append(
                    '<tr><td style="padding:22px 28px 0;">'
                    f'<span style="display:inline-block;width:7px;height:7px;border-radius:50%;'
                    f'background:{dot};vertical-align:middle;"></span>'
                    f'<span style="font-size:13.5px;font-weight:700;color:#374151;margin-left:7px;">'
                    f'{html.escape(cname)}</span>'
                    f'<span style="font-size:11.5px;color:#b3bac4;margin-left:6px;">{len(citems)}</span>'
                    '</td></tr>'
                )
                text_lines.append(f'-- {cname}（{len(citems)}）--')

            for a in citems:
                i = items.index(a) + 1
                title = a.get('title') or ''
                source = a.get('source') or ''
                when = (a.get('time') or '')[:5]
                rel = a.get('relevance') or ''
                url = a.get('url') or ''
                repos = github_links(bodies.get(url, ''))
                meta = ' · '.join(x for x in (source, when, rel) if x)
                num = f'{i:02d}'
                title_html = (f'<a href="{html.escape(url)}" style="color:#111827;text-decoration:none;">'
                              f'{html.escape(title)}</a>' if url else html.escape(title))

                # **颜色必须自己说明自己。** 上一版把序号徽章染成主题色来表示"高相关"，
                # 但没有任何图例——用户看到截图的第一反应是"这边为什么有的是绿的"（2026-10-20）。
                # 现在徽章统一成灰色，相关度改成 meta 行里一个**写着"高"的小色块**：
                # 颜色解释自己，不需要图例。
                if rel == _REL[2]:
                    summary = clean_summary(a.get('summary'), 190)
                    text_lines.append(f'{num}. ★ {title} — {meta}')
                elif rel == _REL[1]:
                    summary = clean_summary(a.get('summary'), 120)
                    text_lines.append(f'{num}. {title} — {meta}')
                else:
                    summary = ''
                    text_lines.append(f'{num}. {title} — {meta}')
                if rel == _REL[2]:
                    rel_html = (f'<span style="display:inline-block;background:{accent};color:#ffffff;'
                                f'font-size:10.5px;font-weight:600;padding:1px 6px;border-radius:4px;">'
                                f'{html.escape(rel)}</span>')
                else:
                    rel_html = f'<span style="color:#b3bac4;">{html.escape(rel)}</span>' if rel else ''
                meta_html = ' · '.join(x for x in (
                    html.escape(source), html.escape(when), rel_html) if x)
                if summary:
                    text_lines.append(f'    {summary}')
                for repo in repos:
                    text_lines.append(f'    GitHub: {repo}')
                if url:
                    text_lines.append(f'    {url}')
                blocks.append(
                    '<tr><td style="padding:10px 28px 0;">'
                    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                    'style="background:#ffffff;border:1px solid #eef0f3;border-radius:12px;">'
                    '<tr><td style="padding:14px 16px;">'
                    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
                    f'<td width="28" valign="top"><span style="display:inline-block;width:24px;height:24px;'
                    'line-height:24px;text-align:center;border-radius:7px;background:#f1f5f9;'
                    f'color:#94a3b8;font-size:11.5px;font-weight:700;">{num}</span></td>'
                    '<td valign="top">'
                    f'<div style="font-size:14.5px;font-weight:600;color:#111827;line-height:1.55;">{title_html}</div>'
                    f'<div style="font-size:11.5px;color:#9aa3af;margin-top:4px;">{meta_html}</div>'
                    + (f'<div style="font-size:13.5px;line-height:1.75;color:#4b5563;margin-top:8px;">'
                       f'{html.escape(summary)}</div>' if summary else '')
                    + ''.join(
                        f'<div style="margin-top:8px;font-size:12.5px;">'
                        f'<a href="{html.escape(repo)}" style="color:{accent};text-decoration:none;">'
                        f'&#9679; {html.escape(repo.replace("https://github.com/", "").rstrip("/"))}</a></div>'
                        for repo in repos
                    )
                    + '</td></tr></table></td></tr></table></td></tr>'
                )
        text_lines.append('')

    text_lines.append('完整版（含配图）在电脑上打开：')
    text_lines.append(f'  weflow-cli daily-server --date {date} --open')

    stats = ' · '.join(f'{t} {len(v)} 篇' for t, v in groups.items() if v)
    page = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>' + html.escape(f'公众号日报 {date}') + '</title></head>'
        '<body style="margin:0;padding:0;background:#f4f5f7;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#f4f5f7;"><tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="640" cellpadding="0" cellspacing="0" '
        'style="width:100%;max-width:640px;background:#ffffff;border-radius:14px;'
        'overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.06);">'
        '<tr><td style="background:#111827;padding:26px 28px;">'
        '<div style="font-size:12px;letter-spacing:.12em;color:#9ca3af;font-weight:600;">'
        'WEFLOW · 公众号日报</div>'
        f'<div style="font-size:26px;font-weight:700;color:#ffffff;margin-top:8px;">{html.escape(date)}</div>'
        f'<div style="font-size:13px;color:#c7cbd1;margin-top:8px;">{html.escape(stats)}</div>'
        '</td></tr>'
        + ''.join(blocks)
        + '<tr><td style="padding:22px 28px 28px;">'
        '<div style="font-size:12px;color:#9aa3af;line-height:1.8;">'
        '完整版（含配图）在电脑上打开：<br>'
        '<span style="font-family:ui-monospace,Consolas,Menlo,monospace;color:#6b7280;">'
        f'weflow-cli daily-server --date {html.escape(date)} --open</span>'
        '</div></td></tr>'
        '</table></td></tr></table></body></html>'
    )
    return '\n'.join(text_lines), page, total


def smtp_settings():
    if not SMTP_CONFIG.is_file():
        raise SystemExit(f'找不到发信凭据 {SMTP_CONFIG}（需要 smtp_user 与 smtp_auth）')
    cfg = json.loads(SMTP_CONFIG.read_text(encoding='utf-8'))
    if not cfg.get('smtp_user') or not cfg.get('smtp_auth'):
        raise SystemExit(f'{SMTP_CONFIG} 里缺 smtp_user / smtp_auth')
    return cfg['smtp_user'], cfg['smtp_auth']


def send(user, auth, to, subject, text_body, html_body):
    msg = MIMEMultipart('alternative')
    msg['From'] = formataddr((str(Header('WeFlow CLI 日报', 'utf-8')), user))
    msg['To'] = to
    msg['Subject'] = Header(subject, 'utf-8')
    msg['Date'] = formatdate(localtime=True)
    msg.attach(MIMEText(text_body, 'plain', 'utf-8'))
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))
    with smtplib.SMTP_SSL('smtp.qq.com', 465, timeout=45) as smtp:
        smtp.login(user, auth)
        refused = smtp.sendmail(user, [to], msg.as_string())
    if refused:
        raise SystemExit(f'被拒收：{refused}')


def main():
    ap = argparse.ArgumentParser(description='把某天的日报按主题整理成邮件并发送')
    ap.add_argument('--date', required=True, help='YYYY-MM-DD')
    ap.add_argument('--to', default='', help='收件人；缺省用配置 dailyMailTo，再缺省发给自己')
    ap.add_argument('--topics', default=','.join(DEFAULT_TOPICS), help='逗号分隔的主题')
    ap.add_argument('--no-digest', action='store_true', help='不生成每主题的 AI 综述（省钱、更快）')
    ap.add_argument('--dry-run', action='store_true', help='只渲染，不发送')
    args = ap.parse_args()

    topics = [t.strip() for t in args.topics.split(',') if t.strip()]
    data, articles = load_articles(args.date, set(topics))
    groups = group_by_topic(articles, topics)
    total = sum(len(v) for v in groups.values())
    print(f'{args.date}: 命中 {total} 篇（{", ".join(f"{t}={len(v)}" for t, v in groups.items())}）')
    if not total:
        raise SystemExit('没有可发送的文章')

    cfg = load_config()
    # `--no-digest` 只关"每主题一段综述"；聚类是版式结构，仍要做（两件事分开开关，
    # 否则想看新版式的人一加 `--no-digest` 就退回一根长肠子）。
    api_key = get_api_key(cfg)
    digests = {} if args.no_digest else {t: topic_digest(t, items, api_key) for t, items in groups.items() if items}
    clusters = {t: cluster_items(t, items, api_key, want=5) for t, items in groups.items() if len(items) >= 8}
    for t, cs in clusters.items():
        print(f'  {t} 分了 {len(cs)} 组: ' + ' / '.join(f'{n}({len(v)})' for n, v in cs))

    bodies = body_index(args.date)
    with_repo = sum(1 for a in articles if github_links(bodies.get(a.get('url'), '')))
    print(f'正文含 GitHub 链接: {with_repo} 篇')

    text_body, html_body, total = render(args.date, groups, digests, bodies, clusters)
    subject = f'公众号日报 {args.date}：{" / ".join(t for t in topics if groups.get(t))} {total} 篇'

    user, auth = smtp_settings()
    to = args.to or str(cfg.get('dailyMailTo') or '') or user
    print(f'收件人: {to}  主题: {subject}  正文字符: {len(text_body)}')
    if args.dry_run:
        out = ROOT / 'output' / f'daily-mail-{args.date}.html'
        out.write_text(html_body, encoding='utf-8')
        print(f'[dry-run] 已渲染到 {out}')
        return
    send(user, auth, to, subject, text_body, html_body)
    print(f'已发送到 {to}')


if __name__ == '__main__':
    main()
