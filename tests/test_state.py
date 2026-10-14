import json
from datetime import UTC, datetime
from decimal import Decimal

import duckdb
import pytest
import yaml
from typer.testing import CliRunner

from rollout.cli import app
from rollout.config import ModelConfig
from rollout.execution import ExecutionStatus, ModelResult
from rollout.models import ModelId
from rollout.quality import CheckStatus, QualityCheckResult
from rollout.seeders import SeederResult
from rollout.state import persist_run_state


def test_cli_state_and_append_only_history(tmp_path, monkeypatch):
    (tmp_path / "seeders").mkdir()
    (tmp_path / "seeders" / "source.py").write_text(
        "def run(connection, staging_dir):\n"
        "    return {'source': connection.sql('SELECT 1 AS id')}\n"
    )
    (tmp_path / "seeders" / "source.yaml").write_text(
        yaml.safe_dump(
            {
                "outputs": {
                    "source": {
                        "quality": [
                            {"type": "row_count", "min": 1, "severity": "ERROR"}
                        ]
                    }
                }
            }
        )
    )
    (tmp_path / "models" / "silver").mkdir(parents=True)
    (tmp_path / "models" / "first.sql").write_text("SELECT * FROM staging.source")
    (tmp_path / "models" / "first.yaml").write_text(
        yaml.safe_dump(
            {
                "quality": [
                    {
                        "type": "custom_sql",
                        "name": "id_total",
                        "sql": "SELECT sum(id) FROM public.first",
                        "severity": "ERROR",
                        "expectations": [
                            {"operator": "gte", "value": 1},
                            {"operator": "lt", "value": 2},
                        ],
                    }
                ]
            }
        )
    )
    (tmp_path / "models" / "silver" / "second.sql").write_text(
        "SELECT * FROM public.first"
    )
    monkeypatch.chdir(tmp_path)
    state_path = tmp_path / ".rollout" / "state.jsonl"
    previous = b""
    for run_count in (1, 2):
        result = CliRunner().invoke(app, ["run"])
        assert result.exit_code == 0, result.output
        assert state_path.is_file()
        content = state_path.read_bytes()
        assert content.startswith(previous)
        assert content.endswith(b"\n")
        lines = content.decode("utf-8").splitlines()
        assert len(lines) == run_count and all(lines)
        events = [json.loads(line) for line in lines]
        assert len({event["run_id"] for event in events}) == run_count
        event = events[-1]
        assert set(event) == {
            "schema_version",
            "run_id",
            "started_at",
            "finished_at",
            "duration",
            "status",
            "seeders",
            "models",
            "lineage",
            "quality",
        }
        assert type(event["schema_version"]) is int and event["schema_version"] == 1
        start = datetime.fromisoformat(event["started_at"])
        finish = datetime.fromisoformat(event["finished_at"])
        assert start.tzinfo == UTC and finish >= start
        assert event["duration"] >= 0 and event["status"] == "SUCCESS"
        run_id = event["run_id"]
        assert (tmp_path / ".rollout" / "runs" / f"{run_id}.md").is_file()
        assert (tmp_path / ".rollout" / "models" / run_id).is_dir()
        (seeder,) = event["seeders"]
        assert seeder["name"] == "seeders/source.py"
        assert (
            start
            <= datetime.fromisoformat(seeder["started_at"])
            <= datetime.fromisoformat(seeder["finished_at"])
            <= finish
        )
        assert seeder["status"] == "SUCCESS"
        assert seeder["duration"] >= seeder["execution_duration"] >= 0
        assert seeder["error"] is None and "output_models" not in seeder
        assert {(model["schema"], model["name"]) for model in event["models"]} == {
            ("staging", "source"),
            ("public", "first"),
            ("silver", "second"),
        }
        for model in event["models"]:
            assert (
                start
                <= datetime.fromisoformat(model["started_at"])
                <= datetime.fromisoformat(model["finished_at"])
                <= finish
            )
            assert model["status"] == "SUCCESS"
            assert model["duration"] >= model["materialization_duration"] >= 0
            assert model["error"] is None
        assert event["lineage"] == [
            {
                "job_type": "SEEDER",
                "job_name": "seeders/source.py",
                "inputs": [],
                "outputs": [{"schema": "staging", "name": "source"}],
            },
            {
                "job_type": "MODEL",
                "job_name": "public.first",
                "inputs": [{"schema": "staging", "name": "source"}],
                "outputs": [{"schema": "public", "name": "first"}],
            },
            {
                "job_type": "MODEL",
                "job_name": "silver.second",
                "inputs": [{"schema": "public", "name": "first"}],
                "outputs": [{"schema": "silver", "name": "second"}],
            },
        ]
        assert len(event["quality"]) == 2
        assert {group["checks"][0]["type"] for group in event["quality"]} == {
            "row_count",
            "custom_sql",
        }
        previous = content


