import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import rollout


def test_reports_and_sql_load_templates_from_packaged_resources(tmp_path):
    package_dir = Path(rollout.__file__).parent
    archive = tmp_path / "rollout.zip"
    with ZipFile(archive, "w") as package:
        for path in package_dir.rglob("*"):
            if path.suffix in {".py", ".j2", ".sql"}:
                package.write(path, path.relative_to(package_dir.parent))
    script = """
import sys
sys.path.insert(0, sys.argv[1])
from rollout.dag import build_dag
from rollout.renderer import render_markdown, render_execution_markdown
from pathlib import Path
from rollout.config import ModelConfig
from rollout.execution import build_model_sql, target_table_exists
from rollout.models import Model, ModelId
from rollout.quality import build_row_count_sql, build_nullness_sql, build_uniqueness_sql
identity = ModelId("main", "numbers")
model = Model(identity, Path("numbers.sql"), "SELECT 1 AS number")
assert build_model_sql(model) == 'CREATE OR REPLACE TABLE "main"."numbers" AS\\nSELECT 1 AS number'
incremental = Model(identity, model.path, model.sql, config=ModelConfig(
    load_strategy="DELETE_INSERT", delete_insert={"where": "target.number = 1"}
))
assert build_model_sql(incremental).startswith('CREATE TABLE "main"."numbers" AS')
assert build_model_sql(incremental, target_exists=True).startswith('DELETE FROM "main"."numbers" AS target')
append = Model(identity, model.path, model.sql, config=ModelConfig(load_strategy="APPEND"))
assert build_model_sql(append).startswith('CREATE TABLE "main"."numbers" AS')
assert build_model_sql(append, target_exists=True).startswith('INSERT INTO "main"."numbers"')
import duckdb
with duckdb.connect() as connection:
    assert not target_table_exists(connection, model)
    connection.execute(build_model_sql(model))
    assert target_table_exists(connection, model)
    assert connection.execute(build_row_count_sql(identity)).fetchone() == (1,)
    assert connection.execute(build_nullness_sql(identity, "number")).fetchone() == (0,)
    assert connection.execute(build_uniqueness_sql(identity, ("number",))).fetchone() == (0,)
print(render_markdown(build_dag([])))
from datetime import UTC, datetime
print(render_execution_markdown(
    build_dag([]), {},
    started_at=datetime(2026, 10, 11, 23, 13, 24, tzinfo=UTC),
    finished_at=datetime(2026, 10, 11, 23, 13, 29, tzinfo=UTC),
    total_duration=5.42,
))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(archive)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "# Rollout DAG" in result.stdout
    assert "No models." in result.stdout
    assert "# Rollout execution" in result.stdout
    assert "## Root failures\n\nNone" in result.stdout
    assert result.stdout.count("```mermaid\nflowchart LR") == 2
