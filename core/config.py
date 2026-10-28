"""Configuration centralisee: config.json (parametres) + .env (secrets).

Les valeurs par defaut sont des points de depart prudents, PAS des valeurs optimales.
Leur justification et la methode de calibration sont dans docs/CONFIGURATION.md.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, model_validator

Mode = Literal["paper", "live", "replay"]
Level = Literal["low", "medium", "high"]


class _Strict(BaseModel):
    # Une cle inconnue (faute de frappe, ancien parametre) est une erreur, pas un defaut silencieux.
    model_config = ConfigDict(extra="forbid")


class ClaudeConfig(_Strict):
    model: str = "claude-sonnet-5-5"
    effort: str = "medium"
    max_tokens: int = 8000
    timeout_s: float = 90
    max_retries: int = 2  # reprises du SDK (429/5xx/reseau) avec backoff exponentiel
    max_calls_per_hour: int = 30
    max_calls_per_day: int = 300
    max_cost_usd_per_day: float = 10.0
    max_concurrent_calls: int = 2
    # Tarifs utilises pour estimer le cout (USD par million de tokens). A mettre a jour si le modele change.
    price_input_per_mtok: float = 2.0
    price_output_per_mtok: float = 10.0
    cache_ttl_s: float = 1800
    cache_price_bucket: float = 0.02  # une analyse est reutilisable si le prix n'a pas bouge de plus d'un bucket


class TriggerConfig(_Strict):
    # Voie 1: actualite sans mouvement prealable -> analyse rapide.
    news_only_enabled: bool = True
    # Voie 2: actualite + mouvement de prix dans la fenetre.
    price_move_threshold: float = 0.03
    price_window_s: float = 300
    news_window_s: float = 180
    # Voie 3: mouvement sans actualite -> anomalie journalisee + surveillance, jamais d'achat.
    anomaly_move_threshold: float = 0.05
    anomaly_watch_s: float = 1800
    market_cooldown_s: float = 900
    max_news_age_s: float = 3600
    min_source_reliability: float = 0.4
    # Pas d'appel a Claude si le marche n'est pas exploitable de toute facon.
    max_spread_for_analysis: float = 0.08
    min_depth_usdc_for_analysis: float = 200


class FeesConfig(_Strict):
    fetch_from_api: bool = True
    # Taux utilise si l'API ne repond pas. Volontairement eleve (prudent): mieux vaut rater un trade
    # que sous-estimer un cout. Formule: fee = parts * taux * p * (1 - p).
    fallback_taker_fee_rate: float = 0.07
    cache_ttl_s: float = 3600


class EdgeConfig(_Strict):
    min_net_edge: float = 0.04  # avantage net minimal par part, apres frais et slippage
    slippage_buffer: float = 0.005  # cout supplementaire suppose par part (latence, carnet qui bouge)
    # Demi-largeur minimale de l'intervalle de probabilite selon l'incertitude declaree.
    min_half_width: dict[Level, float] = {"low": 0.03, "medium": 0.07, "high": 0.15}


class RiskConfig(_Strict):
    kelly_fraction: float = Field(0.25, gt=0, le=1)
    uncertainty_size_multiplier: dict[Level, float] = {"low": 1.0, "medium": 0.5, "high": 0.0}
    min_confidence: Level = "medium"
    max_position_usdc: float = Field(100.0, gt=0)
    max_exposure_per_market_pct: float = Field(0.05, gt=0, le=1)
    max_exposure_per_event_pct: float = Field(0.08, gt=0, le=1)
    max_exposure_per_theme_pct: float = Field(0.20, gt=0, le=1)
    max_total_exposure_pct: float = Field(0.40, gt=0, le=1)
    ignored_theme_tags: list[str] = ["all", "featured", "trending", "new"]
    min_usdc_reserve: float = Field(100.0, ge=0)
    max_book_take_pct: float = Field(0.5, gt=0, le=1)
    min_order_usdc: float = 5.0
    max_daily_loss_pct: float = Field(0.05, gt=0, le=1)
    max_drawdown_pct: float = Field(0.15, gt=0, le=1)


class ExitConfig(_Strict):
    stop_loss_pct: float = Field(0.30, gt=0, lt=1)  # stop de prix: bid <= entree * (1 - x)
    max_loss_at_entry_pct: float = Field(0.40, gt=0, le=1)  # perte max fixee a l'entree (fraction du cout)
    take_profit_price: float = Field(0.97, gt=0, le=1)
    thesis_invalidation: bool = True  # sortie si une nouvelle analyse ne justifie plus la position
    exit_slippage: float = 0.02
    check_interval_s: float = 15


class MarketDataConfig(_Strict):
    resync_interval_s: float = 1800  # nouvel instantane force periodiquement
    max_silence_s: float = 90  # connexion sans aucun message (meme PONG) -> consideree morte
    verify_rest_before_order: bool = True
    max_rest_price_divergence: float = 0.01
    # Compare le carnet reconstruit aux best_bid/best_ask annonces par chaque price_change.
    # A desactiver seulement si les logs montrent des desynchronisations en boucle juste apres un instantane
    # (signe que la semantique de ces champs differe de celle supposee).
    validate_ws_best_prices: bool = True


class XConfig(_Strict):
    enabled: bool = False
    mode: Literal["poll", "stream"] = "poll"
    poll_interval_s: float = 60
    accounts: list[str] = []
    max_results: int = 50


class RssConfig(_Strict):
    enabled: bool = True
    poll_interval_s: float = 60
    feeds: list[str] = []


class NewsConfig(_Strict):
    x: XConfig = XConfig()
    rss: RssConfig = RssConfig()
    # Fiabilite a priori par source (prefixe "rss:<hote>" ou "x:@compte"). Defaut pour les autres.
    source_reliability: dict[str, float] = {}
    default_reliability: float = 0.5
    dedup_window_h: float = 24
    dedup_similarity: float = 0.8


class DiscoveryConfig(_Strict):
    """Selection automatique des marches a surveiller (en plus de la liste manuelle `markets`)."""

    enabled: bool = False
    refresh_interval_s: float = 3600
    max_markets: int = 150
    min_liquidity_usdc: float = 10_000
    min_volume_24h_usdc: float = 5_000
    min_hours_to_end: float = 24
    max_days_to_end: float = 180
    include_tags: list[str] = []
    exclude_tags: list[str] = []
    exclude_question_patterns: list[str] = []
    exclude_slugs: list[str] = []
    keyword_model: str = "claude-haiku-5-5"
    max_pages: int = 10


class MarketWatch(_Strict):
    slug: str
    keywords: list[str]
    notes: str = ""


class StorageConfig(_Strict):
    db_path: str = "state/bot.db"
    record_dir: str = "data/recordings"
    record_market_data: bool = True


class ReplayConfig(_Strict):
    analysis_source: Literal["recorded", "none"] = "recorded"
    default_model_latency_s: float = 10.0  # si l'analyse enregistree n'a pas de latence
    processing_latency_s: float = 0.5
    execution_latency_s: float = 1.0


class LiveConfig(_Strict):
    # Doit valoir exactement LIVE_CONFIRMATION, en plus de la variable d'environnement et de l'option CLI.
    confirmation: str = ""
    # Garde-fous plafonnant les limites acceptees en LIVE (une config trop agressive bloque le demarrage).
    max_allowed_kelly_fraction: float = 0.5
    max_allowed_exposure_per_market_pct: float = 0.10
    max_allowed_total_exposure_pct: float = 0.60


LIVE_CONFIRMATION = "I-ACCEPT-REAL-MONEY-RISK"


class BotConfig(_Strict):
    mode: Mode = "paper"
    paper_bankroll_usdc: float = 1000.0
    claude: ClaudeConfig = ClaudeConfig()
    trigger: TriggerConfig = TriggerConfig()
    fees: FeesConfig = FeesConfig()
    edge: EdgeConfig = EdgeConfig()
    risk: RiskConfig = RiskConfig()
    exits: ExitConfig = ExitConfig()
    market_data: MarketDataConfig = MarketDataConfig()
    news: NewsConfig = NewsConfig()
    discovery: DiscoveryConfig = DiscoveryConfig()
    storage: StorageConfig = StorageConfig()
    replay: ReplayConfig = ReplayConfig()
    live: LiveConfig = LiveConfig()
    markets: list[MarketWatch] = []

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_dry_run(cls, data):
        if isinstance(data, dict) and "dry_run" in data:
            raise ValueError(
                "'dry_run' n'existe plus: utilise \"mode\": \"paper\" (defaut) ou \"live\" "
                "(qui exige en plus un deverrouillage explicite, voir docs/LIVE.md)."
            )
        return data


class Secrets(BaseModel):
    anthropic_api_key: str | None
    x_bearer_token: str | None
    polygon_private_key: str | None
    signature_type: int
    funder_address: str | None
    live_unlock: str | None = None

    def values(self) -> list[str]:
        """Valeurs a masquer dans les logs."""
        return [v for v in (self.anthropic_api_key, self.x_bearer_token, self.polygon_private_key) if v]


def load_config(path: str | Path = "config.json") -> BotConfig:
    with open(path, encoding="utf-8") as f:
        return BotConfig.model_validate(json.load(f))


def load_secrets(env_path: str | Path = ".env") -> Secrets:
    load_dotenv(env_path)
    return Secrets(
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        x_bearer_token=os.getenv("X_BEARER_TOKEN") or None,
        polygon_private_key=os.getenv("POLYGON_PRIVATE_KEY") or None,
        signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1")),
        funder_address=os.getenv("POLYMARKET_FUNDER_ADDRESS") or None,
        live_unlock=os.getenv("POLYBOT_LIVE_UNLOCK") or None,
    )
