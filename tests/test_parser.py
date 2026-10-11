from dataclasses import replace

import duckdb
import pytest

from rollout.config import ModelConfig
from rollout.discovery import discover_models
from rollout.parser import parse_models


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 AS number",
        "WITH numbers AS (VALUES (1)) SELECT * FROM numbers",
        "SELECT * FROM (SELECT 1 AS number) AS numbers",
        "(SELECT 1 AS number)",
        "SELECT 1 UNION ALL SELECT 2",
        "SELECT 1 INTERSECT SELECT 1",
        "SELECT 1 EXCEPT SELECT 2",
        "VALUES (1), (2)",
        "PIVOT cities ON year USING sum(population)",
        "SELECT * FROM cities PIVOT (sum(population) FOR year IN (2020))",
        "WITH towns AS (SELECT * FROM cities) PIVOT towns ON year USING sum(population)",
    ],
)
def test_read_only_query_shapes(write_models, sql):
    root = write_models({"report.sql": sql})
    models = parse_models(discover_models(root))
    assert len(models) == 1
    assert models[0].sql == sql
    # Validate the supported examples against a small real DuckDB database too.
    with duckdb.connect() as connection:
        connection.execute(
            "CREATE TABLE cities AS SELECT 'Amsterdam' AS name, 2020 AS year, 100 AS population"
        )
        assert connection.execute(sql).description is not None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT FROM",
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP TABLE numbers",
        "VALUES (1); VALUES (2)",
        "INSERT INTO numbers SELECT 1",
        "UPDATE numbers SET number = 1",
        "DELETE FROM numbers",
        "CREATE TABLE numbers AS SELECT 1",
        "DROP TABLE numbers",
        "ALTER TABLE numbers ADD COLUMN id INTEGER",
        "SELECT 1 INTO numbers",
        "WITH deleted AS (DELETE FROM numbers RETURNING *) SELECT * FROM deleted",
        "COPY (SELECT 1) TO 'output.csv'",
        "SELECT 1 UNION ALL SELECT 2 INTO numbers",
    ],
)
def test_invalid_or_writable_sql_has_source_path(write_models, sql):
    root = write_models({"report.sql": sql})
    with pytest.raises(ValueError, match="Invalid SQL in .*report.sql"):
        parse_models(discover_models(root))


@pytest.mark.parametrize("sql", ["", " \n\t", "-- comment", "/* comment */", ";"])
def test_empty_models_are_ignored(write_models, caplog, sql):
    root = write_models({"empty.sql": sql, "numbers.sql": "SELECT 1"})
    models = parse_models(discover_models(root))
    assert [model.id.name for model in models] == ["numbers"]
    assert "Ignoring empty SQL model" in caplog.text
    assert str(root / "empty.sql") in caplog.text


def test_parsing_preserves_configuration_and_sql(write_models):
    original = "-- original\nSELECT 1 AS number; -- trailing comment"
    root = write_models({"numbers.sql": original})
    config = ModelConfig(load_strategy="FULL")
    model = replace(discover_models(root)[0], config=config)
    parsed = parse_models([model])[0]
    assert parsed.config is config
    assert parsed.sql == original


@pytest.mark.parametrize("predicate", [
    "target.id = 1",
    "target.id = 1 AND (target.number > 2 OR target.number IS NULL)",
    "target.period IN (SELECT DISTINCT operation_date FROM staging.source)",
    "EXISTS (SELECT 1 FROM staging.source AS source WHERE target.period = source.operation_date)",
    "target.operation_date >= current_date - INTERVAL '7 days'",
])
def test_delete_predicate_accepts_duckdb_expressions(predicate):
    from rollout.parser import validate_delete_predicate

    validate_delete_predicate(predicate)


@pytest.mark.parametrize("predicate", [
    "target.id =",
    "",
    "target.id = 1; DROP TABLE important",
    "target.id = 1; DELETE FROM other_table",
    "target.id = 1; SELECT 2",
    "target.id = 1 ORDER BY target.id",
    "target.id = 1 UNION SELECT 2",
])
def test_delete_predicate_rejects_invalid_structure(predicate):
    from rollout.parser import validate_delete_predicate

    with pytest.raises(ValueError, match="Invalid delete_insert.where"):
        validate_delete_predicate(predicate)
