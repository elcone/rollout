import pytest

from rollout.models import ModelId
from rollout.parser import extract_dependencies


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT * FROM customers", {ModelId("public", "customers")}),
        ("SELECT * FROM raw.orders", {ModelId("raw", "orders")}),
        (
            'SELECT * FROM warehouse.raw.orders JOIN "Some schema"."Some table" ON true',
            {
                ModelId("raw", "orders", "warehouse"),
                ModelId("Some schema", "Some table"),
            },
        ),
        (
            "SELECT * FROM raw.orders, raw.items",
            {ModelId("raw", "orders"), ModelId("raw", "items")},
        ),
        (
            "SELECT * FROM raw.orders JOIN raw.items USING (id)",
            {ModelId("raw", "orders"), ModelId("raw", "items")},
        ),
        (
            "WITH recent_orders AS (SELECT * FROM raw.orders) SELECT * FROM recent_orders",
            {ModelId("raw", "orders")},
        ),
        (
            "SELECT * FROM (SELECT * FROM raw.orders) AS recent_orders",
            {ModelId("raw", "orders")},
        ),
        (
            "WITH orders AS (SELECT * FROM raw.orders) SELECT * FROM orders JOIN silver.orders USING (id)",
            {ModelId("raw", "orders"), ModelId("silver", "orders")},
        ),
        (
            "SELECT * FROM orders WHERE EXISTS (WITH orders AS (SELECT * FROM raw.items) SELECT * FROM orders)",
            {ModelId("public", "orders"), ModelId("raw", "items")},
        ),
        (
            "SELECT * FROM raw.orders a JOIN raw.orders b USING (id)",
            {ModelId("raw", "orders")},
        ),
        ("SELECT * FROM raw.orders; -- trailing comment", {ModelId("raw", "orders")}),
        ("SELECT * FROM read_csv('orders.csv')", set()),
        ("VALUES (1)", set()),
        ("PIVOT raw.cities ON year USING sum(population)", {ModelId("raw", "cities")}),
        (
            "SELECT * FROM raw.cities PIVOT (sum(population) FOR year IN (2020))",
            {ModelId("raw", "cities")},
        ),
        (
            "WITH cities AS (SELECT * FROM raw.cities) PIVOT cities ON year USING sum(population)",
            {ModelId("raw", "cities")},
        ),
        (
            "WITH pivoted AS (PIVOT (SELECT * FROM raw.cities JOIN raw.weather USING (name)) ON year USING sum(population)) SELECT * FROM pivoted",
            {ModelId("raw", "cities"), ModelId("raw", "weather")},
        ),
        (
            "WITH pivoted AS (PIVOT raw.cities ON year USING sum(population)) SELECT * FROM pivoted",
            {ModelId("raw", "cities")},
        ),
    ]
    + [
        (
            f"SELECT * FROM raw.orders {operation} SELECT * FROM raw.items",
            {ModelId("raw", "orders"), ModelId("raw", "items")},
        )
        for operation in ("UNION", "INTERSECT", "EXCEPT")
    ],
)
def test_table_dependencies(sql, expected):
    assert extract_dependencies(sql) == expected


def test_lineage_connects_local_model_chain(model_dag):
    dag = model_dag(
        {
            "gold/summary.sql": "SELECT count(*) AS total FROM silver.cleaned",
            "silver/cleaned.sql": "SELECT * FROM bronze.source",
            "bronze/source.sql": "VALUES (1), (2)",
        }
    )
    bronze = ModelId("bronze", "source")
    silver = ModelId("silver", "cleaned")
    gold = ModelId("gold", "summary")
    assert dag.dependencies[silver] == {bronze}
    assert dag.dependencies[gold] == {silver}
    assert dag.order == [bronze, silver, gold]
    assert dag.external_nodes == set()
