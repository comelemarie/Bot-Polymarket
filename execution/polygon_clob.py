"""Courtier REEL: CLOB Polymarket (reseau Polygon) via py-clob-client 0.34.x.

Instancie uniquement en mode LIVE apres le deverrouillage de core/modes.py.
Classification des resultats d'envoi (d'apres py_clob_client.http_helpers et exceptions):
  - erreur avant l'envoi (construction/signature)        -> rejected (rien n'a quitte la machine)
  - PolyApiException status 4xx (sauf 408)                -> rejected (refus explicite du serveur)
  - PolyApiException sans status (httpx.RequestError: delai, coupure), 408, 5xx
                                                         -> unknown: l'ordre a PU etre execute
  - reponse success + status "matched"                    -> filled
  - status "delayed" / "live" / autre                     -> unknown (verification obligatoire)
Un ordre UNKNOWN bloque toute nouvelle entree sur le marche jusqu'a reconciliation.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from core.config import Secrets
from core.models import OrderRequest, SubmitResult
from ingestion.market_state import BookState

log = logging.getLogger(__name__)

CLOB_HOST = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
POLYGON_CHAIN_ID = 137
USDC_DECIMALS = 6


def classify_exception(e: Exception) -> SubmitResult:
    from py_clob_client.exceptions import PolyApiException

    if isinstance(e, PolyApiException):
        code = e.status_code
        if code is not None and 400 <= code < 500 and code != 408:
            return SubmitResult("rejected", error=f"HTTP {code}: {e.error_msg}")
        return SubmitResult("unknown", error=f"reponse ambigue (HTTP {code}): {e.error_msg}")
    return SubmitResult("unknown", error=f"exception pendant l'envoi: {e!r}")


def parse_post_response(resp: dict | None, side: str, req: OrderRequest) -> SubmitResult:
    if not isinstance(resp, dict):
        return SubmitResult("unknown", error=f"reponse inattendue: {resp!r}")
    oid = str(resp.get("orderID") or resp.get("orderId") or "")
    status = str(resp.get("status", "")).lower()
    if not resp.get("success", False):
        return SubmitResult("rejected", error=str(resp.get("errorMsg") or resp), exchange_order_id=oid, raw=resp)
    if status != "matched":
        return SubmitResult("unknown", error=f"statut '{status}'", exchange_order_id=oid, raw=resp)
    try:  # BUY: making = USDC, taking = parts recues. SELL: making = parts, taking = USDC.
        making, taking = float(resp["makingAmount"]), float(resp["takingAmount"])
    except (KeyError, TypeError, ValueError):
        return SubmitResult("unknown", error="montants executes absents", exchange_order_id=oid, raw=resp)
    if making <= 0 or taking <= 0:
        return SubmitResult("unknown", error="montants executes nuls", exchange_order_id=oid, raw=resp)
    if side == "BUY":
        return SubmitResult("filled", taking, making / taking, 0.0, oid, raw={**resp, "cost_usdc": making})
    return SubmitResult("filled", making, taking / making, 0.0, oid, raw={**resp, "proceeds_usdc": taking})


class LiveClobBroker:
    live = True

    def __init__(self, secrets: Secrets):
        self.secrets = secrets
        self.client = None
        self.http = httpx.AsyncClient(timeout=15)

    async def start(self) -> None:
        from py_clob_client.client import ClobClient

        def _init():
            client = ClobClient(CLOB_HOST, chain_id=POLYGON_CHAIN_ID, key=self.secrets.polygon_private_key,
                                signature_type=self.secrets.signature_type, funder=self.secrets.funder_address)
            client.set_api_creds(client.create_or_derive_api_creds())
            return client

        self.client = await asyncio.to_thread(_init)

    @property
    def account(self) -> str:
        return self.secrets.funder_address or self.client.get_address()

    async def balance(self) -> float | None:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        try:
            resp = await asyncio.to_thread(self.client.get_balance_allowance,
                                           BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            return int(resp["balance"]) / 10**USDC_DECIMALS
        except Exception as e:
            log.error("Solde USDC indisponible: %s", e)
            return None

    async def submit_fok(self, req: OrderRequest, book: BookState, fee_rate: float) -> SubmitResult:
        from py_clob_client.clob_types import OrderArgs, OrderType

        try:
            signed = await asyncio.to_thread(
                self.client.create_order,
                OrderArgs(token_id=req.token_id, price=req.limit_price, size=req.shares, side=req.side))
        except Exception as e:
            return SubmitResult("rejected", error=f"construction de l'ordre impossible (non envoye): {e!r}")
        try:
            resp = await asyncio.to_thread(self.client.post_order, signed, OrderType.FOK)
        except Exception as e:
            return classify_exception(e)
        return parse_post_response(resp, req.side, req)

    async def lookup_order(self, exchange_order_id: str) -> SubmitResult | None:
        """Etat reel d'un ordre connu. None si la requete echoue (l'incertitude demeure)."""
        try:
            o = await asyncio.to_thread(self.client.get_order, exchange_order_id)
        except Exception as e:
            log.warning("Lecture de l'ordre %s impossible: %s", exchange_order_id, e)
            return None
        if not isinstance(o, dict) or not o:
            return None
        status = str(o.get("status", "")).lower()
        matched = float(o.get("size_matched") or 0)
        price = float(o.get("price") or 0)
        if status in ("live", "open"):
            return SubmitResult("unknown", matched, price, exchange_order_id=exchange_order_id, raw=o,
                                error="ordre au carnet")
        if matched > 0:
            return SubmitResult("filled", matched, price, exchange_order_id=exchange_order_id, raw=o)
        if status in ("canceled", "cancelled", "unmatched", "expired"):
            return SubmitResult("rejected", exchange_order_id=exchange_order_id, raw=o, error=status)
        return None

    async def cancel(self, exchange_order_id: str) -> None:
        await asyncio.to_thread(self.client.cancel, exchange_order_id)

    async def positions(self) -> dict[str, float] | None:
        """Positions du compte selon la Data API. None si indisponible (=> exposition inconnue)."""
        try:
            resp = await self.http.get(f"{DATA_API}/positions",
                                       params={"user": self.account, "sizeThreshold": 0, "limit": 500})
            resp.raise_for_status()
            return {str(p["asset"]): float(p["size"]) for p in resp.json() if float(p.get("size") or 0) > 0}
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.error("Positions du compte indisponibles: %s", e)
            return None

    async def cancel_all(self) -> None:
        await asyncio.to_thread(self.client.cancel_all)
