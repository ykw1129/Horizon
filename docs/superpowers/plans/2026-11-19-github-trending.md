# GitHub Trending Projects Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Chinese GitHub trending projects section with up to 20 repositories ranked by stars gained over the previous 24 hours.

**Architecture:** Enable the existing OSS Insight repository trends source and route it to a dedicated `github-trending` profile. The profile creates a Chinese, source-grounded project description without external research tools. Preserve the source's star-gain ranking when Horizon builds the final summary.

**Tech Stack:** Horizon Python application, Pydantic configuration, Markdown-backed profile prompts, OSS Insight public HTTP API.

## Global Constraints

- Rank projects by stars gained during `past_24_hours`, not historical total stars.
- Query `All` languages and retain up to 20 repositories with no minimum gained-star threshold.
- Write short Chinese project descriptions using only OSS Insight descriptions and metadata; state when source information is insufficient.
- Do not change the existing news sources, email subscriber list, or 08:00 Windows schedule.
- Do not include credentials or ignored `data/config.json` in a Git commit.
- Preserve the unrelated, uncommitted email formatter fix in `src/services/email.py`.

---

### Task 1: Add the GitHub trending profile

**Files:**
- Create: `profiles/github-trending/profile.json`
- Create: `profiles/github-trending/match.md`
- Create: `profiles/github-trending/analysis.md`
- Create: `profiles/github-trending/enrichment.md`

**Interfaces:**
- Consumes: OSS Insight `ContentItem` fields `title`, `url`, `content`, `profile`, and metadata `stars_gained`, `primary_language`, and `description`.
- Produces: Profile ID `github-trending`, Chinese display name `GitHub 热门项目`, and one required primary `summary` block with no external tools.

- [ ] **Step 1: Create `profiles/github-trending/profile.json`**

```json
{
  "id": "github-trending",
  "name": "GitHub Trending Projects",
  "display_names": {
    "zh": "GitHub 热门项目"
  },
  "match": "match.md",
  "analysis": "analysis.md",
  "enrichment": {
    "prompt": "enrichment.md",
    "blocks": [
      {
        "id": "summary",
        "type": "section",
        "tools": [],
        "primary": true
      }
    ]
  }
}
```

- [ ] **Step 2: Create the profile prompts**

`profiles/github-trending/match.md`:

```markdown
# GitHub trending projects

Use this profile for repositories collected from OSS Insight's GitHub trending source. The source ranks repositories by stars gained during the configured period. Keep this profile for repository projects, developer tools, libraries, applications, datasets, and open-source models. Route general technology news to another profile.
```

`profiles/github-trending/analysis.md`:

```markdown
# Evaluation goal

Assess whether the supplied repository description gives readers a useful, concrete understanding of a GitHub project. The repository's star gain is evidence of recent community interest, not proof of quality, safety, or technical merit.

# Scoring rubric

- **9-10: Clear and distinctive.** The source explains a useful project with a concrete purpose and differentiating capability.
- **7-8: Useful.** The project has a clear purpose and plausible practical value, though its approach or audience may be familiar.
- **5-6: Somewhat useful.** The source identifies a project and broad purpose, but provides limited detail.
- **3-4: Unclear.** The description is vague, promotional, or too thin to explain the project's value.
- **0-2: Noise.** The repository appears unrelated, spam-like, or has no usable project information.

# Evaluation guidance

Use only the supplied repository title, description, language, and activity metrics. Keep star growth separate from the quality score. Do not infer capabilities, maintenance status, security, licensing, or user counts from popularity. A low score does not remove the repository from the trending section; this profile has no score threshold.

Use three to five specific topic tags when the source supports them.
```

`profiles/github-trending/enrichment.md`:

```markdown
# Output

Write the required `summary` block in Chinese in two to four complete sentences. Explain what the project does and who may find it useful, translating the supplied description into natural Chinese. The summary renderer displays the exact repository name, stars gained, language, and link from metadata; do not repeat or alter these fields in the generated description.

Use only the supplied repository description and metadata. Do not use outside knowledge or infer features from the repository name. If the description is missing or too vague, say that the source provides too little information to explain the project accurately. Do not claim that star growth proves quality, safety, or long-term popularity.
```

- [ ] **Step 3: Confirm profile files align with the loaded profile schema**

Inspect `ProfileDefinition` and `ProfileBlock` in `src/processing/profiles.py`. The ID must match `^[a-z][a-z0-9_-]*$`; the required block must have a unique lowercase ID and at most one non-optional primary block.

---

### Task 2: Configure OSS Insight and the section

**Files:**
- Modify: ignored local config `data/config.json`

**Interfaces:**
- Consumes: profile `github-trending` created in Task 1.
- Produces: OSS Insight items explicitly routed to `github-trending`; no score or topic-dedup filtering for this profile; the section appears first in the digest.

- [ ] **Step 1: Set the `sources.ossinsight` block**

```json
{
  "enabled": true,
  "period": "past_24_hours",
  "languages": ["All"],
  "keywords": [],
  "min_stars": 0,
  "max_items": 20,
  "category": "github-trending",
  "profile": "github-trending"
}
```

