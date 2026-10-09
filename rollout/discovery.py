"""Filesystem discovery of root-level and schema-level SQL models."""

from pathlib import Path

from rollout.logging import get_logger
from rollout.models import Model

logger = get_logger(__name__)


def discover_models(models_dir: str | Path = "models") -> list[Model]:
    """Read SQL models from the root and its schema directories.

    Root-level files belong to public; schema/model.sql uses the parent
    folder as its schema. More deeply nested files are logged at ERROR level
    and skipped. SQL contents are read without parsing or execution.

    Args:
        models_dir: Root directory to scan for .sql files.

    Returns:
        Models in sorted path order, or an empty list if none are found.

    Raises:
        ValueError: The root is not a directory or contains a public directory.
        OSError: A discovered file cannot be read.
        UnicodeDecodeError: A discovered file is not valid UTF-8.
    """
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
        models.append(Model(path.stem, schema, path, path.read_text(encoding="utf-8")))

    logger.info("Found %d models", len(models))

    return models
