from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from pydantic import ValidationError

from rollout.config import ModelConfig, CheckSeverity
from rollout.dag import build_dag
from rollout.execution import ExecutionStatus, build_model_sql, execute_model, execute_models
from rollout.models import Model, ModelId
from rollout.quality import CheckStatus, evaluate_check, observe_numeric_scalar
from rollout.renderer import render_execution_markdown


def model(sql, checks):
    return Model(ModelId("main", "data"), Path("data.sql"), sql,
                 config=ModelConfig(quality=checks))


@pytest.mark.parametrize("count,minimum,severity,status", [
    (1, 1, "ERROR", "PASS"), (0, 1, "WARNING", "FAIL"), (0, 1, "ERROR", "FAIL"),
])
def test_row_count(count, minimum, severity, status):
    item = model(f"SELECT 1 AS id LIMIT {count}", [
        dict(type="row_count", min=minimum, severity=severity)])
    with duckdb.connect() as connection:
        result = execute_model(connection, item, build_model_sql(item))
        assert result.quality_results[0].status.value == status
        assert result.quality_results[0].observed_value == count
        assert result.status is (ExecutionStatus.FAILED if status == "FAIL" and severity == "ERROR" else ExecutionStatus.SUCCESS)


@pytest.mark.parametrize("sql,threshold,observed,status", [
    ('SELECT 1 AS "a""b"', 0, 0, "PASS"),
    ('SELECT NULL AS "a""b"', 1, 1, "PASS"),
    ('SELECT NULL AS "a""b"', 0, 1, "FAIL"),
])
def test_nullness(sql, threshold, observed, status):
    item = model(sql, [dict(type="nullness", column='a"b', max=threshold, severity="WARNING")])
    with duckdb.connect() as connection:
        result = execute_model(connection, item, build_model_sql(item))
    check = result.quality_results[0]
    assert (check.observed_value, check.status.value) == (observed, status)


@pytest.mark.parametrize("rows,columns,observed", [
    ("(1, 1), (2, 1)", ["id"], 0),
    ("(1, 1), (1, 2), (2, 1), (2, 1)", ["id"], 2),
    ("(1, 1), (1, 2)", ["id", "part"], 0),
    ("(1, 1), (1, 1), (1, 2)", ["id", "part"], 1),
])
def test_uniqueness(rows, columns, observed):
    item = model(f"SELECT * FROM (VALUES {rows}) AS t(id, part)", [
        dict(type="uniqueness", columns=columns, max=0, severity="WARNING")])
    with duckdb.connect() as connection:
        result = execute_model(connection, item, build_model_sql(item))
    assert result.quality_results[0].observed_value == observed
    assert result.quality_results[0].status is (CheckStatus.PASS if observed == 0 else CheckStatus.FAIL)


@pytest.mark.parametrize("sql,expected", [
    ("SELECT 42", 42), ("SELECT 1.25", Decimal("1.25")), ("SELECT 1::DOUBLE", 1.0),
])
def test_numeric_scalar(sql, expected):
    with duckdb.connect() as connection:
        assert observe_numeric_scalar(connection, sql) == expected


@pytest.mark.parametrize("sql,message", [
    ("SELECT 1 WHERE FALSE", "one row"),
    ("SELECT 1 UNION ALL SELECT 2", "one row"),
    ("SELECT 1, 2", "one column"),
    ("SELECT 'text'", "numeric scalar"),
    ("SELECT TRUE", "numeric scalar"),
    ("SELECT NULL", "numeric scalar"),
    ("SELECT DATE '2026-01-01'", "numeric scalar"),
])
def test_invalid_scalar(sql, message):
    with duckdb.connect() as connection, pytest.raises(ValueError, match=message):
        observe_numeric_scalar(connection, sql)


@pytest.mark.parametrize("severity", ["WARNING", "ERROR"])
def test_transactions_dependents_and_report(engine_config, severity):
    parent = model("SELECT 2 AS id", [
        dict(type="row_count", min=1, severity="ERROR"),
        dict(type="row_count", min=2, severity=severity)])
    child = Model(ModelId("main", "child"), Path("child.sql"), "SELECT * FROM main.data",
                  dependencies=frozenset([parent.id]))
    engine_config.warehouse_path.parent.mkdir()
    with duckdb.connect(str(engine_config.warehouse_path)) as connection:
        connection.execute("CREATE TABLE main.data AS SELECT 99 AS id")
    dag = build_dag([parent, child])
    results = execute_models(dag, engine_config)
    warning = severity == "WARNING"
    assert results[parent.id].status is (ExecutionStatus.SUCCESS if warning else ExecutionStatus.FAILED)
    assert results[child.id].status is (ExecutionStatus.SUCCESS if warning else ExecutionStatus.SKIPPED)
    check = results[parent.id].quality_results[1]
    assert check.status is CheckStatus.FAIL
    assert check.severity is CheckSeverity(severity)
    with duckdb.connect(str(engine_config.warehouse_path)) as connection:
        assert connection.execute("SELECT * FROM main.data").fetchall() == [(2 if warning else 99,)]
    report = render_execution_markdown(dag, results)
    assert f"1 PASS · 1 {severity}" in report
    assert "## Quality details" in report
    assert f"| main.data | row_count | 1 | >= 2 | FAIL | {severity} |" in report
    assert f"<br/>Quality: 1 PASS · 1 {severity}" in report
    child_node = next(line for line in report.splitlines() if '("child<br/>' in line)
    assert "Quality:" not in child_node
    if not warning:
        assert "Quality rule failed" in results[parent.id].error
        assert not results[child.id].quality_results


