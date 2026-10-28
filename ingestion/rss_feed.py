"""Flux RSS d'agences de presse: source d'actualite gratuite et robuste, complementaire a X.

Respecte les sources: un seul GET par flux et par intervalle, User-Agent identifiable, ETag/Last-Modified
pour eviter de retelecharger un flux inchange.
"""
from __future__ import annotations

import asyncio
import calendar
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

import feedparser
import httpx

from core.clock import Clock
from core.config import NewsConfig
from core.models import NewsItem
from ingestion.news_normalizer import clean_text, source_reliability

log = logging.getLogger(__name__)


def _published(entry) -> datetime | None:
    """Date de publication declaree par la source, ou None. Jamais remplacee par l'heure courante."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
    return None


def entry_to_item(entry, host: str, cfg: NewsConfig, received_at: datetime) -> NewsItem | None:
    uid = entry.get("id") or entry.get("link")
    title = clean_text(entry.get("title"))
    if not uid or not title:
        return None  # pas d'identifiant stable ou pas de titre: inexploitable
    content = clean_text(entry.get("summary"))
    source = f"rss:{host}"
    return NewsItem(
        id=f"rss:{uid}", source=source, title=title, content=content if content != title else "",
        url=entry.get("link", ""), published_at=_published(entry), received_at=received_at,
        reliability=source_reliability(source, cfg),
    )


async def poll_rss(cfg: NewsConfig, queue: asyncio.Queue[NewsItem], clock: Clock) -> None:
    rss = cfg.rss
    primed: set[str] = set()  # au premier passage, on ne rejoue pas l'historique du flux
    seen: dict[str, None] = {}
    validators: dict[str, dict[str, str]] = {}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "polybot/2.0"}) as http:
        while True:
            for url in rss.feeds:
                try:
                    resp = await http.get(url, headers=validators.get(url, {}))
                    if resp.status_code == 304:
                        continue
                    resp.raise_for_status()
                except httpx.HTTPError as e:
                    log.warning("RSS %s indisponible: %s", url, e)
                    continue
                validators[url] = {k: v for k, v in (("If-None-Match", resp.headers.get("etag")),
                                                     ("If-Modified-Since", resp.headers.get("last-modified"))) if v}
                received_at = clock.utcnow()
                feed = feedparser.parse(resp.content)
                host = urlparse(url).netloc
                for entry in reversed(feed.entries):
                    item = entry_to_item(entry, host, cfg, received_at)
                    if item is None or item.id in seen:
                        continue
                    seen[item.id] = None
                    if url in primed:
                        await queue.put(item)
                primed.add(url)
            if len(seen) > 20_000:
                seen = dict.fromkeys(list(seen)[-10_000:])
            await asyncio.sleep(rss.poll_interval_s)