@pytest.mark.parametrize("failure_source", ["seeder", "output", "model", None])
def test_exact_projection_quality_and_status(tmp_path, model_dag, failure_source):
    dag = model_dag({"first.sql": "SELECT * FROM external.source"})
    now = datetime.now(UTC)
    checks = ModelConfig(
        quality=[
            {"type": "row_count", "min": 1, "severity": "ERROR"},
            {"type": "nullness", "column": "email", "max": 0, "severity": "ERROR"},
            {"type": "nullness", "column": "phone", "max": 5, "severity": "WARNING"},
            {
                "type": "uniqueness",
                "columns": ["id", "period"],
                "max": 0,
                "severity": "ERROR",
            },
            {
                "type": "custom_sql",
                "name": "completion_rate",
                "sql": "SELECT 1",
                "expectations": [
                    {"operator": "gte", "value": 0.95},
                    {"operator": "lte", "value": 1},
                ],
                "severity": "ERROR",
            },
        ]
    ).quality
    observations = [1200, Decimal("0"), 2, 0.0, Decimal("0.975")]
    quality = tuple(
        QualityCheckResult(
            check,
            CheckStatus.PASS,
            check.severity,
            observed,
            0.125,
            message="not persisted",
        )
        for check, observed in zip(checks, observations)
    )
    error = 'source failed\n"quoted" \\ path café'
    output = ModelResult(
        ExecutionStatus.FAILED
        if failure_source == "output"
        else ExecutionStatus.SUCCESS,
        error=error if failure_source == "output" else None,
        duration=1.5,
        materialization_duration=1.0,
        quality_results=quality,
        started_at=now,
        finished_at=now,
    )
    model = (
        ModelResult(
            ExecutionStatus.FAILED,
            error=error,
            duration=2,
            materialization_duration=1,
            started_at=now,
            finished_at=now,
        )
        if failure_source == "model"
        else ModelResult(ExecutionStatus.SKIPPED)
    )
    seeder = SeederResult(
        ExecutionStatus.FAILED
        if failure_source == "seeder"
        else ExecutionStatus.SUCCESS,
        error if failure_source == "seeder" else None,
        0.5,
        2.0,
        {ModelId("staging", "source"): output},
        now,
        now,
    )
    path = tmp_path / "nested" / "state.jsonl"
    persist_run_state(
        path,
        run_id="exact",
        started_at=now,
        finished_at=now,
        total_duration=3.0,
        dag=dag,
        results={dag.order[0]: model},
        seeder_results={"seeders/source.py": seeder},
    )
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["run_id"] == "exact"
    assert event["started_at"] == event["finished_at"] == now.isoformat()
    assert event["duration"] == 3.0
    assert event["status"] == ("FAILED" if failure_source else "SUCCESS")
    assert event["seeders"] == [
        {
            "name": "seeders/source.py",
            "started_at": now.isoformat(),
            "finished_at": now.isoformat(),
            "status": seeder.status.value,
            "duration": 2.0,
            "execution_duration": 0.5,
            "error": seeder.error,
        }
    ]
    assert event["models"][1] == {
        "schema": "staging",
        "name": "source",
        "started_at": now.isoformat(),
        "finished_at": now.isoformat(),
        "status": output.status.value,
        "duration": 1.5,
        "materialization_duration": 1.0,
        "error": output.error,
    }
    if failure_source != "model":
        assert event["models"][0] == {
            "schema": "public",
            "name": "first",
            "started_at": None,
            "finished_at": None,
            "status": "SKIPPED",
            "duration": None,
            "materialization_duration": None,
            "error": None,
        }
    else:
        assert event["models"][0]["error"] == error
    assert event["lineage"][1]["inputs"] == [{"schema": "external", "name": "source"}]
    (group,) = event["quality"]
    assert group["model"] == {"schema": "staging", "name": "source"}
    expected_parameters = [
        {"min": 1},
        {"column": "email", "max": 0},
        {"column": "phone", "max": 5},
        {"columns": ["id", "period"], "max": 0},
        {
            "expectations": [
                {"operator": "gte", "value": 0.95},
                {"operator": "lte", "value": 1},
            ]
        },
    ]
    names = [
        "row_count",
        "nullness(email)",
        "nullness(phone)",
        "uniqueness(id, period)",
        "completion_rate",
    ]
    expected = [">= 1", "<= 0", "<= 5", "<= 0", ">= 0.95 AND <= 1"]
    for index, check in enumerate(group["checks"]):
        assert check == {
            "type": checks[index].type,
            "name": names[index],
            "parameters": expected_parameters[index],
            "observed": float(observations[index]),
            "expected": expected[index],
            "status": "PASS",
            "severity": checks[index].severity.value,
            "duration": 0.125,
        }
    assert type(group["checks"][0]["observed"]) is int
    assert type(group["checks"][1]["observed"]) is int
    assert type(group["checks"][4]["observed"]) is float
    assert lines[0] == json.dumps(event, separators=(",", ":"))


def test_empty_run_has_empty_sections(tmp_path, model_dag):
    now = datetime.now(UTC)
    path = tmp_path / "state.jsonl"
    persist_run_state(
        path,
        run_id="empty",
        started_at=now,
        finished_at=now,
        total_duration=0,
        dag=model_dag({}),
        results={},
        seeder_results={},
    )
    event = json.loads(path.read_text())
    assert event["status"] == "SUCCESS"
    for field in ("seeders", "models", "lineage", "quality"):
        assert event[field] == []


@pytest.mark.parametrize("model_fails", [False, True])
def test_state_write_failure_is_noncritical(tmp_path, monkeypatch, model_fails):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "first.sql").write_text(
        "SELECT * FROM missing" if model_fails else "SELECT 1 AS id"
    )
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".rollout" / "state.jsonl").mkdir(parents=True)
    result = CliRunner().invoke(app, ["run"])
    assert result.exit_code == (1 if model_fails else 0), result.output
    assert "Could not persist run state" in result.stderr
    assert len(list((tmp_path / ".rollout" / "runs").glob("*.md"))) == 1
    if not model_fails:
        with duckdb.connect(
            str(tmp_path / ".rollout" / "warehouse.duckdb")
        ) as connection:
            assert connection.execute("SELECT * FROM public.first").fetchall() == [(1,)]
