from __future__ import annotations

import json
import logging
import sqlite3

from krakenbot.config import load_settings
from krakenbot.logging_setup import RedactSecretsFilter
from krakenbot.main import cli, load_dotenv, startup


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    finally:
        conn.close()


def test_startup_writes_audit_entry(config_file, tmp_path):
    settings = load_settings(config_file, env={})
    entry_id = startup(settings)
    rows = _rows(settings.paths.db_path)
    assert [r["id"] for r in rows] == [entry_id]
    assert rows[0]["event"] == "startup" and rows[0]["mode"] == "paper"
    details = json.loads(rows[0]["details"])
    assert details["paused"] is False
    assert details["risk"]["daily_loss_limit_pct"] == 20
    assert (tmp_path / "logs" / "krakenbot.log").is_file()


def test_startup_logs_mode(config_file, tmp_path):
    startup(load_settings(config_file, env={}))
    log_text = (tmp_path / "logs" / "krakenbot.log").read_text(encoding="utf-8")
    assert "Tryb PAPER" in log_text
    assert "Start OK" in log_text


def test_secrets_never_reach_db_or_logs(config_file, tmp_path):
    secret = "SUPER-SECRET-TOKEN-123"
    settings = load_settings(config_file, env={"TELEGRAM_BOT_TOKEN": secret})
    startup(settings)
    logging.getLogger("krakenbot").info("przypadkowy log z tokenem %s", secret)
    db_bytes = settings.paths.db_path.read_bytes()
    wal = settings.paths.db_path.with_name(settings.paths.db_path.name + "-wal")
    if wal.exists():
        db_bytes += wal.read_bytes()
    assert secret.encode() not in db_bytes
    log_text = (tmp_path / "logs" / "krakenbot.log").read_text(encoding="utf-8")
    assert secret not in log_text
    assert "przypadkowy log z tokenem ***" in log_text
    rows = _rows(settings.paths.db_path)
    assert json.loads(rows[0]["details"])["secrets_present"] == ["telegram_bot_token"]


def test_redact_filter_handles_exceptions():
    f = RedactSecretsFilter(["abcdef123"])
    try:
        raise ValueError("klucz abcdef123")
    except ValueError:
        import sys

        rec = logging.LogRecord("krakenbot", logging.ERROR, __file__, 1, "x", None, sys.exc_info())
    f.filter(rec)
    assert "abcdef123" not in rec.exc_text


def test_cli_start_and_repeated_starts(config_file):
    assert cli(["--config", str(config_file), "--env-file", "/nonexistent", "start"]) == 0
    assert cli(["--config", str(config_file), "--env-file", "/nonexistent", "start"]) == 0
    db_path = load_settings(config_file, env={}).paths.db_path
    assert [r["event"] for r in _rows(db_path)] == ["startup", "startup"]


def test_cli_refuses_live_without_confirmation(write_config, capsys, monkeypatch):
    monkeypatch.delenv("KRAKENBOT_ALLOW_LIVE", raising=False)
    path = write_config("mode: live")
    assert cli(["--config", str(path), "--env-file", "/nonexistent", "start"]) == 2
    assert "KRAKENBOT_ALLOW_LIVE" in capsys.readouterr().err


def test_cli_check_config(config_file, capsys):
    assert cli(["--config", str(config_file), "--env-file", "/nonexistent", "check-config"]) == 0
    assert "Tryb: paper" in capsys.readouterr().out


def test_load_dotenv(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# komentarz\nA=1\nexport B='dwa'\nC=\nEXISTING=nowa\n", encoding="utf-8"
    )
    env = {"EXISTING": "stara"}
    assert load_dotenv(env_file, env) == 2
    assert env == {"EXISTING": "stara", "A": "1", "B": "dwa"}
