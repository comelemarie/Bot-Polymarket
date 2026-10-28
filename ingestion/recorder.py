"""Enregistrement brut et horodate des flux pour le replay.

Fichiers JSON lines gzip, un par heure UTC: data/recordings/AAAA-MM-JJ/HH.jsonl.gz
Chaque ligne: {"t": horodatage de RECEPTION (epoch UTC), "k": type, "p": contenu}
Types: ws (message brut), ws_connect / ws_disconnect (liste de tokens), news (NewsItem tel que recu,
avant dedoublonnage), markets (marches surveilles a cet instant), fee (taux de frais obtenu).
On enregistre ce qui etait connu a l'instant t, rien de plus: pas de biais de survivance dans le replay.
"""
from __future__ import annotations

import gzip
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from core.models import Market, NewsItem

log = logging.getLogger(__name__)


class Recorder:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._hour: str | None = None
        self._fh = None

    def _file(self, t: float):
        dt = datetime.fromtimestamp(t, tz=timezone.utc)
        hour = dt.strftime("%Y-%m-%d/%H")
        if hour != self._hour:
            self.close()
            path = self.root / f"{hour}.jsonl.gz"
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = gzip.open(path, "at", encoding="utf-8")
            self._hour = hour
        return self._fh

    def __call__(self, kind: str, payload, t: float) -> None:
        try:
            self._file(t).write(json.dumps({"t": t, "k": kind, "p": payload}, default=str, ensure_ascii=False) + "\n")
        except OSError as e:  # disque plein, etc.: l'enregistrement ne doit jamais bloquer le bot
            log.error("Enregistrement impossible: %s", e)

    def news(self, item: NewsItem) -> None:
        d = asdict(item)
        d["published_at"] = item.published_at.timestamp() if item.published_at else None
        d["received_at"] = item.received_at.timestamp()
        self("news", d, item.received_at.timestamp())

    def markets(self, markets: dict[str, Market], t: float) -> None:
        self("markets", [asdict(m) for m in markets.values()], t)

    def flush(self) -> None:
        if self._fh:
            self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
            self._hour = None


def read_recordings(root: str | Path, start: float, end: float) -> Iterator[dict]:
    """Lignes enregistrees dans [start, end], dans l'ordre chronologique de reception."""
    root = Path(root)
    rows: list[dict] = []
    for path in sorted(root.glob("*/*.jsonl.gz")):
        try:
            hour = datetime.strptime(f"{path.parent.name} {path.name[:2]}", "%Y-%m-%d %H").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if hour.timestamp() + 3600 < start or hour.timestamp() > end:
            continue
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # derniere ligne tronquee par un arret brutal
                if start <= row["t"] <= end:
                    rows.append(row)
    rows.sort(key=lambda r: r["t"])  # tri stable: l'ordre d'ecriture est conserve a horodatage egal
    yield from rows


def news_from_record(d: dict) -> NewsItem:
    return NewsItem(
        id=d["id"], source=d["source"], title=d["title"], content=d.get("content", ""), url=d.get("url", ""),
        published_at=datetime.fromtimestamp(d["published_at"], tz=timezone.utc) if d.get("published_at") else None,
        received_at=datetime.fromtimestamp(d["received_at"], tz=timezone.utc),
        reliability=d.get("reliability", 0.5),
    )


def market_from_record(d: dict) -> Market:
    return Market(**d)
