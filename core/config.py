"""Chargement et validation de config.json + .env."""
from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field


class ClaudeConfig(BaseModel):
    model: str = "claude-sonnet-5-5"
    effort: str = "medium"
    max_tokens: int = 8000
    timeout_s: float = 90
    max_calls_per_hour: int = 20
    max_calls_per_day: int = 150
    max_concurrent_calls: int = 2


class TriggerConfig(BaseModel):
    require_price_move: bool = True
    price_move_threshold: float = 0.03
    price_window_s: float = 300
    news_window_s: float = 180
    market_cooldown_s: float = 900


class RiskConfig(BaseModel):
    kelly_fraction: float = Field(0.25, gt=0, le=1)
    max_exposure_per_market_pct: float = Field(0.05, gt=0, le=1)
    max_total_exposure_pct: float = Field(0.40, gt=0, le=1)
    min_usdc_reserve: float = Field(100.0, ge=0)
    min_edge: float = Field(0.05, ge=0)
    min_confidence: float = Field(0.6, ge=0, le=1)
    max_spread: float = 0.05
    max_slippage: float = 0.02
    max_book_take_pct: float = Field(0.5, gt=0, le=1)
    min_order_usdc: float = 5.0
    stop_loss_pct: float = Field(0.30, gt=0, lt=1)
    take_profit_price: float = Field(0.97, gt=0, le=1)
    max_daily_loss_pct: float = Field(0.10, gt=0, le=1)
    position_check_interval_s: float = 15


class XConfig(BaseModel):
    enabled: bool = False
    mode: str = "poll"  # "poll" (recherche recente) ou "stream" (filtered stream, tier Pro)
    poll_interval_s: float = 60
    accounts: list[str] = []
    max_results: int = 50


class RssConfig(BaseModel):
    enabled: bool = True
    poll_interval_s: float = 60
    feeds: list[str] = []


class NewsConfig(BaseModel):
    x: XConfig = XConfig()
    rss: RssConfig = RssConfig()


class MarketWatch(BaseModel):
    slug: str
    keywords: list[str]
    notes: str = ""


class BotConfig(BaseModel):
    dry_run: bool = True
    paper_bankroll_usdc: float = 1000.0
    claude: ClaudeConfig = ClaudeConfig()
    trigger: TriggerConfig = TriggerConfig()
    risk: RiskConfig = RiskConfig()
    news: NewsConfig = NewsConfig()
    markets: list[MarketWatch]


class Secrets(BaseModel):
    anthropic_api_key: str | None
    x_bearer_token: str | None
    polygon_private_key: str | None
    signature_type: int
    funder_address: str | None


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
    )
