"""Persistance SQLite: tracabilite complete actualite -> evenement -> analyse -> decision -> ordre -> resultat.

Une seule connexion par processus (asyncio = un seul thread). Mode WAL: les commandes CLI
(arret d'urgence, rapport) peuvent lire/ecrire pendant que le bot tourne.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT,
    url TEXT,
    published_at REAL,
    received_at REAL NOT NULL,
    reliability REAL,
    duplicate_of TEXT
);
CREATE INDEX IF NOT EXISTS news_fp ON news(fingerprint);
CREATE INDEX IF NOT EXISTS news_recv ON news(received_at);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,              -- news_only | news_with_move | anomaly | filtered
    market_slug TEXT NOT NULL,
    created_at REAL NOT NULL,
    news_ids TEXT NOT NULL,          -- JSON
    price_move REAL,
    details TEXT                      -- JSON
);
CREATE INDEX IF NOT EXISTS events_market ON events(market_slug, created_at);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_id TEXT NOT NULL,            -- evenement/decision/ordre a l'origine de l'instantane
    ts REAL NOT NULL,
    market_slug TEXT NOT NULL,
    token_id TEXT NOT NULL,
    best_bid REAL, best_ask REAL,
    bid_depth_usdc REAL, ask_depth_usdc REAL,
    synced INTEGER NOT NULL,
    levels TEXT                       -- JSON: 10 meilleurs niveaux de chaque cote
);
CREATE INDEX IF NOT EXISTS snap_ref ON market_snapshots(ref_id);

CREATE TABLE IF NOT EXISTS analyses (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    market_slug TEXT NOT NULL,
    created_at REAL NOT NULL,
    model TEXT,
    request_hash TEXT NOT NULL,
    prompt TEXT,
    response TEXT,                    -- JSON brut renvoye par le modele (ou null)
    valid INTEGER NOT NULL,
    abstain INTEGER NOT NULL,
    abstain_reason TEXT,
    p_yes REAL, p_low REAL, p_high REAL,
    uncertainty TEXT, confidence TEXT,
    assessment TEXT,                  -- JSON de l'Assessment valide
    latency_s REAL,
    input_tokens INTEGER, output_tokens INTEGER,
    cost_usd REAL NOT NULL DEFAULT 0,
    request_id TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS analyses_hash ON analyses(request_hash, created_at);
CREATE INDEX IF NOT EXISTS analyses_time ON analyses(created_at);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    analysis_id TEXT,
    market_slug TEXT NOT NULL,
    created_at REAL NOT NULL,
    action TEXT NOT NULL,             -- trade | reject | exit
    reason TEXT NOT NULL,
    details TEXT                       -- JSON
);
CREATE INDEX IF NOT EXISTS decisions_market ON decisions(market_slug, created_at);

CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY, -- idempotence: une decision = au plus un ordre
    decision_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    market_slug TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    purpose TEXT NOT NULL,            -- entry | exit
    limit_price REAL NOT NULL,
    shares REAL NOT NULL,
    status TEXT NOT NULL,             -- intent | filled | rejected | unknown | cancelled
    exchange_order_id TEXT,
    filled_shares REAL NOT NULL DEFAULT 0,
    avg_price REAL,
    fee_usdc REAL NOT NULL DEFAULT 0,
    observed_best_price REAL,         -- meilleur prix vu juste avant l'envoi (mesure du slippage)
    error TEXT,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS orders_token ON orders(token_id);

CREATE TABLE IF NOT EXISTS positions (
    token_id TEXT PRIMARY KEY,
    market_slug TEXT NOT NULL,
    event_id TEXT,
    tags TEXT,                        -- JSON
    outcome TEXT NOT NULL,
    shares REAL NOT NULL,
    avg_price REAL NOT NULL,          -- cout moyen par part detenue, frais inclus
    opened_at REAL NOT NULL,
    entry_decision_id TEXT,
    max_loss_usdc REAL,
    thesis_p_low REAL,
    thesis_p_high REAL,
    status TEXT NOT NULL DEFAULT 'open'  -- open | resolved_pending_redeem
);

CREATE TABLE IF NOT EXISTS exits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id TEXT NOT NULL,
    market_slug TEXT NOT NULL,
    ts REAL NOT NULL,
    reason TEXT NOT NULL,             -- stop_price | max_loss | take_profit | thesis_invalidated | resolution | emergency
    shares REAL NOT NULL,
    price REAL NOT NULL,
    fee_usdc REAL NOT NULL DEFAULT 0,
    pnl_usdc REAL NOT NULL,
    cost_basis_usdc REAL NOT NULL,
    client_order_id TEXT
);

CREATE TABLE IF NOT EXISTS equity (
    ts REAL PRIMARY KEY,
    usdc REAL NOT NULL,
    equity REAL NOT NULL,
    exposure REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
    market_slug TEXT PRIMARY KEY,
    condition_id TEXT,
    resolved_at REAL,
    winner TEXT NOT NULL              -- YES | NO
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    component TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metrics (
    ts REAL NOT NULL,
    name TEXT NOT NULL,
    value REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._depth = 0

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Transaction atomique (imbricable)."""
        if self._depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield self.conn
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("ROLLBACK")
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("COMMIT")

    # --- utilitaires generiques ---
    def insert(self, table: str, row: dict[str, Any], or_ignore: bool = False) -> bool:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
        cur = self.conn.execute(f"{verb} INTO {table} ({cols}) VALUES ({marks})", [_enc(v) for v in row.values()])
        return cur.rowcount > 0

    def upsert(self, table: str, row: dict[str, Any], key: str) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        updates = ", ".join(f"{c}=excluded.{c}" for c in row if c != key)
        self.conn.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({marks}) ON CONFLICT({key}) DO UPDATE SET {updates}",
            [_enc(v) for v in row.values()],
        )

    def update(self, table: str, key: str, key_value: Any, fields: dict[str, Any]) -> None:
        sets = ", ".join(f"{c}=?" for c in fields)
        self.conn.execute(f"UPDATE {table} SET {sets} WHERE {key}=?", [*(_enc(v) for v in fields.values()), key_value])

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    # --- cle/valeur (etat du filtre, coupe-circuits, compteurs persistants) ---
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value: Any, ts: float | None = None) -> None:
        self.conn.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), ts),
        )

    def kv_delete(self, key: str) -> None:
        self.conn.execute("DELETE FROM kv WHERE key=?", (key,))

    def log_error(self, ts: float, component: str, message: str) -> None:
        self.insert("errors", {"ts": ts, "component": component, "message": message[:2000]})


def _enc(v: Any) -> Any:
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, default=str, ensure_ascii=False)
    if isinstance(v, bool):
        return int(v)
    return v
