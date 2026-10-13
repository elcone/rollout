from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import indent

import duckdb
import pytest
import yaml

from rollout import seeders
from rollout.discovery import discover_seeders
from rollout.execution import ExecutionStatus
from rollout.models import ModelId
from rollout.quality import CheckStatus
from rollout.seeders import execute_seeder


@pytest.fixture
def connection():
    with duckdb.connect() as connection:
        yield connection


@pytest.fixture
def write_seeder(tmp_path, monkeypatch):
    monkeypatch.setattr(
        seeders, "TemporaryDirectory", partial(TemporaryDirectory, dir=tmp_path)
    )

    def write(body, outputs):
        root = tmp_path / "seeders"
        root.mkdir()
        marker = tmp_path / "staging_path.txt"
        (root / "source.py").write_text(
            "from pathlib import Path\n"
            "def run(connection, staging_dir):\n"
            "    assert staging_dir.is_dir()\n"
            f"    Path({str(marker)!r}).write_text(str(staging_dir))\n"
            + indent(body, "    "),
            encoding="utf-8",
        )
        (root / "source.yaml").write_text(
            yaml.safe_dump({"outputs": outputs}, sort_keys=False), encoding="utf-8"
        )
        return discover_seeders(root)[0], marker

    return write


def assert_cleaned(marker, connection):
    assert not Path(marker.read_text()).exists()
    assert (
        connection.sql(
            "SELECT table_name FROM information_schema.tables WHERE table_type = 'VIEW'"
        ).fetchall()
        == []
    )


def row_count(severity="ERROR", minimum=1):
    return {"type": "row_count", "min": minimum, "severity": severity}


def test_parquet_lives_through_materialization_and_quality(
    write_seeder, connection, tmp_path
):
    parquet_glob = str(tmp_path / "rollout-seeder-*" / "orders.parquet")
    seeder, marker = write_seeder(
        "path = staging_dir / 'orders.parquet'\n"
        "connection.sql('SELECT 1 AS id').write_parquet(str(path))\n"
        "return {'orders': connection.read_parquet(str(path))}\n",
        {
            "orders": {
                "quality": [
                    row_count(),
                    {"type": "nullness", "column": "id", "max": 0, "severity": "ERROR"},
                    {
                        "type": "uniqueness",
                        "columns": ["id"],
                        "max": 0,
                        "severity": "ERROR",
                    },
                    {
                        "type": "custom_sql",
                        "name": "temporary_parquet_exists",
                        "sql": f"SELECT count(*) FROM read_parquet('{parquet_glob}')",
                        "expectations": [{"operator": "eq", "value": 1}],
                        "severity": "ERROR",
                    },
                ]
            }
        },
    )
    result = execute_seeder(seeder, connection).outputs[ModelId("staging", "orders")]
    assert result.status is ExecutionStatus.SUCCESS
    assert result.duration is not None and result.duration >= 0
    assert len(result.quality_results) == 4
    assert all(check.status is CheckStatus.PASS for check in result.quality_results)
    assert connection.sql("SELECT * FROM staging.orders").fetchall() == [(1,)]
    assert_cleaned(marker, connection)


def test_full_replaces_existing_target_and_preserves_identifier_case(
    write_seeder, connection
):
    connection.execute(
        'CREATE SCHEMA staging; CREATE TABLE staging."Mixed""Name" AS SELECT 99 AS id'
    )
    seeder, marker = write_seeder(
        "return {'Mixed\"Name': connection.sql('SELECT 2 AS id')}\n",
        {'Mixed"Name': {}},
    )
    result = execute_seeder(seeder, connection).outputs[ModelId("staging", 'Mixed"Name')]
    assert result.status is ExecutionStatus.SUCCESS
    assert connection.sql('SELECT * FROM staging."Mixed""Name"').fetchall() == [(2,)]
    assert_cleaned(marker, connection)


@pytest.mark.parametrize("existing_second", [False, True])
def test_independent_transactions_and_yaml_processing_order(
    write_seeder, connection, existing_second
):
    connection.execute("CREATE SCHEMA staging")
    if existing_second:
        connection.execute("CREATE TABLE staging.second AS SELECT 99 AS id")
    seeder, marker = write_seeder(
        "return {\n"
        "    'third': connection.sql('SELECT 3 AS id'),\n"
        "    'second': connection.sql('SELECT 2 AS id'),\n"
        "    'first': connection.sql('SELECT 1 AS id'),\n"
        "}\n",
        {
            "first": {},
            "second": {"quality": [row_count(minimum=2)]},
            "third": {
                "quality": [
                    {
                        "type": "custom_sql",
                        "name": "first_already_committed",
                        "sql": "SELECT count(*) FROM staging.first",
                        "expectations": [{"operator": "eq", "value": 1}],
                        "severity": "ERROR",
                    }
                ]
            },
        },
    )
    aggregate = execute_seeder(seeder, connection)
    assert aggregate.status is ExecutionStatus.FAILED
    assert aggregate.error is None
    results = aggregate.outputs
    assert list(results) == [
        ModelId("staging", name) for name in ("first", "second", "third")
    ]
    assert [result.status for result in results.values()] == [
        ExecutionStatus.SUCCESS,
        ExecutionStatus.FAILED,
        ExecutionStatus.SUCCESS,
    ]
    failed = results[ModelId("staging", "second")]
    assert "Quality rule failed" in failed.error
    assert failed.quality_results[0].status is CheckStatus.FAIL
    assert all(
        result.duration is not None and result.duration >= 0
        for result in results.values()
    )
    assert connection.sql("SELECT * FROM staging.first").fetchall() == [(1,)]
    assert connection.sql("SELECT * FROM staging.third").fetchall() == [(3,)]
    if existing_second:
        assert connection.sql("SELECT * FROM staging.second").fetchall() == [(99,)]
    else:
        assert (
            connection.sql(
                "SELECT table_name FROM information_schema.tables WHERE table_name = 'second'"
            ).fetchall()
            == []
        )
    assert_cleaned(marker, connection)


