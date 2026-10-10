from pathlib import Path

import pytest

from rollout.config import EngineConfig
from rollout.dag import build_dag
from rollout.discovery import discover_models
from rollout.parser import parse_models


@pytest.fixture
def write_models(tmp_path):
    def write(files: dict[str, str]) -> Path:
        root = tmp_path / "models"
        root.mkdir(exist_ok=True)
        for name, sql in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(sql, encoding="utf-8")
        return root

    return write


@pytest.fixture
def model_dag(write_models):
    def build(files: dict[str, str]):
        return build_dag(parse_models(discover_models(write_models(files))))

    return build


@pytest.fixture
def engine_config(tmp_path):
    return EngineConfig(warehouse_path=tmp_path / ".rollout" / "warehouse.duckdb")
