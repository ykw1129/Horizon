"""GitHub trending repos scraper with OSS Insight as its primary source.

Fetches star-gain rankings from api.ossinsight.io and emits them as
ContentItems. If OSS Insight explicitly marks its event-derived ranking
unavailable, the 24-hour configuration falls back to GitHub's daily trending
page. An optional keyword filter narrows results to repos whose description,
repo name, or collection names match at least one configured substring
(case-insensitive).
"""

from datetime import datetime, timezone
import logging
import re
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

from ..models import ContentItem, OSSInsightConfig, SourceType
from .base import BaseScraper, FetchResult

logger = logging.getLogger(__name__)


class OSSInsightScraper(BaseScraper):
    """Scraper for OSS Insight trends with a GitHub daily-page fallback."""

    BASE_URL = "https://api.ossinsight.io/v1/trends/repos"

    def __init__(self, config: OSSInsightConfig, http_client: httpx.AsyncClient):
        """Initialize scraper.

        Args:
            config: OSS Insight source configuration
            http_client: Shared async HTTP client
        """
        super().__init__(config, http_client)
        self.cfg: OSSInsightConfig = config
        self._keywords_lower = [kw.lower() for kw in self.cfg.keywords if kw]

    async def fetch(self, since: datetime) -> FetchResult:
        """Fetch trending repos for each configured language and apply filters."""
        result = FetchResult()
        if not self.cfg.enabled:
            return result

        items: List[ContentItem] = []
        seen_ids: set[str] = set()

        for lang in self.cfg.languages:
            await result.collect(lang, self._fetch_language(lang))
        for item in result.items:
            if item.id not in seen_ids:
                seen_ids.add(item.id)
                items.append(item)

        items.sort(key=lambda x: x.metadata.get("stars_gained", 0), reverse=True)
        result.items = items[: self.cfg.max_items]
        return result

    async def _fetch_language(self, language: str) -> List[ContentItem]:
        rows = await self._fetch_period(self.cfg.period, language)
        items = []
        for row in rows:
            if self.cfg.min_stars and self._stars_int(row) < self.cfg.min_stars:
                continue
            if self._keywords_lower and not self._matches_keywords(row):
                continue
            item = self._row_to_item(row, language)
            if item is not None:
                items.append(item)
        return items

    async def _fetch_period(self, period: str, language: str) -> List[dict]:
        """Call OSS Insight API for one (period, language) combo."""
        params = {"period": period, "language": language}
        response = await self.client.get(
            self.BASE_URL,
            params=params,
            headers={"Accept": "application/json", "User-Agent": "Horizon/1.0"},
            timeout=20.0,
        )
        response.raise_for_status()

        payload = response.json()
        data = payload.get("data") or {}
        rows = data.get("rows") or []
        data_quality = payload.get("data_quality") or {}
        if (
            data_quality.get("status") == "unavailable"
            and data_quality.get("metric") == "github_event_derived_ranking"
        ):
            if period != "past_24_hours":
                logger.warning(
                    "OSS Insight trend ranking is unavailable; no GitHub daily fallback "
                    "exists for period %s",
                    period,
                )
                return []
            logger.warning(
                "OSS Insight star-gain ranking is unavailable; using GitHub daily trending"
            )
            return await self._fetch_github_daily_trending(language)
        return rows

    async def _fetch_github_daily_trending(self, language: str) -> List[dict]:
        """Read GitHub's daily trending page when OSS Insight marks its metric unavailable."""
        response = await self.client.get(
            "https://github.com/trending",
            params={"since": "daily"},
            headers={
                "Accept": "text/html",
                "User-Agent": "Mozilla/5.0 Horizon/1.0",
            },
            timeout=30.0,
            follow_redirects=True,
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        rows = []
        for card in soup.select("article.Box-row"):
            repo_link = card.select_one("h2 a")
            if repo_link is None:
                continue
            repo_name = str(repo_link.get("href") or "").strip("/")
            if not repo_name or repo_name.count("/") != 1:
                continue

            card_text = " ".join(card.get_text(" ", strip=True).split())
            stars_match = re.search(
                r"([\d,]+)\s+stars?\s+today", card_text, re.IGNORECASE
            )
            if stars_match is None:
                continue

            language_node = card.select_one('[itemprop="programmingLanguage"]')
            primary_language = (
                language_node.get_text(" ", strip=True) if language_node else None
            )
            if language.casefold() != "all" and primary_language != language:
                continue

            description_node = card.select_one("p")
            rows.append(
                {
                    "repo_name": repo_name,
                    "repo_id": repo_name,
                    "stars": int(stars_match.group(1).replace(",", "")),
                    "forks": 0,
                    "pushes": 0,
                    "pull_requests": 0,
                    "primary_language": primary_language,
                    "description": (
                        description_node.get_text(" ", strip=True)
                        if description_node
                        else ""
                    ),
                    "collection_names": None,
                    "ranking_source": "github-trending",
                }
            )
        return rows

    def _row_to_item(self, row: dict, language: str) -> Optional[ContentItem]:
        """Convert a raw OSS Insight row into a ContentItem."""
        repo_name = row.get("repo_name")
        repo_id = row.get("repo_id")
        if not repo_name or not repo_id:
            return None

        stars_gained = self._stars_int(row)
        description = (row.get("description") or "").strip()
        primary_language = (row.get("primary_language") or "").strip() or None
        if primary_language is None and language.casefold() != "all":
            primary_language = language

        title = f"{repo_name} (+{stars_gained}⭐ {self.cfg.period})"
        url = f"https://github.com/{repo_name}"
        ranking_source = row.get("ranking_source", "ossinsight")
        source_type = (
            SourceType.GITHUB
            if ranking_source == "github-trending"
            else SourceType.OSSINSIGHT
        )

        content_lines = [
            f"Trending GitHub repo: {repo_name}",
            f"Stars gained ({self.cfg.period}): {stars_gained}",
            f"Language: {primary_language or 'Unknown'}",
        ]
        if ranking_source == "ossinsight":
            content_lines[2:2] = [
                f"Forks gained: {row.get('forks', 0)}",
                f"Pushes: {row.get('pushes', 0)}",
                f"Pull requests: {row.get('pull_requests', 0)}",
            ]
        if description:
            content_lines.append("")
            content_lines.append(description)
        collections = row.get("collection_names")
        if collections:
            content_lines.append("")
            content_lines.append(f"OSS Insight collections: {collections}")

        return ContentItem(
            id=self._generate_id(source_type.value, "trending", str(repo_id)),
            source_type=source_type,
            title=title,
            url=url,
            content="\n".join(content_lines),
            author=repo_name.split("/")[0] if "/" in repo_name else None,
            published_at=datetime.now(timezone.utc),
            profile=self.cfg.profile,
            metadata={
                "repo": repo_name,
                "stars_gained": stars_gained,
                "forks_gained": self._int(row.get("forks")),
                "pushes": self._int(row.get("pushes")),
                "pull_requests": self._int(row.get("pull_requests")),
                "primary_language": primary_language,
                "period": self.cfg.period,
                "collection_names": collections,
                "description": description,
                "category": self.cfg.category,
                "ranking_source": ranking_source,
            },
        )

    @staticmethod
    def _stars_int(row: dict) -> int:
        """Pull star count out of a row, coercing to int."""
        return OSSInsightScraper._int(row.get("stars"))

    @staticmethod
    def _int(value) -> int:
        """Best-effort conversion to int, defaulting to 0."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _matches_keywords(self, row: dict) -> bool:
        """Case-insensitive substring match against description, name, collections."""
        haystack = " ".join(
            [
                (row.get("description") or "").lower(),
                (row.get("collection_names") or "").lower(),
                (row.get("repo_name") or "").lower(),
            ]
        )
        return any(kw in haystack for kw in self._keywords_lower)
