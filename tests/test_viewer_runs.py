import json

import pytest
from fastapi.testclient import TestClient

from tools.viewer.app import app


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / ".rollout" / "state.jsonl"
    path.parent.mkdir()
    return path


def run_document(run_id="first", status="SUCCESS", started_at="2026-11-04T12:00:00+00:00"):
    return {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": "2026-11-04T13:00:00+00:00",
        "duration": 1.25,
        "status": status,
        "seeders": [{
            "name": "seeders/source.py", "status": status,
            "started_at": started_at, "finished_at": "2026-11-04T13:00:00+00:00",
            "duration": 0.5, "execution_duration": 0.2,
            "error": "Import failed\nDetails" if status == "FAILED" else None,
        }],
        "models": [{
            "schema": "public", "name": "report", "status": "SKIPPED",
            "started_at": None, "finished_at": None, "duration": None,
            "materialization_duration": None, "error": None,
        }],
        "lineage": [{
            "job_type": "MODEL", "job_name": "public.report",
            "inputs": [{"schema": "staging", "name": "source"}],
            "outputs": [{"schema": "public", "name": "report"}],
        }],
        "quality": [{
            "model": {"schema": "staging", "name": "source"},
            "checks": [{
                "type": "row_count", "name": "row_count", "parameters": {"min": 1},
                "observed": 2, "expected": ">= 1", "status": "PASS",
                "severity": "ERROR", "duration": 0.01,
            }],
        }],
    }


@pytest.mark.parametrize("exists", [False, True])
def test_missing_or_empty_history(history, exists):
    if exists:
        history.write_text("")
    client = TestClient(app)
    assert client.get("/api/runs").json() == []
    assert client.get("/api/runs/missing").status_code == 404


def test_summary_order_detail_and_fresh_reads(history):
    # IDs and file order deliberately differ from timestamp order; offsets matter.
    older = run_document("z", started_at="2026-11-04T12:30:00+02:00")
    newer = run_document("a", "FAILED")
    history.write_text(json.dumps(newer) + "\n" + json.dumps(older) + "\n")
    client = TestClient(app)
    response = client.get("/api/runs")
    assert response.status_code == 200
    fields = ("schema_version", "run_id", "started_at", "finished_at", "duration", "status")
    assert response.json() == [{key: run[key] for key in fields} for run in (newer, older)]
    for run in (older, newer):
        detail = client.get(f"/api/runs/{run['run_id']}")
        assert detail.status_code == 200
        assert detail.json() == run
    assert client.get("/api/runs/unknown").status_code == 404
    history.write_text(json.dumps(older))  # A complete final JSON needs no newline.
    assert len(client.get("/api/runs").json()) == 1


@pytest.mark.parametrize("tail, message", [
    ("{invalid}\n", "line 2"),
    ('{"run_id":', "line 2"),
    ("\n", "line 2"),
    (json.dumps(run_document()), "duplicate run_id"),
    (json.dumps({**run_document("other"), "schema_version": 2}), "unsupported schema_version"),
    (json.dumps({**run_document("other"), "schema_version": True}), "unsupported schema_version"),
    ("[]", "run must be an object"),
    (json.dumps({**run_document("other"), "duration": float("nan")}), "invalid JSON numeric constant"),
    (json.dumps({**run_document("other"), "models": None}), "models must be an array"),
    (json.dumps({**run_document("other"), "started_at": "invalid"}), "line 2"),
    (json.dumps({**run_document("other"), "started_at": "2026-11-04T12:00:00"}), "timezone"),
    (json.dumps({**run_document("other"), "duration": -1}), "duration"),
    (json.dumps({**run_document("other"), "status": "RUNNING"}), "status"),
])
def test_corrupt_history_is_explicit_for_both_routes(history, tail, message):
    history.write_text(json.dumps(run_document()) + "\n" + tail)
    client = TestClient(app)
    for url in ("/api/runs", "/api/runs/first", "/api/runs/unknown"):
        response = client.get(url)
        assert response.status_code == 400
        assert message in response.json()["detail"]


def test_unreadable_history_is_explicit(history):
    history.mkdir()
    assert TestClient(app).get("/api/runs").status_code == 400


def test_invalid_encoding_is_explicit(history):
    history.write_bytes(b"\xff")
    assert TestClient(app).get("/api/runs").status_code == 400
