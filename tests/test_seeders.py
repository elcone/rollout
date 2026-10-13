from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from rollout.config import SeederConfig, SeederOutputConfig
from rollout.discovery import discover_models, discover_seeders
from rollout.models import ModelId, Seeder
from rollout.parser import parse_models


@pytest.fixture
def write_seeders(tmp_path):
    def write(files):
        root = tmp_path / "seeders"
        root.mkdir(exist_ok=True)
        for name, contents in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")
        return root

    return write


def test_discovery_order_identity_and_no_python_execution(write_seeders):
    root = write_seeders(
        {
            "z.py": "raise RuntimeError('must not execute')",
            "z.yaml": "outputs:\n  services: {}",
            "nested/a.py": "this is not valid Python!",
            "nested/a.yaml": "outputs:\n  orders: {}\n  appointments: {}",
            "orphan.yaml": "invalid: [",
            "ignored.yml": "invalid: [",
            "notes.txt": "notes",
        }
    )
    (root / "directory.py").mkdir()
    seeders = discover_seeders(root)
    assert all(isinstance(seeder, Seeder) for seeder in seeders)
    assert [seeder.path for seeder in seeders] == [root / "nested/a.py", root / "z.py"]
    assert seeders[0].output_ids == (
        ModelId("staging", "orders"),
        ModelId("staging", "appointments"),
    )
    assert str(seeders[0].output_ids[0]) == "staging.orders"
    assert list(seeders[0].config.outputs) == ["orders", "appointments"]
    assert seeders == discover_seeders(root)
    with pytest.raises(FrozenInstanceError):
        seeders[0].path = root


@pytest.mark.parametrize("buddy", [None, "alleva.yml"])
def test_required_yaml_and_yml_is_not_a_buddy(write_seeders, buddy):
    files = {"alleva.py": ""}
    if buddy:
        files[buddy] = "outputs:\n  orders: {}"
    root = write_seeders(files)
    with pytest.raises(ValueError) as error:
        discover_seeders(root)
    assert str(root / "alleva.py") in str(error.value)
    assert str(root / "alleva.yaml") in str(error.value)


def test_missing_directory(tmp_path):
    with pytest.raises(ValueError, match="Seeders directory does not exist"):
        discover_seeders(tmp_path / "missing")


def test_no_seeders(write_seeders):
    assert discover_seeders(write_seeders({"orphan.yaml": "invalid: ["})) == []


@pytest.mark.parametrize("outputs", [{"orders": {}}, {"orders": {}, "services": {}}])
def test_valid_output_configuration(outputs):
    config = SeederConfig(outputs=outputs)
    assert list(config.outputs) == list(outputs)
    assert all(output.quality == () for output in config.outputs.values())


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"outputs": {}},
        {"outputs": {"": {}}},
        {"outputs": {" ": {}}},
        {"outputs": {"orders": {}}, "unknown": True},
        {"outputs": {"orders": {}}, "depends_on": []},
        {"outputs": {"orders": {}}, "schema": "other"},
        *(
            {"outputs": {"orders": {field: value}}}
            for field, value in [
                ("unknown", True),
                ("load_strategy", "FULL"),
                ("depends_on", []),
                ("delete_insert", {"where": "TRUE"}),
                ("schema", "other"),
            ]
        ),
    ],
)
def test_invalid_configuration(values):
    with pytest.raises(ValidationError):
        SeederConfig.model_validate(values)


def test_quality_configuration_is_reused_and_frozen(write_seeders):
    root = write_seeders(
        {
            "alleva.py": "",
            "alleva.yaml": """outputs:
  orders:
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
      - type: custom_sql
        name: positive_count
        sql: SELECT count(*) FROM staging.orders
        expectations:
          - operator: gt
            value: 0
        severity: ERROR
""",
        }
    )
    config = discover_seeders(root)[0].config
    output = config.outputs["orders"]
    assert isinstance(output, SeederOutputConfig)
    assert [check.type for check in output.quality] == [
        "row_count",
        "nullness",
        "uniqueness",
        "custom_sql",
    ]
    assert output.quality[2].columns == ("id",)
    assert output.quality[3].expectations[0].value == 0
    with pytest.raises(TypeError):
        config.outputs["services"] = SeederOutputConfig()
    with pytest.raises(ValidationError):
        config.outputs = {}
    with pytest.raises(ValidationError):
        output.quality = ()
    with pytest.raises(ValidationError):
        output.quality[0].min = 2


@pytest.mark.parametrize(
    "contents",
    [
        "outputs: [",
        "",
        "[]",
        "outputs: {}",
        "unknown: true",
        "outputs:\n  orders:\n    quality:\n      - type: row_count\n        severity: ERROR",
        "!!python/object:builtins.object {}",
    ],
)
def test_configuration_errors_include_yaml_and_details(write_seeders, contents):
    root = write_seeders({"alleva.py": "", "alleva.yaml": contents})
    with pytest.raises(ValueError) as error:
        discover_seeders(root)
    assert f"Invalid seeder configuration in {root / 'alleva.yaml'}:" in str(
        error.value
    )
    assert error.value.__cause__ is not None
    assert str(error.value.__cause__) in str(error.value)


def test_yaml_read_error_includes_path(write_seeders):
    root = write_seeders({"alleva.py": ""})
    (root / "alleva.yaml").mkdir()
    with pytest.raises(ValueError, match="Invalid seeder configuration") as error:
        discover_seeders(root)
    assert str(root / "alleva.yaml") in str(error.value)


def test_duplicate_outputs_fail_in_deterministic_order(write_seeders):
    root = write_seeders(
        {
            "z.py": "",
            "z.yaml": "outputs:\n  orders: {}",
            "a.py": "",
            "a.yaml": "outputs:\n  orders: {}",
        }
    )
    with pytest.raises(ValueError) as error:
        discover_seeders(root)
    assert str(error.value) == (
        f"Duplicate seeder output staging.orders: {root / 'a.yaml'} and {root / 'z.yaml'}"
    )


def test_model_cannot_own_staging_output(write_models):
    root = write_models({"staging/orders.sql": "SELECT 1"})
    with pytest.raises(
        ValueError, match="Schema 'staging' is reserved for seeder outputs"
    ):
        discover_models(root)


def test_model_can_reference_staging_output(write_models):
    root = write_models({"orders.sql": "SELECT * FROM staging.orders"})
    model = parse_models(discover_models(root))[0]
    assert model.dependencies == frozenset({ModelId("staging", "orders")})
