import duckdb
import pytest

from rollout.execution import ExecutionStatus, execute_models
from rollout.quality import CheckStatus


def append_dag(model_dag, sql="SELECT 2 AS number", quality=""):
    return model_dag({
        "numbers.sql": sql,
        "numbers.yaml": f"load_strategy: APPEND\n{quality}",
    })


def seed_target(engine_config, rows="(1)"):
    engine_config.warehouse_path.parent.mkdir()
    with duckdb.connect(str(engine_config.warehouse_path)) as connection:
        connection.execute("CREATE SCHEMA public")
        connection.execute("CREATE TABLE public.numbers (number INTEGER)")
        if rows:
            connection.execute(f"INSERT INTO public.numbers VALUES {rows}")


def target_rows(engine_config):
    with duckdb.connect(str(engine_config.warehouse_path)) as connection:
        return connection.execute(
            "SELECT * FROM public.numbers ORDER BY number"
        ).fetchall()


def generated_sql(engine_config):
    return next(
        (engine_config.warehouse_path.parent / "models").glob("*/public/numbers.sql")
    ).read_text()


def test_bootstrap_creates_target_and_saves_sql(model_dag, engine_config):
    dag = append_dag(model_dag, sql="SELECT 1 AS number")
    result = next(iter(execute_models(dag, engine_config, run_id="test-runZ").values()))
    assert result.status is ExecutionStatus.SUCCESS
    assert target_rows(engine_config) == [(1,)]
    assert generated_sql(engine_config) == (
        'CREATE TABLE "public"."numbers" AS\nSELECT 1 AS number'
    )


@pytest.mark.parametrize("rows, expected", [("(1)", [(1,), (2,)]), ("", [(2,)])])
def test_existing_target_appends_and_saves_insert_sql(
    model_dag, engine_config, rows, expected
):
    seed_target(engine_config, rows)
    dag = append_dag(model_dag)
    result = next(iter(execute_models(dag, engine_config, run_id="test-runZ").values()))
    assert result.status is ExecutionStatus.SUCCESS
    assert target_rows(engine_config) == expected
    assert generated_sql(engine_config) == (
        'INSERT INTO "public"."numbers"\nSELECT 2 AS number'
    )


def test_repeated_execution_appends_duplicates(model_dag, engine_config):
    dag = append_dag(model_dag)
    for run in range(2):
        result = next(iter(execute_models(dag, engine_config, run_id=f"test-run-{run}").values()))
        assert result.status is ExecutionStatus.SUCCESS
    assert target_rows(engine_config) == [(2,), (2,)]
    artifacts = list(
        (engine_config.warehouse_path.parent / "models").glob("*/public/numbers.sql")
    )
    assert len(artifacts) == 2
    assert {path.read_text() for path in artifacts} == {
        'CREATE TABLE "public"."numbers" AS\nSELECT 2 AS number',
        'INSERT INTO "public"."numbers"\nSELECT 2 AS number',
    }


def test_insert_failure_preserves_existing_rows(model_dag, engine_config):
    seed_target(engine_config)
    dag = append_dag(model_dag, sql="SELECT 'invalid' AS number")
    result = next(iter(execute_models(dag, engine_config, run_id="test-runZ").values()))
    assert result.status is ExecutionStatus.FAILED
    assert result.error
    assert target_rows(engine_config) == [(1,)]
    assert generated_sql(engine_config).startswith('INSERT INTO "public"."numbers"')


@pytest.mark.parametrize("severity, status, expected", [
    ("ERROR", ExecutionStatus.FAILED, [(1,)]),
    ("WARNING", ExecutionStatus.SUCCESS, [(1,), (2,)]),
])
def test_quality_controls_append_transaction(
    model_dag, engine_config, severity, status, expected
):
    seed_target(engine_config)
    quality = f"quality:\n  - type: row_count\n    severity: {severity}\n    min: 3\n"
    dag = append_dag(model_dag, quality=quality)
    result = next(iter(execute_models(dag, engine_config, run_id="test-runZ").values()))
    assert result.status is status
    assert len(result.quality_results) == 1
    assert result.quality_results[0].status is CheckStatus.FAIL
    assert result.quality_results[0].observed_value == 2
    assert target_rows(engine_config) == expected


def test_bootstrap_quality_error_removes_new_target(model_dag, engine_config):
    quality = "quality:\n  - type: row_count\n    severity: ERROR\n    min: 2\n"
    result = next(iter(execute_models(
        append_dag(model_dag, quality=quality), engine_config, run_id="test-runZ"
    ).values()))
    assert result.status is ExecutionStatus.FAILED
    assert result.quality_results[0].observed_value == 1
    with duckdb.connect(str(engine_config.warehouse_path)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'numbers'"
        ).fetchone() == (0,)


def test_append_rejects_delete_insert_yaml_before_execution(model_dag, engine_config):
    with pytest.raises(ValueError, match="APPEND does not allow"):
        model_dag({
            "numbers.sql": "SELECT 2 AS number",
            "numbers.yaml": "load_strategy: APPEND\ndelete_insert:\n  where: target.number = 1\n",
        })
    assert not engine_config.warehouse_path.exists()
