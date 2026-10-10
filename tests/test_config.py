from pathlib import Path

import pytest
from pydantic import ValidationError

from rollout.config import EngineConfig, LoadStrategy, ModelConfig


def test_configuration_defaults_and_string_inputs():
    assert ModelConfig().load_strategy is LoadStrategy.FULL
    assert ModelConfig(load_strategy="FULL").load_strategy is LoadStrategy.FULL
    assert list(LoadStrategy) == [LoadStrategy.FULL]
    assert EngineConfig().warehouse_path == Path(".rollout/warehouse.duckdb")
    assert EngineConfig(warehouse_path="warehouse.duckdb").warehouse_path == Path(
        "warehouse.duckdb"
    )


@pytest.mark.parametrize(
    "config_type, values",
    [
        (ModelConfig, {"load_strategy": "APPEND"}),
        (ModelConfig, {"load_strategy": None}),
        (ModelConfig, {"merge_key": "id"}),
        (EngineConfig, {"engine": "ducklake"}),
        (EngineConfig, {"warehouse_path": None}),
        (EngineConfig, {"warehouse_path": 123}),
        (EngineConfig, {"warehouse_path": {}}),
    ],
)
def test_invalid_external_configuration(config_type, values):
    with pytest.raises(ValidationError):
        config_type(**values)


@pytest.mark.parametrize(
    "config, attribute, value",
    [
        (EngineConfig(), "warehouse_path", Path("another.duckdb")),
        (ModelConfig(), "load_strategy", LoadStrategy.FULL),
    ],
)
def test_configuration_is_immutable(config, attribute, value):
    with pytest.raises(ValidationError) as error:
        setattr(config, attribute, value)
    assert error.value.errors()[0]["type"] == "frozen_instance"
    assert type(config).model_config["extra"] == "forbid"
    assert type(config).model_config["frozen"] is True
