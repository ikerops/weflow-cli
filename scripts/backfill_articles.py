#!/usr/bin/env python3
"""历史文章回填 — 把过去几个月的公众号文章抓成正文，喂给知识库。

用法:
  python scripts/backfill_articles.py --since 2026-03-01 --until 2026-08-31 --dry-run --json
  python scripts/backfill_articles.py --since 2026-03-01 --until 2026-08-31 --workers 12 --yes
  python scripts/backfill_articles.py --since 2025-09-05 --until 2026-03-02 --topic AI --yes

## 为什么不是直接跑 `pipeline run --date`

三条实测出来的理由，都不是推测：

1. **`pipeline run` 有硬编码的 10 分钟超时**（`bin/weflow-cli.ts:3906` 的 `timeout: 600_000`）。
   那是给"当天日报"那条交互路径设的；一天 100+ 篇历史文章抓不完就被它掐死，产出为零。
2. **`biz_daily` 是逐篇串行抓的，而且每篇都下图片**。实测试点 25 分钟只处理了约 47 篇
   （≈30 秒/篇，大头在 13–21 张配图），29,667 篇要 150 小时以上。
3. **知识库用不到图片**。下游 `create_reading_notes` 只读 md 的 frontmatter（title/source/
   url/topic/date）与正文前 10 行；`compile_wiki` 只认 `## AI 摘要` 那一段。图片一张都不读。

所以本脚本只做三件与 `biz_daily` 不同的事：**并发抓**、**不下图片**、**不落 HTML 阅读器**。
取数、主题归一化、md 格式、`.articles.json` 的字段全部复用 `biz_daily` 里那几个函数，
不另写一份——两份写同一件事就会分叉，本仓库已经因为这类分叉吃过亏（见 `_normalize_topics`
的注释）。

## 一条必须说清的限制

**图片型文章抓不到正文。** 实测 3/4、7/4 两天：27/40、44/60 有正文，其余是图文消息
（内容全在图片里），清洗掉图片与链接后不足 100 字，与 `biz_daily` 一样被跳过。这不是
抓取失败，是那类文章本来就没有文字。所以"2 万篇"这个量级是有正文的部分，不是总量。

## `--topic`：只回填一部分主题（先筛后抓）

`--topic AI` 用的就是本脚本自己判主题的那套 `_guess_topic`（只看标题与号名、**不看正文**），
所以它筛出来的 AI 集合，与全量跑完之后落在 `AI/` 目录里的那批**逐个相同** —— 不是另立一套
口径，只是省掉了其余主题的抓取（实测 2025-09-05~2026-03-02 那段：全量 10,880 篇里 AI 占
1,759 篇，抓取量约 1/6）。

**代价必须写出来：那一天就不再是完整的一天。** 所以 `.articles.json` 里会多一个 `topicFilter`
（README 里也写明），`day_done` 据此在之后跑**全量**时**不会**跳过这一天。少了这一步，那天
其余五个主题的文章会被永久漏掉、而且不报错 —— 文件在、json 在、"已回填"看起来处处成立。
"""
import argparse
import hashlib
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _utils import load_config  # noqa: E402
from biz_daily import (DEFAULT_RELEVANCE, DEFAULT_TOPIC, TOPICS,  # noqa: E402
                       _group_by_topic, _guess_topic, _serializable_article,
                       _tags_for_write, extract_article_info, fetch_article,
                       fetch_article_cached, get_db_keys, sanitize_filename,
                       summary_section)
from _utils import strip_wx_ads_document, write_with_frontmatter  # noqa: E402

TZ = timezone(timedelta(hours=8))
OUTPUT_ROOT = 'output/biz-daily'
# Vault 里"原始素材"那一层的落点，与 `pipeline.py` 用的是同一个（但那边整份拷含图片）
VAULT_SOURCES = 'output/wechat-vault/Sources/WeChat'
# 与 biz_daily 的 `内容过短` 闸门同一个阈值。换成别的数会让两条路对"哪些文章算有正文"
# 给出不同答案——那正是本仓库不许再出现的分叉。
MIN_BODY_CHARS = 100
# 支付/服务通知不是文章，biz_daily 在这张表上过滤过一次，这里沿用同一张表。
SKIP_TITLE_PREFIXES = ('已支付', '已扣费', '支付成功', '扣费预通知', '你已关闭', '下单成功')


