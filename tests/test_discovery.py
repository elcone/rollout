import pytest

from rollout.discovery import discover_models
from rollout.models import ModelId


def test_layout_order_and_original_sql(write_models):
    sql = "-- numbers\nSELECT 1 AS number;\n"
    root = write_models(
        {
            "staging/customers.sql": "SELECT 2 AS id",
            "report.sql": sql,
            "ignored.py": "not SQL",
        }
    )
    models = discover_models(root)
    assert [model.id for model in models] == [
        ModelId("public", "report"),
        ModelId("staging", "customers"),
    ]
    assert models[0].path == root / "report.sql"
    assert models[0].sql == sql
    assert discover_models(root) == models


@pytest.mark.parametrize("files", [{}, {"public/report.sql": "SELECT 1"}])
def test_explicit_public_directory_is_rejected(write_models, files):
    root = write_models(files)
    (root / "public").mkdir(exist_ok=True)
    with pytest.raises(ValueError, match="Explicit public schema"):
        discover_models(root)


def test_nested_models_are_logged_and_skipped(write_models, caplog):
    root = write_models(
        {
            "staging/nested/report.sql": "SELECT FROM",
            "staging/nested/deeper/report.sql": "SELECT 1",
            "report.sql": "SELECT 1",
            "silver/report.sql": "SELECT * FROM report",
        }
    )
    assert {str(model.id) for model in discover_models(root)} == {
        "public.report",
        "silver.report",
    }
    errors = [
        record.getMessage() for record in caplog.records if record.levelname == "ERROR"
    ]
    assert len(errors) == 2
    assert all("Ignoring nested model path" in message for message in errors)


def test_missing_directory(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        discover_models(tmp_path / "missing")


def test_invalid_utf8(write_models):
    root = write_models({})
    (root / "numbers.sql").write_bytes(b"SELECT '\xff'")
    with pytest.raises(UnicodeDecodeError):
        discover_models(root)


def test_no_sql_models(write_models):
    root = write_models({"notes.txt": "notes"})
    (root / "directory.sql").mkdir()
    assert discover_models(root) == []
