"""Portefeuille persistant (SQLite): positions, liquidites papier, PnL du jour, plus-haut d'equity, coupe-circuits."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.store import Store


@dataclass
class Position:
    market_slug: str
    token_id: str
    outcome: str
    shares: float
    avg_price: float  # cout par part detenue, frais inclus
    opened_at: float = 0.0
    event_id: str = ""
    tags: list[str] = field(default_factory=list)
    entry_decision_id: str = ""
    max_loss_usdc: float | None = None
    thesis_p_low: float | None = None
    thesis_p_high: float | None = None
    status: str = "open"

    @property
    def cost_usdc(self) -> float:
        return self.shares * self.avg_price


@dataclass
class DailyStats:
    day: str  # date UTC
    start_equity: float
    halted: bool = False


class Portfolio:
    def __init__(self, store: Store):
        self.store = store

    # --- positions ---
    def positions(self) -> dict[str, Position]:
        out = {}
        for r in self.store.query("SELECT * FROM positions"):
            out[r["token_id"]] = Position(
                market_slug=r["market_slug"], token_id=r["token_id"], outcome=r["outcome"], shares=r["shares"],
                avg_price=r["avg_price"], opened_at=r["opened_at"], event_id=r["event_id"] or "",
                tags=json.loads(r["tags"] or "[]"), entry_decision_id=r["entry_decision_id"] or "",
                max_loss_usdc=r["max_loss_usdc"], thesis_p_low=r["thesis_p_low"], thesis_p_high=r["thesis_p_high"],
                status=r["status"],
            )
        return out

    def position(self, token_id: str) -> Position | None:
        return self.positions().get(token_id)

    def add_fill(self, *, market_slug: str, token_id: str, outcome: str, shares: float, cost_usdc: float, ts: float,
                 event_id: str = "", tags: list[str] | None = None, decision_id: str = "",
                 max_loss_usdc: float | None = None, p_low: float | None = None, p_high: float | None = None) -> None:
        """`shares` = parts effectivement recues (apres frais preleves en parts), `cost_usdc` = USDC payes."""
        with self.store.tx():
            pos = self.position(token_id)
            if pos is None:
                self.store.insert("positions", {
                    "token_id": token_id, "market_slug": market_slug, "event_id": event_id, "tags": tags or [],
                    "outcome": outcome, "shares": shares, "avg_price": cost_usdc / shares, "opened_at": ts,
                    "entry_decision_id": decision_id, "max_loss_usdc": max_loss_usdc,
                    "thesis_p_low": p_low, "thesis_p_high": p_high,
                })
            else:
                total = pos.shares + shares
                self.store.update("positions", "token_id", token_id, {
                    "shares": total,
                    "avg_price": (pos.cost_usdc + cost_usdc) / total,
                    "max_loss_usdc": (pos.max_loss_usdc or 0) + (max_loss_usdc or 0),
                })

    def reduce(self, *, token_id: str, shares: float, proceeds_usdc: float, fee_usdc: float, ts: float,
               reason: str, client_order_id: str | None = None) -> float:
        """Sortie partielle ou totale. `proceeds_usdc` = USDC recus nets de frais. Retourne le PnL realise."""
        with self.store.tx():
            pos = self.position(token_id)
            if pos is None:
                raise KeyError(f"aucune position sur {token_id}")
            shares = min(shares, pos.shares)
            cost_basis = shares * pos.avg_price
            pnl = proceeds_usdc - cost_basis
            remaining = pos.shares - shares
            if remaining <= 1e-6:
                self.store.conn.execute("DELETE FROM positions WHERE token_id=?", (token_id,))
            else:
                frac = remaining / pos.shares
                self.store.update("positions", "token_id", token_id, {
                    "shares": remaining,
                    "max_loss_usdc": pos.max_loss_usdc * frac if pos.max_loss_usdc is not None else None,
                })
            self.store.insert("exits", {
                "token_id": token_id, "market_slug": pos.market_slug, "ts": ts, "reason": reason, "shares": shares,
                "price": (proceeds_usdc + fee_usdc) / shares if shares else 0.0, "fee_usdc": fee_usdc,
                "pnl_usdc": pnl, "cost_basis_usdc": cost_basis, "client_order_id": client_order_id,
            })
        return pnl

    def set_shares(self, token_id: str, shares: float) -> None:
        """Alignement sur le compte reel (reconciliation)."""
        if shares <= 1e-6:
            self.store.conn.execute("DELETE FROM positions WHERE token_id=?", (token_id,))
        else:
            self.store.update("positions", "token_id", token_id, {"shares": shares})

    # --- expositions (au cout) ---
    def exposure_for_market(self, slug: str) -> float:
        return sum(p.cost_usdc for p in self.positions().values() if p.market_slug == slug)

    def exposure_for_event(self, event_id: str) -> float:
        if not event_id:
            return 0.0
        return sum(p.cost_usdc for p in self.positions().values() if p.event_id == event_id)

    def exposure_for_tag(self, tag: str) -> float:
        return sum(p.cost_usdc for p in self.positions().values() if tag in p.tags)

    def total_exposure(self) -> float:
        return sum(p.cost_usdc for p in self.positions().values())

    # --- liquidites papier ---
    @property
    def paper_usdc(self) -> float | None:
        return self.store.kv_get("paper_usdc")

    def set_paper_usdc(self, value: float) -> None:
        self.store.kv_set("paper_usdc", value)

    # --- PnL du jour (UTC), plus-haut d'equity ---
    def roll_day(self, equity: float, ts: float) -> DailyStats:
        today = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        d = self.store.kv_get("daily")
        if d is None or d["day"] != today:
            d = {"day": today, "start_equity": equity, "halted": False}
            self.store.kv_set("daily", d, ts)
        return DailyStats(**d)

    def set_daily_halted(self, ts: float) -> None:
        d = self.store.kv_get("daily")
        if d:
            d["halted"] = True
            self.store.kv_set("daily", d, ts)

    def update_peak(self, equity: float, ts: float) -> float:
        peak = self.store.kv_get("peak_equity")
        if peak is None or equity > peak:
            peak = equity
            self.store.kv_set("peak_equity", peak, ts)
        return peak

    def record_equity(self, ts: float, usdc: float, equity: float) -> None:
        self.store.insert("equity", {"ts": ts, "usdc": usdc, "equity": equity, "exposure": self.total_exposure()},
                          or_ignore=True)

    # --- coupe-circuits persistants (leves uniquement par une action manuelle --resume) ---
    def halts(self) -> dict[str, str]:
        return self.store.kv_get("halts", {})

    def halt(self, reason: str, details: str, ts: float) -> None:
        h = self.halts()
        if reason not in h:
            h[reason] = details
            self.store.kv_set("halts", h, ts)

    def clear_halts(self, ts: float) -> None:
        self.store.kv_set("halts", {}, ts)
