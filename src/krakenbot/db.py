"""Baza SQLite: schemat, migracje i podstawowe operacje.

Zasady:
- plik bazy ma uprawnienia 600, katalog 700, brak portu sieciowego,
- tabele audytowe są tylko dopisywane: UPDATE i DELETE blokują triggery w samej bazie,
- zmiana statusu zlecenia to nowy wiersz w order_events, a nie edycja orders,
- w bazie nie ma żadnych kluczy ani tokenów.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Tabele, w których wiersze wolno tylko dopisywać.
APPEND_ONLY_TABLES: tuple[str, ...] = (
    "audit_log",
    "decisions",
    "agent_debates",
    "orders",
    "order_events",
    "portfolio_snapshots",
    "risk_param_changes",
    "capital_flows",
)

_SCHEMA_V1 = """
CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    mode        TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    category    TEXT NOT NULL,   -- system, risk, order, telegram, agent, config
    event       TEXT NOT NULL,
    details     TEXT NOT NULL DEFAULT '{}'  -- JSON
);
CREATE INDEX idx_audit_ts ON audit_log (ts);

-- Jedna decyzja na aktywo w cyklu (przegląd godzinny albo decyzja dobowa).
CREATE TABLE decisions (
    id              INTEGER PRIMARY KEY,
    ts              TEXT NOT NULL,
    mode            TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    cycle_id        TEXT NOT NULL,
    cycle_kind      TEXT NOT NULL CHECK (cycle_kind IN ('hourly_review', 'daily_decision')),
    pair            TEXT NOT NULL,
    action          TEXT NOT NULL CHECK (action IN ('BUY', 'SELL', 'HOLD')),
    size_pct        REAL,        -- proponowany udział w portfelu
    stop_loss       REAL,
    take_profit     REAL,
    agent_tech      TEXT,        -- JSON wyjścia agenta technicznego
    agent_news      TEXT,        -- JSON wyjścia agenta newsowego
    joint_decision  TEXT,        -- JSON wspólnej decyzji
    risk_verdict    TEXT NOT NULL CHECK (
        risk_verdict IN ('approved', 'reduced', 'rejected', 'not_applicable')
    ),
    risk_reason     TEXT
);
CREATE INDEX idx_decisions_cycle ON decisions (cycle_id);

CREATE TABLE agent_debates (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    cycle_id  TEXT NOT NULL,
    round     INTEGER NOT NULL CHECK (round >= 1),
    agent     TEXT NOT NULL CHECK (agent IN ('tech', 'news', 'joint')),
    content   TEXT NOT NULL
);
CREATE INDEX idx_debates_cycle ON agent_debates (cycle_id);

CREATE TABLE orders (
    id               INTEGER PRIMARY KEY,
    ts               TEXT NOT NULL,
    mode             TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    decision_id      INTEGER REFERENCES decisions (id),
    client_order_id  TEXT NOT NULL UNIQUE,   -- idempotentność po restarcie
    pair             TEXT NOT NULL,
    side             TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    order_type       TEXT NOT NULL CHECK (order_type IN ('market', 'stop-loss', 'take-profit')),
    volume           REAL NOT NULL CHECK (volume > 0),
    trigger_price    REAL,                   -- dla stop-loss i take-profit
    request          TEXT NOT NULL           -- JSON wysłanego żądania (bez podpisu)
);