def test_quality_execution_error_rolls_back_even_for_warning():
    item = model("SELECT 2 AS id", [
        dict(type="row_count", min=1, severity="ERROR"),
        dict(type="nullness", column="missing", max=0, severity="WARNING")])
    with duckdb.connect() as connection:
        connection.execute("CREATE TABLE data AS SELECT 99 AS id")
        result = execute_model(connection, item, build_model_sql(item))
        assert result.status is ExecutionStatus.FAILED
        assert "Quality check execution error (nullness)" in result.error
        assert len(result.quality_results) == 1
        assert connection.execute("SELECT * FROM data").fetchall() == [(99,)]


def test_model_sql_error_does_not_run_checks():
    item = model("SELECT missing", [dict(type="row_count", min=1, severity="ERROR")])
    with duckdb.connect() as connection:
        result = execute_model(connection, item, build_model_sql(item))
    assert result.status is ExecutionStatus.FAILED
    assert result.quality_results == ()


@pytest.mark.parametrize("check", [
    dict(type="row_count", severity="ERROR"),
    dict(type="row_count", severity="ERROR", min=-1),
    dict(type="row_count", severity="ERROR", min=True),
    dict(type="row_count", severity="ERROR", min=1.5),
    dict(type="row_count", severity="OTHER", min=1),
    dict(type="row_count", min=1),
    dict(type="custom_sql", severity="ERROR", min=1),
    dict(type="nullness", severity="WARNING", column="", max=0),
    dict(type="uniqueness", severity="ERROR", columns=[], max=0),
    dict(type="row_count", severity="ERROR", min=1, extra=2),
])
def test_invalid_configuration(check):
    with pytest.raises(ValidationError):
        ModelConfig(quality=[check])


def test_configuration_is_deeply_immutable():
    config = ModelConfig(quality=[dict(type="uniqueness", severity="ERROR", columns=["id"], max=0)])
    assert isinstance(config.quality, tuple)
    assert config.quality[0].columns == ("id",)
    with pytest.raises(ValidationError):
        config.quality[0].max = 1


def test_sql_quotes_target_and_key_identifiers():
    from rollout.quality import build_row_count_sql, build_nullness_sql, build_uniqueness_sql

    identity = ModelId('s"x', 't"y')
    assert build_row_count_sql(identity) == 'SELECT COUNT(*) FROM "s""x"."t""y"'
    assert build_nullness_sql(identity, 'c"z') == (
        'SELECT COUNT(*) FROM "s""x"."t""y" WHERE "c""z" IS NULL')
    assert build_uniqueness_sql(identity, ('c"z', 'order')) == (
        'SELECT COUNT(*) FROM (SELECT "c""z", "order" FROM "s""x"."t""y" '
        'GROUP BY "c""z", "order" HAVING COUNT(*) > 1) AS duplicate_groups')


def test_warning_details_and_names_are_escaped_in_report():
    item = model('SELECT NULL AS "a|<b>"', [
        dict(type="nullness", column="a|<b>", max=0, severity="WARNING")])
    with duckdb.connect() as connection:
        result = execute_model(connection, item, build_model_sql(item))
    report = render_execution_markdown(build_dag([item]), {item.id: result})
    assert "| main.data | nullness(a&#124;&lt;b&gt;) | 1 | <= 0 | FAIL | WARNING |" in report
    assert "| 1 WARNING |" in report
    assert "<br/>Quality: 1 WARNING" in report


@pytest.mark.parametrize(
    "configuration, expected",
    [
        ({"type": "row_count", "min": 1}, [False, True, True]),
        ({"type": "nullness", "column": "id", "max": 1}, [True, True, False]),
        ({"type": "uniqueness", "columns": ["id"], "max": 1}, [True, True, False]),
    ],
)
def test_quality_checks_evaluate_expectations(configuration, expected):
    check = ModelConfig(quality=[{**configuration, "severity": "ERROR"}]).quality[0]
    assert [evaluate_check(check, value) for value in (0, 1, 2)] == expected

