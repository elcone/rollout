import logging
from collections.abc import Mapping
from pathlib import Path

import yaml

from rollout.config import ModelConfig, SeederConfig, SeederOutputConfig
from rollout.models import STAGING_SCHEMA, Model, ModelId, Seeder

logger = logging.getLogger(__name__)


def _load_model_config(path: Path) -> ModelConfig:
    if not path.exists():
        return ModelConfig()

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if data is None:
            data = {}

        if not isinstance(data, Mapping):
            raise ValueError("Expected a YAML mapping/object")

        return ModelConfig(**data)

    except (OSError, UnicodeError, yaml.YAMLError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid model configuration in {path}: {exc}") from exc


def discover_models(models_dir: str | Path = "models") -> list[Model]:
    logger.info("Discovering SQL models in %s", models_dir)
    root = Path(models_dir)

    if not root.is_dir():
        raise ValueError(f"Models directory does not exist: {root}")

    reserved_schemas = {
        "public": f"Explicit public schema directory is invalid: {root / 'public'}; put SQL files directly under {root}",
        STAGING_SCHEMA: f"Schema '{STAGING_SCHEMA}' is reserved for seeder outputs: {root / STAGING_SCHEMA}",
    }
    for schema, message in reserved_schemas.items():
        if (root / schema).is_dir():
            raise ValueError(message)

    models = []

    for path in sorted(root.rglob("*.sql")):
        if not path.is_file():
            continue

        parts = path.relative_to(root).parts
        if len(parts) > 2:
            raise ValueError(
                f"Invalid nested model path: {path}; expected model.sql or schema/model.sql"
            )

        schema = parts[0] if len(parts) == 2 else "public"
        logger.debug("Discovered model %s.%s at %s", schema, path.stem, path)
        models.append(
            Model(
                ModelId(schema, path.stem),
                path,
                path.read_text(encoding="utf-8"),
                config=_load_model_config(path.with_suffix(".yaml")),
            )
        )

    logger.info("Found %d models", len(models))

    return models


def discover_seeders(seeders_dir: str | Path = "seeders") -> list[Seeder]:
    logger.info("Discovering seeders in %s", seeders_dir)
    root = Path(seeders_dir)
    if not root.exists():
        return []
    if not root.is_dir():
        raise ValueError(f"Seeders path is not a directory: {root}")

    seeders = []
    output_paths: dict[ModelId, Path] = {}
    for path in sorted(root.rglob("*.py")):
        if not path.is_file():
            continue
        yaml_path = path.with_suffix(".yaml")
        config_path = path
        if yaml_path.exists():
            config_path = yaml_path
            try:
                data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
                config = SeederConfig.model_validate(data)
            except (OSError, UnicodeError, yaml.YAMLError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid seeder configuration in {yaml_path}: {exc}"
                ) from exc
        else:
            config = SeederConfig(outputs={path.stem: SeederOutputConfig()})

        seeder = Seeder(path, config)
        for output_id in seeder.output_ids:
            if output_id in output_paths:
                raise ValueError(
                    f"Duplicate seeder output {output_id}: "
                    f"{output_paths[output_id]} and {config_path}"
                )
            output_paths[output_id] = config_path
        seeders.append(seeder)

    logger.info("Found %d seeders", len(seeders))
    return seeders