def body_without_media(markdown: str) -> str:
    """去掉图片与链接后的正文——判"够不够长"用的是它，不是原文。

    与 `biz_daily` 的清洗口径一致（去图、去链、去换行后再数字数）。
    """
    text = ''.join(str(markdown or '').split('\n'))
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    text = re.sub(r'\[.*?\]\(.*?\)', '', text)
    return text.strip()


def collect_articles(cursor, users, name_map, day):
    """某一天库里所有文章（还没抓正文）。字段与 biz_daily 的 Phase 1 一致。"""
    start = int(datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=TZ).timestamp())
    end = start + 86400
    out = []
    for user in users:
        table = 'Msg_' + hashlib.md5(user.encode()).hexdigest()
        try:
            rows = cursor.execute(
                'SELECT create_time, message_content FROM "%s" '
                'WHERE create_time >= ? AND create_time < ? ORDER BY create_time' % table,
                (start, end)).fetchall()
        except Exception:
            continue
        for create_time, content in rows:
            if not content:
                continue
            info = extract_article_info(content)
            if not info['title']:
                continue
            if any(info['title'].startswith(p) for p in SKIP_TITLE_PREFIXES):
                continue
            entry = {
                'account': user,
                'account_name': name_map.get(user, user),
                'title': info['title'],
                'digest': info['digest'],
                'url': info['url'],
                'cover': info['cover'],
                'local_text': info['local_text'],
                'time': datetime.fromtimestamp(create_time, tz=TZ).strftime('%H:%M'),
                'timestamp': create_time,
            }
            out.append(entry)
    out.sort(key=lambda a: a['timestamp'])
    return apply_topics(out)


def apply_topics(articles):
    """给每篇定主题（关键词），**就地**改并返回。

    主题必须在这里定，**不能留给下游兜底**：`_group_by_topic` 对没有 topic 的文章一律
    折成 `DEFAULT_TOPIC`，于是整批会静默落进同一个目录。这条踩过——第一次跑完 3/5，
    106 篇 100% 落在 `学术/`，而当时的输出里只写着一句"主题兜底 113 篇"。

    用 `_guess_topic`（关键词），这是 `biz_daily` 在 AI 分类失败时走的同一条退路；
    本脚本不调模型，所以它是这里唯一可用的那条。
    """
    for article in articles:
        if article.get('topic') not in TOPICS:
            article['topic'] = _guess_topic(article)
    return articles


def parse_topics(values):
    """把 `--topic` 的取值展开成一个列表：**可重复，也可逗号分隔**。

    两种都收的理由是仓库里另一条命令（`article_notes.py`）的 `--topic` 就是 `action='append'`
    且写明"可重复或逗号分隔"。只认一种的话，照另一边习惯写就会**静默只做一个主题**。

    `values` 传字符串也当单个处理：`for chunk in 'AI'` 会**逐字符**迭代，而每个字符都不在分类法里
    —— 于是"整批被拒"看起来跟"参数拼错"一模一样，很难查。这里把它挡住。
    """
    if isinstance(values, str) or values is None:
        values = [values or '']
    return [t.strip() for chunk in values for t in str(chunk).split(',') if t.strip()]


def select_topics(articles, topics):
    """只留下选中主题的文章；`topics` 为空/None 表示全要（原样返回）。

    必须在 `apply_topics` **之后**调：主题是那个函数的输出。顺序对"选中哪些"没有影响
    （`_guess_topic` 逐篇独立、只看标题与号名），所以筛出来的集合与全量跑完落在对应主题
    目录里的那批逐个相同。
    """
    if not topics:
        return articles
    return [a for a in articles if a.get('topic') in topics]


