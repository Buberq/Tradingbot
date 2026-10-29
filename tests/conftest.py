from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = ROOT / "config" / "config.example.yaml"


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    """Kopia przykładowej konfiguracji z bazą i logami w katalogu tymczasowym."""
    text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    db = tmp_path / "data" / "bot.sqlite3"
    text = text.replace("db_path: data/krakenbot.sqlite3", f"db_path: {db}")
    text = text.replace("log_dir: logs", f"log_dir: {tmp_path / 'logs'}")
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def write_config(tmp_path: Path, config_file: Path):
    """Zwraca funkcję, która dopisuje/zmienia fragment YAML na końcu pliku."""

    def _write(extra_yaml: str) -> Path:
        path = tmp_path / "custom.yaml"
        shutil.copy(config_file, path)
        with path.open("a", encoding="utf-8") as f:
            f.write("\n" + extra_yaml + "\n")
        return path

    return _write


@pytest.fixture(autouse=True)
def _reset_logger():
    yield
    logger = logging.getLogger("krakenbot")
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
