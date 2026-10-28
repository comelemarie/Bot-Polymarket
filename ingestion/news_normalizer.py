"""Normalisation et dedoublonnage multi-sources des actualites.

- Empreinte exacte du titre normalise (minuscules, ponctuation retiree, suffixe " - Editeur" de Google News retire).
- Similarite de Jaccard sur les mots significatifs pour les reformulations legeres.
- Une reprise n'est pas reanalysee mais incremente le compteur de corroborations de l'original,
  qui est transmis a l'analyse comme indice de qualite.
"""
from __future__ import annotations

import hashlib
import html
import re
from collections import deque
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from core.clock import Clock
from core.config import NewsConfig
from core.models import NewsItem
from core.store import Store

_TAG = re.compile(r"<[^>]+>")
_SUFFIX = re.compile(r"\s+[-|–—]\s+[^-|–—]{2,40}$")  # " - Reuters", " | BBC News"
_NON_WORD = re.compile(r"[^\w\s]")
_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "has", "have", "will", "after", "over",
    "into", "about", "says", "said", "new", "its", "his", "her", "their", "amid", "what", "who", "how", "why",
}


def clean_text(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", text))).strip()


def normalize_title(title: str) -> str:
    t = _SUFFIX.sub("", clean_text(title))
    t = _NON_WORD.sub(" ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def fingerprint(title: str) -> str:
    return hashlib.sha1(normalize_title(title).encode()).hexdigest()[:16]


def tokens(title: str) -> frozenset[str]:
    return frozenset(w for w in normalize_title(title).split() if len(w) >= 3 and w not in _STOP)


def canonical_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url)
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith(("utm_", "ref"))])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, ""))


def source_reliability(source: str, cfg: NewsConfig) -> float:
    if source in cfg.source_reliability:
        return cfg.source_reliability[source]
    for prefix, value in cfg.source_reliability.items():
        if source.startswith(prefix):
            return value
    return cfg.default_reliability


@dataclass
class _Seen:
    ts: float
    id: str
    fp: str
    toks: frozenset[str]
    url: str


class NewsDeduplicator:
    def __init__(self, store: Store, cfg: NewsConfig, clock: Clock, max_recent: int = 5000):
        self.store = store
        self.cfg = cfg
        self.clock = clock
        self.recent: deque[_Seen] = deque(maxlen=max_recent)
        since = clock.now() - cfg.dedup_window_h * 3600
        for r in self.store.query(
            "SELECT id, fingerprint, title, url, received_at FROM news WHERE received_at>=? AND duplicate_of IS NULL "
            "ORDER BY received_at", (since,)
        ):
            self.recent.append(_Seen(r["received_at"], r["id"], r["fingerprint"], tokens(r["title"]),
                                     canonical_url(r["url"] or "")))

    def _find_original(self, item: NewsItem, toks: frozenset[str], url: str) -> _Seen | None:
        horizon = self.clock.now() - self.cfg.dedup_window_h * 3600
        for s in reversed(self.recent):
            if s.ts < horizon:
                break
            if s.fp == item.fingerprint or (url and s.url == url):
                return s
            if len(toks) >= 4 and len(s.toks) >= 4:
                jaccard = len(toks & s.toks) / len(toks | s.toks)
                if jaccard >= self.cfg.dedup_similarity:
                    return s
        return None

    def process(self, item: NewsItem) -> NewsItem | None:
        """Enregistre l'actualite. Retourne l'item s'il est nouveau, None si doublon (deja vu ou reprise)."""
        if self.store.one("SELECT 1 FROM news WHERE id=?", (item.id,)):
            return None  # meme identifiant de source: deja traite (y compris avant un redemarrage)
        item.fingerprint = item.fingerprint or fingerprint(item.title)
        toks, url = tokens(item.title), canonical_url(item.url)
        original = self._find_original(item, toks, url)
        self.store.insert("news", {
            "id": item.id, "fingerprint": item.fingerprint, "source": item.source, "title": item.title,
            "content": item.content, "url": item.url,
            "published_at": item.published_at.timestamp() if item.published_at else None,
            "received_at": item.received_at.timestamp(), "reliability": item.reliability,
            "duplicate_of": original.id if original else None,
        }, or_ignore=True)
        if original is not None:
            return None
        item.corroborations = 0
        self.recent.append(_Seen(item.received_at.timestamp(), item.id, item.fingerprint, toks, url))
        return item

    def corroborations(self, news_id: str) -> int:
        row = self.store.one("SELECT COUNT(*) AS n FROM news WHERE duplicate_of=?", (news_id,))
        return row["n"] if row else 0