def fetch_bodies(articles, workers: int, log=None, use_cache: bool = True):
    """并发把这批文章的正文抓回来，写进 `fetched_md`。

    **只抓正文，不碰图片**——`fetch_article` 返回的 markdown 里图片是链接，本脚本
    就此打住，不再像 `biz_daily` 那样逐张下到本地（那一步占了实测 30 秒/篇里的绝大部分）。

    **走 `fetch_article_cached`（日报那条同一个缓存）**，理由是把"补历史"和"正日报"
    接起来：补完的正文落在 `output/.cache/fetch/<md5(url)>.md`，随后逐天
    `daily --date X` 全是缓存命中——而日报的 8–12 秒节流在它的**调用循环**里
    （`biz_daily` 抓取那一段），命中缓存既不联网也不睡，所以正式路径能快速产出
    与平常完全一样的完整日报（含摘要与配图）。反过来也一样：日报跑过的天，这里重抓
    时直接命中，不会再去打扰上游。

    `use_cache=False`（`--no-fetch-cache`）**只跳过"读"**：仍然照常联网，成功结果照常
    写回缓存。这样"强制重抓"既是"这次不看旧缓存"，也是"把旧缓存刷新成新的"。
    **失败不写缓存**（由 `fetch_article_cached` 保证），所以重抓失败不会把已有的有效
    缓存清掉或弄脏。
    """
    todo = [a for a in articles
            if len(body_without_media(a.get('local_text'))) < MIN_BODY_CHARS and a.get('url')]
    if not todo:
        return {'attempted': 0, 'ok': 0, 'failed': 0}

    def one(article):
        try:
            body, _hit = fetch_article_cached(article['url'], use_cache=use_cache)
        except Exception:
            return False
        if body:
            article['fetched_md'] = body
            return True
        return False

    ok = 0
    with ThreadPoolExecutor(max_workers=max(workers, 1)) as pool:
        for result in pool.map(one, todo):
            ok += 1 if result else 0
    failed = len(todo) - ok
    if log:
        log('    抓正文: 尝试 %d，成功 %d%s，并发 %d'
            % (len(todo), ok, ('，失败 %d' % failed) if failed else '', workers))
    return {'attempted': len(todo), 'ok': ok, 'failed': failed}


def _read_index(day_dir) -> dict:
    """读这一天已有的 `.articles.json`（没有就空表）。

    **读不了的时候不是安静地返回空表**：那等于把已有条目从索引里抹掉，而这件事不会报错
    —— 正是这一整段要防的事。所以把原文件改名留成 `.json.bad` 再继续，至少还能人工救。
    """
    path = Path(day_dir) / '.articles.json'
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:
        try:
            path.rename(path.with_name('.articles.json.bad'))
        except OSError:
            pass
        print('    警告：%s 读不了（%s），已留成 .articles.json.bad，旧索引未参与合并'
              % (path.name, exc))
        return {}


def _merge_articles(kept, fresh):
    """合并两组 `.articles.json` 条目，按「来源-标题」去重（那是它在盘上的文件名）。

    顺序按 `time` 再 `title` 排，是为了让同一天的索引**稳定**：分两次回填的那天，
    第二次不该把先前的条目挤到别处去，否则每次跑都产生一份看着像"改了"的索引。
    没有 `time` 的老条目（理论上不会有）排在前面，不参与排序。
    """
    merged = {}
    for entry in list(kept) + list(fresh):
        if not isinstance(entry, dict):
            continue
        merged[(str(entry.get('source', '')), str(entry.get('title', '')))] = entry
    return sorted(merged.values(), key=lambda e: (str(e.get('time', '')), str(e.get('title', ''))))


