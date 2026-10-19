"""Surveillance de comptes X via l'API officielle v2 (tweepy).

Deux modes:
- "poll"   : recherche recente toutes les N secondes (fonctionne avec les tiers d'API bas de gamme / pay-per-use).
- "stream" : filtered stream temps reel (necessite un acces qui l'autorise, tier Pro ou equivalent).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import tweepy
from tweepy.asynchronous import AsyncClient, AsyncStreamingClient

from core.config import XConfig
from core.models import NewsItem

log = logging.getLogger(__name__)

TWEET_FIELDS = ["created_at", "author_id"]


def build_query(accounts: list[str]) -> str:
    accounts_q = " OR ".join(f"from:{a.lstrip('@')}" for a in accounts)
    return f"({accounts_q}) -is:retweet"


def _to_item(tweet, usernames: dict[str, str]) -> NewsItem:
    username = usernames.get(str(tweet.author_id), str(tweet.author_id))
    created = tweet.created_at or datetime.now(timezone.utc)
    return NewsItem(
        source=f"x:@{username}",
        text=tweet.text,
        url=f"https://x.com/{username}/status/{tweet.id}",
        published_at=created,
        id=f"x:{tweet.id}",
    )


def _usernames(includes: dict | None) -> dict[str, str]:
    return {str(u.id): u.username for u in (includes or {}).get("users", [])}


async def poll_x(cfg: XConfig, bearer_token: str, queue: asyncio.Queue[NewsItem]) -> None:
    client = AsyncClient(bearer_token=bearer_token, wait_on_rate_limit=True)
    query = build_query(cfg.accounts)
    since_id: str | None = None
    log.info("Polling X: %s", query)
    while True:
        try:
            resp = await client.search_recent_tweets(
                query,
                since_id=since_id,
                max_results=max(10, min(cfg.max_results, 100)),
                tweet_fields=TWEET_FIELDS,
                expansions=["author_id"],
                user_fields=["username"],
            )
            tweets = resp.data or []
            if since_id is None:
                # Premier passage: on se cale sur le plus recent sans rejouer l'historique.
                log.info("X: %d tweets historiques ignores au demarrage", len(tweets))
            else:
                users = _usernames(resp.includes)
                for tw in reversed(tweets):  # du plus ancien au plus recent
                    await queue.put(_to_item(tw, users))
            if resp.meta and resp.meta.get("newest_id"):
                since_id = resp.meta["newest_id"]
        except tweepy.TweepyException as e:
            log.warning("Erreur API X: %s", e)
        await asyncio.sleep(cfg.poll_interval_s)


class _Stream(AsyncStreamingClient):
    def __init__(self, bearer_token: str, queue: asyncio.Queue[NewsItem]):
        super().__init__(bearer_token, wait_on_rate_limit=True)
        self.queue = queue

    async def on_response(self, response):
        if response.data is not None:
            await self.queue.put(_to_item(response.data, _usernames(response.includes)))

    async def on_errors(self, errors):
        log.warning("Erreur stream X: %s", errors)


async def stream_x(cfg: XConfig, bearer_token: str, queue: asyncio.Queue[NewsItem]) -> None:
    stream = _Stream(bearer_token, queue)
    existing = await stream.get_rules()
    if existing.data:
        await stream.delete_rules([r.id for r in existing.data])
    await stream.add_rules(tweepy.StreamRule(value=build_query(cfg.accounts), tag="polybot"))
    log.info("Filtered stream X demarre")
    await stream.filter(tweet_fields=TWEET_FIELDS, expansions=["author_id"], user_fields=["username"])


async def run_x(cfg: XConfig, bearer_token: str, queue: asyncio.Queue[NewsItem]) -> None:
    if cfg.mode == "stream":
        await stream_x(cfg, bearer_token, queue)
    else:
        await poll_x(cfg, bearer_token, queue)
