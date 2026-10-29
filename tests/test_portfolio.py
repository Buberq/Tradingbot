from __future__ import annotations

import json
from decimal import Decimal

import pytest
from kraken_fakes import (
    ASSET_PAIRS,
    BALANCES,
    DOC_SECRET,
    TICKERS,
    FakeTransport,
    err,
    kraken_routes,
)

from krakenbot import kraken_commands
from krakenbot.config import load_settings
from krakenbot.db import connect
from krakenbot.main import cli
from krakenbot.portfolio import (
    eur_pairs,
    format_valuation,
    pairs_to_price,
    split_balances,
    value_portfolio,
)


def test_eur_pairs_skip_other_quotes_dark_pools_and_delisted():
    assert eur_pairs(ASSET_PAIRS) == {"XXBT": "XXBTZEUR", "XETH": "XETHZEUR", "SOL": "SOLEUR"}


def test_split_balances():
    spot, other = split_balances(BALANCES)
    assert set(spot) == {"ZEUR", "XXBT", "XETH", "ZUSD"}  # SOL = 0 pominięty
    assert other == {"DOT.S": Decimal("10.0")}


def test_valuation_in_eur():
    v = value_portfolio(BALANCES, ASSET_PAIRS, TICKERS)
    assert v.cash_eur == Decimal("120.5")
    assert v.positions["XXBT"].value_eur == Decimal("60")       # 0.001 × 60000
    assert v.positions["XETH"].value_eur == Decimal("50.01")    # 0.02 × 2500.5
    assert v.total_eur == Decimal("230.51")
    assert v.unpriced == {"ZUSD": Decimal("5.00")}             # brak pary do EUR
    assert v.non_spot == {"DOT.S": Decimal("10.0")}            # nie wliczane


def test_pairs_to_price_only_held_assets():
    assert pairs_to_price(BALANCES, ASSET_PAIRS) == ["XETHZEUR", "XXBTZEUR"]


def test_empty_account():
    v = value_portfolio({"ZEUR": "0.0"}, ASSET_PAIRS, {})
    assert v.total_eur == 0 and v.positions == {}
    assert "0.00 EUR" in format_valuation(v)


@pytest.fixture
def settings_with_key(config_file):
    return load_settings(
        config_file,
        env={"KRAKEN_READ_API_KEY": "pubkey", "KRAKEN_READ_API_SECRET": DOC_SECRET},
    )


def test_portfolio_command_saves_snapshot(settings_with_key, tmp_path):
    t = FakeTransport(kraken_routes())
    assert kraken_commands.portfolio(settings_with_key, t) == 0
    conn = connect(settings_with_key.paths.db_path)
    snap = conn.execute("SELECT * FROM portfolio_snapshots").fetchone()
    assert snap["total_eur"] == pytest.approx(230.51)
    assert snap["cash_eur"] == pytest.approx(120.5)
    assert snap["source"] == "kraken" and snap["mode"] == "paper"
    assert set(json.loads(snap["positions"])["positions"]) == {"XXBT", "XETH"}
    events = [r[0] for r in conn.execute("SELECT event FROM audit_log")]
    assert events == ["portfolio_snapshot"]
    conn.close()
    log = (tmp_path / "logs" / "krakenbot.log").read_text(encoding="utf-8")
    assert "Wartość portfela: 230.51 EUR" in log
    assert DOC_SECRET not in log


def test_check_kraken_ok(settings_with_key):
    t = FakeTransport(kraken_routes())
    assert kraken_commands.check_kraken(settings_with_key, t) == 0
    assert [c.path for c in t.calls] == [
        "/0/public/SystemStatus", "/0/public/Time", "/0/private/Balance",
    ]


def test_check_kraken_reports_ip_whitelist_rejection(settings_with_key, tmp_path):
    t = FakeTransport(kraken_routes(**{"/0/private/Balance": err("EAPI:Invalid key")}))
    assert kraken_commands.check_kraken(settings_with_key, t) == 1
    log = (tmp_path / "logs" / "krakenbot.log").read_text(encoding="utf-8")
    assert "ODRZUCIŁO" in log and "whitelisty" in log
    conn = connect(settings_with_key.paths.db_path)
    row = conn.execute("SELECT details FROM audit_log WHERE event='kraken_auth_failed'").fetchone()
    assert json.loads(row[0]) == {"errors": ["EAPI:Invalid key"]}
    conn.close()


def test_check_kraken_without_key_tests_public_only(config_file):
    t = FakeTransport(kraken_routes())
    assert kraken_commands.check_kraken(load_settings(config_file, env={}), t) == 0
    assert all("/public/" in c.path for c in t.calls)


def test_cli_portfolio_uses_transport(config_file, monkeypatch):
    t = FakeTransport(kraken_routes())
    monkeypatch.setattr(kraken_commands, "urllib_transport", t)
    monkeypatch.setenv("KRAKEN_READ_API_KEY", "pubkey")
    monkeypatch.setenv("KRAKEN_READ_API_SECRET", DOC_SECRET)
    argv = ["--config", str(config_file), "--env-file", "/nonexistent", "portfolio"]
    assert cli(argv) == 0
    assert any(c.path == "/0/private/Balance" for c in t.calls)


def test_cli_portfolio_without_key_fails_cleanly(config_file, monkeypatch, capsys):
    monkeypatch.setattr(kraken_commands, "urllib_transport", FakeTransport(kraken_routes()))
    monkeypatch.delenv("KRAKEN_READ_API_KEY", raising=False)
    monkeypatch.delenv("KRAKEN_READ_API_SECRET", raising=False)
    argv = ["--config", str(config_file), "--env-file", "/nonexistent", "portfolio"]
    assert cli(argv) == 1
    assert "brak klucza" in capsys.readouterr().err
