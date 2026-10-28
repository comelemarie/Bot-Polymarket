"""Modes de fonctionnement et deverrouillage du mode reel.

PAPER (defaut): flux en direct, aucun ordre reel. Le client d'ordres reel n'est jamais instancie.
REPLAY: donnees enregistrees, horloge simulee, aucun acces reseau d'execution.
LIVE: ordres reels. Exige QUATRE conditions independantes, pour qu'aucune modification accidentelle
      isolee (un parametre, une variable d'environnement) ne suffise:
        1. "mode": "live" dans config.json
        2. "live.confirmation" == LIVE_CONFIRMATION dans config.json
        3. POLYBOT_LIVE_UNLOCK == LIVE_CONFIRMATION dans l'environnement (.env)
        4. l'option --confirm-live sur la ligne de commande
      plus des identifiants complets et des limites de risque dans les bornes autorisees.
Les conditions dynamiques (flux synchronises, reconciliation, moteur de risque) sont verifiees
au demarrage puis avant chaque ordre.
"""
from __future__ import annotations

from core.config import LIVE_CONFIRMATION, BotConfig, Secrets


class LiveGateError(RuntimeError):
    pass


def live_gate_failures(cfg: BotConfig, secrets: Secrets, cli_confirm: bool) -> list[str]:
    fails = []
    if cfg.mode != "live":
        fails.append('config.json: "mode" n\'est pas "live"')
    if cfg.live.confirmation != LIVE_CONFIRMATION:
        fails.append(f'config.json: "live.confirmation" doit valoir "{LIVE_CONFIRMATION}"')
    if secrets.live_unlock != LIVE_CONFIRMATION:
        fails.append(f'.env: POLYBOT_LIVE_UNLOCK doit valoir "{LIVE_CONFIRMATION}"')
    if not cli_confirm:
        fails.append("ligne de commande: option --confirm-live absente")
    if not secrets.polygon_private_key:
        fails.append(".env: POLYGON_PRIVATE_KEY manquant")
    if secrets.signature_type in (1, 2) and not secrets.funder_address:
        fails.append(".env: POLYMARKET_FUNDER_ADDRESS requis pour signature_type 1 ou 2")
    if not secrets.anthropic_api_key:
        fails.append(".env: ANTHROPIC_API_KEY manquant")
    r, lim = cfg.risk, cfg.live
    if r.kelly_fraction > lim.max_allowed_kelly_fraction:
        fails.append(f"risk.kelly_fraction {r.kelly_fraction} > {lim.max_allowed_kelly_fraction} autorise en LIVE")
    if r.max_exposure_per_market_pct > lim.max_allowed_exposure_per_market_pct:
        fails.append("risk.max_exposure_per_market_pct depasse la borne LIVE")
    if r.max_total_exposure_pct > lim.max_allowed_total_exposure_pct:
        fails.append("risk.max_total_exposure_pct depasse la borne LIVE")
    if r.max_exposure_per_event_pct < r.max_exposure_per_market_pct:
        fails.append("risk.max_exposure_per_event_pct doit etre >= max_exposure_per_market_pct")
    if cfg.edge.min_net_edge <= 0:
        fails.append("edge.min_net_edge doit etre > 0 en LIVE")
    return fails


def assert_live_allowed(cfg: BotConfig, secrets: Secrets, cli_confirm: bool) -> None:
    fails = live_gate_failures(cfg, secrets, cli_confirm)
    if fails:
        raise LiveGateError("Mode LIVE refuse:\n  - " + "\n  - ".join(fails))


def effective_mode(cfg: BotConfig, cli_mode: str | None) -> str:
    """Le mode effectif est PAPER sauf demande explicite. --mode sur la CLI ne peut qu'abaisser le risque."""
    if cli_mode == "replay":
        return "replay"
    if cli_mode == "paper":
        return "paper"
    return cfg.mode
