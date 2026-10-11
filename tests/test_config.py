from pathlib import Path

import pytest
from pydantic import ValidationError

from rollout.config import EngineConfig, LoadStrategy, ModelConfig


def test_configuration_defaults_and_string_inputs():
    assert ModelConfig().load_strategy is LoadStrategy.FULL
    assert ModelConfig(load_strategy="FULL").load_strategy is LoadStrategy.FULL
    assert list(LoadStrategy) == [LoadStrategy.FULL, LoadStrategy.DELETE_INSERT]
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


@pytest.mark.parametrize("delete_insert", [None, {}, {"where": None}, {"where": ""}, {"where": " \n\t"}, {"where": 123}])
def test_delete_insert_requires_valid_predicate(delete_insert):
    with pytest.raises(ValidationError):
        ModelConfig(load_strategy="DELETE_INSERT", delete_insert=delete_insert)


def test_delete_insert_requires_configuration():
    with pytest.raises(ValidationError):
        ModelConfig(load_strategy="DELETE_INSERT")


def test_delete_insert_configuration_preserves_sql():
    predicate = " EXISTS (SELECT 1 FROM staging.source WHERE target.period = operation_date)\n"
    config = ModelConfig(load_strategy="DELETE_INSERT", delete_insert={"where": predicate})
    assert config.delete_insert.where == predicate
    with pytest.raises(ValidationError):
        config.delete_insert.where = "TRUE"


def test_full_rejects_delete_insert_configuration():
    with pytest.raises(ValidationError, match="FULL does not allow"):
        ModelConfig(load_strategy="FULL", delete_insert={"where": "target.id = 1"})
