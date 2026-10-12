import duckdb
import pytest

from rollout.execution import ExecutionStatus, execute_models


def replacement_dag(model_dag, sql="SELECT 20 AS number", where="target.number = 2", quality=""):
    return model_dag({
        "numbers.sql": sql,
        "numbers.yaml": f"load_strategy: DELETE_INSERT\ndelete_insert:\n  where: |\n    {where}\n{quality}",
    })


def seed_target(engine_config, rows="(1), (2)"):
    engine_config.warehouse_path.parent.mkdir()
    with duckdb.connect(str(engine_config.warehouse_path)) as connection:
        connection.execute("CREATE SCHEMA public")
        connection.execute("CREATE TABLE public.numbers (number INTEGER)")
        if rows:
            connection.execute(f"INSERT INTO public.numbers VALUES {rows}")


def target_rows(engine_config):
    with duckdb.connect(str(engine_config.warehouse_path)) as connection:
        return connection.execute("SELECT * FROM public.numbers ORDER BY number").fetchall()


def generated_sql(engine_config):
    return next((engine_config.warehouse_path.parent / "models").glob("*/public/numbers.sql")).read_text()


def test_bootstrap_uses_model_sql_without_delete_predicate(model_dag, engine_config):
    dag = replacement_dag(model_dag, where="target.missing = 1")
    result = next(iter(execute_models(dag, engine_config).values()))
    assert result.status is ExecutionStatus.SUCCESS
    assert target_rows(engine_config) == [(20,)]
    assert generated_sql(engine_config) == 'CREATE TABLE "public"."numbers" AS\nSELECT 20 AS number'


@pytest.mark.parametrize("rows, expected", [("(1), (2)", [(1,), (20,)]), ("", [(20,)])])
def test_existing_target_deletes_selected_rows_and_inserts_unfiltered_model(
    model_dag, engine_config, rows, expected
):
    seed_target(engine_config, rows)
    original = "-- replacement\nSELECT 20 AS number; -- trailing comment"
    dag = replacement_dag(model_dag, sql=original)
    result = next(iter(execute_models(dag, engine_config).values()))
    assert result.status is ExecutionStatus.SUCCESS
    assert target_rows(engine_config) == expected
    assert generated_sql(engine_config) == (
        'DELETE FROM "public"."numbers" AS target\nWHERE target.number = 2;\n\n'
        'INSERT INTO "public"."numbers"\n' + original
    )


def test_delete_predicate_can_reference_staging_with_different_column(model_dag, engine_config):
    seed_target(engine_config)
    with duckdb.connect(str(engine_config.warehouse_path)) as connection:
        connection.execute("CREATE SCHEMA staging")
        connection.execute("CREATE TABLE staging.source AS SELECT 2 AS operation_date")
    dag = replacement_dag(model_dag, where=(
        "EXISTS (SELECT 1 FROM staging.source AS source "
        "WHERE target.number = source.operation_date)"
    ))
    result = next(iter(execute_models(dag, engine_config).values()))
    assert result.status is ExecutionStatus.SUCCESS
    assert target_rows(engine_config) == [(1,), (20,)]


@pytest.mark.parametrize("failure", ["delete", "insert"])
def test_sql_failure_restores_existing_target(model_dag, engine_config, failure):
    seed_target(engine_config)
    dag = replacement_dag(
        model_dag,
        sql="SELECT CAST('invalid' AS INTEGER) AS number" if failure == "insert" else "SELECT 20 AS number",
        where="target.missing = 2" if failure == "delete" else "target.number = 2",
    )
    result = next(iter(execute_models(dag, engine_config).values()))
    assert result.status is ExecutionStatus.FAILED
    assert result.error
    assert result.duration >= 0
    assert target_rows(engine_config) == [(1,), (2,)]
    assert "DELETE FROM" in generated_sql(engine_config)
    assert "INSERT INTO" in generated_sql(engine_config)


@pytest.mark.parametrize("severity, status, expected", [
    ("ERROR", ExecutionStatus.FAILED, [(1,), (2,)]),
    ("WARNING", ExecutionStatus.SUCCESS, [(1,), (20,)]),
])
def test_quality_controls_transaction(model_dag, engine_config, severity, status, expected):
    seed_target(engine_config)
    quality = f"quality:\n  - type: row_count\n    severity: {severity}\n    min: 3\n"
    dag = replacement_dag(model_dag, quality=quality)
    result = next(iter(execute_models(dag, engine_config).values()))
    assert result.status is status
    assert len(result.quality_results) == 1
    assert result.quality_results[0].observed_value == 2
    assert target_rows(engine_config) == expected


