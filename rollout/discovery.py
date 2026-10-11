import logging
from collections.abc import Mapping
from pathlib import Path

import yaml

from rollout.config import ModelConfig
from rollout.models import Model, ModelId

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

    if (root / "public").is_dir():
        raise ValueError(
            f"Explicit public schema directory is invalid: {root / 'public'}; put SQL files directly under {root}"
        )

    models = []

    for path in sorted(root.rglob("*.sql")):
        if not path.is_file():
            continue

        parts = path.relative_to(root).parts
        if len(parts) > 2:
            logger.error(
                "Ignoring nested model path: %s; expected model.sql or schema/model.sql",
                path,
            )
            continue

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