- [ ] **Step 2: Register the profile's filtering behavior and summary priority**

Set `processing.profile_settings.github-trending` to:

```json
{
  "threshold": null,
  "topic_dedup": false
}
```

Set `digest.profile_order` to:

```json
["github-trending", "tech-news", "tech-blog", "finance-news"]
```

Do not modify other source settings, AI settings, email settings, or schedule settings.

---

### Task 3: Keep the visible project order by star gain

**Files:**
- Modify: `src/scrapers/ossinsight.py`, method `OSSInsightScraper._row_to_item`
- Modify: `src/ai/summarizer.py`, methods `DailySummarizer.build_view` and `_format_item`

**Interfaces:**
- Consumes: OSS Insight row language and metadata plus grouped `ContentItem` lists after Horizon's score-based selection.
- Produces: missing language is represented as unavailable rather than the `All` query bucket; `github-trending` items display exact repository name, gained stars, and language from metadata, then sort by `stars_gained` descending. Other profile groups retain their existing order.

- [ ] **Step 1: Keep `All` from becoming a false repository language**

In `_row_to_item`, replace the language fallback with:

```python
primary_language = (row.get("primary_language") or "").strip() or None
if primary_language is None and language.casefold() != "all":
    primary_language = language
```

Format the input content line as `Language: {primary_language or 'Unknown'}` and preserve `None` in `metadata["primary_language"]` when the all-language response lacks a language.

- [ ] **Step 2: Sort the GitHub trending group before creating view items**

Inside the loop over `ordered_groups`, before enumerating `profile_items`, use:

```python
if profile_id == "github-trending":
    profile_items = sorted(
        profile_items,
        key=lambda item: int(item.metadata.get("stars_gained") or 0),
        reverse=True,
    )
```

Keep `global_index`, item anchors, profile ordering, and all other group ordering unchanged.

- [ ] **Step 3: Render project identifiers and metrics from metadata**

In `_format_item`, set `meta = item.metadata` and `profile_id = self._profile_id(item)` before building the title. For `github-trending`, use `meta["repo"]` instead of the generated title when it is a non-empty string, so the repository name stays exact. Build metric values as follows:

```python
if profile_id == "github-trending":
    repo_name = meta.get("repo")
    if isinstance(repo_name, str) and repo_name.strip():
        _title = repo_name

raw_stars = meta.get("stars_gained")
try:
    stars_display = f"{int(raw_stars):,}" if raw_stars is not None else "未提供"
except (TypeError, ValueError):
    stars_display = "未提供"
raw_language = meta.get("primary_language")
project_language = raw_language.strip() if isinstance(raw_language, str) else ""
if not project_language or project_language.casefold() == "all":
    project_language = "未提供"
```

Render `**GitHub 热度**：过去 24 小时新增 {stars_display} Star · 主要语言：{project_language}` in Chinese, or the equivalent English line for an English digest. Use `_escape_markdown` for the language value and insert the metric line before the generated description. Keep the repository URL as the existing linked heading. Do not render this line for other profiles.

---

### Task 4: Validate the local configuration and commit tracked files

**Files:**
- Review: `profiles/github-trending/profile.json`
- Review: `profiles/github-trending/match.md`
- Review: `profiles/github-trending/analysis.md`
- Review: `profiles/github-trending/enrichment.md`
- Review: `src/scrapers/ossinsight.py`
- Review: `src/ai/summarizer.py`
- Validate only: ignored `data/config.json`

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: Loaded profile and configuration, with only intended tracked feature files committed.

- [ ] **Step 1: Load the application config and profile registry**

From the repository root, run this read-only integrity check:

```powershell
@'
from pathlib import Path
from src.storage.manager import StorageManager
from src.processing.profiles import ProfileRegistry

storage = StorageManager()
config = storage.load_config()
profiles = ProfileRegistry.load(
    Path(config.processing.profiles_dir), config.processing.default_profile
)
profiles.validate_source_references(config.sources.model_dump(mode="json"))
assert config.sources.ossinsight.enabled
assert config.sources.ossinsight.profile == "github-trending"
assert config.sources.ossinsight.languages == ["All"]
assert config.sources.ossinsight.max_items == 20
assert config.sources.ossinsight.min_stars == 0
assert config.processing.profile_settings["github-trending"].threshold is None
assert not config.processing.profile_settings["github-trending"].topic_dedup
assert config.digest.profile_order[0] == "github-trending"
assert "github-trending" in profiles.ids
print("config_and_profile_loaded=True")
'@ | .\.venv\Scripts\python.exe -
```

Expected output: `config_and_profile_loaded=True`.

- [ ] **Step 2: Review and commit tracked feature files only**

Run `git diff --check`, then stage only `profiles/github-trending/`, `src/scrapers/ossinsight.py`, and `src/ai/summarizer.py`. Commit with:

```powershell
git add -- profiles/github-trending src/scrapers/ossinsight.py src/ai/summarizer.py
git commit -m "feat: add GitHub trending digest section"
```

Do not stage `src/services/email.py` or ignored `data/config.json`.
