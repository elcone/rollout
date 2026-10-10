import re

from rollout.renderer import render_markdown


def test_summary_sections_order_and_mermaid_edges(model_dag):
    dag = model_dag(
        {
            "silver/orders.sql": "SELECT * FROM raw.orders",
            "report.sql": "SELECT * FROM silver.orders",
        }
    )
    document = render_markdown(dag)
    assert "Models: 2" in document
    assert "Dependency relationships: 2" in document
    assert "External dependencies: 1" in document
    assert "1. `silver.orders`\n2. `public.report`" in document
    assert "### `silver.orders`" in document
    assert "- Dependencies: `raw.orders`" in document
    assert "- Dependents: `public.report`" in document
    assert "- External dependencies: `raw.orders`" in document
    assert "```mermaid\nflowchart LR" in document
    # Identify nodes by labels within their schemas, without fixing numeric IDs.
    groups = dict(
        re.findall(r'subgraph \w+\["([^"]+)"\]\n(.*?)    end', document, re.S)
    )
    nodes = {
        schema: re.search(r'(n\d+)\("', content).group(1)
        for schema, content in groups.items()
    }
    assert f"{nodes['raw']} --> {nodes['silver']}" in document
    assert f"{nodes['silver']} --> {nodes['public']}" in document
    assert "classDef external stroke-dasharray: 5 5" in document
    assert f"class {nodes['raw']} external" in document
    assert render_markdown(dag) == document


def test_groups_internal_and_external_nodes_in_one_schema(model_dag):
    dag = model_dag(
        {
            "silver/orders.sql": "SELECT * FROM silver.source",
            "silver/report.sql": "SELECT * FROM silver.orders",
        }
    )
    document = render_markdown(dag)
    assert document.count('["silver"]') == 1
    for name in ("orders", "report", "source"):
        assert f'("{name}")' in document


def test_escapes_schema_labels(model_dag):
    dag = model_dag({"report.sql": 'SELECT * FROM "a&<b>".orders'})
    assert '["a&amp;&lt;b&gt;"]' in render_markdown(dag)


def test_empty_graph(model_dag):
    document = render_markdown(model_dag({}))
    assert "Models: 0" in document
    assert "No models." in document
    assert "subgraph" not in document


def test_rendering_reuses_validated_order(model_dag, monkeypatch):
    dag = model_dag({"numbers.sql": "SELECT 1", "report.sql": "SELECT * FROM numbers"})

    def unexpected_sort(*args):
        raise AssertionError("Rendering must reuse dag.order")

    monkeypatch.setattr("rollout.dag.topological_sort", unexpected_sort)
    assert "1. `public.numbers`\n2. `public.report`" in render_markdown(dag)
