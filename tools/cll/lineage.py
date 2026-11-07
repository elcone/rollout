from dataclasses import dataclass

from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.lineage import lineage as sql_lineage
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope
from sqlglot.schema import MappingSchema

from rollout.models import Model, ModelId
from rollout.parser import parse_read_only_query


@dataclass(frozen=True, order=True)
class ColumnRef:
    model: ModelId
    column: str


@dataclass(frozen=True, order=True)
class ColumnEdge:
    source: ColumnRef
    target: ColumnRef


@dataclass(frozen=True)
class ColumnLineage:
    root: ColumnRef
    edges: tuple[ColumnEdge, ...]


@dataclass(frozen=True)
class DatasetColumns:
    model: ModelId
    columns: tuple[str, ...]


@dataclass(frozen=True)
class ModelColumnLineage:
    root: ModelId
    datasets: tuple[DatasetColumns, ...]
    edges: tuple[ColumnEdge, ...]


def _column_key(ref: ColumnRef) -> tuple[str, str, str, str]:
    return ref.model.catalog, ref.model.schema, ref.model.name, ref.column


def _normalize_pivots(statement: exp.Expression) -> exp.Expression:
    # SQLGlot's scope traversal does not support DuckDB's standalone PIVOT.
    # Rewrite only the analysis AST, never the model's SQL.
    for pivot in reversed(list(statement.find_all(exp.Pivot))):
        if pivot.this is None:
            continue  # SQL-standard table PIVOT is already handled by SQLGlot.
        on = pivot.expressions
        aggregates = pivot.args.get("using", [])
        group = pivot.args.get("group")
        if len(on) != 1 or len(aggregates) != 1 or group is None:
            raise ValueError(
                "DuckDB PIVOT lineage requires one ON column, one aggregate, "
                "and explicit GROUP BY"
            )
        selector = on[0].this if isinstance(on[0], exp.In) else on[0]
        if not isinstance(selector, exp.Column):
            raise ValueError("DuckDB PIVOT lineage requires a simple ON column")
        values = on[0].expressions if isinstance(on[0], exp.In) else []
        if not values and isinstance(pivot.this, exp.Subquery):
            query = pivot.this.this
            where = query.args.get("where")
            projections = [
                select
                for select in query.selects
                if select.alias_or_name == selector.name
            ]
            original = projections[0].unalias() if len(projections) == 1 else None
            # Only a top-level AND conjunct bounds the category domain safely.
            predicates = []
            if where is not None:
                predicates = (
                    list(where.this.flatten())
                    if isinstance(where.this, exp.And)
                    else [where.this]
                )
            for predicate in predicates:
                if not isinstance(predicate, exp.In) or predicate.args.get("query"):
                    continue
                candidate = predicate.this
                if candidate == original or (
                    isinstance(candidate, exp.Column)
                    and not candidate.table
                    and isinstance(original, exp.Column)
                    and candidate.name == original.name
                ):
                    values = predicate.expressions
                    break
        if not values or any(not isinstance(value, exp.Literal) for value in values):
            raise ValueError(
                "Cannot determine DuckDB PIVOT categories: use literal "
                "ON ... IN (...) or a matching conjunctive WHERE ... IN (...)"
            )
        aggregate = aggregates[0]
        projections = [item.copy() for item in group.expressions]
        for value in values:
            name = value.this
            if aggregate.alias:
                name += "_" + aggregate.alias
            # Filter each aggregate so the selector is a column dependency too.
            expression = aggregate.unalias().copy()
            functions = list(expression.find_all(exp.AggFunc))
            if not functions:
                raise ValueError("DuckDB PIVOT USING requires an aggregate")
            for function in functions:
                filtered = exp.Filter(
                    this=function.copy(),
                    expression=exp.Where(
                        this=exp.EQ(this=selector.copy(), expression=value.copy())
                    ),
                )
                if function is expression:
                    expression = filtered
                else:
                    function.replace(filtered)
            projections.append(exp.alias_(expression, name, quoted=True))
        query = (
            exp.select(*projections)
            .from_(pivot.this.copy())
            .group_by(*(item.copy() for item in group.expressions))
        )
        if pivot.args.get("with_") is not None:
            query.set("with_", pivot.args["with_"].copy())
        if pivot is statement:
            statement = query
        else:
            pivot.replace(query)
    return statement


