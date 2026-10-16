from pathlib import Path

from rollout.dag import build_dag
from rollout.discovery import discover_models, discover_seeders
from rollout.models import ModelId
from rollout.parser import parse_models


def model_ref(identity: ModelId) -> dict:
    return {
        "type": "MODEL",
        "schema": identity.schema,
        "name": identity.name,
        "catalog": identity.catalog,
    }


def load_project(project_dir: Path) -> dict:
    models = discover_models(project_dir / "models")
    models = parse_models(models)
    dag = build_dag(models)
    seeders = discover_seeders(project_dir / "seeders")
    outputs = {identity for seeder in seeders for identity in seeder.output_ids}
    identities = dag.internal_nodes | dag.external_nodes | outputs

    model_records = []
    for identity in sorted(identities):
        if identity in dag.internal_nodes:
            origin = "internal"
        elif identity in outputs:
            origin = "seeder"
        else:
            origin = "source"
        model_records.append({**model_ref(identity), "origin": origin})

    lineage = [
        {"source": model_ref(source), "target": model_ref(target)}
        for source, target in sorted(dag.edges)
    ]
    seeder_records = []
    for seeder in seeders:
        identity = seeder.path.relative_to(project_dir).as_posix()
        seeder_records.append(
            {
                "id": identity,
                "name": seeder.path.name,
                "outputs": [model_ref(output) for output in seeder.output_ids],
            }
        )
        lineage.extend(
            {
                "source": {"type": "SEEDER", "id": identity},
                "target": model_ref(output),
            }
            for output in seeder.output_ids
        )

    return {"models": model_records, "seeders": seeder_records, "lineage": lineage}
