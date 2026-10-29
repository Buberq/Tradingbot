from __future__ import annotations

import json
import sqlite3
import stat

import pytest

from krakenbot.db import (
    APPEND_ONLY_TABLES,
    SCHEMA_VERSION,
    audit,
    connect,
    get_state,
    set_state,
    transaction,
    utcnow,
)

EXPECTED_TABLES = {
    "audit_log",
    "decisions",
    "agent_debates",
    "orders",
    "order_events",
    "portfolio_snapshots",
    "capital_flows",
    "risk_param_changes",
    "agent_memory",
    "market_candles",
    "bot_state",
}


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "db" / "bot.sqlite3")
    yield c
    c.close()


def test_schema_created(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED_TABLES <= tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_file_permissions(tmp_path):
    path = tmp_path / "db" / "bot.sqlite3"
    connect(path).close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_reconnect_is_idempotent(tmp_path):
    path = tmp_path / "bot.sqlite3"
    c1 = connect(path)
    audit(c1, mode="paper", category="system", event="a")
    c1.close()
    c2 = connect(path)
    assert c2.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1
    c2.close()


def test_newer_schema_refused(tmp_path):
    path = tmp_path / "bot.sqlite3"
    c = connect(path)
    c.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    c.close()
    with pytest.raises(RuntimeError, match="wersję schematu"):
        connect(path)


@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
def test_append_only_tables_block_update_and_delete(conn, table):
    _insert_sample(conn, table)
    with pytest.raises(sqlite3.IntegrityError, match="tylko do dopisywania"):
        conn.execute(f"UPDATE {table} SET ts = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="tylko do dopisywania"):
        conn.execute(f"DELETE FROM {table}")
    assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] >= 1


def test_audit_entry_roundtrip(conn):
    entry = audit(conn, mode="paper", category="system", event="test", details={"ą": 1})
    row = conn.execute("SELECT * FROM audit_log WHERE id = ?", (entry,)).fetchone()
    assert row["event"] == "test"
    assert json.loads(row["details"]) == {"ą": 1}


def test_mode_is_constrained(conn):
    with pytest.raises(sqlite3.IntegrityError):
        audit(conn, mode="yolo", category="system", event="x")


def test_order_rules_enforced_in_schema(conn):
    base = (utcnow(), "paper", "cid-1", "XBTEUR", "buy", "market", 0.001, "{}")
    sql = (
        "INSERT INTO orders (ts, mode, client_order_id, pair, side, order_type, volume, request)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )
    conn.execute(sql, base)
    with pytest.raises(sqlite3.IntegrityError):  # duplikat client_order_id
        conn.execute(sql, base)
    with pytest.raises(sqlite3.IntegrityError):  # zlecenie limitowe niedozwolone
        conn.execute(sql, (*base[:2], "cid-2", "XBTEUR", "buy", "limit", 0.001, "{}"))
    with pytest.raises(sqlite3.IntegrityError):  # zerowy wolumen
        conn.execute(sql, (*base[:2], "cid-3", "XBTEUR", "buy", "market", 0, "{}"))


def test_state_survives_reconnect_and_is_audited(tmp_path):
    path = tmp_path / "bot.sqlite3"
    c = connect(path)
    set_state(c, "paused", "1", mode="paper", actor="test")
    c.close()
    c = connect(path)
    assert get_state(c, "paused") == "1"
    row = c.execute("SELECT details FROM audit_log WHERE event = 'state_changed'").fetchone()
    assert json.loads(row["details"]) == {"key": "paused", "old": None, "new": "1", "actor": "test"}
    c.close()


def test_transaction_rolls_back(conn):
    with pytest.raises(RuntimeError), transaction(conn):
        audit(conn, mode="paper", category="system", event="x")
        raise RuntimeError
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def _insert_sample(conn: sqlite3.Connection, table: str) -> None:
    ts = utcnow()
    if table == "audit_log":
        audit(conn, mode="paper", category="system", event="x")
    elif table == "decisions":
        conn.execute(
            "INSERT INTO decisions (ts, mode, cycle_id, cycle_kind, pair, action, risk_verdict)"
            " VALUES (?, 'paper', 'c1', 'daily_decision', 'XBTEUR', 'HOLD', 'not_applicable')",
            (ts,),
        )
    elif table == "agent_debates":
        conn.execute(
            "INSERT INTO agent_debates (ts, cycle_id, round, agent, content)"
            " VALUES (?, 'c1', 1, 'tech', 'x')",
            (ts,),
        )
    elif table in ("orders", "order_events"):
        conn.execute(
            "INSERT INTO orders (ts, mode, client_order_id, pair, side, order_type, volume,"
            " request) VALUES (?, 'paper', ?, 'XBTEUR', 'buy', 'market', 1, '{}')",
            (ts, f"cid-{table}"),
        )
        if table == "order_events":
            oid = conn.execute("SELECT id FROM orders").fetchone()[0]
            conn.execute(
                "INSERT INTO order_events (ts, order_id, status) VALUES (?, ?, 'filled')",
                (ts, oid),
            )
    elif table == "portfolio_snapshots":
        conn.execute(
            "INSERT INTO portfolio_snapshots (ts, mode, total_eur, cash_eur, positions, source)"
            " VALUES (?, 'paper', 100, 100, '{}', 'paper')",
            (ts,),
        )
    elif table == "capital_flows":
        conn.execute(
            "INSERT INTO capital_flows (ts, kraken_refid, kind, asset, amount)"
            " VALUES (?, 'R1', 'deposit', 'ZEUR', 50)",
            (ts,),
        )
    elif table == "risk_param_changes":
        conn.execute(
            "INSERT INTO risk_param_changes (ts, param, old_value, new_value, source)"
            " VALUES (?, 'daily_loss_limit_pct', '20', '15', 'telegram')",
            (ts,),
        )
    else:  # pragma: no cover
        raise AssertionError(f"brak przykładu dla {table}")
