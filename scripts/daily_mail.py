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
DEFAULT_TOPICS = ('AI', '文学', '学术')
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


def one_line(summary: str, limit=220) -> str:
    text = ' '.join((summary or '').split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + '…'


def topic_digest(topic: str, items, api_key: str) -> str:
    """每个主题一次调用：让它读完全部标题+摘要，写一段综述。失败就返回空串（不影响发信）。"""
    if not api_key:
        return ''
    lines = [f'- {a.get("title")}｜{a.get("source")}｜{one_line(a.get("summary"), 90)}' for a in items]
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


def render(date: str, groups, digests) -> tuple[str, str, int]:
    total = sum(len(v) for v in groups.values())
    text_lines = [f'公众号日报 {date}', f'共 {total} 篇（AI / 文学 / 学术）', '']
    rows = [f'<h1>公众号日报 {date}</h1>',
            f'<p class="meta">共 {total} 篇 · AI / 文学 / 学术（新闻、政治、投资已略）</p>']

    for topic, items in groups.items():
        if not items:
            continue
        text_lines.append(f'=== {topic}（{len(items)} 篇）===')
        rows.append(f'<h2>{html.escape(topic)} <span class="count">{len(items)} 篇</span></h2>')
        digest = (digests or {}).get(topic) or ''
        if digest:
            text_lines.append(digest)
            rows.append(f'<p class="digest">{html.escape(digest)}</p>')
        text_lines.append('')
        for i, a in enumerate(items, 1):
            title = a.get('title') or ''
            source = a.get('source') or ''
            t = (a.get('time') or '')[:5]
            summary = one_line(a.get('summary'))
            url = a.get('url') or ''
            text_lines.append(f'{i}. {title} — {source} {t}')
            if summary:
                text_lines.append(f'   {summary}')
            if url:
                text_lines.append(f'   {url}')
            rows.append(
                '<div class="item">'
                f'<div class="t">{i}. {html.escape(title)}</div>'
                f'<div class="s">{html.escape(source)} · {html.escape(t)} · {html.escape(a.get("relevance") or "")}</div>'
                + (f'<div class="d">{html.escape(summary)}</div>' if summary else '')
                + (f'<div class="u"><a href="{html.escape(url)}">原文</a></div>' if url else '')
                + '</div>'
            )
        text_lines.append('')
    text_lines.append('完整版（含配图）在电脑上打开：')
    text_lines.append(f'  weflow-cli daily-server --date {date} --open')

    page = f"""<!doctype html><html><head><meta charset="utf-8">
<style>
body{{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;line-height:1.6;color:#222;max-width:760px;margin:0 auto;padding:16px}}
h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:17px;margin:28px 0 8px;padding-bottom:6px;border-bottom:1px solid #e5e5e5}}
.meta{{color:#888;font-size:13px;margin:0 0 8px}} .count{{color:#999;font-size:13px;font-weight:400}}
.digest{{background:#f7f7f9;padding:10px 12px;border-radius:6px;font-size:14px;color:#333}}
.item{{margin:14px 0 16px}} .t{{font-weight:600}} .s{{color:#999;font-size:12px;margin:2px 0}}
.d{{font-size:14px;color:#333;margin:4px 0}} .u a{{color:#0b6bcb;font-size:13px;text-decoration:none}}
</style></head><body>
{''.join(rows)}
</body></html>"""
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

    text_body, html_body, total = render(args.date, groups, digests)
    subject = f'公众号日报 {args.date}：AI / 文学 / 学术 {total} 篇'

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
