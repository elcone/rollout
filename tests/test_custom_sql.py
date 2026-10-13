from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from pydantic import ValidationError
from test_quality import RUN_TIMING

from rollout.config import CustomSqlCheck, ModelConfig
from rollout.dag import build_dag
from rollout.execution import (
    ExecutionStatus,
    build_model_sql,
    execute_model,
    execute_models,
)
from rollout.models import Model, ModelId
from rollout.quality import CheckStatus, evaluate_check
from rollout.renderer import render_execution_markdown


def custom(**changes):
    return (
        dict(
            type="custom_sql",
            name="completion_rate",
            sql="SELECT count(*) FROM main.data",
            expectations=[dict(operator="eq", value=1)],
            severity="ERROR",
        )
        | changes
    )


def model(check):
    return Model(
        ModelId("main", "data"),
        Path("data.sql"),
        "SELECT 2 AS id",
        config=ModelConfig(quality=[check]),
    )


def test_configuration_preserves_sql_and_is_frozen():
    sql = "  select 1 -- {{ untouched }}\n"
    check = ModelConfig(quality=[custom(sql=sql)]).quality[0]
    assert isinstance(check, CustomSqlCheck)
    assert check.sql == sql
    assert isinstance(check.expectations, tuple)
    for obj, field, value in [
        (check, "name", "other"),
        (check.expectations[0], "value", 2),
    ]:
        with pytest.raises(ValidationError):
            setattr(obj, field, value)


@pytest.mark.parametrize("field", ["name", "sql", "expectations", "severity"])
def test_required_fields(field):
    config = custom()
    del config[field]
    with pytest.raises(ValidationError):
        ModelConfig(quality=[config])


@pytest.mark.parametrize(
    "changes",
    [
        dict(name=""),
        dict(name="  "),
        dict(sql=""),
        dict(sql=" \n"),
        dict(expectations=[]),
        dict(description="extra"),
        dict(min=0),
        dict(max=1),
        dict(expectations=[dict(operator="other", value=1)]),
        dict(expectations=[dict(operator="eq", value="1")]),
        dict(expectations=[dict(operator="eq", value=True)]),
        dict(expectations=[dict(operator="eq", value=None)]),
        dict(expectations=[dict(operator="eq", value=1, extra=True)]),
    ],
)
def test_invalid_configuration(changes):
    with pytest.raises(ValidationError):
        ModelConfig(quality=[custom(**changes)])


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "WITH x AS (SELECT 1 AS n) SELECT n FROM x",
        "SELECT count(*) FROM main.data a JOIN main.data b ON a.id = b.id",
        "SELECT (SELECT count(*) FROM main.data)",
        "  select 1 -- {{ not_a_template }}\n",
    ],
)
def test_read_only_queries(sql):
    item = model(custom(sql=sql))
    with duckdb.connect() as connection:
        result = execute_model(connection, item, build_model_sql(item))
    assert result.status is ExecutionStatus.SUCCESS
    assert result.quality_results[0].observed_value == 1


@pytest.mark.parametrize("severity", ["WARNING", "ERROR"])
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT (",
        "SELECT 1; SELECT 2",
        "INSERT INTO main.data VALUES (3)",
        "UPDATE main.data SET id = 3",
        "DELETE FROM main.data",
        "CREATE TABLE other AS SELECT 1",
        "DROP TABLE main.data",
        "ALTER TABLE main.data ADD COLUMN other INT",
        "MERGE INTO main.data USING (SELECT 3 AS id) s ON data.id = s.id WHEN MATCHED THEN DELETE",
        "SELECT * INTO other FROM main.data",
        "COPY main.data TO '/tmp/rollout-custom.csv'",
        "WITH x AS (DELETE FROM main.data RETURNING *) SELECT count(*) FROM x",
        "SELECT missing FROM main.data",
        "SELECT 1 WHERE FALSE",
        "SELECT 1 UNION ALL SELECT 2",
        "SELECT 1, 2",
        "SELECT 'text'",
        "SELECT TRUE",
        "SELECT NULL",
        "SELECT DATE '2026-01-01'",
        "-- empty query",
    ],
)
def test_execution_errors_always_roll_back(sql, severity):
    item = model(custom(sql=sql, severity=severity))
    with duckdb.connect() as connection:
        connection.execute("CREATE TABLE data AS SELECT 99 AS id")
        result = execute_model(connection, item, build_model_sql(item))
        assert result.status is ExecutionStatus.FAILED
        assert "Quality check execution error (custom_sql)" in result.error
        assert connection.execute("SELECT * FROM data").fetchall() == [(99,)]


@pytest.mark.parametrize(
    "operator,expected",
    [
        ("gt", [False, False, True]),
        ("gte", [False, True, True]),
        ("eq", [False, True, False]),
        ("neq", [True, False, True]),
        ("lt", [True, False, False]),
        ("lte", [True, True, False]),
    ],
)
def test_operators(operator, expected):
    check = ModelConfig(
        quality=[custom(expectations=[dict(operator=operator, value=1)])]
    ).quality[0]
    assert [evaluate_check(check, value) for value in (0, 1, 2)] == expected


