import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import rollout


def test_reports_load_templates_from_packaged_resources(tmp_path):
    package_dir = Path(rollout.__file__).parent
    archive = tmp_path / "rollout.zip"
    with ZipFile(archive, "w") as package:
        for path in package_dir.rglob("*"):
            if path.suffix in {".py", ".j2"}:
                package.write(path, path.relative_to(package_dir.parent))
    script = """
import sys
sys.path.insert(0, sys.argv[1])
from rollout.dag import build_dag
from rollout.renderer import render_markdown, render_execution_markdown
print(render_markdown(build_dag([])))
print(render_execution_markdown(build_dag([]), {}))
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
