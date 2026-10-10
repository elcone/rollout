from pathlib import Path

import pytest

from rollout.dag import build_dag
from rollout.models import Model, ModelId


def test_registry_relationships_and_external_sources():
    source = ModelId("raw", "orders")
    cleaned = ModelId("silver", "orders")
    report = ModelId("gold", "report")
    independent = ModelId("public", "numbers")
    models = [
        Model(cleaned, Path("orders.sql"), "SELECT 1", frozenset({source})),
        Model(report, Path("report.sql"), "SELECT 1", frozenset({cleaned})),
        Model(independent, Path("numbers.sql"), "SELECT 1"),
    ]
    dag = build_dag(models)
    assert dag.models == {model.id: model for model in models}
    assert dag.internal_nodes == {cleaned, report, independent}
    assert dag.external_nodes == {source}
    assert dag.dependencies == {
        source: frozenset(),
        cleaned: {source},
        report: {cleaned},
        independent: frozenset(),
    }
    assert dag.dependents == {
        source: {cleaned},
        cleaned: {report},
        report: frozenset(),
        independent: frozenset(),
    }
    assert dag.edges == {(source, cleaned), (cleaned, report)}
    assert set(dag.order) == dag.internal_nodes
    assert dag.order.index(cleaned) < dag.order.index(report)
    assert build_dag(reversed(models)).order == dag.order


@pytest.mark.parametrize(
    "files",
    [
        {"orders.sql": "SELECT * FROM items", "items.sql": "SELECT * FROM orders"},
        {"orders.sql": "SELECT * FROM orders"},
    ],
)
def test_cycles_are_rejected(model_dag, files):
    with pytest.raises(ValueError, match="Dependency cycle detected") as error:
        model_dag(files)
    assert "public.orders" in str(error.value)


def test_duplicate_model_ids():
    model = Model(ModelId("public", "numbers"), Path("numbers.sql"), "SELECT 1")
    with pytest.raises(ValueError, match="Duplicate model: public.numbers"):
        build_dag([model, model])


def test_empty_dag():
    dag = build_dag([])
    assert dag.models == {}
    assert dag.order == []
    assert dag.internal_nodes == dag.external_nodes == dag.edges == frozenset()
