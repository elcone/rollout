from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tools.viewer.app import app
from tools.viewer.project import load_project


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "models").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write(root, name, contents):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)


def test_html_and_exact_routes(project):
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "fetch('/api/project')" in response.text
    assert "joint.min.js" in response.text
    assert "{{" not in response.text
    assert {route.path for route in app.routes} == {"/", "/api/project"}


def test_empty_project(project):
    assert TestClient(app).get("/api/project").json() == {
        "models": [],
        "seeders": [],
        "lineage": [],
    }


def test_models_only_and_dynamic_filesystem(project):
    write(project, "models/a.sql", "SELECT 1")
    client = TestClient(app)
    data = client.get("/api/project").json()
    assert [(m["schema"], m["name"], m["origin"]) for m in data["models"]] == [
        ("public", "a", "internal")
    ]
    assert data["seeders"] == []
    assert data["lineage"] == []
    write(project, "models/b.sql", "SELECT * FROM a")
    assert len(client.get("/api/project").json()["models"]) == 2


def test_dependencies_external_catalog_and_direction(project):
    write(project, "models/bronze/a.sql", "SELECT * FROM warehouse.raw.source")
    write(project, "models/silver/b.sql", "SELECT * FROM bronze.a")
    write(project, "models/gold/c.sql", "SELECT * FROM silver.b")
    data = load_project(project)
    assert len({m["schema"] for m in data["models"]}) == 4
    external = next(m for m in data["models"] if m["origin"] == "source")
    assert external["catalog"] == "warehouse"
    assert external["schema"] == "raw"
    pairs = {(e["source"]["name"], e["target"]["name"]) for e in data["lineage"]}
    # Source is the immediate upstream dependency; target is its downstream dependent.
    assert pairs == {("source", "a"), ("a", "b"), ("b", "c")}
    assert ("a", "c") not in pairs


def test_seeders_outputs_including_unreferenced_and_no_execution(project):
    write(project, "seeders/nested/import.py", "raise AssertionError('never execute')")
    write(project, "seeders/nested/import.yaml", "outputs:\n  orders: {}\n  unused: {}")
    write(project, "models/bronze/orders.sql", "SELECT * FROM staging.orders")
    # Malformed runtime history must be irrelevant.
    write(project, ".rollout/state.jsonl", "invalid history")
    data = TestClient(app).get("/api/project").json()
    seeder = data["seeders"][0]
    assert seeder["id"] == "seeders/nested/import.py"
    assert seeder["name"] == "import.py"
    assert {m["name"] for m in seeder["outputs"]} == {"orders", "unused"}
    assert {m["name"] for m in data["models"] if m["origin"] == "seeder"} == {
        "orders",
        "unused",
    }
    output_edges = [e for e in data["lineage"] if e["source"]["type"] == "SEEDER"]
    assert len(output_edges) == 2
    assert all(e["source"]["id"] == seeder["id"] for e in output_edges)
    assert len(data["models"]) == 3


@pytest.mark.parametrize(
    "files, message",
    [
        ({"models/a.sql": "SELECT ("}, "Invalid SQL"),
        (
            {"models/a.sql": "SELECT * FROM b", "models/b.sql": "SELECT * FROM a"},
            "cycle",
        ),
        (
            {"models/a.sql": "SELECT 1", "models/a.yaml": "unknown: true"},
            "configuration",
        ),
        ({"seeders/a.py": "", "seeders/a.yaml": "outputs: {}"}, "configuration"),
        (
            {
                "seeders/a.py": "",
                "seeders/b.py": "",
                "seeders/b.yaml": "outputs:\n  a: {}",
            },
            "Duplicate seeder output",
        ),
    ],
)
def test_invalid_projects_are_api_errors(project, files, message):
    for name, contents in files.items():
        write(project, name, contents)
    response = TestClient(app).get("/api/project")
    assert response.status_code == 400
    assert message.lower() in response.json()["detail"].lower()


def test_missing_models_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    response = TestClient(app).get("/api/project")
    assert response.status_code == 400
    assert "Models directory does not exist" in response.json()["detail"]


