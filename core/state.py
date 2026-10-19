"""Etat persistant du portefeuille (positions, PnL du jour) + journal des decisions.

Ecrit sur disque a chaque changement pour survivre aux redemarrages (crash, coupure).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path


@dataclass
class Position:
    market_slug: str
    token_id: str
    outcome: str
    shares: float
    avg_price: float
    opened_at: float = field(default_factory=time.time)

    @property
    def cost_usdc(self) -> float:
        return self.shares * self.avg_price


@dataclass
class DailyStats:
    day: str
    start_equity: float
    realized_pnl: float = 0.0
    halted: bool = False


class PortfolioState:
    def __init__(self, path: str | Path = "state/portfolio.json", journal: str | Path = "logs/decisions.jsonl"):
        self.path = Path(path)
        self.journal_path = Path(journal)
        self.positions: dict[str, Position] = {}
        self.paper_usdc: float | None = None
        self.daily: DailyStats | None = None
        self._load()

    # --- persistance ---
    def _load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.positions = {k: Position(**v) for k, v in data.get("positions", {}).items()}
        self.paper_usdc = data.get("paper_usdc")
        if data.get("daily"):
            self.daily = DailyStats(**data["daily"])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "positions": {k: asdict(v) for k, v in self.positions.items()},
                    "paper_usdc": self.paper_usdc,
                    "daily": asdict(self.daily) if self.daily else None,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)  # ecriture atomique

    def journal(self, kind: str, **payload) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        line = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **payload}
        with self.journal_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, default=str, ensure_ascii=False) + "\n")

    # --- positions ---
    def add_fill(self, market_slug: str, token_id: str, outcome: str, price: float, shares: float) -> None:
        pos = self.positions.get(token_id)
        if pos is None:
            self.positions[token_id] = Position(market_slug, token_id, outcome, shares, price)
        else:
            total = pos.shares + shares
            pos.avg_price = (pos.avg_price * pos.shares + price * shares) / total
            pos.shares = total
        self.save()

    def reduce(self, token_id: str, price: float, shares: float) -> float:
        """Vend `shares` du token, retourne le PnL realise."""
        pos = self.positions[token_id]
        shares = min(shares, pos.shares)
        pnl = (price - pos.avg_price) * shares
        pos.shares -= shares
        if pos.shares <= 1e-6:
            del self.positions[token_id]
        if self.daily:
            self.daily.realized_pnl += pnl
        self.save()
        return pnl

    def exposure_for_market(self, market_slug: str) -> float:
        return sum(p.cost_usdc for p in self.positions.values() if p.market_slug == market_slug)

    def total_exposure(self) -> float:
        return sum(p.cost_usdc for p in self.positions.values())

    # --- kill switch journalier ---
    def roll_day(self, equity: float) -> DailyStats:
        today = date.today().isoformat()
        if self.daily is None or self.daily.day != today:
            self.daily = DailyStats(day=today, start_equity=equity)
            self.save()
        return self.daily