def test_bootstrap_quality_error_removes_new_table(model_dag, engine_config):
    dag = replacement_dag(model_dag, quality="quality:\n  - type: row_count\n    severity: ERROR\n    min: 2\n")
    result = next(iter(execute_models(dag, engine_config).values()))
    assert result.status is ExecutionStatus.FAILED
    with duckdb.connect(str(engine_config.warehouse_path)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'numbers'"
        ).fetchone() == (0,)
    assert generated_sql(engine_config).startswith('CREATE TABLE "public"."numbers" AS')


def test_invalid_yaml_configuration_fails_before_execution(model_dag, engine_config):
    with pytest.raises(ValueError, match="DELETE_INSERT requires"):
        model_dag({
            "numbers.sql": "SELECT 20 AS number",
            "numbers.yaml": "load_strategy: DELETE_INSERT\n",
        })
    assert not engine_config.warehouse_path.exists()


def test_quoted_target_bootstraps_then_uses_delete_insert(model_dag, engine_config):
    dag = model_dag({
        'a"b/x"y.sql': "SELECT 20 AS number",
        'a"b/x"y.yaml': "load_strategy: DELETE_INSERT\ndelete_insert:\n  where: target.number = 20\n",
    })
    for _ in range(2):
        result = next(iter(execute_models(dag, engine_config).values()))
        assert result.status is ExecutionStatus.SUCCESS
    with duckdb.connect(str(engine_config.warehouse_path)) as connection:
        assert connection.execute('SELECT * FROM "a""b"."x""y"').fetchall() == [(20,)]
    artifacts = list((engine_config.warehouse_path.parent / "models").glob('*/a"b/x"y.sql'))
    assert any('DELETE FROM "a""b"."x""y" AS target' in path.read_text() for path in artifacts)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("predicate", [
    "target.number =",
    "target.number = 2; DROP TABLE public.numbers",
    "target.number = 2; DELETE FROM public.numbers",
])
def test_invalid_predicate_is_rejected_before_loading(
    model_dag, engine_config, existing, predicate
):
    if existing:
        seed_target(engine_config)
    dag = replacement_dag(model_dag, where=predicate)
    with pytest.raises(ValueError, match="Invalid delete_insert.where"):
        execute_models(dag, engine_config)
    if existing:
        assert target_rows(engine_config) == [(1,), (2,)]
    else:
        with duckdb.connect(str(engine_config.warehouse_path)) as connection:
            assert connection.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'numbers'"
            ).fetchone() == (0,)
    assert not (engine_config.warehouse_path.parent / "models").exists()


@pytest.mark.parametrize("strategy, target_exists, prefix", [
    ("APPEND", False, 'CREATE TABLE "public"."numbers" AS\n'),
    ("APPEND", True, 'INSERT INTO "public"."numbers"\n'),
    ("FULL", False, 'CREATE OR REPLACE TABLE "public"."numbers" AS\n'),
    ("FULL", True, 'CREATE OR REPLACE TABLE "public"."numbers" AS\n'),
    ("DELETE_INSERT", False, 'CREATE TABLE "public"."numbers" AS\n'),
    ("DELETE_INSERT", True,
     'DELETE FROM "public"."numbers" AS target\nWHERE target.number = 2;\n\n'
     'INSERT INTO "public"."numbers"\n'),
])
def test_sql_templates_preserve_model_sql_exactly(strategy, target_exists, prefix):
    from pathlib import Path

    from rollout.config import ModelConfig
    from rollout.execution import build_model_sql
    from rollout.models import Model, ModelId

    original = "-- authored SQL\nSELECT 20 AS number; -- trailing comment\n \n"
    values = {"load_strategy": strategy}
    if strategy == "DELETE_INSERT":
        values["delete_insert"] = {"where": " \n target.number = 2\n  "}
    model = Model(
        ModelId("public", "numbers"), Path("numbers.sql"), original,
        config=ModelConfig(**values),
    )
    assert build_model_sql(model, target_exists=target_exists) == prefix + original
    assert model.sql == original