def test_duplicate_model_is_api_error(project, monkeypatch):
    from rollout.models import Model, ModelId

    model = Model(ModelId("public", "a"), Path("models/a.sql"), "SELECT 1")
    # Duplicate identities cannot be expressed in the normal filesystem layout.
    # Inject discovery output while retaining the real parser and DAG validation.
    monkeypatch.setattr(
        "tools.viewer.project.discover_models", lambda _: [model, model]
    )
    response = TestClient(app).get("/api/project")
    assert response.status_code == 400
    assert "Duplicate model" in response.json()["detail"]


def test_schema_reentry_preserves_model_dag(project):
    write(project, "models/bronze/a.sql", "SELECT 1")
    write(project, "models/silver/b.sql", "SELECT * FROM bronze.a")
    write(project, "models/bronze/c.sql", "SELECT * FROM silver.b")
    response = TestClient(app).get("/api/project")
    assert response.status_code == 200
    assert len(response.json()["lineage"]) == 2


def test_seeder_output_matches_external_node_and_preserves_chain(project):
    from rollout.dag import build_dag
    from rollout.discovery import discover_models
    from rollout.models import ModelId
    from rollout.parser import parse_models

    write(project, "seeders/alleva.py", 'raise AssertionError("never execute")')
    write(project, "seeders/alleva.yaml", "outputs:\n  services: {}\n  unused: {}")
    write(
        project,
        "models/silver/services.sql",
        "SELECT * FROM staging.services JOIN raw.customers USING (id)",
    )
    write(project, "models/gold/services.sql", "SELECT * FROM silver.services")
    dag = build_dag(parse_models(discover_models(project / "models")))
    assert ModelId("staging", "services") in dag.external_nodes

    data = TestClient(app).get("/api/project").json()
    by_id = {(m["schema"], m["name"]): m for m in data["models"]}
    assert by_id["staging", "services"]["origin"] == "seeder"
    assert by_id["staging", "unused"]["origin"] == "seeder"
    assert by_id["raw", "customers"]["origin"] == "source"
    assert by_id["silver", "services"]["origin"] == "internal"
    assert by_id["gold", "services"]["origin"] == "internal"
    assert len([m for m in data["models"] if m["origin"] == "source"]) == 1
    assert len(by_id) == len(data["models"]) == 5
    pairs = {
        (
            e["source"].get("id") or (e["source"]["schema"], e["source"]["name"]),
            (e["target"]["schema"], e["target"]["name"]),
        )
        for e in data["lineage"]
    }
    assert pairs == {
        ("seeders/alleva.py", ("staging", "services")),
        ("seeders/alleva.py", ("staging", "unused")),
        (("staging", "services"), ("silver", "services")),
        (("raw", "customers"), ("silver", "services")),
        (("silver", "services"), ("gold", "services")),
    }
    # The Viewer classification does not alter the core's external-node semantics.
    assert ModelId("staging", "services") in dag.external_nodes


def test_multiple_seeders_keep_producers_and_outputs_separate(project):
    write(project, "seeders/a.py", "")
    write(project, "seeders/a.yaml", "outputs:\n  first: {}\n  second: {}")
    write(project, "seeders/nested/b.py", "")
    write(project, "seeders/nested/b.yaml", "outputs:\n  third: {}")
    data = load_project(project)
    assert [s["id"] for s in data["seeders"]] == ["seeders/a.py", "seeders/nested/b.py"]
    assert len(data["models"]) == 3
    assert all(m["origin"] == "seeder" for m in data["models"])
    assert {(e["source"]["id"], e["target"]["name"]) for e in data["lineage"]} == {
        ("seeders/a.py", "first"),
        ("seeders/a.py", "second"),
        ("seeders/nested/b.py", "third"),
    }


def test_no_seeders_directory_leaves_unknown_dependencies_as_sources(project):
    write(project, "models/orders.sql", "SELECT * FROM staging.orders")
    data = load_project(project)
    assert not (project / "seeders").exists()
    assert data["seeders"] == []
    assert {(m["name"], m["origin"]) for m in data["models"]} == {
        ("orders", "internal"),
        ("orders", "source"),
    }
    assert len(data["lineage"]) == 1
