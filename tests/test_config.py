from __future__ import annotations

import pytest

from krakenbot.config import ConfigError, Mode, load_settings


def test_example_config_loads_in_paper_mode(config_file):
    s = load_settings(config_file, env={})
    assert s.mode is Mode.PAPER
    assert s.risk.daily_loss_limit_pct == 20
    assert s.risk.max_open_positions == 10
    assert s.risk.max_position_pct == 50


def test_missing_config_file_is_clear_error(tmp_path):
    with pytest.raises(ConfigError, match="Brak pliku konfiguracji"):
        load_settings(tmp_path / "nope.yaml", env={})


def test_live_requires_env_confirmation(write_config):
    path = write_config("mode: live")
    with pytest.raises(ConfigError, match="KRAKENBOT_ALLOW_LIVE"):
        load_settings(path, env={})
    with pytest.raises(ConfigError):
        load_settings(path, env={"KRAKENBOT_ALLOW_LIVE": "true"})  # tylko dokładnie "yes"
    s = load_settings(path, env={"KRAKENBOT_ALLOW_LIVE": "yes"})
    assert s.mode is Mode.LIVE


def test_env_flag_alone_does_not_enable_live(config_file):
    s = load_settings(config_file, env={"KRAKENBOT_ALLOW_LIVE": "yes"})
    assert s.mode is Mode.PAPER


def test_env_cannot_override_mode_or_risk(config_file):
    s = load_settings(
        config_file, env={"KRAKENBOT_MODE": "live", "KRAKENBOT_DAILY_LOSS_LIMIT_PCT": "50"}
    )
    assert s.mode is Mode.PAPER
    assert s.risk.daily_loss_limit_pct == 20


def test_secrets_in_yaml_are_rejected(write_config):
    path = write_config("secrets:\n  telegram_bot_token: abc")
    with pytest.raises(ConfigError, match="sekrety nie mogą być w pliku YAML"):
        load_settings(path, env={})


def test_secrets_come_from_env_and_are_hidden(config_file):
    s = load_settings(config_file, env={"TELEGRAM_BOT_TOKEN": "123456:SECRETVALUE"})
    assert s.secrets.telegram_bot_token.get_secret_value() == "123456:SECRETVALUE"
    assert "SECRETVALUE" not in repr(s)
    assert "SECRETVALUE" not in str(s.model_dump())
    assert s.secrets.missing("telegram_bot_token", "anthropic_api_key") == ["ANTHROPIC_API_KEY"]


def test_empty_env_values_count_as_missing(config_file):
    s = load_settings(config_file, env={"ANTHROPIC_API_KEY": ""})
    assert s.secrets.anthropic_api_key is None


def test_risk_value_outside_config_bounds_rejected(write_config):
    path = write_config("risk:\n  daily_loss_limit_pct: 30")  # granica w przykładzie: max 25
    with pytest.raises(ConfigError, match="daily_loss_limit_pct"):
        load_settings(path, env={})


def test_config_bounds_cannot_exceed_code_limits(write_config):
    path = write_config("risk_bounds:\n  daily_loss_limit_pct: {min: 5, max: 80}")
    with pytest.raises(ConfigError, match="twarde granice"):
        load_settings(path, env={})


def test_unknown_keys_rejected(write_config):
    with pytest.raises(ConfigError):
        load_settings(write_config("leverage: 5"), env={})
    with pytest.raises(ConfigError, match="nieznane klucze"):
        load_settings(write_config("risk_bounds:\n  leverage: {min: 1, max: 5}"), env={})


def test_kraken_url_must_be_https(write_config):
    path = write_config("kraken:\n  base_url: http://api.kraken.com")
    with pytest.raises(ConfigError, match="HTTPS"):
        load_settings(path, env={})


def test_env_overrides_paths_and_chat_id(config_file, tmp_path):
    s = load_settings(
        config_file,
        env={"KRAKENBOT_DB_PATH": str(tmp_path / "x.db"), "TELEGRAM_CHAT_ID": "424242"},
    )
    assert s.paths.db_path == tmp_path / "x.db"
    assert s.telegram_chat_id == 424242
