from pathlib import Path

import pytest

from rollout.config import SeederConfig
from rollout.models import Model, ModelId, Seeder
from rollout.parser import parse_models
from tools.cll.lineage import (
    ColumnEdge,
    ColumnLineage,
    ColumnRef,
    render_text,
    resolve_column_lineage,
    resolve_model_lineage,
)


def project(queries):
    return parse_models(
        [
            Model(ModelId(*name.split(".")), Path(f"{name}.sql"), sql)
            for name, sql in queries.items()
        ]
    )


def ref(name):
    schema, model, column = name.split(".")
    return ColumnRef(ModelId(schema, model), column)


def edge(source, target="public.result.name"):
    return ColumnEdge(ref(source), ref(target))


@pytest.mark.parametrize(
    ("sql", "column", "sources"),
    [
        (
            "SELECT patient_id, patient_name FROM staging.services",
            "patient_name",
            ["staging.services.patient_name"],
        ),
        (
            "SELECT patient_name AS name FROM staging.services",
            "name",
            ["staging.services.patient_name"],
        ),
        (
            "SELECT concat(first_name, ' ', last_name) AS full_name FROM staging.clients",
            "full_name",
            ["staging.clients.first_name", "staging.clients.last_name"],
        ),
        (
            "WITH clients AS (SELECT patient_id, first_name FROM staging.clients) SELECT patient_id, first_name FROM clients",
            "first_name",
            ["staging.clients.first_name"],
        ),
        (
            "SELECT c.first_name, a.appointment_date FROM staging.clients c JOIN staging.appointments a ON c.patient_id = a.patient_id",
            "first_name",
            ["staging.clients.first_name"],
        ),
        (
            "SELECT c.first_name, a.appointment_date FROM staging.clients c JOIN staging.appointments a ON c.patient_id = a.patient_id",
            "appointment_date",
            ["staging.appointments.appointment_date"],
        ),
        (
            "SELECT patient_name FROM external_system.patients",
            "patient_name",
            ["external_system.patients.patient_name"],
        ),
        ("SELECT 1 AS version FROM staging.clients", "version", []),
    ],
)
def test_direct_lineage(sql, column, sources):
    models = project({"public.result": sql})
    result = resolve_column_lineage(models[0], column, models)
    assert result.root == ref(f"public.result.{column}")
    assert set(result.edges) == {
        edge(source, f"public.result.{column}") for source in sources
    }


def recursive_project():
    return project(
        {
            "bronze.clients": "SELECT first_name, last_name FROM staging.clients",
            "silver.clients": "SELECT concat(first_name, ' ', last_name) AS full_name FROM bronze.clients",
            "gold.clients": "SELECT full_name AS patient_name FROM silver.clients",
        }
    )


def test_complete_recursive_graph_and_determinism():
    models = recursive_project()
    result = resolve_column_lineage(models[-1], "patient_name", models)
    assert set(result.edges) == {
        edge("silver.clients.full_name", "gold.clients.patient_name"),
        edge("bronze.clients.first_name", "silver.clients.full_name"),
        edge("bronze.clients.last_name", "silver.clients.full_name"),
        edge("staging.clients.first_name", "bronze.clients.first_name"),
        edge("staging.clients.last_name", "bronze.clients.last_name"),
    }
    expected = """gold.clients.patient_name
└── silver.clients.full_name
    ├── bronze.clients.first_name
    │   └── staging.clients.first_name
    └── bronze.clients.last_name
        └── staging.clients.last_name"""
    assert render_text(result) == expected
    for _ in range(3):
        repeated = resolve_column_lineage(
            models[-1], "patient_name", list(reversed(models))
        )
        assert repeated == result
        assert render_text(repeated) == expected


@pytest.mark.parametrize(
    ("column", "source"),
    [("full_name", "clients"), ("last_appointment", "appointments")],
)
def test_only_selected_model_branch(column, source):
    models = project(
        {
            "silver.clients": "SELECT patient_id, name AS full_name FROM staging.clients",
            "silver.appointments": "SELECT patient_id, date AS last_appointment FROM staging.appointments",
            "gold.result": "SELECT c.full_name, a.last_appointment FROM silver.clients c JOIN silver.appointments a USING (patient_id)",
        }
    )
    result = resolve_column_lineage(models[-1], column, models)
    assert len(result.edges) == 2
    assert all(item.source.model.name == source for item in result.edges)


