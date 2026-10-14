import hashlib
import sys

import duckdb
import pytest

from rollout.discovery import discover_seeders
from rollout.seeders import run_seeder


def assert_module_removed(seeder):
    path_hash = hashlib.sha256(str(seeder.path.resolve()).encode()).hexdigest()
    assert f"_rollout_seeder_{path_hash}" not in sys.modules


@pytest.fixture
def connection():
    with duckdb.connect() as connection:
        yield connection


@pytest.fixture
def staging_dir(tmp_path):
    path = tmp_path / "staging"
    path.mkdir()
    return path


@pytest.fixture
def write_seeder(tmp_path):
    def write(source, outputs=("orders",), name="source.py"):
        path = tmp_path / "seeders" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        path.with_suffix(".yaml").write_text(
            "outputs:\n" + "".join(f"  {name}: {{}}\n" for name in outputs),
            encoding="utf-8",
        )
        return next(
            seeder
            for seeder in discover_seeders(tmp_path / "seeders")
            if seeder.path == path
        )

    return write


@pytest.mark.parametrize("name", ["source.py", "nested/source.py"])
def test_loads_and_runs_without_materialization(
    write_seeder, connection, staging_dir, name
):
    seeder = write_seeder(
        "def run(connection, staging_dir):\n"
        "    return {'orders': connection.sql('SELECT 1 AS id')}\n",
        name=name,
    )
    original_sys_path = sys.path.copy()
    outputs = run_seeder(seeder, connection, staging_dir)
    assert_module_removed(seeder)
    assert list(outputs) == ["orders"]
    assert isinstance(outputs["orders"], duckdb.DuckDBPyRelation)
    assert outputs["orders"].fetchall() == [(1,)]
    assert connection.sql("SELECT * FROM information_schema.tables").fetchall() == []
    assert (
        connection.sql(
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'staging'"
        ).fetchall()
        == []
    )
    assert list(staging_dir.iterdir()) == []
    assert staging_dir.is_dir()
    assert sys.path == original_sys_path


def test_passes_exact_connection_and_path(write_seeder, connection, staging_dir):
    seeder = write_seeder(
        "def run(connection, staging_dir):\n"
        f"    assert id(connection) == {id(connection)}\n"
        f"    assert id(staging_dir) == {id(staging_dir)}\n"
        "    return {'orders': connection.sql('SELECT 1')}\n"
    )
    assert run_seeder(seeder, connection, staging_dir)["orders"].fetchall() == [(1,)]
    assert_module_removed(seeder)


@pytest.mark.parametrize(
    "source, cause_type, message",
    [
        ("def run(:", SyntaxError, "invalid syntax"),
        ("raise LookupError('import failed')", LookupError, "import failed"),
        ("value = 1", ValueError, "callable run"),
        ("run = 42", ValueError, "callable run"),
        (
            "def run(connection, staging_dir):\n    raise KeyError('run failed')",
            KeyError,
            "run failed",
        ),
        ("def run():\n    return {}", TypeError, "positional"),
    ],
)
def test_module_and_run_errors_include_path_and_cause(
    write_seeder, connection, staging_dir, source, cause_type, message
):
    seeder = write_seeder(source)
    with pytest.raises(RuntimeError, match="Seeder execution failed") as error:
        run_seeder(seeder, connection, staging_dir)
    assert str(seeder.path) in str(error.value)
    assert message in str(error.value)
    assert isinstance(error.value.__cause__, cause_type)
    assert_module_removed(seeder)


def test_same_filename_modules_are_independent(write_seeder, connection, staging_dir):
    first = write_seeder(
        "def run(connection, staging_dir):\n"
        "    return {'first': connection.sql(f\"SELECT 1, '{__name__}'\")}\n",
        outputs=("first",),
        name="source_a/load.py",
    )
    second = write_seeder(
        "def run(connection, staging_dir):\n"
        "    return {'second': connection.sql(f\"SELECT 2, '{__name__}'\")}\n",
        outputs=("second",),
        name="source_b/load.py",
    )
    first_row = run_seeder(first, connection, staging_dir)["first"].fetchone()
    second_row = run_seeder(second, connection, staging_dir)["second"].fetchone()
    assert first_row[0] == 1
    assert second_row[0] == 2
    assert first_row[1] != second_row[1]
    assert_module_removed(first)
    assert_module_removed(second)


