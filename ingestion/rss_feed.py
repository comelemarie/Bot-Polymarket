"""Flux RSS d'agences de presse: source d'actualite gratuite et robuste, complementaire a X."""
from __future__ import annotations

import asyncio
import calendar
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

import feedparser
import httpx

from core.config import RssConfig
from core.models import NewsItem

log = logging.getLogger(__name__)


def _published(entry) -> datetime:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
    return datetime.now(timezone.utc)


async def poll_rss(cfg: RssConfig, queue: asyncio.Queue[NewsItem]) -> None:
    seen: dict[str, None] = {}  # dict = ensemble ordonne par insertion
    primed: set[str] = set()  # flux deja lus une fois
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "polybot/1.0"}) as http:
        while True:
            for url in cfg.feeds:
                try:
                    resp = await http.get(url)
                    resp.raise_for_status()
                except httpx.HTTPError as e:
                    log.warning("RSS %s indisponible: %s", url, e)
                    continue
                feed = feedparser.parse(resp.content)
                host = urlparse(url).netloc
                for entry in reversed(feed.entries):
                    uid = entry.get("id") or entry.get("link") or entry.get("title", "")
                    if uid in seen:
                        continue
                    seen[uid] = None
                    if url not in primed:
                        continue  # pas de rejeu de l'historique au demarrage
                    text = entry.get("title", "")
                    summary = entry.get("summary", "")
                    if summary and summary != text:
                        text = f"{text}\n{summary}"
                    await queue.put(
                        NewsItem(
                            source=f"rss:{host}",
                            text=text,
                            url=entry.get("link", ""),
                            published_at=_published(entry),
                            id=f"rss:{uid}",
                        )
                    )
                primed.add(url)
            if len(seen) > 20_000:
                seen = dict.fromkeys(list(seen)[-10_000:])
            await asyncio.sleep(cfg.poll_interval_s)
