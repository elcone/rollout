import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter

from rollout.models import Model, ModelId

logger = logging.getLogger(__name__)


@dataclass
class DAG:
    models: dict[ModelId, Model]
    dependencies: dict[ModelId, frozenset[ModelId]]
    dependents: dict[ModelId, frozenset[ModelId]]

    internal_nodes: frozenset[ModelId]
    external_nodes: frozenset[ModelId]
    edges: frozenset[tuple[ModelId, ModelId]]
    order: list[ModelId] = field(default_factory=list)


def build_dag(models: Iterable[Model]) -> DAG:
    logger.info("Building dependency DAG")
    registry = {}

    for model in models:
        if model.id in registry:
            raise ValueError(f"Duplicate model: {model.id}")
        registry[model.id] = model

    dependencies = {
        identity: model.dependencies for identity, model in registry.items()
    }
    for model in registry.values():
        for dependency in model.dependencies:
            dependencies.setdefault(dependency, frozenset())

    dependents: dict[ModelId, set[ModelId]] = {node: set() for node in dependencies}
    edges = set()
    for node, upstream in dependencies.items():
        for dependency in upstream:
            dependents[dependency].add(node)
            edges.add((dependency, node))

    internal_nodes = frozenset(registry)
    dag = DAG(
        models=registry,
        dependencies=dependencies,
        dependents={node: frozenset(values) for node, values in dependents.items()},
        internal_nodes=internal_nodes,
        external_nodes=frozenset(dependencies) - internal_nodes,
        edges=frozenset(edges),
    )

    dag.order = topological_sort(dag)
    logger.info(
        "Found %d dependencies and %d external sources; DAG valid",
        len(dag.edges),
        len(dag.external_nodes),
    )

    return dag


def topological_sort(dag: DAG) -> list[ModelId]:
    logger.info("Validating cycles and computing topological order")
    try:
        order = TopologicalSorter(
            {node: sorted(dag.dependencies[node]) for node in sorted(dag.dependencies)}
        ).static_order()

        return [node for node in order if node in dag.models]

    except CycleError as exc:
        cycle = " → ".join(map(str, exc.args[1]))
        raise ValueError(f"Dependency cycle detected: {cycle}") from exc
