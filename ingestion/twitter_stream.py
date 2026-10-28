"""Surveillance de comptes X via l'API officielle v2 (tweepy).

Deux modes:
- "poll"   : recherche recente toutes les N secondes (tiers d'API d'entree de gamme / pay-per-use).
- "stream" : filtered stream temps reel (acces superieur requis).
Limites d'API: tweepy attend automatiquement la reinitialisation (wait_on_rate_limit).
"""
from __future__ import annotations

import asyncio
import logging

import tweepy
from tweepy.asynchronous import AsyncClient, AsyncStreamingClient

from core.clock import Clock
from core.config import NewsConfig
from core.models import NewsItem
from ingestion.news_normalizer import source_reliability

log = logging.getLogger(__name__)

TWEET_FIELDS = ["created_at", "author_id"]


def build_query(accounts: list[str]) -> str:
    accounts_q = " OR ".join(f"from:{a.lstrip('@')}" for a in accounts)
    return f"({accounts_q}) -is:retweet"


def tweet_to_item(tweet, usernames: dict[str, str], cfg: NewsConfig, clock: Clock) -> NewsItem:
    username = usernames.get(str(tweet.author_id), str(tweet.author_id))
    source = f"x:@{username}"
    return NewsItem(
        id=f"x:{tweet.id}", source=source, title=tweet.text, content="",
        url=f"https://x.com/{username}/status/{tweet.id}",
        published_at=tweet.created_at,  # None si absent: jamais invente
        received_at=clock.utcnow(), reliability=source_reliability(source, cfg),
    )


def _usernames(includes: dict | None) -> dict[str, str]:
    return {str(u.id): u.username for u in (includes or {}).get("users", [])}


async def poll_x(cfg: NewsConfig, bearer_token: str, queue: asyncio.Queue[NewsItem], clock: Clock) -> None:
    x = cfg.x
    client = AsyncClient(bearer_token=bearer_token, wait_on_rate_limit=True)
    query = build_query(x.accounts)
    since_id: str | None = None
    backoff = x.poll_interval_s
    log.info("Polling X: %s", query)
    while True:
        try:
            resp = await client.search_recent_tweets(
                query, since_id=since_id, max_results=max(10, min(x.max_results, 100)),
                tweet_fields=TWEET_FIELDS, expansions=["author_id"], user_fields=["username"],
            )
            tweets = resp.data or []
            if since_id is None:
                log.info("X: %d tweets historiques ignores au demarrage", len(tweets))
            else:
                users = _usernames(resp.includes)
                for tw in reversed(tweets):
                    await queue.put(tweet_to_item(tw, users, cfg, clock))
            if resp.meta and resp.meta.get("newest_id"):
                since_id = resp.meta["newest_id"]
            backoff = x.poll_interval_s
        except (tweepy.TweepyException, OSError) as e:
            backoff = min(backoff * 2, 900)
            log.warning("Erreur API X (%s), nouvel essai dans %.0fs", e, backoff)
        await asyncio.sleep(backoff)


class _Stream(AsyncStreamingClient):
    def __init__(self, bearer_token: str, queue: asyncio.Queue[NewsItem], cfg: NewsConfig, clock: Clock):
        super().__init__(bearer_token, wait_on_rate_limit=True)
        self.queue, self.cfg, self.clock = queue, cfg, clock

    async def on_response(self, response):
        if response.data is not None:
            await self.queue.put(tweet_to_item(response.data, _usernames(response.includes), self.cfg, self.clock))

    async def on_errors(self, errors):
        log.warning("Erreur stream X: %s", errors)


async def stream_x(cfg: NewsConfig, bearer_token: str, queue: asyncio.Queue[NewsItem], clock: Clock) -> None:
    stream = _Stream(bearer_token, queue, cfg, clock)
    existing = await stream.get_rules()
    if existing.data:
        await stream.delete_rules([r.id for r in existing.data])
    await stream.add_rules(tweepy.StreamRule(value=build_query(cfg.x.accounts), tag="polybot"))
    log.info("Filtered stream X demarre")
    await stream.filter(tweet_fields=TWEET_FIELDS, expansions=["author_id"], user_fields=["username"])


async def run_x(cfg: NewsConfig, bearer_token: str, queue: asyncio.Queue[NewsItem], clock: Clock) -> None:
    if cfg.x.mode == "stream":
        await stream_x(cfg, bearer_token, queue, clock)
    else:
        await poll_x(cfg, bearer_token, queue, clock)
