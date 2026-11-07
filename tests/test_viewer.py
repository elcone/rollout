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
    assert {route.path for route in app.routes} == {
        "/", "/api/project", "/api/cll", "/api/runs", "/api/runs/{run_id}"
    }


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


def test_referenced_datasets_keep_distinct_catalog_namespaces(project):
    write(project, 'models/customers.sql', 'SELECT * FROM warehouse_a.raw.customers')
    write(project, 'models/orders.sql', 'SELECT * FROM warehouse_b.raw.orders')
    # The same dataset name in another catalog must also retain its identity.
    write(project, 'models/other_customers.sql', 'SELECT * FROM warehouse_b.raw.customers')
    response = TestClient(app).get('/api/project')
    assert response.status_code == 200
    data = response.json()
    referenced = {
        (m['catalog'], m['schema'], m['name'])
        for m in data['models'] if m['origin'] == 'source'
    }
    assert referenced == {
        ('warehouse_a', 'raw', 'customers'),
        ('warehouse_b', 'raw', 'orders'),
        ('warehouse_b', 'raw', 'customers'),
    }
    assert {
        (edge['source']['catalog'], edge['source']['schema'], edge['source']['name'])
        for edge in data['lineage']
    } == referenced
    assert len(data['models']) == 6


def cll_request(**params):
    return TestClient(app).get("/api/cll", params={
        "catalog": "", "schema": "public", "model": "result", **params,
    })


def col(schema, model, column="patient_name", catalog=""):
    return dict(catalog=catalog, schema=schema, model=model, column=column)


def test_cll_complete_model_and_dynamic_filesystem(project):
    write(project, "models/result.sql", "SELECT patient_id, patient_name, 1 AS version FROM staging.services")
    response = cll_request()
    assert response.status_code == 200
    data = response.json()
    assert data["root"] == {"catalog": "", "schema": "public", "model": "result"}
    assert data["datasets"] == [
        {**data["root"], "columns": ["patient_id", "patient_name", "version"]},
        {"catalog": "", "schema": "staging", "model": "services", "columns": ["patient_id", "patient_name"]},
    ]
    assert data["edges"] == [
        {"source": col("staging", "services", column), "target": col("public", "result", column)}
        for column in ["patient_id", "patient_name"]
    ]
    assert next(c for c in data["columns"] if c["column"] == "version")["text"] == "public.result.version"
    write(project, "models/result.sql", "SELECT patient_name FROM external.services")
    assert cll_request().json()["edges"][0]["source"]["schema"] == "external"


def test_cll_recursive_branching_and_seeder_boundary(project):
    write(project, "seeders/import.py", "raise AssertionError('never execute')")
    # Even invalid Seeder configuration is irrelevant to CLL.
    write(project, "seeders/import.yaml", "invalid: true")
    write(project, "models/bronze/clients.sql", "SELECT first_name, last_name, patient_id FROM staging.clients")
    write(project, "models/silver/clients.sql", "SELECT concat(first_name, ' ', last_name) AS full_name, patient_id FROM bronze.clients")
    write(project, "models/gold/clients.sql", "SELECT full_name AS patient_name, patient_id FROM silver.clients")
    response = cll_request(schema="gold", model="clients")
    assert response.status_code == 200
    data = response.json()
    assert {d["schema"] for d in data["datasets"]} == {"staging", "bronze", "silver", "gold"}
    assert {(e["source"]["schema"], e["source"]["column"], e["target"]["schema"], e["target"]["column"]) for e in data["edges"]} == {
        ("staging", "first_name", "bronze", "first_name"),
        ("staging", "last_name", "bronze", "last_name"),
        ("bronze", "first_name", "silver", "full_name"),
        ("bronze", "last_name", "silver", "full_name"),
        ("silver", "full_name", "gold", "patient_name"),
        ("staging", "patient_id", "bronze", "patient_id"),
        ("bronze", "patient_id", "silver", "patient_id"),
        ("silver", "patient_id", "gold", "patient_id"),
    }
    texts = {(c["schema"], c["column"]): c["text"] for c in data["columns"]}
    assert texts["gold", "patient_name"] == """gold.clients.patient_name
└── silver.clients.full_name
    ├── bronze.clients.first_name
    │   └── staging.clients.first_name
    └── bronze.clients.last_name
        └── staging.clients.last_name"""
    assert texts["bronze", "first_name"] == "bronze.clients.first_name\n└── staging.clients.first_name"
    assert texts["staging", "first_name"] == "staging.clients.first_name"
    assert "patient_id" not in texts["gold", "patient_name"]