def test_warning_commits(write_seeder, connection):
    seeder, marker = write_seeder(
        "return {'orders': connection.sql('SELECT 1 AS id')}\n",
        {"orders": {"quality": [row_count("WARNING", minimum=2)]}},
    )
    result = execute_seeder(seeder, connection).outputs[ModelId("staging", "orders")]
    assert result.status is ExecutionStatus.SUCCESS
    assert result.quality_results[0].status is CheckStatus.FAIL
    assert connection.sql("SELECT * FROM staging.orders").fetchall() == [(1,)]
    assert_cleaned(marker, connection)


@pytest.mark.parametrize(
    "check",
    [
        {
            "type": "custom_sql",
            "name": "invalid",
            "sql": "SELECT FROM",
            "expectations": [{"operator": "eq", "value": 1}],
            "severity": "ERROR",
        },
        {"type": "nullness", "column": "missing", "max": 0, "severity": "ERROR"},
    ],
)
def test_quality_execution_error_rolls_back_and_continues(
    write_seeder, connection, check
):
    connection.execute(
        "CREATE SCHEMA staging; CREATE TABLE staging.first AS SELECT 99 AS id"
    )
    seeder, marker = write_seeder(
        "return {'first': connection.sql('SELECT 1 AS id'), "
        "'second': connection.sql('SELECT 2 AS id')}\n",
        {"first": {"quality": [check]}, "second": {}},
    )
    aggregate = execute_seeder(seeder, connection)
    assert aggregate.status is ExecutionStatus.FAILED
    assert aggregate.error is None
    results = aggregate.outputs
    assert results[ModelId("staging", "first")].status is ExecutionStatus.FAILED
    assert "Quality check execution error" in results[ModelId("staging", "first")].error
    assert results[ModelId("staging", "second")].status is ExecutionStatus.SUCCESS
    assert connection.sql("SELECT * FROM staging.first").fetchall() == [(99,)]
    assert connection.sql("SELECT * FROM staging.second").fetchall() == [(2,)]
    assert_cleaned(marker, connection)


def test_materialization_error_rolls_back_and_continues(write_seeder, connection):
    connection.execute(
        "CREATE SCHEMA staging; CREATE TABLE staging.first AS SELECT 99 AS id"
    )
    seeder, marker = write_seeder(
        "path = staging_dir / 'orders.parquet'\n"
        "connection.sql('SELECT 1 AS id').write_parquet(str(path))\n"
        "relation = connection.read_parquet(str(path))\n"
        "path.unlink()\n"
        "return {'first': relation, 'second': connection.sql('SELECT 2 AS id')}\n",
        {"first": {}, "second": {}},
    )
    aggregate = execute_seeder(seeder, connection)
    assert aggregate.status is ExecutionStatus.FAILED
    assert aggregate.error is None
    results = aggregate.outputs
    assert results[ModelId("staging", "first")].status is ExecutionStatus.FAILED
    assert results[ModelId("staging", "second")].status is ExecutionStatus.SUCCESS
    assert connection.sql("SELECT * FROM staging.first").fetchall() == [(99,)]
    assert connection.sql("SELECT * FROM staging.second").fetchall() == [(2,)]
    assert_cleaned(marker, connection)


@pytest.mark.parametrize(
    "body, cause",
    [
        ("raise LookupError('source failed')\n", LookupError),
        ("return {'orders': []}\n", ValueError),
        ("return {'unexpected': connection.sql('SELECT 1')}\n", ValueError),
    ],
)
def test_seeder_failure_cleans_temp_and_materializes_nothing(
    write_seeder, connection, body, cause
):
    seeder, marker = write_seeder(body, {"orders": {}})
    result = execute_seeder(seeder, connection)
    assert result.status is ExecutionStatus.FAILED
    assert result.outputs == {}
    assert "Seeder execution failed" in result.error
    assert str(seeder.path) in result.error
    assert connection.sql("SELECT * FROM information_schema.tables").fetchall() == []
    assert_cleaned(marker, connection)