CREATE TABLE order_events (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    order_id    INTEGER NOT NULL REFERENCES orders (id),
    -- status: submitted, open, filled, partially_filled, canceled, rejected, error
    status      TEXT NOT NULL,
    kraken_txid TEXT,
    filled_vol  REAL,
    avg_price   REAL,
    fee_eur     REAL,
    details     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_order_events_order ON order_events (order_id);

CREATE TABLE portfolio_snapshots (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    mode        TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    total_eur   REAL NOT NULL,
    cash_eur    REAL NOT NULL,
    positions   TEXT NOT NULL,   -- JSON: aktywo -> {volume, value_eur}
    source      TEXT NOT NULL    -- kraken, paper, reconciliation
);
CREATE INDEX idx_snapshots_ts ON portfolio_snapshots (ts);

-- Wpłaty i wypłaty z księgi Krakena: wyłączane z wyniku i z dziennej straty.
CREATE TABLE capital_flows (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    kraken_refid TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL CHECK (kind IN ('deposit', 'withdrawal')),
    asset       TEXT NOT NULL,
    amount      REAL NOT NULL,
    amount_eur  REAL
);

CREATE TABLE risk_param_changes (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    param       TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT NOT NULL,
    source      TEXT NOT NULL,   -- telegram, config
    actor       TEXT
);

-- Pamięć agentów: osobna dla każdego agenta, może być przycinana.
CREATE TABLE agent_memory (
    id          INTEGER PRIMARY KEY,
    agent       TEXT NOT NULL CHECK (agent IN ('tech', 'news')),
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,   -- signal_outcome, lesson, note
    pair        TEXT,
    content     TEXT NOT NULL,
    expires_at  TEXT
);
CREATE INDEX idx_memory_agent ON agent_memory (agent, ts);

-- Cache świec z Krakena (OHLC).
CREATE TABLE market_candles (
    pair        TEXT NOT NULL,
    interval_m  INTEGER NOT NULL,  -- 240 (4h), 1440 (1d)
    open_time   INTEGER NOT NULL,  -- unix
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    vwap        REAL,
    volume      REAL NOT NULL,
    trades      INTEGER,
    PRIMARY KEY (pair, interval_m, open_time)
) WITHOUT ROWID;

-- Stan bota przetrwający restart (np. flaga pauzy). Każda zmiana trafia do audit_log.
CREATE TABLE bot_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


def _append_only_triggers(table: str) -> str:
    return f"""
CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
BEGIN SELECT RAISE(ABORT, '{table} jest tylko do dopisywania'); END;
CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
BEGIN SELECT RAISE(ABORT, '{table} jest tylko do dopisywania'); END;
"""


MIGRATIONS: list[str] = [
    _SCHEMA_V1 + "".join(_append_only_triggers(t) for t in APPEND_ONLY_TABLES),
]
SCHEMA_VERSION = len(MIGRATIONS)


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _secure_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
    os.chmod(path, 0o600)


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    _secure_file(path)
    old_umask = os.umask(0o077)  # pliki -wal i -shm też tylko dla właściciela
    try:
        conn = sqlite3.connect(path, isolation_level=None)  # transakcje jawnie
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 5000")
    finally:
        os.umask(old_umask)
    migrate(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> int:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"Baza ma wersję schematu {current}, a kod zna tylko {SCHEMA_VERSION}. "
            "Zaktualizuj kod zamiast cofać bazę."
        )
    for version in range(current + 1, SCHEMA_VERSION + 1):
        with transaction(conn):
            for stmt in _split_sql(MIGRATIONS[version - 1]):
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {version}")
    return SCHEMA_VERSION


def _split_sql(script: str) -> list[str]:
    """Dzieli skrypt na instrukcje, nie rozcinając ciał triggerów (BEGIN ... END;)."""
    statements: list[str] = []
    buf = ""
    for line in script.splitlines(keepends=True):
        stripped = line.split("--", 1)[0]
        buf += line
        if stripped.rstrip().endswith(";") and sqlite3.complete_statement(buf):
            if buf.strip():
                statements.append(buf.strip())
            buf = ""
    if buf.strip():
        statements.append(buf.strip())
    return statements


def audit(
    conn: sqlite3.Connection,
    *,
    mode: str,
    category: str,
    event: str,
    details: dict[str, Any] | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO audit_log (ts, mode, category, event, details) VALUES (?, ?, ?, ?, ?)",
        (utcnow(), mode, category, event, json.dumps(details or {}, ensure_ascii=False)),
    )
    return int(cur.lastrowid)


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str, *, mode: str, actor: str) -> None:
    with transaction(conn):
        old = get_state(conn, key)
        conn.execute(
            "INSERT INTO bot_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, utcnow()),
        )
        audit(
            conn,
            mode=mode,
            category="system",
            event="state_changed",
            details={"key": key, "old": old, "new": value, "actor": actor},
        )