def _normalize_generate_series(statement: exp.Expression) -> exp.Expression:
    # SQLGlot treats FROM generate_series(...) as an opaque table. For value
    # lineage only, a projection exposes its argument subqueries to the resolver.
    # This AST is never executed: scalar and table-valued series differ in shape.
    for table in reversed(list(statement.find_all(exp.Table))):
        series = table.this
        if not isinstance(series, exp.GenerateSeries) or series.args.get(
            "is_end_exclusive"
        ):
            continue
        if table.args.get("ordinality"):
            raise ValueError("generate_series WITH ORDINALITY lineage is not supported")
        alias = table.args.get("alias")
        if alias and len(alias.columns) > 1:
            raise ValueError("generate_series requires a single output column alias")
        column = (
            alias.columns[0].copy()
            if alias and alias.columns
            else exp.to_identifier("generate_series")
        )
        alias = (
            alias.copy()
            if alias
            else exp.TableAlias(this=exp.to_identifier("generate_series"))
        )
        alias.set("columns", None)
        projection = exp.select(exp.alias_(series.copy(), column)).subquery()
        projection.set("alias", alias)
        table.replace(projection)
    return statement


def _direct_columns(scope: Scope, column: str) -> set[ColumnRef]:
    outputs = [select.alias_or_name for select in scope.expression.selects]
    if outputs.count(column) != 1:
        raise ValueError(f"Output column {column!r} is missing or ambiguous")
    # The prepared scope owns the analysis AST; Rollout never uses SQLGlot's
    # copied/trimmed SQL for node labels, only the upstream table expressions.
    node = sql_lineage(
        column,
        scope.expression,
        scope=scope,
        dialect="duckdb",
        copy=False,
        trim_selects=False,
    )
    sources = set()
    for upstream in node.walk():
        if isinstance(upstream.expression, exp.Table):
            table = upstream.expression
            name = exp.to_column(upstream.name, dialect="duckdb").name
            if name == "*" or not isinstance(table.this, exp.Identifier):
                raise ValueError(
                    "Cannot resolve column ownership without dataset metadata"
                )
            sources.add(
                ColumnRef(
                    ModelId(table.db or "public", table.name, table.catalog), name
                )
            )
        elif not upstream.downstream and isinstance(
            upstream.expression, exp.Placeholder
        ):
            raise ValueError(f"Cannot resolve upstream column {upstream.name!r}")
    return sources


def _prepare_scope(
    current: Model,
    lookup: dict[ModelId, Model],
    scopes: dict[ModelId, Scope],
    preparing: set[ModelId],
) -> Scope:
    if current.id in scopes:
        return scopes[current.id]
    if current.id in preparing:
        raise ValueError(f"Model cycle while resolving {current.id}")
    preparing.add(current.id)
    statement = parse_read_only_query(current.sql)
    if statement is None:
        raise ValueError(f"Empty SQL in {current.id}")
    statement = _normalize_pivots(statement)
    statement = _normalize_generate_series(statement)
    schema = MappingSchema(dialect="duckdb")
    for dependency in sorted(current.dependencies):
        if dependency not in lookup:
            continue
        upstream = _prepare_scope(lookup[dependency], lookup, scopes, preparing)
        selects = upstream.expression.selects
        if any(select.is_star for select in selects):
            continue
        table = exp.Table(
            this=exp.to_identifier(dependency.name),
            db=exp.to_identifier(dependency.schema),
            catalog=exp.to_identifier(dependency.catalog),
        )
        schema.add_table(
            table, {select.alias_or_name: "UNKNOWN" for select in selects}
        )
    qualified = qualify(
        statement,
        dialect="duckdb",
        db="public",
        schema=schema,
        infer_schema=True,
        validate_qualify_columns=True,
    )
    scope = build_scope(qualified)
    if scope is None:
        raise ValueError(f"Cannot build SQL scope for {current.id}")
    scopes[current.id] = scope
    preparing.remove(current.id)
    return scope


