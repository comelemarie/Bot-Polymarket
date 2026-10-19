"""Envoi des ordres sur le CLOB Polymarket (py-clob-client), avec mode papier (dry_run).

Ordres FOK (fill-or-kill) uniquement: soit l'ordre est entierement execute au prix limite ou mieux,
soit il est annule. Aucun ordre ne reste dans le carnet a l'insu du bot.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import AssetType, BalanceAllowanceParams, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from core.config import Secrets
from core.models import Fill, OrderBook
from core.state import PortfolioState

log = logging.getLogger(__name__)

CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137
USDC_DECIMALS = 6


def simulate_fok(levels: dict[float, float], shares: float, limit: float, side: str) -> float | None:
    """Prix moyen d'execution d'un FOK contre le carnet, ou None si profondeur insuffisante."""
    if side == BUY:
        prices = sorted(p for p in levels if p <= limit + 1e-9)
    else:
        prices = sorted((p for p in levels if p >= limit - 1e-9), reverse=True)
    remaining, cost = shares, 0.0
    for p in prices:
        take = min(remaining, levels[p])
        cost += take * p
        remaining -= take
        if remaining <= 1e-9:
            return cost / shares
    return None


class ClobExecutor:
    def __init__(self, secrets: Secrets, dry_run: bool, state: PortfolioState, paper_bankroll: float):
        self.secrets = secrets
        self.dry_run = dry_run
        self.state = state
        self.paper_bankroll = paper_bankroll
        self.client: ClobClient | None = None

    async def start(self) -> None:
        if self.dry_run:
            if self.state.paper_usdc is None:
                self.state.paper_usdc = self.paper_bankroll
                self.state.save()
            log.warning("MODE PAPIER (dry_run): aucun ordre reel ne sera envoye. Solde fictif %.2f USDC",
                        self.state.paper_usdc)
            return
        if not self.secrets.polygon_private_key:
            raise RuntimeError("POLYGON_PRIVATE_KEY manquant dans .env (requis hors dry_run)")

        def _init() -> ClobClient:
            client = ClobClient(
                CLOB_HOST,
                chain_id=POLYGON_CHAIN_ID,
                key=self.secrets.polygon_private_key,
                signature_type=self.secrets.signature_type,
                funder=self.secrets.funder_address,
            )
            client.set_api_creds(client.create_or_derive_api_creds())
            return client

        self.client = await asyncio.to_thread(_init)
        log.warning("MODE REEL: connecte au CLOB Polymarket. Solde %.2f USDC", await self.usdc_balance())

    async def usdc_balance(self) -> float:
        if self.dry_run:
            return float(self.state.paper_usdc or 0.0)
        resp = await asyncio.to_thread(
            self.client.get_balance_allowance, BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        return int(resp["balance"]) / 10**USDC_DECIMALS

    async def buy(self, token_id: str, limit: float, shares: float, book: OrderBook) -> Fill | None:
        return await self._execute(token_id, BUY, limit, shares, book)

    async def sell(self, token_id: str, limit: float, shares: float, book: OrderBook) -> Fill | None:
        return await self._execute(token_id, SELL, limit, shares, book)

    async def _execute(self, token_id: str, side: str, limit: float, shares: float, book: OrderBook) -> Fill | None:
        if self.dry_run:
            levels = book.asks if side == BUY else book.bids
            avg = simulate_fok(levels, shares, limit, side)
            if avg is None:
                log.info("[PAPIER] FOK %s %.2f @ %.3f annule: profondeur insuffisante", side, shares, limit)
                return None
            notional = avg * shares
            self.state.paper_usdc += -notional if side == BUY else notional
            self.state.save()
            log.info("[PAPIER] %s %.2f shares @ %.4f (%.2f USDC)", side, shares, avg, notional)
            return Fill(token_id, side, avg, shares, f"paper-{uuid.uuid4().hex[:8]}", simulated=True)

        def _post():
            signed = self.client.create_order(OrderArgs(token_id=token_id, price=limit, size=shares, side=side))
            return self.client.post_order(signed, OrderType.FOK)

        try:
            resp = await asyncio.to_thread(_post)
        except Exception as e:  # py-clob-client leve PolyApiException et diverses erreurs HTTP
            log.error("Ordre %s %s refuse: %s", side, token_id, e)
            return None
        if not resp or not resp.get("success") or resp.get("status") not in ("matched", "MATCHED"):
            log.warning("Ordre %s non execute: %s", side, resp)
            return None
        price = limit
        try:  # BUY: making=USDC, taking=shares. SELL: making=shares, taking=USDC.
            making, taking = float(resp["makingAmount"]), float(resp["takingAmount"])
            if making > 0 and taking > 0:
                price = making / taking if side == BUY else taking / making
                shares = taking if side == BUY else making
        except (KeyError, TypeError, ValueError):
            pass
        log.info("ORDRE EXECUTE %s %.2f shares @ %.4f (id %s)", side, shares, price, resp.get("orderID"))
        return Fill(token_id, side, price, shares, resp.get("orderID", ""), simulated=False)