def test_seeder_boundary():
    seeder = Seeder(Path("seed.py"), SeederConfig(outputs={"clients": {}}))
    models = project({"bronze.clients": "SELECT first_name FROM staging.clients"})
    result = resolve_column_lineage(models[0], "first_name", models)
    assert result.edges == (
        edge("staging.clients.first_name", "bronze.clients.first_name"),
    )
    assert result.edges[0].source.model in seeder.output_ids


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM staging.customers c JOIN staging.orders o ON c.id = o.id",
        "SELECT missing AS name FROM bronze.clients",
    ],
)
def test_ambiguous_or_invalid_columns(sql):
    models = project(
        {"bronze.clients": "SELECT id FROM staging.clients", "public.result": sql}
    )
    with pytest.raises(ValueError, match="public.result.*(id|name)"):
        resolve_column_lineage(
            models[-1], "id" if "SELECT id" in sql else "name", models
        )


def test_unknown_output():
    models = project({"public.result": "SELECT id FROM staging.clients"})
    with pytest.raises(ValueError, match="public.result.does_not_exist"):
        resolve_column_lineage(models[0], "does_not_exist", models)


def test_star_with_known_schema():
    models = project(
        {
            "bronze.clients": "SELECT first_name FROM staging.clients",
            "public.result": "SELECT * FROM bronze.clients",
        }
    )
    result = resolve_column_lineage(models[-1], "first_name", models)
    assert len(result.edges) == 2


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM external.clients",
        "WITH c AS (SELECT * FROM external.clients) SELECT * FROM c",
    ],
)
def test_unknown_star_does_not_invent_columns(sql):
    models = project({"public.result": sql})
    with pytest.raises(ValueError, match="public.result.first_name"):
        resolve_column_lineage(models[0], "first_name", models)


def test_cycle_is_rejected():
    models = project(
        {"public.a": "SELECT id FROM public.b", "public.b": "SELECT id FROM public.a"}
    )
    with pytest.raises(ValueError, match="cycle"):
        resolve_column_lineage(models[0], "id", models)


def test_catalog_identity_and_default_schema():
    models = parse_models(
        [
            Model(
                ModelId("bronze", "clients", "lake"),
                Path("bronze.sql"),
                "SELECT first_name FROM raw.staging.clients",
            ),
            Model(
                ModelId("public", "result"),
                Path("result.sql"),
                "SELECT * FROM lake.bronze.clients",
            ),
        ]
    )
    result = resolve_column_lineage(models[-1], "first_name", models)
    assert set(result.edges) == {
        ColumnEdge(
            ColumnRef(ModelId("staging", "clients", "raw"), "first_name"),
            ColumnRef(models[0].id, "first_name"),
        ),
        ColumnEdge(
            ColumnRef(models[0].id, "first_name"), ref("public.result.first_name")
        ),
    }


def test_cte_star_without_metadata_cannot_prove_ownership():
    models = project(
        {
            "public.result": "WITH c AS (SELECT * FROM external.clients) SELECT first_name FROM c"
        }
    )
    with pytest.raises(ValueError, match="metadata"):
        resolve_column_lineage(models[0], "first_name", models)


@pytest.mark.parametrize(
    "on, where",
    [
        ("status IN ('ATTENDED', 'MISSED')", ""),
        ("status", "WHERE status IN ('ATTENDED', 'MISSED') AND minutes >= 0"),
    ],
)
def test_duckdb_pivot_aggregate_and_group_columns(on, where):
    models = project(
        {
            "bronze.events": "SELECT provider, status, minutes FROM staging.events",
            "gold.result": f"""
            WITH pivoted AS (
                PIVOT (SELECT provider, status, minutes FROM bronze.events {where})
                ON {on} USING coalesce(sum(minutes), 0) GROUP BY provider
            ), formatted AS (
                SELECT provider, ATTENDED AS attended_minutes,
                       attended_minutes * INTERVAL '1 minute' AS attended_interval
                FROM pivoted
            )
            SELECT provider, attended_minutes, attended_interval FROM formatted
        """,
        }
    )
    original = models[-1].sql
    for column in ("attended_minutes", "attended_interval"):
        result = resolve_column_lineage(models[-1], column, models)
        assert set(result.edges) == {
            edge("bronze.events.minutes", f"gold.result.{column}"),
            edge("bronze.events.status", f"gold.result.{column}"),
            edge("staging.events.minutes", "bronze.events.minutes"),
            edge("staging.events.status", "bronze.events.status"),
        }
        assert "pivoted" not in render_text(result)
    grouped = resolve_column_lineage(models[-1], "provider", models)
    assert set(grouped.edges) == {
        edge("bronze.events.provider", "gold.result.provider"),
        edge("staging.events.provider", "bronze.events.provider"),
    }
    assert models[-1].sql == original


