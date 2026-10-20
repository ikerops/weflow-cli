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


def render(date: str, groups, digests, bodies=None) -> tuple[str, str, int]:
    bodies = bodies or {}
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

        blocks.append(
            '<tr><td style="padding:26px 28px 0;">'
            f'<span style="display:inline-block;background:{tint};color:{accent};'
            'font-size:13px;font-weight:600;padding:5px 12px;border-radius:999px;">'
            f'{html.escape(topic)} · {len(items)} 篇</span></td></tr>'
        )
        if digest:
            blocks.append(
                '<tr><td style="padding:12px 28px 0;">'
                '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                f'style="background:#f8fafc;border-left:3px solid {accent};border-radius:8px;">'
                '<tr><td style="padding:14px 16px;font-size:14px;line-height:1.75;color:#3f4652;">'
                f'{html.escape(digest)}</td></tr></table></td></tr>'
            )

        for i, a in enumerate(items, 1):
            title = a.get('title') or ''
            source = a.get('source') or ''
            when = (a.get('time') or '')[:5]
            rel = a.get('relevance') or ''
            summary = clean_summary(a.get('summary'))
            url = a.get('url') or ''
            repos = github_links(bodies.get(url, ''))
            meta = ' · '.join(x for x in (source, when, rel) if x)

            text_lines.append(f'{i}. {title} — {meta}')
            if summary:
                text_lines.append(f'   {summary}')
            for repo in repos:
                text_lines.append(f'   GitHub: {repo}')
            if url:
                text_lines.append(f'   {url}')

            blocks.append(
                '<tr><td style="padding:18px 28px 0;">'
                f'<div style="font-size:15px;font-weight:600;color:#111827;line-height:1.55;">'
                f'{i}. {html.escape(title)}</div>'
                f'<div style="font-size:12px;color:#9aa3af;margin-top:5px;">{html.escape(meta)}</div>'
                + (f'<div style="font-size:14px;line-height:1.75;color:#3f4652;margin-top:8px;">'
                   f'{html.escape(summary)}</div>' if summary else '')
                + ''.join(
                    f'<div style="margin-top:8px;font-size:13px;">'
                    f'<span style="color:#9aa3af;">GitHub </span>'
                    f'<a href="{html.escape(repo)}" style="color:#0f766e;text-decoration:none;">'
                    f'{html.escape(repo.replace("https://github.com/", "").rstrip("/"))}</a></div>'
                    for repo in repos
                )
                + (f'<div style="margin-top:10px;"><a href="{html.escape(url)}" '
                   'style="color:#2563eb;font-size:13px;text-decoration:none;">阅读原文 &rarr;</a></div>' if url else '')
                + '<div style="border-top:1px solid #f1f5f9;margin-top:18px;"></div>'
                '</td></tr>'
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
    api_key = '' if args.no_digest else get_api_key(cfg)
    digests = {t: topic_digest(t, items, api_key) for t, items in groups.items() if items}

    bodies = body_index(args.date)
    with_repo = sum(1 for a in articles if github_links(bodies.get(a.get('url'), '')))
    print(f'正文含 GitHub 链接: {with_repo} 篇')

    text_body, html_body, total = render(args.date, groups, digests, bodies)
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