def test_and_semantics_and_contradictions():
    check = ModelConfig(
        quality=[
            custom(
                expectations=[
                    dict(operator="gt", value=0),
                    dict(operator="lt", value=10),
                ]
            )
        ]
    ).quality[0]
    assert [evaluate_check(check, value) for value in (5, 0, 10)] == [
        True,
        False,
        False,
    ]
    contradictory = ModelConfig(
        quality=[
            custom(
                expectations=[
                    dict(operator="eq", value=10),
                    dict(operator="neq", value=10),
                ]
            )
        ]
    ).quality[0]
    assert not evaluate_check(contradictory, 10)


@pytest.mark.parametrize(
    "value,severity,success",
    [(1, "ERROR", True), (0, "WARNING", True), (0, "ERROR", False)],
)
def test_transactions_dependents_and_report(engine_config, value, severity, success):
    parent = model(
        custom(
            expectations=[
                dict(operator="gte", value=value),
                dict(operator="lte", value=value),
            ],
            severity=severity,
        )
    )
    child = Model(
        ModelId("main", "child"),
        Path("child.sql"),
        "SELECT * FROM main.data",
        dependencies=frozenset([parent.id]),
    )
    engine_config.warehouse_path.parent.mkdir()
    with duckdb.connect(str(engine_config.warehouse_path)) as connection:
        connection.execute("CREATE TABLE data AS SELECT 99 AS id")
    dag = build_dag([parent, child])
    results = execute_models(dag, engine_config)
    assert results[parent.id].status is (
        ExecutionStatus.SUCCESS if success else ExecutionStatus.FAILED
    )
    assert results[child.id].status is (
        ExecutionStatus.SUCCESS if success else ExecutionStatus.SKIPPED
    )
    with duckdb.connect(str(engine_config.warehouse_path)) as connection:
        assert connection.execute("SELECT * FROM data").fetchall() == [
            (2 if success else 99,)
        ]
    status = "PASS" if value == 1 else "FAIL"
    assert results[parent.id].quality_results[0].status is CheckStatus(status)
    report = render_execution_markdown(dag, results, **RUN_TIMING)
    assert (
        f"| main.data | completion_rate | 1 | >= {value} AND <= {value} | {status} | {severity} |"
        in report
    )
    assert f"<br/>Quality: 1 {'PASS' if value == 1 else severity}" in report
    mermaid = report.split("```mermaid")[1].split("```")[0]
    assert "SELECT" not in mermaid
    assert "completion_rate" not in mermaid


@pytest.mark.parametrize(
    "operator,symbol",
    [
        ("gt", ">"),
        ("gte", ">="),
        ("eq", "="),
        ("neq", "<>"),
        ("lt", "<"),
        ("lte", "<="),
    ],
)
def test_report_symbols(operator, symbol):
    item = model(
        custom(
            name="a|<b>",
            expectations=[dict(operator=operator, value=0.95)],
            severity="WARNING",
        )
    )
    with duckdb.connect() as connection:
        result = execute_model(connection, item, build_model_sql(item))
    report = render_execution_markdown(
        build_dag([item]), {item.id: result}, **RUN_TIMING
    )
    assert f"| main.data | a&#124;&lt;b&gt; | 1 | {symbol} 0.95 |" in report


@pytest.mark.parametrize("value", [0.123456789, 1234567890123456789])
def test_report_preserves_expectation_precision(value):
    item = model(
        custom(expectations=[dict(operator="eq", value=value)], severity="WARNING")
    )
    with duckdb.connect() as connection:
        result = execute_model(connection, item, build_model_sql(item))
    report = render_execution_markdown(
        build_dag([item]), {item.id: result}, **RUN_TIMING
    )
    assert f"| = {value} |" in report


def test_decimal_scalar_uses_shared_observation():
    item = model(
        custom(sql="SELECT 0.95", expectations=[dict(operator="gte", value=0.9)])
    )
    with duckdb.connect() as connection:
        result = execute_model(connection, item, build_model_sql(item))
    assert result.status is ExecutionStatus.SUCCESS
    assert str(result.quality_results[0].observed_value) == "0.95"


@pytest.mark.parametrize(
    "observed,operator,value,passed",
    [
        (Decimal("0.1"), "eq", 0.1, True),
        (Decimal("0.1"), "neq", 0.1, False),
        (Decimal("1.0"), "eq", 1, True),
        (Decimal("0.1"), "gt", 0.1, False),
        (Decimal("0.1"), "gte", 0.1, True),
        (Decimal("0.1"), "lt", 0.1, False),
        (Decimal("0.1"), "lte", 0.1, True),
        (Decimal("0.2"), "gt", 0.1, True),
        (Decimal("0.1"), "lt", 0.2, True),
        (0.1, "eq", 0.1, True),
        (1, "eq", 1.0, True),
    ],
)
def test_custom_numeric_comparisons(observed, operator, value, passed):
    check = ModelConfig(
        quality=[
            custom(
                expectations=[dict(operator=operator, value=value)],
            )
        ]
    ).quality[0]
    assert evaluate_check(check, observed) is passed


def test_duckdb_decimal_equality_preserves_observed_value():
    item = model(
        custom(sql="SELECT 0.1", expectations=[dict(operator="eq", value=0.1)])
    )
    with duckdb.connect() as connection:
        result = execute_model(connection, item, build_model_sql(item))
    assert result.status is ExecutionStatus.SUCCESS
    assert result.quality_results[0].status is CheckStatus.PASS
    assert isinstance(result.quality_results[0].observed_value, Decimal)
    assert result.quality_results[0].observed_value == Decimal("0.1")
