from pathlib import Path

import duckdb
import pytest

from rollout.config import ModelConfig
from rollout.execution import (
    ExecutionStatus,
    build_full_sql,
    build_model_sql,
    execute_models,
)
from rollout.models import Model, ModelId


@pytest.mark.parametrize(
    "schema, name, target",
    [
        ("silver", "numbers", '"silver"."numbers"'),
        ('a"b', 'x"y', '"a""b"."x""y"'),
        ("select", "order", '"select"."order"'),
    ],
)
def test_full_sql_quotes_identifiers_and_preserves_original(schema, name, target):
    original = "-- original\nSELECT 1 AS number; -- trailing comment"
    model = Model(ModelId(schema, name), Path("numbers.sql"), original)
    expected = f"CREATE OR REPLACE TABLE {target} AS\n{original}"
    assert build_full_sql(model) == expected
    assert build_model_sql(model) == expected
    assert model.sql == original


def test_full_selection_delegates_to_full_generator(monkeypatch):
    import rollout.execution as execution

    model = Model(ModelId("public", "numbers"), Path("numbers.sql"), "SELECT 1")
    calls = []

    def build_full(model):
        calls.append(model)
        return "generated SQL"

    monkeypatch.setattr(execution, "build_full_sql", build_full)
    assert build_model_sql(model) == "generated SQL"
    assert calls == [model]


def test_execution_uses_model_sql_selection(model_dag, engine_config, monkeypatch):
    import rollout.execution as execution

    dag = model_dag({"numbers.sql": "SELECT 1 AS number"})
    select_sql = execution.build_model_sql
    calls = []

    def track_selection(model):
        calls.append(model.id)
        return select_sql(model)

    def unexpected_existence_check(connection, model):
        pytest.fail("FULL must not check target existence")

    monkeypatch.setattr(execution, "build_model_sql", track_selection)
    monkeypatch.setattr(execution, "target_table_exists", unexpected_existence_check)
    results = execute_models(dag, engine_config)
    assert calls == dag.order
    assert all(result.status is ExecutionStatus.SUCCESS for result in results.values())
    sql_path = next(
        (engine_config.warehouse_path.parent / "models").glob("*/public/numbers.sql")
    )
    assert sql_path.read_text() == build_model_sql(dag.models[dag.order[0]])


def test_full_replaces_entire_table(model_dag, engine_config):
    dag = model_dag({"numbers.sql": "SELECT 1 AS number UNION ALL SELECT 2"})
    execute_models(dag, engine_config)
    with duckdb.connect(str(engine_config.warehouse_path)) as connection:
        connection.execute("INSERT INTO public.numbers VALUES (3)")
    execute_models(dag, engine_config)
    with duckdb.connect(str(engine_config.warehouse_path)) as connection:
        assert connection.execute(
            "SELECT * FROM public.numbers ORDER BY number"
        ).fetchall() == [(1,), (2,)]


def test_quoted_target_materializes_from_external_source(model_dag, engine_config):
    dag = model_dag({'a"b/x"y.sql': "SELECT * FROM raw.numbers"})
    engine_config.warehouse_path.parent.mkdir()
    with duckdb.connect(str(engine_config.warehouse_path)) as connection:
        connection.execute("CREATE SCHEMA raw")
        connection.execute("CREATE TABLE raw.numbers AS SELECT 5 AS number")
    results = execute_models(dag, engine_config)
    assert next(iter(results.values())).status is ExecutionStatus.SUCCESS
    with duckdb.connect(str(engine_config.warehouse_path)) as connection:
        assert connection.execute('SELECT * FROM "a""b"."x""y"').fetchall() == [(5,)]