def write_day(articles, day, out_root, topic_filter=None, truncated=False, fetch_failed=0):
    """落盘：`<日期>/<主题>/<来源>-<标题>.md` + `.articles.json` + README。

    格式与 `biz_daily` 一致——`create_reading_notes` 与 `compile_wiki` 都按那个形状读。
    正文那段写 `## AI 摘要`（内容用文章自带的 digest）：**本脚本不调模型**，所以这里
    放的只能是原文已有的东西，不能假装是生成的摘要。

    传了 `topic_filter`（`--topic`）时，这里会**再筛一次**并在元数据里留下 `topicFilter`：

    - 筛在**这里**做，是为了让标记不可能撒谎。如果筛只发生在调用方（`main`），那么有人
      漏调那一步时，`write_day` 会把六个主题全写出去、同时盖上"只回填了 AI"的章 ——
      一个自称只回填了一半、实际写满了的目录，比不写标记更坏。
    - README 里也写明这一天是哪几个主题。**这两处不是装饰**：一天只回填了一半却看起来像
      完整的一天，是这份产出唯一会"静默变少"的方式（见 `day_done`）。
    - 传了 `truncated`（`--limit-per-day` 截过）时同理，多一个 `truncated` 标记。
    - **索引是与这一天已有的内容合并的，不是覆盖**：先 `--topic AI` 再来 `--topic 学术`，
      先前那个主题的条目必须留着。覆盖会让**索引比盘上少几条**而 md 还在 —— 不报错，
      而"索引与盘不一致"正是这一支反复吃的那个形状（见 `_merge_articles`、`_read_index`）。
      `topicFilter` 取并集；只要这次是全量、或这天本来就是完整的，标记就**消失**。

    `truncated` 只影响元数据（它记录的是"调用方截过"，不宜在这里重算——截在调用方发生）。
    """
    articles = select_topics(articles, topic_filter)
    groups, fallbacks = _group_by_topic(articles)
    out_dir = Path(out_root) / day
    written, skipped = [], []
    for topic in TOPICS:
        for article in groups[topic]:
            markdown = article.get('fetched_md') or article.get('local_text') or ''
            if len(body_without_media(markdown)) < MIN_BODY_CHARS:
                skipped.append({'title': article['title'][:30], 'reason': '正文不足 %d 字' % MIN_BODY_CHARS})
                continue
            safe = sanitize_filename('%s-%s' % (article['account_name'], article['title']))
            target = out_dir / topic / (safe + '.md')
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = article.get('digest') or ''
            frontmatter = {
                'title': '"%s"' % article['title'],
                'source': '"%s"' % article['account_name'],
                'date': day,
                'topic': topic,
                'relevance': article.get('relevance', DEFAULT_RELEVANCE),
                'tags': _tags_for_write(article, topic),
                'created': day,
                # 来源标记：正文是回填脚本抓的，不是当天日报那条路
                'backfilled': 'true',
            }
            if article.get('url'):
                frontmatter['url'] = '"%s"' % article['url']
            body = [
                '# %s\n' % article['title'],
                '> 来源：%s  ' % article['account_name'],
                '> 时间：%s %s  ' % (day, article.get('time', '')),
                '> 原文：[阅读原文](%s)\n' % article.get('url', ''),
                '\n---\n',
                summary_section(digest).rstrip('\n'),
                '\n---\n',
                markdown,
            ]
            # 序列化**用 _utils 里那一份**，不另写：frontmatter 的引号规则（含 `:`/`#`/`"`
            # 才加引号、列表写成 `[a, b]`）在两边各写一遍就会分叉，而 `parse_frontmatter`
            # 只认其中一种。
            write_with_frontmatter(str(target), frontmatter, '\n'.join(body))
            written.append(article)
    out_dir.mkdir(parents=True, exist_ok=True)
    serializable = [_serializable_article(a, day) for a in written]
    # **索引描述的是这一天盘上全部的文章，不是这一轮写的那批。** 分两次、用不同主题回填同一天
    # 时（`--topic AI` 之后再来 `--topic 学术`），只写本轮的话，先前那个主题的条目会从索引里
    # 消失而 md 还留在盘上——索引少一条、盘上多一篇，不报错。所以先把不属于本轮的旧条目接过来。
    prior = _read_index(out_dir)
    kept = [e for e in prior.get('articles', [])
            if e.get('topic') not in set(topic_filter or ())]
    stored_filter = prior.get('topicFilter')
    # 「**没有**旧索引」与「旧索引是**完整**的一天」是两件事，第一版把它们混成了一个 `None`：
    # 于是第一次做过滤回填时标记根本没写，那天看起来像完整的一天 —— 正是要防的那件事。
    # 区分它们的是"旧索引里有没有东西"，不是"有没有那个键"。
    if topic_filter is None:
        scope_filter = None                       # 全量跑过 → 这天完整
    elif not prior.get('articles'):
        scope_filter = sorted(topic_filter)       # 第一次写这天，且只写了一部分
    elif stored_filter is None:
        scope_filter = None                       # 本来就完整，这次只是重做其中一部分
    else:
        scope_filter = sorted(set(stored_filter) | set(topic_filter))
    if scope_filter and set(scope_filter) >= set(TOPICS):
        # 六个主题都做过一遍了 —— 这一天"能做的都做了"，与全量跑过没有区别。
        # 不撤标记的话它**永远**被当成没做完：以后随便跑一次全量都会把整个窗口重抓一遍。
        scope_filter = None
    payload = {'date': day, 'generated_at': datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S'),
               'backfilled': True, 'articles': _merge_articles(kept, serializable)}
    # 只在**确实是一部分**的时候才加这两个键：全量回填写出来的文件与以前逐字节相同，
    # 而 `day_done` 把"没有 topicFilter"读作"完整的一天"。缺了它就等于把半天的产出
    # 冒充成整天——这正是这条路径唯一能静默丢东西的地方。
    if scope_filter:
        payload['topicFilter'] = scope_filter
    if truncated or prior.get('truncated'):
        # `truncated` 一旦记上就不撤销：这一次没截，不代表上一次截掉的那些补回来了。
        payload['truncated'] = True
    # **抓取失败要与"正文本来就短"分开记**，否则一句"跳过 N 篇（正文太短）"把两件事混成
    # 一件：前者这一轮可重试、后者是文章自己的属性。这个标记让 `day_done` 把这一天判成
    # **没做完**，于是下次重跑会再试一遍——正文命中缓存的那批不花钱，只重试失败的。
    #
    # 它**不是 sticky 的**（与 `truncated` 不同）：全量跑完一轮、一篇都没失败，就说明
    # 上一次失败的那些也补齐了，标记该消失。但**局部**跑（`--topic`）只覆盖一部分文章，
    # 它没重试到的那些（别的主题）还在，所以那一档只保证"至少还有这么多"，取两轮的较大者。
    carried_failed = int(prior.get('fetchFailed') or 0)
    failed_total = fetch_failed if topic_filter is None else max(fetch_failed, carried_failed)
    if failed_total:
        payload['fetchFailed'] = failed_total
    (out_dir / '.articles.json').write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    if scope_filter:
        scope = ('**只回填了 %s 主题** —— 这一天并非全部文章，其余主题还没拉。\n'
                 % '、'.join(scope_filter))
    else:
        scope = ''
    # README 说的是**这一天**有几篇（合并后的），不是这一轮写了几篇 —— 分两次回填的那天，
    # 前者才是读它的人想要的数。
    failed_note = ''
    if failed_total:
        # 让人**看得见**这天没做完：README 是这个目录里唯一给人看的文件，
        # 只写"共 N 篇"会让"少了 N 篇"读起来像"这天就这些文章"。
        failed_note = ('\n其中 **%d 篇正文没抓到**（网络/来源问题），这一天不算完成；'
                       '重跑会只重试这些篇（其余走缓存）。\n' % failed_total)
    (out_dir / 'README.md').write_text(
        '# 公众号文章 — %s（历史回填）\n\n共 %d 篇。正文由 `backfill_articles.py` 抓取，'
        '**未下载图片、未调用模型**。\n%s%s' % (day, len(payload['articles']), scope, failed_note),
        encoding='utf-8')
    return {'written': len(written), 'skipped': len(skipped),
            'fallbackTopics': fallbacks, 'skipSample': skipped[:3],
            'fetchFailed': failed_total}


