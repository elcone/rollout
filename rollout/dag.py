"""Dependency graph construction, cycle validation, and model ordering."""

from collections.abc import Iterable
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter

from rollout.logging import get_logger
from rollout.models import Model, ModelId

logger = get_logger(__name__)


@dataclass
class DAG:
    """Dependency graph containing local models and external source nodes.

    Attributes:
        models: Local model records indexed by identity.
        dependencies: Upstream identities for every local or external node.
        dependents: Downstream identities for every node.
    """
    models: dict[ModelId, Model]
    dependencies: dict[ModelId, frozenset[ModelId]]
    dependents: dict[ModelId, frozenset[ModelId]]

    @property
    def internal_nodes(self) -> frozenset[ModelId]:
        """frozenset[ModelId]: Identities backed by local SQL model files."""
        return frozenset(self.models)

    @property
    def external_nodes(self) -> frozenset[ModelId]:
        """frozenset[ModelId]: Referenced identities without a local model."""
        return frozenset(self.dependencies) - self.internal_nodes

    @property
    def edges(self) -> frozenset[tuple[ModelId, ModelId]]:
        """frozenset[tuple[ModelId, ModelId]]: Source-to-dependent edges."""
        return frozenset(
            (dependency, model)
            for model, dependencies in self.dependencies.items()
            for dependency in dependencies
        )


def build_dag(models: Iterable[Model]) -> DAG:
    """Build a dependency graph and verify that it is acyclic.

    Args:
        models: Model records with dependencies already populated by the parser.

    Returns:
        A validated graph with external sources and reverse dependency links.

    Raises:
        ValueError: Model identities are duplicated or dependencies contain a cycle.
    """
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
    for node, upstream in dependencies.items():
        for dependency in upstream:
            dependents[dependency].add(node)

    dag = DAG(
        registry,
        dependencies,
        {node: frozenset(values) for node, values in dependents.items()},
    )

    logger.info("Validating dependency cycles")
    topological_sort(dag)
    logger.info(
        "Found %d dependencies and %d external sources; DAG valid",
        len(dag.edges),
        len(dag.external_nodes),
    )

    return dag


def topological_sort(dag: DAG) -> list[ModelId]:
    """Order local models so their local dependencies appear first.

    External nodes participate in cycle analysis but are omitted from the
    result. Sorted identities provide deterministic ordering for a fixed graph.

    Args:
        dag: Graph containing upstream references for every node.

    Returns:
        Local model identities in topological order, or an empty list.

    Raises:
        ValueError: The graph contains a cycle; the message includes its path.
    """
    logger.debug("Validating cycles and computing topological order")
    try:
        order = TopologicalSorter(
            {node: sorted(dag.dependencies[node]) for node in sorted(dag.dependencies)}
        ).static_order()

        return [node for node in order if node in dag.models]

    except CycleError as exc:
        cycle = " → ".join(map(str, exc.args[1]))
        raise ValueError(f"Dependency cycle detected: {cycle}") from exc
