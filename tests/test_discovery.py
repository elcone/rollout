import pytest

from rollout.discovery import discover_models
from rollout.models import ModelId


def test_layout_order_and_original_sql(write_models):
    sql = "-- numbers\nSELECT 1 AS number;\n"
    root = write_models(
        {
            "silver/customers.sql": "SELECT 2 AS id",
            "report.sql": sql,
            "ignored.py": "not SQL",
        }
    )
    models = discover_models(root)
    assert [model.id for model in models] == [
        ModelId("public", "report"),
        ModelId("silver", "customers"),
    ]
    assert models[0].path == root / "report.sql"
    assert models[0].sql == sql
    assert discover_models(root) == models


@pytest.mark.parametrize("schema", ["public", "staging"])
@pytest.mark.parametrize("populated", [False, True])
def test_reserved_schema_directories_are_rejected(write_models, schema, populated):
    files = {f"{schema}/report.sql": "SELECT 1"} if populated else {}
    root = write_models(files)
    (root / schema).mkdir(exist_ok=True)
    with pytest.raises(ValueError) as error:
        discover_models(root)
    assert str(root / schema) in str(error.value)


@pytest.mark.parametrize(
    "name", ["bronze/nested/report.sql", "bronze/nested/deeper/report.sql"]
)
def test_nested_models_are_rejected(write_models, name):
    root = write_models({name: "SELECT 1", "report.sql": "SELECT 1"})
    with pytest.raises(ValueError, match="expected model.sql or schema/model.sql") as error:
        discover_models(root)
    assert str(root / name) in str(error.value)


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


@pytest.mark.parametrize("contents", [None, "", "# configuration\n", "{}\n"])
def test_missing_or_empty_yaml_uses_defaults(write_models, contents):
    from rollout.config import ModelConfig

    files = {"numbers.sql": "SELECT 1"}
    if contents is not None:
        files["numbers.yaml"] = contents
    models = discover_models(write_models(files))
    assert models[0].config == ModelConfig()


def test_sibling_yaml_loads_quality_and_preserves_model(write_models):
    from rollout.config import CheckSeverity, LoadStrategy
    from rollout.parser import parse_models

    sql = "SELECT 1 AS id"
    root = write_models(
        {
            "bronze/numbers.sql": sql,
            "bronze/numbers.yaml": """load_strategy: FULL
quality:
  - type: row_count
    min: 1
    severity: ERROR
  - type: nullness
    column: id
    max: 0
    severity: WARNING
  - type: uniqueness
    columns: [id]
    max: 0
    severity: ERROR
""",
            "numbers.sql": "SELECT 2 AS id",
            "unrelated.yaml": "invalid: [",
        }
    )
    models = discover_models(root)
    configured = models[0]
    assert configured.id == ModelId("bronze", "numbers")
    assert configured.path == root / "bronze/numbers.sql"
    assert configured.sql == sql
    assert configured.dependencies == frozenset()
    assert configured.config.load_strategy is LoadStrategy.FULL
    checks = configured.config.quality
    assert [check.type for check in checks] == ["row_count", "nullness", "uniqueness"]
    assert checks[0].min == 1
    assert checks[1].column == "id"
    assert checks[1].severity is CheckSeverity.WARNING
    assert checks[2].columns == ("id",)
    assert models[1].config.quality == ()
    assert parse_models(models)[0].config == configured.config


def test_yml_is_not_loaded(write_models):
    from rollout.config import ModelConfig

    root = write_models({"numbers.sql": "SELECT 1", "numbers.yml": "invalid: ["})
    assert discover_models(root)[0].config == ModelConfig()


@pytest.mark.parametrize(
    "contents, message",
    [
        ("quality: [", "Invalid model configuration"),
        ("- item", "mapping/object"),
        ("[]", "mapping/object"),
        ("false", "mapping/object"),
        ("0", "mapping/object"),
        ('""', "mapping/object"),
        ("plain text", "mapping/object"),
        ("1: value", "keywords must be strings"),
        ("unknown: value", "Extra inputs are not permitted"),
        ("load_strategy: MERGE", "FULL"),
        ("quality:\n  - type: row_count\n    severity: ERROR", "min"),
        ("quality:\n  - type: custom_sql\n    severity: ERROR", "name"),
        ("!!python/object:builtins.object {}", "Invalid model configuration"),
    ],
)
def test_invalid_yaml_configuration_identifies_file(write_models, contents, message):
    root = write_models(
        {"bronze/numbers.sql": "SELECT 1", "bronze/numbers.yaml": contents}
    )
    with pytest.raises(ValueError) as error:
        discover_models(root)
    assert str(root / "bronze/numbers.yaml") in str(error.value)
    assert message in str(error.value)
    assert error.value.__cause__ is not None


def test_yaml_read_error_identifies_file(write_models):
    root = write_models({"numbers.sql": "SELECT 1"})
    (root / "numbers.yaml").mkdir()
    with pytest.raises(ValueError) as error:
        discover_models(root)
    assert str(root / "numbers.yaml") in str(error.value)


def test_loaded_quality_runs_during_execution(write_models, engine_config):
    from rollout.dag import build_dag
    from rollout.execution import ExecutionStatus, execute_models
    from rollout.parser import parse_models
    from rollout.quality import CheckStatus

    root = write_models(
        {
            "numbers.sql": "SELECT 1 AS id WHERE FALSE",
            "numbers.yaml": "quality:\n  - type: row_count\n    min: 1\n    severity: ERROR\n",
            "report.sql": "SELECT * FROM numbers",
        }
    )
    dag = build_dag(parse_models(discover_models(root)))
    results = execute_models(dag, engine_config, run_id="test-runZ")
    parent = results[ModelId("public", "numbers")]
    assert parent.status is ExecutionStatus.FAILED
    assert parent.quality_results[0].status is CheckStatus.FAIL
    assert results[ModelId("public", "report")].status is ExecutionStatus.SKIPPED