@pytest.mark.parametrize(
    "where",
    [
        "",
        "WHERE status IN ('ATTENDED') OR minutes > 0",
        "WHERE NOT status IN ('ATTENDED')",
        "WHERE status IN (SELECT status FROM staging.allowed)",
        "WHERE other.status IN ('ATTENDED')",
    ],
)
def test_dynamic_pivot_does_not_guess_categories(where):
    models = project(
        {
            "gold.result": f"""
        WITH p AS (
            PIVOT (SELECT provider, status, minutes FROM staging.events {where})
            ON status USING sum(minutes) GROUP BY provider
        )
        SELECT ATTENDED AS attended FROM p
    """
        }
    )
    with pytest.raises(ValueError, match="gold.result.attended.*categories"):
        resolve_column_lineage(models[0], "attended", models)


def test_pivot_aliased_aggregate_and_qualified_category_filter():
    models = project(
        {
            "gold.result": """
        WITH p AS (
            PIVOT (
                SELECT e.provider, e.status AS category, e.minutes
                FROM staging.events e WHERE e.status IN ('ATTENDED')
            )
            ON category USING sum(minutes) AS total GROUP BY provider
        )
        SELECT ATTENDED_total AS minutes FROM p
    """
        }
    )
    result = resolve_column_lineage(models[0], "minutes", models)
    assert set(result.edges) == {
        edge("staging.events.minutes", "gold.result.minutes"),
        edge("staging.events.status", "gold.result.minutes"),
    }


@pytest.mark.parametrize(
    "pivot",
    [
        "PIVOT staging.events ON status, category USING sum(minutes) GROUP BY provider",
        "PIVOT staging.events ON status IN ('ATTENDED') USING sum(minutes), count(*) GROUP BY provider",
        "PIVOT staging.events ON status IN ('ATTENDED') USING sum(minutes)",
    ],
)
def test_unsupported_duckdb_pivot_forms_fail_explicitly(pivot):
    models = project({"gold.result": f"WITH p AS ({pivot}) SELECT ATTENDED FROM p"})
    with pytest.raises(ValueError, match="DuckDB PIVOT lineage requires"):
        resolve_column_lineage(models[0], "attended", models)


@pytest.mark.parametrize("column", ["service_code", "minutes_per_unit"])
def test_values_star_uses_alias_columns_without_upstream_dependencies(column):
    models = project(
        {
            "bronze.codes": """
        SELECT * FROM (VALUES ('H2019', 15), ('H2020', NULL))
        AS codes(service_code, minutes_per_unit)
    """
        }
    )
    result = resolve_column_lineage(models[0], column, models)
    assert result.root == ref(f"bronze.codes.{column}")
    assert result.edges == ()
    assert render_text(result) == f"bronze.codes.{column}"


def test_recursive_star_stops_at_values_model():
    models = project(
        {
            "bronze.codes": "SELECT * FROM (VALUES ('H2019', 15)) AS codes(service_code, minutes_per_unit)",
            "silver.codes": "SELECT * FROM bronze.codes",
        }
    )
    result = resolve_column_lineage(models[-1], "service_code", models)
    assert result.edges == (
        edge("bronze.codes.service_code", "silver.codes.service_code"),
    )


def test_values_unknown_output_is_an_error():
    models = project(
        {"bronze.codes": "SELECT * FROM (VALUES ('H2019')) AS codes(service_code)"}
    )
    with pytest.raises(ValueError, match="bronze.codes.missing"):
        resolve_column_lineage(models[0], "missing", models)


