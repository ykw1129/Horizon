# 编辑部日报邮件版式实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Horizon 每日邮件设计为内容清楚、可扫读、兼容主流邮件客户端的中文单列编辑部日报。

**Architecture:** DailySummarizer 输出稳定的栏目、今日重点和空栏目提示；EmailManager 把现有安全 Markdown 转为适合邮件客户端的表格布局与行内样式。继续用 multipart/alternative 同时发送 HTML 与原始纯文本，并在 HTML 构造失败时退回安全的纯文本预览 HTML。

**Tech Stack:** Python 3、Python-Markdown、标准库 email MIME、内联 HTML 邮件样式。

**Spec:** `docs/superpowers/specs/2026-11-19-editorial-email-x-finance-design.md`

## Global Constraints

- 邮件为单列编辑部日报，内容宽度约 640px，深海军蓝标题栏、浅色背景和青绿色强调色。
- 日期与最多 3 条今日重点放在邮件顶部；正文按 GitHub 热门、科技/开发者、X 热点、财经等栏目分组。
- HTML 主要布局用表格和行内样式；不加载外部字体、图片或样式表。
- MIME multipart/alternative 同时提供内容一致的 HTML 和纯文本；保留退订说明。
- 无可展示 X 或财经条目时明确显示空栏目提示，不编造新闻。

## Review Focus

- 摘要中出现 HTML 特殊字符或不可信原文：渲染时继续转义，不执行原始 HTML。
- 标题、摘要或链接为空/过长：布局保持有效，链接仍执行现有 HTTP(S) 安全检查。
- 邮件客户端剥离 head/style：表格布局和行内样式仍提供可读层级。
- 今日重点不足 3 条或没有评分：显示实际可用标题，不生成占位新闻。
- HTML Markdown 渲染缺失/异常：发送流程仍能包含纯文本和安全的纯文本 HTML 回退。

---

### Task 1: 摘要结构与空栏目状态

**Files:**
- Modify: `src/ai/summarizer.py`

**Interfaces:**
- Consumes: Existing `DailySummaryView`, analyzed ContentItem list, profile names and order.
- Produces: Markdown summary with up to three highest-priority headlines, stable section order, and explicit empty states for x-hotspots and finance-news when the overall report is generated.

- [x] Add a “今日重点” block after the report date using at most the three highest-scored selected items; use existing normalized titles and safe links.
- [x] When a summary contains other content but x-hotspots or finance-news has no selected items, render the matching section with a concise empty-state message; do not invent source status or content.
- [x] Keep the current zero-total-content early-exit behavior and all non-X/non-finance grouping rules unchanged.

### Task 2: HTML email presentation and fallback

**Files:**
- Modify: `src/services/email.py`
- Modify: `src/orchestrator.py` only if needed for Chinese subject wording

**Interfaces:**
- Consumes: The Markdown summary from Task 1 and existing EmailConfig sender/unsubscribe settings.
- Produces: Full HTML document with 640px single-column presentation, editorial group and item cards, inline styles, and an always-present plain-text MIME alternative.

- [x] Add a dedicated HTML email renderer that wraps rendered Markdown in presentation tables, styles section and item cards inline, and emits no external resource references.
- [x] Keep the current Markdown text as the plain-text MIME alternative; convert only the HTML part so plain text remains readable and content-equivalent.
- [x] Catch HTML rendering failures and create a safely escaped preformatted HTML fallback, allowing the plain-text MIME part to be sent.
- [x] Keep sender attribution and unsubscribe instructions; ensure the subject visibly includes Horizon’s daily report name and date.
- [x] Commit the email renderer and summary changes separately from the ignored local mail configuration.