def copy_to_vault(day: str, out_root: str, vault_root: str) -> dict:
    """把这一天的文章 md 拷进 Vault 的 `Sources/WeChat/<日期>/`。**只拷 md，不下图片。**

    `pipeline.py` 那条路是 `copytree` 整份拷（含图片）：一天约 200 MB，165 天约 33 GB。
    知识库那条线**一张图都不读**（下游只读 md 的 frontmatter 与正文），所以这里只拷
    `.md`，约 14 KB/篇、165 天约 0.4 GB。

    **为什么非要补这一步**：`Sources/` 是"原始素材"那一层（见 `user_notes.py` 的说明），
    而 `002_Literature/` 是读后笔记。只写后者的话，Vault 里"读过什么"是全的、"原文在哪"
    却是残缺的——2026-09-27 用户就是这么发现的：165 天的 `Sources` 根本没有，
    因为回填时我只在对话里说了一句"跳过了"，没在库里留下任何痕迹。

    逐 md 拷而不是整目录替换：`copytree` 的语义是"删掉再放"，而这里要的是**补缺**——
    Vault 是用户的东西，不能因为要补一天就把那天底下别的东西清掉。

    拷过去的是**洗过的正文**（`strip_wx_ads_document`：删微信界面短语，frontmatter 与代码块
    不动）。用户 2026-09-28 定的就是"原文与笔记都清"——`Sources/` 是给人读的那一层，
    "继续滑动看下一个"这种按钮文字搜出来只会添乱。清洗落在这里的理由与代价写在循环那段。
    """
    source = Path(out_root) / day
    target = Path(vault_root) / day
    if not source.is_dir():
        return {'copied': 0, 'reason': '没有这天的产出'}
    copied = 0
    for path in source.rglob('*.md'):
        rel = path.relative_to(source)
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, encoding='utf-8', newline='') as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError):
            # 读不成文本就照原样拷：宁可库里有几篇没洗的，也不要因为一篇读不了就整天不补。
            dest.write_bytes(path.read_bytes())
            copied += 1
            continue
        # **在这里洗，而不是在 fetch 那一步**：`biz-daily` 是**原始存档**（洗了就再也拿不回
        # 原文），`Sources/` 才是给人读的那一层。代价是两边不再逐字节相同 —— 所以这一步必须
        # 落在拷贝函数里，否则任何一次补拷都会把洗过的又覆盖回去（2026-09-28 就这么发生过一次）。
        with open(dest, 'w', encoding='utf-8', newline='') as handle:
            handle.write(strip_wx_ads_document(text))
        copied += 1
    return {'copied': copied, 'reason': ''}