def test_model_expands_values_alias_columns():
    models = project(
        {
            "bronze.codes": "SELECT * FROM (VALUES ('H2019', 15)) AS codes(service_code, minutes_per_unit)"
        }
    )
    result = resolve_model_lineage(models[0], models)
    assert result.root == models[0].id
    assert dataset_columns(result) == {
        "bronze.codes": ("minutes_per_unit", "service_code"),
    }
    assert result.edges == ()
    for column in ("service_code", "minutes_per_unit"):
        assert (
            render_text(ColumnLineage(ref(f"bronze.codes.{column}"), result.edges))
            == f"bronze.codes.{column}"
        )


def test_model_rejects_external_star_without_metadata():
    models = project({"bronze.codes": "SELECT * FROM external.codes"})
    with pytest.raises(ValueError) as exc:
        resolve_model_lineage(models[0], models)
    assert "bronze.codes" in str(exc.value)
    assert "without dataset metadata" in str(exc.value)


def dataset_columns(lineage):
    return {str(dataset.model): dataset.columns for dataset in lineage.datasets}


def test_model_all_root_columns_and_constant():
    models = project(
        {
            "public.result": "SELECT patient_id, patient_name, 1 AS version FROM staging.clients"
        }
    )
    result = resolve_model_lineage(models[0], models)
    assert result.root == models[0].id
    assert dataset_columns(result) == {
        "public.result": ("patient_id", "patient_name", "version"),
        "staging.clients": ("patient_id", "patient_name"),
    }
    assert set(result.edges) == {
        edge("staging.clients.patient_id", "public.result.patient_id"),
        edge("staging.clients.patient_name", "public.result.patient_name"),
    }


def test_model_recursive_union_and_determinism():
    models = recursive_project()
    result = resolve_model_lineage(models[-1], models)
    assert (
        result.edges == resolve_column_lineage(models[-1], "patient_name", models).edges
    )
    assert dataset_columns(result) == {
        "bronze.clients": ("first_name", "last_name"),
        "gold.clients": ("patient_name",),
        "silver.clients": ("full_name",),
        "staging.clients": ("first_name", "last_name"),
    }
    assert [str(d.model) for d in result.datasets] == sorted(dataset_columns(result))
    assert resolve_model_lineage(models[-1], list(reversed(models))) == result


def test_model_shared_upstream_columns_match_individual_lineage():
    models = project(
        {
            "bronze.clients": "SELECT first_name, last_name FROM staging.clients",
            "silver.clients": """
            WITH names AS (SELECT first_name, last_name FROM bronze.clients)
            SELECT concat(first_name, ' ', last_name) AS full_name, first_name
            FROM names
        """,
            "gold.result": """
            SELECT full_name AS display_name, upper(full_name) AS label, first_name
            FROM silver.clients
        """,
        }
    )
    root = models[-1]
    individual = [
        resolve_column_lineage(root, column, models)
        for column in ("display_name", "label", "first_name")
    ]
    result = resolve_model_lineage(root, models)
    assert set(result.edges) == {
        edge for lineage in individual for edge in lineage.edges
    }
    for lineage in individual:
        assert render_text(ColumnLineage(lineage.root, result.edges)) == render_text(
            lineage
        )
    assert resolve_model_lineage(root, models) == result


def test_model_independent_branches_and_known_internal_columns():
    models = project(
        {
            "silver.clients": "SELECT patient_id, name AS full_name, 1 AS version FROM staging.clients",
            "silver.appointments": "SELECT patient_id, date AS last_appointment FROM staging.appointments",
            "gold.result": "SELECT c.full_name AS patient_name, a.last_appointment FROM silver.clients c JOIN silver.appointments a USING (patient_id)",
            "public.unrelated": "SELECT id FROM elsewhere.unrelated",
        }
    )
    root = next(m for m in models if str(m.id) == "gold.result")
    result = resolve_model_lineage(root, models)
    columns = dataset_columns(result)
    assert columns["gold.result"] == ("last_appointment", "patient_name")
    assert columns["silver.clients"] == ("full_name", "patient_id", "version")
    assert columns["staging.clients"] == ("name",)
    assert columns["staging.appointments"] == ("date",)
    assert "public.unrelated" not in columns
    assert set(result.edges) == {
        edge("silver.clients.full_name", "gold.result.patient_name"),
        edge("silver.appointments.last_appointment", "gold.result.last_appointment"),
        edge("staging.clients.name", "silver.clients.full_name"),
        edge("staging.appointments.date", "silver.appointments.last_appointment"),
    }