@pytest.mark.parametrize(
    "expression",
    [
        "None",
        "[]",
        "()",
        "42",
        "{'orders': relation}.items()",
        "__import__('types').MappingProxyType({'orders': relation})",
        "type('OutputDict', (dict,), {})({'orders': relation})",
    ],
)
def test_requires_exact_dict(write_seeder, connection, staging_dir, expression):
    seeder = write_seeder(
        "def run(connection, staging_dir):\n"
        "    relation = connection.sql('SELECT 1')\n"
        f"    return {expression}\n"
    )
    with pytest.raises(RuntimeError, match="must return a Python dict"):
        run_seeder(seeder, connection, staging_dir)
    assert_module_removed(seeder)


@pytest.mark.parametrize(
    "returned, missing, unexpected",
    [
        ("{}", "['orders']", "[]"),
        ("{'orders': relation, 'extra': relation}", "[]", "['extra']"),
        ("{'extra': relation}", "['orders']", "['extra']"),
        ("{1: relation}", "['orders']", "[1]"),
    ],
)
def test_exact_output_names(
    write_seeder, connection, staging_dir, returned, missing, unexpected
):
    seeder = write_seeder(
        "def run(connection, staging_dir):\n"
        "    relation = connection.sql('SELECT 1')\n"
        f"    return {returned}\n"
    )
    with pytest.raises(RuntimeError) as error:
        run_seeder(seeder, connection, staging_dir)
    assert f"missing outputs {missing}" in str(error.value)
    assert f"unexpected outputs {unexpected}" in str(error.value)
    assert_module_removed(seeder)


@pytest.mark.parametrize(
    "value, type_name",
    [
        ("[]", "list"),
        ("()", "tuple"),
        ("{}", "dict"),
        ("object()", "object"),
        ("None", "NoneType"),
    ],
)
def test_requires_relation_for_every_output(
    write_seeder, connection, staging_dir, value, type_name
):
    seeder = write_seeder(
        "def run(connection, staging_dir):\n"
        "    return {'first': connection.sql('SELECT 1'), "
        f"'second': {value}}}\n",
        outputs=("first", "second"),
    )
    with pytest.raises(RuntimeError) as error:
        run_seeder(seeder, connection, staging_dir)
    assert "Output 'second' must be DuckDBPyRelation" in str(error.value)
    assert f"got {type_name}" in str(error.value)
    assert str(seeder.path) in str(error.value)
    assert_module_removed(seeder)


def test_multiple_outputs_follow_yaml_order(write_seeder, connection, staging_dir):
    seeder = write_seeder(
        "def run(connection, staging_dir):\n"
        "    return {'second': connection.sql('SELECT 2'), "
        "'first': connection.sql('SELECT 1')}\n",
        outputs=("first", "second"),
    )
    outputs = run_seeder(seeder, connection, staging_dir)
    assert_module_removed(seeder)
    assert list(outputs) == ["first", "second"]
    assert [relation.fetchall() for relation in outputs.values()] == [[(1,)], [(2,)]]


@pytest.mark.parametrize(
    "returned, error",
    [
        ("{'customers': relation}", None),
        ("relation", "must return a Python dict"),
        ("{'other': relation}", "missing outputs"),
        ("{'customers': []}", "must be DuckDBPyRelation"),
    ],
)
def test_inferred_output_keeps_run_contract(
    tmp_path, connection, staging_dir, returned, error
):
    path = tmp_path / "customers.py"
    path.write_text(
        "def run(connection, staging_dir):\n"
        "    relation = connection.sql('SELECT 1 AS id')\n"
        f"    return {returned}\n",
        encoding="utf-8",
    )
    seeder = discover_seeders(tmp_path)[0]
    if error:
        with pytest.raises(RuntimeError, match=error):
            run_seeder(seeder, connection, staging_dir)
    else:
        outputs = run_seeder(seeder, connection, staging_dir)
        assert type(outputs) is dict
        assert list(outputs) == ["customers"]
        assert outputs["customers"].fetchall() == [(1,)]
    assert_module_removed(seeder)