def resolve_column_lineage(
    model: Model, column: str, models: list[Model]
) -> ColumnLineage:
    lookup = {item.id: item for item in models}
    lookup[model.id] = model
    scopes: dict[ModelId, Scope] = {}
    preparing: set[ModelId] = set()
    return _resolve_column_lineage(model, column, lookup, scopes, preparing)


def _resolve_column_lineage(
    model: Model,
    column: str,
    lookup: dict[ModelId, Model],
    scopes: dict[ModelId, Scope],
    preparing: set[ModelId],
) -> ColumnLineage:
    root = ColumnRef(model.id, column)
    edges: set[ColumnEdge] = set()
    visited: set[ColumnRef] = set()

    def expand(target: ColumnRef) -> None:
        if target in visited or target.model not in lookup:
            return
        visited.add(target)
        try:
            scope = _prepare_scope(lookup[target.model], lookup, scopes, preparing)
            sources = _direct_columns(scope, target.column)
        except (SqlglotError, ValueError) as exc:
            raise ValueError(
                f"Cannot resolve {target.model}.{target.column}: {exc}"
            ) from exc
        for source in sorted(sources, key=_column_key):
            edges.add(ColumnEdge(source, target))
            expand(source)

    expand(root)
    return ColumnLineage(
        root,
        tuple(
            sorted(
                edges,
                key=lambda edge: (_column_key(edge.source), _column_key(edge.target)),
            )
        ),
    )


def resolve_model_lineage(model: Model, models: list[Model]) -> ModelColumnLineage:
    lookup = {item.id: item for item in models}
    lookup[model.id] = model
    scopes: dict[ModelId, Scope] = {}
    preparing: set[ModelId] = set()

    def output_columns(current: Model) -> tuple[str, ...]:
        scope = _prepare_scope(current, lookup, scopes, preparing)
        selects = scope.expression.selects
        if any(select.is_star or not select.alias_or_name for select in selects):
            raise ValueError(
                "Cannot determine complete output columns without dataset metadata"
            )
        return tuple(sorted(select.alias_or_name for select in selects))

    try:
        root_columns = output_columns(model)
    except (SqlglotError, ValueError) as exc:
        raise ValueError(
            f"Cannot determine output columns for {model.id}: {exc}"
        ) from exc
    edges: set[ColumnEdge] = set()
    for column in root_columns:
        try:
            edges.update(
                _resolve_column_lineage(model, column, lookup, scopes, preparing).edges
            )
        except ValueError as exc:
            raise ValueError(
                f"Cannot resolve {model.id}.{column}:\n{exc}"
            ) from exc
    columns = {model.id: set(root_columns)}
    for edge in edges:
        for ref in (edge.source, edge.target):
            columns.setdefault(ref.model, set()).add(ref.column)
    for identity, known in columns.items():
        if identity in lookup and identity != model.id:
            try:
                known.update(output_columns(lookup[identity]))
            except (SqlglotError, ValueError):
                # An upstream schema may be incomplete; edge references remain valid.
                pass
    return ModelColumnLineage(
        model.id,
        tuple(
            DatasetColumns(identity, tuple(sorted(columns[identity])))
            for identity in sorted(
                columns, key=lambda item: (item.catalog, item.schema, item.name)
            )
        ),
        tuple(
            sorted(
                edges,
                key=lambda edge: (_column_key(edge.source), _column_key(edge.target)),
            )
        ),
    )


def render_text(lineage: ColumnLineage) -> str:
    inputs: dict[ColumnRef, list[ColumnRef]] = {}
    for edge in lineage.edges:
        inputs.setdefault(edge.target, []).append(edge.source)
    lines = [f"{lineage.root.model}.{lineage.root.column}"]

    def append(ref: ColumnRef, prefix: str, path: set[ColumnRef]) -> None:
        sources = sorted(set(inputs.get(ref, [])), key=_column_key)
        for index, source in enumerate(sources):
            last = index == len(sources) - 1
            lines.append(
                f"{prefix}{'└── ' if last else '├── '}{source.model}.{source.column}"
            )
            if source not in path:
                append(source, prefix + ("    " if last else "│   "), path | {source})

    append(lineage.root, "", {lineage.root})
    return "\n".join(lines)