def test_model_star_expands_known_outputs():
    models = project(
        {
            "bronze.clients": "SELECT patient_id, 1 AS version FROM staging.clients",
            "public.result": "SELECT * FROM bronze.clients",
        }
    )
    result = resolve_model_lineage(models[-1], models)
    assert dataset_columns(result)["public.result"] == ("patient_id", "version")
    assert (
        len(result.edges) == 3
    )  # Includes bronze's constant output feeding result.version.


def test_model_unknown_star_fails_without_inventing_columns():
    models = project({"public.result": "SELECT * FROM external.clients"})
    with pytest.raises(ValueError, match="public.result.*metadata"):
        resolve_model_lineage(models[0], models)


def test_model_broken_root_output_fails_entire_request():
    models = project(
        {
            "public.result": "SELECT id, first_name AS duplicate, last_name AS duplicate FROM staging.clients"
        }
    )
    with pytest.raises(ValueError, match="public.result.duplicate.*ambiguous"):
        resolve_model_lineage(models[0], models)


def test_model_constant_only_has_a_root_card():
    models = project({"public.result": "SELECT 1 AS version"})
    result = resolve_model_lineage(models[0], models)
    assert dataset_columns(result) == {"public.result": ("version",)}
    assert result.edges == ()


def test_model_upstream_unknown_star_keeps_only_edge_columns():
    models = project(
        {
            "bronze.clients": "SELECT *, first_name AS name FROM staging.clients",
            "public.result": "SELECT name FROM bronze.clients",
        }
    )
    result = resolve_model_lineage(models[-1], models)
    assert dataset_columns(result)["bronze.clients"] == ("name",)
    assert dataset_columns(result)["staging.clients"] == ("first_name",)


def test_model_error_identifies_root_output_and_upstream_reason():
    models = project(
        {
            "bronze.clients": "WITH c AS (SELECT * FROM external.clients) SELECT first_name FROM c",
            "gold.result": "SELECT first_name AS patient_name FROM bronze.clients",
        }
    )
    with pytest.raises(ValueError) as failure:
        resolve_model_lineage(models[-1], models)
    assert "gold.result.patient_name" in str(failure.value)
    assert "bronze.clients.first_name" in str(failure.value)
    assert "metadata" in str(failure.value)


def test_generate_series_calendar_follows_bound_subquery_through_ctes():
    models = project(
        {
            "bronze.diagnoses": "SELECT created_time, client_id FROM staging.diagnoses",
            "gold.result": """
            WITH cte_periods AS (
                SELECT period::DATE AS period, last_day(period)::DATE AS period_end
                FROM generate_series(
                    (SELECT date_trunc('month', min(created_time))::DATE FROM bronze.diagnoses),
                    date_trunc('month', current_date)::DATE,
                    INTERVAL 1 MONTH
                ) AS calendar(period)
            ), cte_diagnoses AS (
                SELECT p.period, p.period AS period_start, p.period_end,
                       year(p.period)::INTEGER AS year,
                       quarter(p.period)::INTEGER AS quarter,
                       month(p.period)::INTEGER AS month,
                       d.client_id, d.created_time
                FROM bronze.diagnoses d
                JOIN cte_periods p ON d.created_time < p.period_end + INTERVAL 1 DAY
            )
            SELECT period, period_start, period_end, year, quarter, month, client_id
            FROM cte_diagnoses
            QUALIFY row_number() OVER (PARTITION BY period, client_id ORDER BY created_time DESC) = 1
        """,
        }
    )
    original = models[-1].sql
    for column in ("period", "period_start", "period_end", "year", "quarter", "month"):
        result = resolve_column_lineage(models[-1], column, models)
        assert set(result.edges) == {
            edge("bronze.diagnoses.created_time", f"gold.result.{column}"),
            edge("staging.diagnoses.created_time", "bronze.diagnoses.created_time"),
        }
    assert set(resolve_column_lineage(models[-1], "client_id", models).edges) == {
        edge("bronze.diagnoses.client_id", "gold.result.client_id"),
        edge("staging.diagnoses.client_id", "bronze.diagnoses.client_id"),
    }
    complete = resolve_model_lineage(models[-1], models)
    assert dataset_columns(complete) == {
        "bronze.diagnoses": ("client_id", "created_time"),
        "gold.result": (
            "client_id",
            "month",
            "period",
            "period_end",
            "period_start",
            "quarter",
            "year",
        ),
        "staging.diagnoses": ("client_id", "created_time"),
    }
    assert len(complete.edges) == 9
    assert models[-1].sql == original