def days_with_articles(out_root) -> list:
    """`biz-daily` 下**真的有文章**的那些天（升序）。

    判据是"那天底下有 md"，**不是** `.articles.json` 里的 `backfilled` 标记。这两个判据
    实测不等价：2026-09-28 量过，199 天里有 12 天有文章却没有那个标记（`.articles.json`
    是 `daily` 那条路写的，不写这个键），其中 08-24/08-25 两天在 Vault 里**根本没有**对应
    目录 —— 拿标记当判据，这两天既不会被补也不会被报出来。

    只在**扫到的目录里**判断，不去查库、不联网：补拷要能在离线时跑。
    """
    root = Path(out_root)
    days = []
    for directory in sorted(root.iterdir()) if root.exists() else []:
        if not directory.is_dir():
            continue
        if any(p.name != 'README.md' for p in directory.rglob('*.md')):
            days.append(directory.name)
    return days


def day_done(out_root, day, topics=None) -> bool:
    """这一天**我们要的那些主题**已经回填过了吗（增量：免得重跑把同样的文章再抓一遍）。

    `topics=None` 表示"全量"。**判据不只是"写过"**，还要看写的是不是完整的一天：

    - 只回填过 AI 的一天，`.articles.json` 里带着 `topicFilter`。此后跑**全量**时必须
      **不能**被跳过 —— 否则那天其余五个主题的文章会被永久漏掉，而且不报错：文件在、
      `.articles.json` 在、"已回填"看起来成立。这就是 `days_with_articles` 那条注释里
      记的同一类事故（拿一个不完整的判据当"做完了"）。
    - 反向：`--topic AI` 之后又跑 `--topic AI,学术`，存过的是子集，也要重做；
      而存过的**覆盖**了这次要的（或那天本来就完整），才算做完。
    - 被 `--limit-per-day` 截过的一天**从来不算做完**：截掉的那些既没抓、也看不出来。
    """
    path = Path(out_root) / day / '.articles.json'
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return False
    if not payload.get('articles'):
        return False
    if payload.get('truncated'):
        return False
    if payload.get('fetchFailed'):
        # 上一次这天有文章**抓取失败**（不是"正文本来就短"）。不算做完 ⇒ 下次重跑会再试。
        # 不计这一条的话，一次网络抖动就会被"这天已回填"永久埋掉：文件在、索引在、md 也有，
        # 少的那几篇谁都看不出来。仓库里量过同类事故：84 次失败里 28 次重试就能拿到。
        return False
    stored = payload.get('topicFilter')
    if stored is None:
        return True            # 完整的一天，所有主题都在
    if set(stored) >= set(TOPICS):
        # 六个主题在标记里都在 = 这天完整（`write_day` 现在会直接把标记撤掉，但**旧代码写的**
        # 天数带着这个形态留在盘上 —— 读的判据也认它，免得那几天永远"没做完"）。
        return True
    if topics is None:
        return False           # 只要过一部分，全量的活还没干
    return set(stored) >= set(topics)


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='历史文章回填（只抓正文，不下图片、不调模型）')
    parser.add_argument('--since', required=True, help='起始日期 YYYY-MM-DD（含）')
    parser.add_argument('--until', required=True, help='结束日期 YYYY-MM-DD（含）')
    parser.add_argument('--out', default=OUTPUT_ROOT, help='输出根目录')
    parser.add_argument('--vault-root', default=VAULT_SOURCES, help='Vault 的 Sources/WeChat 目录')
    parser.add_argument('--vault-copy', action='store_true',
                        help='回填时顺带把文章 md 拷进 Vault 的 Sources（只拷 md，不下图片）')
    parser.add_argument('--vault-sync', action='store_true',
                        help='只做这件事：把已回填的天补拷进 Vault 的 Sources（本地，不抓取、不调模型）')
    parser.add_argument('--workers', type=int, default=8, help='抓正文的并发数（默认 8）')
    parser.add_argument('--limit-per-day', type=int, default=0,
                        help='每天最多抓几篇（0=不限）。设了就不是完整的一天：'
                             '截掉的那些既没抓也看不出来，所以增量判断会重做这天')
    parser.add_argument('--topic', action='append', default=[], metavar='T',
                        help='只回填这些主题（可重复或逗号分隔，合法值 %s），不写=全部。'
                             '只回填过一部分主题的一天，之后跑全量时不会被跳过。'
                             % '/'.join(TOPICS))
    parser.add_argument('--no-fetch-cache', action='store_true',
                        help='忽略正文抓取缓存，强制重新抓取（默认命中缓存，用于续跑与重跑）。'
                             '与日报的同名开关行为一致；**与 --refresh 不是一回事**，'
                             '后者重做"哪几天"，它重抓"哪几篇的正文"')
    parser.add_argument('--refresh', action='store_true',
                        help='已回填过的天也重做（默认跳过）。它决定**处理哪些天**，'
                             '不改变正文的抓取方式——要绕过缓存重抓正文用 --no-fetch-cache')
    parser.add_argument('--max-days', type=int, default=0, help='最多处理几天（0=不限，用于试跑）')
    parser.add_argument('--dry-run', action='store_true', help='只报到要抓多少，不抓不写')
    parser.add_argument('--yes', action='store_true', help='确认抓取并写入文件')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    try:
        start = datetime.strptime(args.since, '%Y-%m-%d').date()
        end = datetime.strptime(args.until, '%Y-%m-%d').date()
    except ValueError as error:
        print(json.dumps({'success': False, 'error': '日期格式要 YYYY-MM-DD：%s' % error})
              if args.json else '日期格式要 YYYY-MM-DD：%s' % error)
        return 1
    if end < start:
        print('--until 不能早于 --since'); return 1
    if args.workers < 1:
        print('--workers 要 ≥ 1'); return 1
    picked = parse_topics(args.topic)
    unknown = [t for t in picked if t not in TOPICS]
    if unknown:
        # 拼错一个词（比如 `--topic ai` 小写）会静默筛出 0 篇、然后报一句"没有要处理的"——
        # 看着像"这天本来就没文章"。所以这里当场报错并列出合法值，放在碰库之前。
        message = ('--topic 里有不在分类法里的主题：%s\n合法值：%s'
                   % ('、'.join(unknown), '、'.join(TOPICS)))
        print(json.dumps({'success': False, 'error': message}, ensure_ascii=False)
              if args.json else message)
        return 1
    topics = sorted(set(picked)) or None

    if args.vault_sync:
        # **补拷模式**：不抓取、不调模型，只把已经躺在 biz-daily 的那几天补进 Vault。
        # 为什么要有它：回填那天可以先不拷（省体积），后来想补就得能单独补——2026-09-27
        # 就是这么补的，165 天早就在 biz-daily 里，只是 Vault 的 Sources 里没有。
        #
        # 重复跑是**安全**的：`copy_to_vault` 逐 md 覆盖、不删别的东西，而它现在还会洗残留，
        # 所以"再补一次"同时也是"把库里那一层重新洗一遍"（2026-09-28 就是这么把 1,755 篇
        # 漏网的补上的 —— 那天被 `backfilled` 标记挡在门外的 12 天）。
        if topics:
            print('提示：--vault-sync 只把**已回填的** md 补拷进 Vault，--topic 对这一步不起作用。')
        days = days_with_articles(args.out)
        if args.max_days:
            days = days[:args.max_days]
        copied_days = total = 0
        for day in days:
            result = copy_to_vault(day, args.out, args.vault_root)
            if result['copied']:
                copied_days += 1
                total += result['copied']
        payload = {'success': True, 'action': 'backfill-articles.vault-sync',
                   'days': len(days), 'daysCopied': copied_days, 'files': total,
                   'target': args.vault_root, 'invokesAI': False, 'downloadsImages': False}
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json
              else '补拷 %d 天、共 %d 个 md → %s' % (copied_days, total, args.vault_root))
        return 0

    days = []
    cursor_day = start
    while cursor_day <= end:
        days.append(cursor_day.isoformat())
        cursor_day += timedelta(days=1)
    if args.max_days:
        days = days[:args.max_days]

    keys = get_db_keys(load_config())
    import sqlcipher3.dbapi2 as sqlcipher
    conn = sqlcipher.connect(keys['biz_db'])
    conn.execute('PRAGMA key = "x\'%s%s\'";' % (keys['biz_key'], keys['biz_salt']))
    cursor = conn.cursor()
    users = [row[0] for row in cursor.execute(
        "SELECT user_name FROM Name2Id WHERE user_name LIKE 'gh_%'").fetchall()]
    name_map = {}
    if keys.get('contact_key') and os.path.exists(keys.get('contact_db', '')):
        try:
            contact = sqlcipher.connect(keys['contact_db'])
            contact.execute('PRAGMA key = "x\'%s%s\'";'
                            % (keys['contact_key'], keys['contact_salt']))
            name_map = dict(contact.execute(
                "SELECT username, COALESCE(NULLIF(remark,''), NULLIF(nick_name,''), username) "
                "FROM contact WHERE username LIKE 'gh_%'").fetchall())
            contact.close()
        except Exception:
            pass

    todo = []
    for day in days:
        if not args.refresh and day_done(args.out, day, topics):
            continue
        articles = collect_articles(cursor, users, name_map, day)
        # 筛选在 `collect_articles` **之后**：主题是它内部 `apply_topics` 的输出，
        # 先筛就无从筛起（理由与等价性写在 `select_topics` 上）。
        articles = select_topics(articles, topics)
        # 截断要在**筛完之后**判断：`--limit-per-day 5 --topic AI` 该解作"每天 5 篇 AI"。
        truncated = bool(args.limit_per_day) and len(articles) > args.limit_per_day
        if args.limit_per_day:
            articles = articles[:args.limit_per_day]
        if articles:
            todo.append((day, articles, truncated))

    if args.dry_run:
        total = sum(len(items) for _, items, _ in todo)
        need = sum(1 for _, items, _ in todo for a in items
                   if len(body_without_media(a.get('local_text'))) < MIN_BODY_CHARS and a.get('url'))
        payload = {
            'success': True, 'dryRun': True, 'action': 'backfill-articles',
            'topics': topics or '全部',
            'days': len(todo), 'articles': total, 'needFetch': need,
            'workers': args.workers, 'invokesAI': False, 'downloadsImages': False,
            'readsLocalData': True,
            'note': '只抓正文，不下图片、不调用模型；抓完还要跑 vault notes 与 article-notes',
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json
              else '%d 天、%d 篇（其中 %d 篇要联网抓正文），并发 %d，主题 %s'
                   % (len(todo), total, need, args.workers,
                      '全部' if not topics else '、'.join(topics)))
        conn.close()
        return 0

    if not todo:
        print('没有要处理的（都已回填；要重做加 --refresh）')
        conn.close()
        return 0
    if not args.yes:
        print('会抓 %d 天、%d 篇的正文（不调模型，主题 %s）。加 --yes 确认。'
              % (len(todo), sum(len(i) for _, i, _ in todo),
                 '全部' if not topics else '、'.join(topics)))
        conn.close()
        return 1

    report = []
    for index, (day, articles, truncated) in enumerate(todo, 1):
        print('\n[%d/%d] %s — 选中 %d 篇' % (index, len(todo), day, len(articles)))
        fetch = fetch_bodies(articles, args.workers, log=print,
                             use_cache=not args.no_fetch_cache)
        result = write_day(articles, day, args.out, topic_filter=topics, truncated=truncated,
                            fetch_failed=fetch['failed'])
        print('    写入 %d 篇，跳过 %d 篇（正文太短），主题兜底 %d 篇'
              % (result['written'], result['skipped'], result['fallbackTopics']))
        if fetch['failed']:
            print('    ⚠ 有 %d 篇正文没抓到：这天不算完成，重跑会只重试这些篇'
                  % fetch['failed'])
        if args.vault_copy:
            sync = copy_to_vault(day, args.out, args.vault_root)
            print('    入 Vault 的 Sources: %d 个 md' % sync['copied'])
        report.append({'date': day, 'inDb': len(articles), 'fetched': fetch['ok'],
                       'fetchFailed': fetch['failed'], 'written': result['written'],
                       'skippedThin': result['skipped'], 'truncated': truncated})
    conn.close()

    total_written = sum(r['written'] for r in report)
    if args.json:
        print(json.dumps({'success': True, 'action': 'backfill-articles',
                          'days': report, 'written': total_written},
                         ensure_ascii=False, indent=2))
    else:
        print('\n共写入 %d 篇，覆盖 %d 天' % (total_written, len(report)))
        print('接着跑: vault notes --date <日期>  然后  article-notes')
    return 0 if total_written else 1


if __name__ == '__main__':
    sys.exit(main())