def test_cll_missing_model_and_ambiguous_column(project):
    response = cll_request()
    assert response.status_code == 404
    assert "public.result" in response.json()["detail"]
    write(project, "models/result.sql", "SELECT id AS duplicate, name AS duplicate FROM staging.services")
    response = cll_request()
    assert response.status_code == 400
    assert "public.result.duplicate" in response.json()["detail"]
    assert "missing or ambiguous" in response.json()["detail"]


def test_cll_unknown_star_error(project):
    write(project, "models/result.sql", "SELECT * FROM external.clients")
    response = cll_request()
    assert response.status_code == 400
    assert "public.result" in response.json()["detail"]
    assert "metadata" in response.json()["detail"]


def test_cll_catalog_dependency_and_exact_selection(project):
    write(project, "models/result.sql", "SELECT patient_name FROM warehouse.raw.services")
    data = cll_request().json()
    assert data["edges"] == [{
        "source": col("raw", "services", catalog="warehouse"),
        "target": col("public", "result"),
    }]
    assert {"catalog": "warehouse", "schema": "raw", "model": "services", "columns": ["patient_name"]} in data["datasets"]
    assert cll_request(catalog="warehouse").status_code == 404


def test_cll_catalog_model_identity(project, monkeypatch):
    from rollout.models import Model, ModelId

    selected = Model(ModelId("gold", "a.b", "warehouse"), Path("unused.sql"),
                     "SELECT patient_name FROM upstream.raw.services")
    monkeypatch.setattr("tools.viewer.app.discover_models", lambda _: [selected])
    data = cll_request(catalog="warehouse", schema="gold", model="a.b").json()
    assert data["root"] == {"catalog": "warehouse", "schema": "gold", "model": "a.b"}
    assert data["edges"][0]["source"] == col("raw", "services", catalog="upstream")


def test_cll_catalog_is_optional_and_column_is_not_required(project):
    write(project, "models/gold/clients.sql", "SELECT patient_name FROM staging.clients")
    response = TestClient(app).get("/api/cll", params={"schema": "gold", "model": "clients"})
    assert response.status_code == 200
    assert response.json()["root"]["catalog"] == ""


@pytest.mark.parametrize("missing", ["schema", "model"])
def test_cll_requires_model_identity(project, missing):
    params = dict(schema="public", model="result")
    del params[missing]
    assert TestClient(app).get("/api/cll", params=params).status_code == 422


def test_cll_model_calendar_generated_from_known_upstream_column(project):
    write(project, "models/bronze/diagnoses.sql", "SELECT created_time FROM staging.diagnoses")
    write(project, "models/gold/calendar.sql", """
        WITH periods AS (
            SELECT period::DATE AS period FROM generate_series(
                (SELECT date_trunc('month', min(created_time))::DATE FROM bronze.diagnoses),
                current_date, INTERVAL 1 MONTH
            ) AS calendar(period)
        )
        SELECT month(period) AS month, year(period) AS year FROM periods
    """)
    response = cll_request(schema="gold", model="calendar")
    assert response.status_code == 200
    data = response.json()
    assert next(d for d in data["datasets"] if d["schema"] == "gold")["columns"] == ["month", "year"]
    assert len(data["datasets"]) == 3
    assert len(data["edges"]) == 3
    month = next(c for c in data["columns"] if c["schema"] == "gold" and c["column"] == "month")
    assert month["text"] == """gold.calendar.month
└── bronze.diagnoses.created_time
    └── staging.diagnoses.created_time"""
