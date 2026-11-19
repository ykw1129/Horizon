# X 中文热点栏目实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Horizon 日报中加入基于 Apify 的中文 X 热点栏目，每天最多展示过去 24 小时内互动热度最高的 10 条相关帖子。

**Architecture:** 复用现有 TwitterScraper 与 Apify Xquik Actor，在单次搜索运行中为每个关键词设置 30 条硬上限，按时间过滤、ID 去重、互动分排序后交给新 profile 分析。摘要层保留 X 的热度顺序并从原始元数据展示互动数字。

**Tech Stack:** Python 3、Pydantic、httpx、Apify Xquik、Horizon profiles 与 DailySummarizer。

**Spec:** `docs/superpowers/specs/2026-11-19-editorial-email-x-finance-design.md`

## Global Constraints

- 搜索词为“AI Agent”和“开源 AI”，每个词最多抓取 30 条。
- 只保留过去 24 小时发布的帖子，合并后每天最多展示 10 条。
- 互动热度 = 点赞数 × 2 + 转发数 × 3 + 回复数 × 2；热度相同优先较新的帖子。
- 新增独立 x-hotspots profile，使用 5.0 的最低相关性分数（profile rubric 将 0–4 定义为低信息量或推广噪声），低于普通科技新闻的 7.0 门槛，并关闭主题去重。
- 中文解释只依据原帖与抓取到的互动信息；不抓取回复，不编造帖子未提供的事实。
- APIFY_TOKEN 仅保存在本机环境变量中。

## Review Focus

- 缺少 APIFY_TOKEN：跳过 X 抓取并写明日志，其他来源和投递流程继续。
- 两个查询返回同一 Tweet ID：合并后只保留一条。
- 帖子时间戳缺失、格式错误或恰好处在 24 小时边界：不崩溃且按窗口定义处理。
- 点赞、转发或回复计数缺失或不是数字：按零计入，不影响其他帖子。
- 超过 10 条候选、热度相同：按热度公式和发布时间稳定排序后截取。

---

### Task 1: 限额抓取与热点排序

**Files:**
- Modify: `src/scrapers/twitter.py`
- Modify: ignored local config `data/config.json`

**Interfaces:**
- Consumes: `TwitterScraper.fetch(since: datetime) -> FetchResult` and existing TwitterConfig fields `keywords`, `fetch_limit`, `apify_token_env`.
- Produces: At most 10 deduplicated, recent ContentItem records carrying `engagement_score` plus the existing tweet ID, author, timestamp and interaction metadata.

- [x] Configure Apify mode, keywords `AI Agent` and `开源 AI`, `fetch_limit` 30, Chinese language filter, no profile users, and `fetch_reply_text=false` in `data/config.json`.
- [x] Use Xquik `searchTerms`, `maxItemsPerTarget` and `maxItems` to enforce the configured 30-per-term, 60-total limits; request Chinese-language Latest results.
- [x] When APIFY_TOKEN is absent, log a clear warning and return an empty result without marking the X source as a fatal failure; isolate profile-run and search-run failures through FetchResult.collect.
- [x] After combining query results, deduplicate by normalized tweet ID, retain the configured 24-hour window, compute the specified engagement score with missing counts treated as zero, sort by score then recency, and keep at most 10.
- [x] Commit the source change separately from ignored local configuration.

### Task 2: X profile and digest rendering

**Files:**
- Create: `profiles/x-hotspots/profile.json`
- Create: `profiles/x-hotspots/match.md`
- Create: `profiles/x-hotspots/analysis.md`
- Create: `profiles/x-hotspots/enrichment.md`
- Modify: `src/ai/summarizer.py`
- Modify: ignored local config `data/config.json`

**Interfaces:**
- Consumes: ContentItem records from Task 1 with profile route and engagement metadata.
- Produces: A dedicated Chinese X section whose item order follows `engagement_score` and whose interaction numbers and links come directly from item metadata.

- [x] Define x-hotspots with Chinese display name “X 中文热点”, original-post-only analysis rules, no web-search enrichment, and a concise Chinese context explanation.
- [x] Register x-hotspots with a 5.0 score threshold and topic deduplication disabled; add it to digest profile order after technology/dev discussions and before finance.
- [x] Render X engagement metadata directly (likes, reposts, replies, optional views), author, publication time and safe original-post link; sort this section by `engagement_score` without changing other groups.
- [x] Commit profile, scraper, and summary changes as focused commits; do not commit `data/config.json`.
