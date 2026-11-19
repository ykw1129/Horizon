"""X scraper using the Xquik Apify actor."""

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import List, Optional

from dateutil.parser import isoparse
import httpx

from .base import BaseScraper, FetchResult
from ..models import ContentItem, SourceType, TwitterConfig

logger = logging.getLogger(__name__)

_APIFY_BASE = "https://api.apify.com/v2"
_POLL_INTERVAL = 3.0
_MAX_WAIT = 180
_HOTSPOTS_PROFILE_ID = "x-hotspots"
_HOTSPOTS_MAX_ITEMS = 10
_ENGAGEMENT_FIELDS = ("favorite_count", "retweet_count", "reply_count")


def _non_negative_count(value: object) -> int:
    """Return a usable, non-negative interaction count."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _engagement_score(metadata: dict) -> int:
    """Weight reposts and replies more strongly than likes for hotspot ranking."""
    return (
        _non_negative_count(metadata.get("favorite_count")) * 2
        + _non_negative_count(metadata.get("retweet_count")) * 3
        + _non_negative_count(metadata.get("reply_count")) * 2
    )


def _first_value(*values):
    return next((value for value in values if value is not None and value != ""), None)


class TwitterScraper(BaseScraper):
    """Fetch X posts via the Apify Xquik actor."""

    def __init__(self, config: TwitterConfig, http_client: httpx.AsyncClient):
        super().__init__(config, http_client)
        self.config = config

    async def fetch(self, since: datetime) -> FetchResult:
        result = FetchResult()
        if not self.config.enabled:
            return result

        users = [u.strip().lstrip("@") for u in self.config.users if u.strip()]
        keywords = [k.strip() for k in self.config.keywords if k.strip()]
        if not users and not keywords:
            logger.debug("No X users or keywords configured, skipping.")
            return result

        token = os.environ.get(self.config.apify_token_env)
        if not token:
            logger.warning(
                "Skipping X (Apify): missing environment variable %s",
                self.config.apify_token_env,
            )
            return result

        limit = max(1, self.config.fetch_limit)
        hotspot_profile = self._uses_hotspots_profile()
        now = datetime.now(timezone.utc)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        window_start = since.astimezone(timezone.utc)
        if hotspot_profile:
            window_start = max(window_start, now - timedelta(hours=24))
        headers = self._auth_headers(token)

        if users:
            logger.info("Fetching X (Apify) for %d configured users.", len(users))
            await result.collect(
                "profiles",
                self._collect_from_payload(
                    headers,
                    self._build_profile_payload(users, limit, window_start, now),
                    window_start,
                    now,
                ),
            )

        if keywords:
            logger.info("Fetching X (Apify) for %d search terms.", len(keywords))
            await result.collect(
                "search",
                self._collect_from_payload(
                    headers,
                    self._build_search_payload(
                        keywords, limit, window_start, now, chinese=hotspot_profile
                    ),
                    window_start,
                    now,
                ),
            )

        if hotspot_profile:
            by_tweet_id = {}
            for item in result.items:
                tweet_id = str(item.metadata.get("tweet_id") or item.id)
                item.metadata["engagement_score"] = _engagement_score(item.metadata)
                current = by_tweet_id.get(tweet_id)
                if current is None:
                    by_tweet_id[tweet_id] = item
                    continue

                # The same post may have slightly different counts across terms.
                for field in _ENGAGEMENT_FIELDS:
                    current.metadata[field] = max(
                        _non_negative_count(current.metadata.get(field)),
                        _non_negative_count(item.metadata.get(field)),
                    )
                current.metadata["engagement_score"] = _engagement_score(
                    current.metadata
                )

            result.items = sorted(
                by_tweet_id.values(),
                key=lambda item: (
                    _non_negative_count(item.metadata.get("engagement_score")),
                    item.published_at.timestamp() if item.published_at else 0,
                ),
                reverse=True,
            )[:_HOTSPOTS_MAX_ITEMS]
        else:
            result.items = list({item.id: item for item in result.items}.values())
        logger.info("Fetched %d X posts via Apify.", len(result.items))
        return result

    @staticmethod
    def _auth_headers(token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _uses_hotspots_profile(self) -> bool:
        profile = self.config.profile
        if isinstance(profile, str):
            return profile == _HOTSPOTS_PROFILE_ID
        return isinstance(profile, list) and _HOTSPOTS_PROFILE_ID in profile

    @staticmethod
    def _actor_time(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )

    def _build_search_payload(
        self,
        keywords: List[str],
        limit: int,
        since: datetime,
        until: datetime,
        *,
        chinese: bool = False,
    ) -> dict:
        # Xquik supports a hard per-search cap, allowing 30 posts for each of
        # the two configured topics in a single run (60 total).
        payload = {
            "mode": "search",
            "searchTerms": keywords,
            "queryType": "Latest",
            "maxItems": limit * len(keywords),
            "maxItemsPerTarget": limit,
            "since_time": self._actor_time(since),
            "until_time": self._actor_time(until),
            "outputVariant": "rich",
            "fieldStyle": "snake_case",
            "inputStatus": False,
        }
        if chinese:
            payload["lang"] = "zh"
        return payload

    def _build_profile_payload(
        self,
        users: List[str],
        limit: int,
        since: datetime,
        until: datetime,
    ) -> dict:
        return {
            "mode": "profileTweets",
            "twitterHandles": users,
            "maxItems": limit * len(users),
            "maxItemsPerTarget": limit,
            "since_time": self._actor_time(since),
            "until_time": self._actor_time(until),
            "outputVariant": "rich",
            "fieldStyle": "snake_case",
            "inputStatus": False,
        }

    async def _collect_from_payload(
        self,
        headers: dict,
        payload: dict,
        since: datetime,
        until: datetime,
    ) -> List[ContentItem]:
        run_id, dataset_id = await self._start_run_with_payload(headers, payload)
        if not run_id or not dataset_id:
            raise RuntimeError("Apify did not return a run and dataset ID")
        if not await self._wait_for_run(headers, run_id):
            raise RuntimeError("Apify run failed or timed out")
        raw_items = await self._fetch_dataset(headers, dataset_id)
        items = []
        for raw in raw_items:
            if not isinstance(raw, dict) or raw.get("noResults"):
                continue
            parsed = self._parse_item(raw, since, until)
            if parsed:
                items.append(parsed)
        return items

    async def _start_run_with_payload(
        self, headers: dict, payload: dict
    ) -> tuple[Optional[str], Optional[str]]:
        url = f"{_APIFY_BASE}/acts/{self.config.actor_id}/runs"
        try:
            resp = await self.client.post(url, json=payload, headers=headers, timeout=30.0)
            resp.raise_for_status()
            data = resp.json()["data"]
            run_id = data["id"]
            dataset_id = data["defaultDatasetId"]
            logger.debug("Started Apify run %s, dataset %s", run_id, dataset_id)
            return run_id, dataset_id
        except Exception as exc:
            raise RuntimeError("Failed to start Apify run") from exc

    async def _wait_for_run(self, headers: dict, run_id: str) -> bool:
        url = f"{_APIFY_BASE}/actor-runs/{run_id}"
        deadline = time.monotonic() + _MAX_WAIT
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                async with asyncio.timeout(remaining):
                    resp = await self.client.get(
                        url, headers=headers, timeout=min(10.0, remaining)
                    )
                if time.monotonic() >= deadline:
                    break
                resp.raise_for_status()
                status = resp.json()["data"]["status"]
                if status == "SUCCEEDED":
                    return True
                if status in ("FAILED", "ABORTED", "TIMED-OUT"):
                    logger.error("Apify run %s ended with status: %s", run_id, status)
                    return False
            except Exception as exc:
                # Do not log HTTP exception strings; they can contain request URLs.
                logger.warning("Error polling Apify run %s (%s)", run_id, type(exc).__name__)
            remaining = deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(_POLL_INTERVAL, remaining))
        logger.warning("Apify run %s timed out after %ss.", run_id, _MAX_WAIT)
        return False

    async def _fetch_dataset(self, headers: dict, dataset_id: str) -> list:
        url = f"{_APIFY_BASE}/datasets/{dataset_id}/items"
        try:
            resp = await self.client.get(url, headers=headers, timeout=30.0)
            resp.raise_for_status()
            rows = resp.json()
            if not isinstance(rows, list):
                raise ValueError("Invalid Apify dataset")
            return rows
        except Exception as exc:
            raise RuntimeError("Failed to fetch Apify dataset") from exc

    async def fetch_replies_for_item(self, item: ContentItem) -> List[str]:
        """Fetch reply texts for one post using Xquik's conversation search."""
        if not self.config.fetch_reply_text:
            return []

        token = os.environ.get(self.config.apify_token_env)
        if not token:
            return []

        conversation_id = str(item.metadata.get("conversation_id") or "")
        if not conversation_id:
            return []

        max_replies = max(self.config.max_replies_per_tweet, 0)
        if max_replies == 0:
            return []

        now = datetime.now(timezone.utc)
        since = now - timedelta(days=7)
        payload = {
            "mode": "search",
            "searchTerms": [f"conversation_id:{conversation_id}"],
            "queryType": "Latest",
            "maxItems": max(1, max_replies * 5),
            "maxItemsPerTarget": max(1, max_replies * 5),
            "since_time": self._actor_time(since),
            "until_time": self._actor_time(now),
            "outputVariant": "rich",
            "fieldStyle": "snake_case",
            "inputStatus": False,
        }
        headers = self._auth_headers(token)
        run_id, dataset_id = await self._start_run_with_payload(headers, payload)
        if not run_id or not dataset_id or not await self._wait_for_run(headers, run_id):
            return []

        rows = await self._fetch_dataset(headers, dataset_id)
        return self._extract_reply_lines(item, rows, max_replies)

    def _extract_reply_lines(self, item: ContentItem, rows: list, max_replies: int) -> List[str]:
        """Convert Xquik rows into compact reply lines."""
        min_likes = max(self.config.reply_min_likes, 0)
        tweet_id = str(item.metadata.get("tweet_id") or "")
        own_author = (item.metadata.get("screen_name") or item.author or "").lstrip("@")
        candidates = []

        for row in rows:
            if not isinstance(row, dict) or row.get("noResults"):
                continue

            row_id = str(_first_value(row.get("id_str"), row.get("id")) or "")
            if row_id.startswith("tweet-"):
                row_id = row_id[6:]
            if tweet_id and row_id == tweet_id:
                continue

            user = row.get("author") or row.get("user") or {}
            handle = _first_value(
            user.get("username"), user.get("user_name"), user.get("userName"),
            user.get("screen_name"), user.get("handle"),
                row.get("author_username"), row.get("username"), row.get("handle"),
            ) or "unknown"
            if handle and own_author and handle.lstrip("@").lower() == own_author.lower():
                continue

            text = unescape(str(_first_value(row.get("full_text"), row.get("text")) or "").strip())
            if not text:
                continue

            likes = _non_negative_count(_first_value(row.get("like_count"), row.get("favorite_count")))
            replies = _non_negative_count(row.get("reply_count"))
            if likes < min_likes:
                continue

            score = likes * 2 + replies
            line = f"[@{handle} | ❤️ {likes} | 💬 {replies}] {text[:280]}"
            candidates.append((score, line))

        candidates.sort(key=lambda x: x[0], reverse=True)
        return [line for _, line in candidates[:max_replies]]

    @staticmethod
    def append_discussion_content(item: ContentItem, reply_lines: List[str]) -> bool:
        """Append reply lines under Top Comments marker."""
        if not reply_lines:
            return False

        existing = item.content or ""
        marker = "--- Top Comments ---"
        block = "\n".join(reply_lines)

        if marker in existing:
            if block in existing:
                return False
            item.content = existing + "\n" + block
            return True

        if existing:
            item.content = existing + f"\n\n{marker}\n" + block
        else:
            item.content = f"{marker}\n" + block
        return True

    def _parse_item(
        self,
        item: dict,
        since: datetime,
        until: Optional[datetime] = None,
    ) -> Optional[ContentItem]:
        try:
            created_at_str = _first_value(item.get("created_at"), item.get("createdAt"))
            if not created_at_str:
                return None

            try:
                published_at = datetime.strptime(
                    created_at_str, "%a %b %d %H:%M:%S %z %Y"
                )
            except (TypeError, ValueError):
                published_at = isoparse(str(created_at_str))

            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
            published_at = published_at.astimezone(timezone.utc)
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            if published_at < since.astimezone(timezone.utc):
                return None
            if until is not None:
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
                if published_at > until.astimezone(timezone.utc):
                    return None

            raw_id = str(_first_value(item.get("id_str"), item.get("id")) or "")
            numeric_id = raw_id.removeprefix("tweet-")
            if not numeric_id:
                return None

            conversation_id = str(
                _first_value(
                    item.get("conversation_id"), item.get("conversationId"),
                    (item.get("tweet") or {}).get("conversation_id"),
                ) or numeric_id
            )
            user = item.get("author") or item.get("user") or {}
            screen_name = str(_first_value(
                user.get("username"), user.get("user_name"), user.get("userName"),
                user.get("screen_name"), user.get("handle"),
                item.get("author_username"), item.get("authorUsername"),
                item.get("handle"), item.get("username"),
            ) or "unknown").lstrip("@")
            author = str(_first_value(
                user.get("name"), user.get("display_name"), user.get("displayName"),
                user.get("user_name"), user.get("userName"),
                item.get("author_name"), item.get("authorName"),
            ) or screen_name)

            text = _first_value(item.get("full_text"), item.get("text"))
            if not text:
                return None
            text = unescape(str(text))

            url = _first_value(item.get("url"), item.get("tweet_url"), item.get("tweetUrl"))
            if not url:
                url = f"https://x.com/{screen_name}/status/{numeric_id}"

            title_body = text[:50].replace("\n", " ").strip()
            if len(text) > 50:
                title_body += "..."

            favorite_count = _non_negative_count(_first_value(
                item.get("like_count"), item.get("likeCount"), item.get("favorite_count"),
            ))
            retweet_count = _non_negative_count(_first_value(
                item.get("retweet_count"), item.get("retweetCount"),
            ))
            reply_count = _non_negative_count(_first_value(
                item.get("reply_count"), item.get("replyCount"),
            ))
            view_count = _first_value(item.get("view_count"), item.get("viewCount"))

            return ContentItem(
                id=self._generate_id(SourceType.TWITTER.value, "tweet", numeric_id),
                source_type=SourceType.TWITTER,
                title=f"@{screen_name}: {title_body}",
                url=str(url),
                content=text,
                author=author,
                published_at=published_at,
                profile=self.config.profile,
                metadata={
                    "tweet_id": numeric_id,
                    "screen_name": screen_name,
                    "conversation_id": conversation_id,
                    "favorite_count": favorite_count,
                    "retweet_count": retweet_count,
                    "reply_count": reply_count,
                    "view_count": view_count,
                    "is_reply": bool(_first_value(item.get("is_reply"), item.get("isReply"), False)),
                    "in_reply_to_status_id": _first_value(
                        item.get("in_reply_to_status_id"), item.get("inReplyToStatusId")
                    ),
                    "in_reply_to_screen_name": _first_value(
                        item.get("in_reply_to_screen_name"), item.get("inReplyToScreenName")
                    ),
                    "category": self.config.category,
                },
            )
        except Exception as exc:
            logger.debug("Failed to parse X post (%s)", type(exc).__name__)
            return None