def test_generate_series_includes_start_end_and_step_dependencies():
    models = project(
        {
            "bronze.bounds": "SELECT start_value, end_value, step_value FROM staging.bounds",
            "public.result": """
            WITH bounds AS (SELECT * FROM bronze.bounds)
            SELECT n FROM generate_series(
                (SELECT min(start_value) FROM bounds),
                (SELECT max(end_value) FROM bounds),
                (SELECT max(step_value) FROM bounds)
            ) AS series(n)
        """,
        }
    )
    result = resolve_column_lineage(models[-1], "n", models)
    assert set(result.edges) == {
        item
        for column in ("start_value", "end_value", "step_value")
        for item in (
            edge(f"bronze.bounds.{column}", "public.result.n"),
            edge(f"staging.bounds.{column}", f"bronze.bounds.{column}"),
        )
    }


@pytest.mark.parametrize(
    "sql, column",
    [
        ("SELECT generate_series FROM generate_series(1, 3)", "generate_series"),
        ("SELECT s.generate_series FROM generate_series(1, 3) AS s", "generate_series"),
        ("SELECT * FROM generate_series(1, 3) AS calendar(period)", "period"),
        (
            'SELECT "Calendar"."period.value" FROM generate_series(1, 3) AS "Calendar"("period.value")',
            "period.value",
        ),
    ],
)
def test_constant_generate_series_has_known_output_without_fake_dataset(sql, column):
    models = project({"public.result": sql})
    assert resolve_column_lineage(models[0], column, models).edges == ()
    assert dataset_columns(resolve_model_lineage(models[0], models)) == {
        "public.result": (column,),
    }


@pytest.mark.parametrize(
    "suffix, message",
    [
        ("WITH ORDINALITY AS calendar(period, ordinality)", "ORDINALITY"),
        ("AS calendar(period, extra)", "single output column"),
    ],
)
def test_generate_series_does_not_silently_drop_extra_outputs(suffix, message):
    models = project({"public.result": f"SELECT * FROM generate_series(1, 3) {suffix}"})
    with pytest.raises(ValueError, match=message):
        resolve_model_lineage(models[0], models)


def test_unsupported_table_function_still_fails_without_metadata():
    models = project(
        {"public.result": "SELECT value FROM unknown_function(1) AS source(value)"}
    )
    with pytest.raises(ValueError, match="metadata"):
        resolve_column_lineage(models[0], "value", models)


def test_model_scalar_subquery_and_union_preserve_direct_ownership():
    models = project(
        {
            "public.result": """
            SELECT (SELECT max(value) FROM warehouse.raw.measurements) AS metric,
                   1 AS version
            UNION ALL
            SELECT other_value AS metric, 2 AS version FROM external.metrics
        """,
        }
    )
    model = models[0]
    original_sql = model.sql
    result = resolve_model_lineage(model, models)
    assert set(result.edges) == {
        ColumnEdge(
            ColumnRef(ModelId("raw", "measurements", "warehouse"), "value"),
            ref("public.result.metric"),
        ),
        edge("external.metrics.other_value", "public.result.metric"),
    }
    assert dataset_columns(result)["public.result"] == ("metric", "version")
    assert resolve_column_lineage(model, "metric", models).edges == result.edges
    assert resolve_column_lineage(model, "version", models).edges == ()
    assert resolve_model_lineage(model, models) == result
    assert model.sql == original_sql
