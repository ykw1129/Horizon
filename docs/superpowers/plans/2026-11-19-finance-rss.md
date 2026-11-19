# 财经 RSS 栏目实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有财经日报栏目中补充 A 股/港股、美股科技股和全球宏观新闻来源。

**Architecture:** 保留 CNBC Finance，使用现有 RSSScraper 抓取三个简体中文 Google News RSS 搜索源，并把所有新增来源路由到 finance-news profile。沿用项目现有 24 小时窗口、财经评分与主题去重策略。

**Tech Stack:** Python 3、现有 RSSScraper、feedparser、Pydantic RSSSourceConfig、Google News RSS 搜索端点。

**Spec:** `docs/superpowers/specs/2026-11-19-editorial-email-x-finance-design.md`

## Global Constraints

- 覆盖 A 股/港股、美股科技股和全球宏观。
- 保留现有 CNBC Finance RSS；新增 3 个简体中文 RSS 搜索源，全部进入 finance-news。
- 使用 Horizon 现有过去 24 小时收集窗口及 finance-news 分析、7 分阈值和主题去重设置。
- 本次只呈现报道摘要和来源，不增加行情报价、投资建议或收益预测。
- 某个 RSS 源失败时，其它源和日报继续运行。

## Review Focus

- Google News 某一个市场主题暂时没有结果：其它 feed 仍然正常汇总。
- 新 RSS 条目没有可用的发布日期：沿用现有 RSSScraper 的处理并跳过无法确认时间的条目。
- 搜索结果与 CNBC 重复：沿用 Horizon 已有跨来源 URL 合并处理。
- Google News RSS 返回重定向/无条目：按单源失败隔离，不阻断其他 feed。
- 财经内容未达到既有 7 分阈值：不改变既有筛选规则或伪造栏目内容。

---

### Task 1: 增加本机财经搜索源

**Files:**
- Modify: ignored local config `data/config.json`

**Interfaces:**
- Consumes: Existing `sources.rss` list and `RSSSourceConfig` fields `name`, `url`, `enabled`, `category`, and `profile`.
- Produces: Three enabled RSS entries routed to finance-news and ordered after the existing CNBC Finance source.

- [x] Add an A 股/港股 Google News RSS search feed using `hl=zh-CN`, `gl=CN`, and `ceid=CN:zh-Hans`.
- [x] Add a 美股科技股 search feed with the same localization parameters and finance-news routing.
- [x] Add a 全球宏观 search feed covering macroeconomic developments, the Federal Reserve, inflation and rates.
- [x] Keep the current CNBC URL and existing finance-news threshold/dedup settings unchanged; set profile order to `github-trending`, `tech-news`, `tech-blog`, `x-hotspots`, `finance-news`.
- [x] Keep the configuration local and ignored; do not include mail credentials, API keys, or data/config.json in a commit.

### Task 2: Confirm section routing in the digest

**Files:**
- Inspect: `src/orchestrator.py`
- Inspect: `src/scrapers/rss.py`

**Interfaces:**
- Consumes: RSS source profile routes and existing `DailySummarizer.profile_order`.
- Produces: No code interface change; the configured items appear under finance-news and an individual feed failure remains isolated.

- [x] Review the existing per-feed failure handling and profile routing to ensure the new config uses current supported fields; change application code only if the current implementation cannot satisfy this routing contract.
- [x] Commit no local secrets or ignored configuration.
