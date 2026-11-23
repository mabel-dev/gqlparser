"""Lower a GQL `Query`, against a `PropertyGraph`, to one SQL `SELECT`.

The result is the statement dict sqlparser-rs serialises (the release in
`SQLPARSER_FORMAT`); render it with sqlparser-rs (`opteryx.compute.restore_ast`,
sqloxide) and run it as ordinary SQL.

Rules (see docs/LOWERING_DESIGN.md):
- each element variable is a table alias; each edge joins to its endpoints on
  `SOURCE` / `DESTINATION` `KEY ... REFERENCES`;
- successive `MATCH` statements are inner joins on their shared variables;
- an `OPTIONAL MATCH` is one derived table, `LEFT JOIN`ed on its shared variables;
- the match mode is repeatable elements: no edge-inequality predicates.
"""

from __future__ import annotations

import difflib
import re

from gqlparser import _sql
from gqlparser import ast
from gqlparser.ast import Span
from gqlparser.errors import GqlNotSupportedError
from gqlparser.errors import GqlSemanticError
from gqlparser.graph import EdgeTable
from gqlparser.graph import PropertyGraph
from gqlparser.graph import SqlName
from gqlparser.graph import VertexTable

SQLPARSER_FORMAT = _sql.SQLPARSER_FORMAT

_AGGREGATES = frozenset({"COUNT", "SUM", "AVG", "MIN", "MAX", "COLLECT_LIST"})
_RENAMED_FUNCTIONS = {"COLLECT_LIST": "ARRAY_AGG"}
# XOR of two booleans is their inequality, with the same three-valued result.
_BINARY_OPERATORS = {
    "OR": "Or", "AND": "And", "XOR": "NotEq", "=": "Eq", "<>": "NotEq", "<": "Lt",
    "<=": "LtEq", ">": "Gt", ">=": "GtEq", "||": "StringConcat", "+": "Plus",
    "-": "Minus", "*": "Multiply", "/": "Divide", "%": "Modulo",
}  # fmt: skip
_UNARY_OPERATORS = {"NOT": "Not", "-": "Minus", "+": "Plus"}
_DURATION = re.compile(r"^P(?:(\d+)([YMWD])|T(\d+)([HMS]))$")
_DATE_FIELDS = {"Y": "Year", "M": "Month", "W": "Day", "D": "Day"}
_TIME_FIELDS = {"H": "Hour", "M": "Minute", "S": "Second"}


def lower(query: ast.Query, graph: PropertyGraph) -> dict:
    """Lower `query`, which must `USE` `graph`, to a sqlparser-rs statement dict."""
    return {"Query": _Lowering(graph).query(query)}


def _suggest(name: str, options) -> str:
    match = difflib.get_close_matches(name, sorted(options), n=1)
    return f" Did you mean `{match[0]}`?" if match else ""


def _temporal_type(type_name: str):
    if type_name == "DATE":
        return "Date"
    if type_name == "TIME":
        return {"Time": [None, "None"]}
    return {"Timestamp": [None, "None"]}  # TIMESTAMP, DATETIME


class _Binding:
    """A bound element variable, as SQL sees it.

    `alias` qualifies its columns. When the variable was bound by an OPTIONAL
    MATCH, `alias` is the derived table's and `projected` maps each of the
    element's columns to the name it was projected under.
    """

    __slots__ = ("alias", "element", "is_edge", "projected")

    def __init__(self, element, is_edge: bool, alias: str, projected: dict | None = None):
        self.element = element
        self.is_edge = is_edge
        self.alias = alias
        self.projected = projected


def _column(binding: _Binding, column: SqlName, at: Span) -> dict:
    qualifier = _sql.ident(binding.alias, True, at)
    if binding.projected is None:
        return _sql.column(qualifier, _sql.ident(column.value, column.quoted, at))
    return _sql.column(qualifier, _sql.ident(binding.projected[column.value], True, at))


class _From:
    """A FROM clause being assembled: relations joined in an order that keeps each
    join connected to what came before, with every predicate placed in the first
    ON clause that can evaluate it."""

    __slots__ = ("joined", "joins", "relation", "where")

    def __init__(self):
        self.relation = None
        self.joins: list[dict] = []
        self.joined: set[str] = set()
        self.where: list[dict] = []

    def add(self, relations: list[tuple[str, dict]], predicates: list[tuple[dict, set]]) -> None:
        pending = list(relations)
        unused = list(predicates)
        while pending:
            chosen = next(
                (
                    r
                    for r in pending
                    if any(
                        r[0] in refs and refs - {r[0]} and refs <= self.joined | {r[0]}
                        for _, refs in unused
                    )
                ),
                pending[0],
            )
            pending.remove(chosen)
            alias, factor = chosen
            on = [p for p in unused if p[1] <= self.joined | {alias}]
            for p in on:
                unused.remove(p)
            conditions = [expression for expression, _ in on]
            if self.relation is None:
                self.relation = factor
                self.where.extend(conditions)
            elif conditions:
                self.joins.append(_sql.inner_join(factor, _sql.conjunction(conditions)))
            else:
                self.joins.append(_sql.cross_join(factor))
            self.joined.add(alias)
        # predicates between relations that were already joined
        self.where.extend(expression for expression, _ in unused)


class _Lowering:
    def __init__(self, graph: PropertyGraph):
        self.graph = graph
        self.by_label = {e.label: e for e in (*graph.vertex_tables, *graph.edge_tables)}
        self.vertices = {v.name: v for v in graph.vertex_tables}
        self.generated = 0

    def fresh(self, prefix: str) -> str:
        self.generated += 1
        return f"${prefix}{self.generated}"

    # --- query -----------------------------------------------------------------

    def query(self, query: ast.Query) -> dict:
        self.check_graph(query)
        first = query.match_statements[0]
        if first.optional:
            raise GqlNotSupportedError(
                "a query that starts with OPTIONAL MATCH is not supported yet",
                first.span.line,
                first.span.column,
            )
        scope: dict[str, _Binding] = {}
        frm = _From()
        for statement in query.match_statements:
            if statement.optional:
                self.optional_match(statement, scope, frm)
            else:
                self.match(statement, scope, frm)
        return self.result(query, scope, frm)

    def check_graph(self, query: ast.Query) -> None:
        names = tuple(part.value for part in self.graph.name)
        full = ".".join(names)
        if query.graph is None:
            raise GqlSemanticError(
                f"the query has no USE; start it with `USE {full}`",
                query.span.line,
                query.span.column,
            )
        # The full name, or its last part alone.
        used = tuple(part.name for part in query.graph)
        if used not in (names, names[-1:]):
            raise GqlSemanticError(
                f"the query uses `{'.'.join(used)}`, but the graph is `{full}`",
                query.graph[0].span.line,
                query.graph[0].span.column,
            )

    # --- MATCH -----------------------------------------------------------------

    def match(self, statement: ast.MatchStatement, scope: dict, frm: _From) -> None:
        relations: list[tuple[str, dict]] = []
        predicates: list[tuple[dict, set]] = []
        self.bind_paths(statement.paths, scope, None, relations, predicates)
        frm.add(relations, predicates)
        frm.where.extend(self.filters(statement, scope, None))

    def filters(self, statement: ast.MatchStatement, scope: dict, outer: dict | None) -> list[dict]:
        found = [
            element.where
            for path in statement.paths
            for element in path.elements
            if element.where is not None
        ]
        if statement.where is not None:
            found.append(statement.where)
        return [self.expression(e, scope, outer) for e in found]

    def optional_match(self, statement: ast.MatchStatement, scope: dict, frm: _From) -> None:
        inner_scope: dict[str, _Binding] = {}
        relations: list[tuple[str, dict]] = []
        predicates: list[tuple[dict, set]] = []
        shared = self.bind_paths(statement.paths, inner_scope, scope, relations, predicates)
        at = statement.span
        if not shared:
            raise GqlNotSupportedError(
                "an OPTIONAL MATCH that shares no variable with the statements before it "
                "is not supported yet",
                at.line,
                at.column,
            )
        inner = _From()
        inner.add(relations, predicates)
        inner.where.extend(self.filters(statement, inner_scope, scope))

        derived_alias = self.fresh("o")
        projection: list[dict] = []
        projected_names: dict[tuple[str, str], str] = {}

        def project(binding: _Binding, column: SqlName) -> str:
            key = (binding.alias, column.value)
            if key not in projected_names:
                name = f"$c{len(projected_names) + 1}"
                projected_names[key] = name
                projection.append(
                    _sql.select_item(_column(binding, column, at), _sql.ident(name, True, at))
                )
            return projected_names[key]

        conditions = []
        for variable in shared:
            binding = inner_scope[variable]
            for key_column in binding.element.key:
                name = project(binding, key_column)
                conditions.append(
                    _sql.binary(
                        _sql.column(
                            _sql.ident(derived_alias, True, at), _sql.ident(name, True, at)
                        ),
                        "Eq",
                        _column(scope[variable], key_column, at),
                    )
                )
        for variable, binding in inner_scope.items():
            if variable in shared:
                continue
            columns = {c.value: project(binding, c) for c in self.known_columns(binding.element)}
            scope[variable] = _Binding(binding.element, binding.is_edge, derived_alias, columns)

        subquery = _sql.query(
            at=at,
            distinct=False,
            projection=projection,
            relation=inner.relation,
            joins=inner.joins,
            selection=_sql.conjunction(inner.where),
            group_by=[],
            order_by=[],
            limit=None,
            offset=None,
        )
        factor = _sql.derived(subquery, _sql.ident(derived_alias, True, at))
        frm.joins.append(_sql.left_join(factor, _sql.conjunction(conditions)))
        frm.joined.add(derived_alias)

    def known_columns(self, element) -> list[SqlName]:
        """Every column of an element the graph refers to: what an OPTIONAL MATCH
        projects so that later statements can use the element."""
        columns = list(element.key)
        if type(element) is EdgeTable:
            columns += [*element.source.key, *element.destination.key]
        else:
            for edge in self.graph.edge_tables:
                for endpoint in (edge.source, edge.destination):
                    if endpoint.vertex == element.name:
                        columns += endpoint.references
        columns += [column for _, column in element.properties]
        unique: dict[str, SqlName] = {}
        for column in columns:
            unique.setdefault(column.value, column)
        return list(unique.values())

    # --- patterns ----------------------------------------------------------------

    def bind_paths(
        self, paths, scope: dict, outer: dict | None, relations, predicates
    ) -> list[str]:
        """Bind every element of `paths` into `scope`, adding a relation for each new
        element and the edge-to-node join predicates.

        Inside an OPTIONAL MATCH, `outer` is the enclosing scope: a variable bound
        there is re-bound locally, to a fresh scan of the same element, and is
        returned in the list of shared variables."""
        shared: list[str] = []
        for path in paths:
            elements = path.elements
            tables = [None] * len(elements)
            for i in range(1, len(elements), 2):
                tables[i] = self.edge_table(elements[i], scope, outer)
            for i in range(0, len(elements), 2):
                tables[i] = self.vertex_table(elements, i, tables, scope, outer)
            bindings = []
            for element, table in zip(elements, tables):
                variable = element.variable.name if element.variable is not None else None
                if (
                    outer is not None
                    and variable is not None
                    and variable in outer
                    and variable not in scope
                ):
                    shared.append(variable)
                bindings.append(self.bind(element, table, scope, relations))
            for i in range(1, len(elements), 2):
                self.connect(elements, bindings, i, predicates)
        return shared

    def known(self, name: str, scope: dict, outer: dict | None) -> _Binding | None:
        if name in scope:
            return scope[name]
        if outer is not None and name in outer:
            return outer[name]
        return None

    def labelled(self, label: ast.Identifier, edge: bool):
        table = self.by_label.get(label.name)
        if table is None:
            raise GqlSemanticError(
                f"the graph has no label `{label.name}`.{_suggest(label.name, self.by_label)}",
                label.span.line,
                label.span.column,
            )
        if (type(table) is EdgeTable) != edge:
            kind, wanted = ("a node", "an edge") if edge else ("an edge", "a node")
            raise GqlSemanticError(
                f"`{label.name}` is {kind} label, used here on {wanted}",
                label.span.line,
                label.span.column,
            )
        return table

    def edge_table(self, edge: ast.EdgePattern, scope: dict, outer: dict | None) -> EdgeTable:
        bound = self.known(edge.variable.name, scope, outer) if edge.variable is not None else None
        if edge.label is not None:
            table = self.labelled(edge.label, True)
            if bound is not None and bound.element is not table:
                self.relabelled(edge.variable, bound)
            return table
        if bound is not None:
            if not bound.is_edge:
                raise GqlSemanticError(
                    f"`{edge.variable.name}` is a node variable, used here on an edge",
                    edge.span.line,
                    edge.span.column,
                )
            return bound.element
        raise GqlSemanticError(
            "this edge needs a label: the graph can't tell which edge table it is",
            edge.span.line,
            edge.span.column,
        )

    def vertex_table(
        self, elements, i: int, tables, scope: dict, outer: dict | None
    ) -> VertexTable:
        node: ast.NodePattern = elements[i]
        bound = self.known(node.variable.name, scope, outer) if node.variable is not None else None
        if bound is not None and bound.is_edge:
            raise GqlSemanticError(
                f"`{node.variable.name}` is an edge variable, used here on a node",
                node.span.line,
                node.span.column,
            )
        if node.label is not None:
            table = self.labelled(node.label, False)
            if bound is not None and bound.element is not table:
                self.relabelled(node.variable, bound)
            return table
        if bound is not None:
            return bound.element
        # infer from the edges beside it: each fixes which vertex table this node must be
        implied = set()
        if i > 0:
            edge, table = elements[i - 1], tables[i - 1]
            implied.add(
                table.destination.vertex
                if edge.direction is ast.EdgeDirection.POINTING_RIGHT
                else table.source.vertex
            )
        if i + 1 < len(elements):
            edge, table = elements[i + 1], tables[i + 1]
            implied.add(
                table.source.vertex
                if edge.direction is ast.EdgeDirection.POINTING_RIGHT
                else table.destination.vertex
            )
        if len(implied) == 1:
            return self.vertices[implied.pop()]
        if implied:
            labels = " and ".join(f"`{self.vertices[v].label}`" for v in sorted(implied))
            message = f"this node would have to be both {labels}"
        else:
            message = "this node needs a label: the graph can't tell which vertex table it is"
        raise GqlSemanticError(message, node.span.line, node.span.column)

    def relabelled(self, variable: ast.Identifier, bound: _Binding):
        raise GqlSemanticError(
            f"`{variable.name}` is already a `{bound.element.label}`",
            variable.span.line,
            variable.span.column,
        )

    def bind(self, element, table, scope: dict, relations) -> _Binding:
        is_edge = type(element) is ast.EdgePattern
        variable = element.variable
        if variable is not None:
            if variable.name.startswith("$"):
                raise GqlNotSupportedError(
                    "variable names starting with `$` are not supported yet",
                    variable.span.line,
                    variable.span.column,
                )
            if variable.name in scope:
                return scope[variable.name]
        alias = variable.name if variable is not None else self.fresh("e" if is_edge else "v")
        binding = _Binding(table, is_edge, alias)
        if variable is not None:
            scope[variable.name] = binding
        at = element.span
        name = [_sql.ident(part.value, part.quoted, at) for part in table.relation]
        relations.append((alias, _sql.table(name, _sql.ident(alias, True, at))))
        return binding

    def connect(self, elements, bindings, i: int, predicates) -> None:
        """Join edge `i` to the nodes either side of it, in its declared direction."""
        edge: ast.EdgePattern = elements[i]
        edge_binding = bindings[i]
        table: EdgeTable = edge_binding.element
        if edge.direction is ast.EdgeDirection.POINTING_RIGHT:
            source, destination = bindings[i - 1], bindings[i + 1]
        else:
            source, destination = bindings[i + 1], bindings[i - 1]
        declared = (table.source.vertex, table.destination.vertex)
        written = (source.element.name, destination.element.name)
        if written != declared:
            declared_labels = [self.vertices[v].label for v in declared]
            written_labels = [self.vertices[v].label for v in written]
            if written == declared[::-1]:
                message = (
                    f"`{table.label}` points from `{declared_labels[0]}` to "
                    f"`{declared_labels[1]}`, but here it points from `{written_labels[0]}` "
                    f"to `{written_labels[1]}`; reverse the arrow"
                )
            else:
                message = (
                    f"`{table.label}` connects `{declared_labels[0]}` to "
                    f"`{declared_labels[1]}`, not `{written_labels[0]}` to `{written_labels[1]}`"
                )
            raise GqlSemanticError(message, edge.span.line, edge.span.column)
        at = edge.span
        for endpoint, node in ((table.source, source), (table.destination, destination)):
            for edge_column, node_column in zip(endpoint.key, endpoint.references):
                predicates.append(
                    (
                        _sql.binary(
                            _column(edge_binding, edge_column, at),
                            "Eq",
                            _column(node, node_column, at),
                        ),
                        {edge_binding.alias, node.alias},
                    )
                )

    # --- RETURN ----------------------------------------------------------------

    def result(self, query: ast.Query, scope: dict, frm: _From) -> dict:
        statement = query.return_statement
        projection = []
        named: dict[str, ast.Expression] = {}
        for item in statement.items:
            lowered = self.expression(item.expression, scope)
            if item.alias is not None:
                name, at = item.alias.name, item.alias.span
            elif type(item.expression) is ast.PropertyReference:
                name, at = item.expression.property.name, item.expression.property.span
            else:
                raise GqlNotSupportedError(
                    "a RETURN item that isn't a property needs `AS name`; implicit names for "
                    "other expressions are not supported yet",
                    item.span.line,
                    item.span.column,
                )
            if name in named:
                raise GqlSemanticError(
                    f"two RETURN items are both named `{name}`; give one a different `AS` name",
                    at.line,
                    at.column,
                )
            named[name] = item.expression
            projection.append(_sql.select_item(lowered, _sql.ident(name, True, at)))

        group_by = []
        for name in statement.group_by:
            if name.name not in named:
                if name.name in scope:
                    raise GqlNotSupportedError(
                        "grouping by an element variable is not supported yet; group by a "
                        "RETURN item",
                        name.span.line,
                        name.span.column,
                    )
                raise GqlSemanticError(
                    f"`GROUP BY {name.name}` names no RETURN item.{_suggest(name.name, named)}",
                    name.span.line,
                    name.span.column,
                )
            group_by.append(self.expression(named[name.name], scope))
        if not group_by:
            aggregated = [_has_aggregate(item.expression) for item in statement.items]
            if any(aggregated) and not all(aggregated):
                raise GqlNotSupportedError(
                    "RETURN mixes aggregates with other items without GROUP BY, which is not "
                    "supported yet; add `GROUP BY` naming the other items",
                    statement.span.line,
                    statement.span.column,
                )

        order_by = []
        for sort in statement.order_by:
            expression = sort.expression
            if type(expression) is ast.Variable and expression.name in named:
                lowered = _sql.identifier(_sql.ident(expression.name, True, expression.span))
            else:
                lowered = self.expression(expression, scope)
            order_by.append(_sql.sort_item(lowered, sort.descending))

        return _sql.query(
            at=statement.span,
            distinct=statement.distinct,
            projection=projection,
            relation=frm.relation,
            joins=frm.joins,
            selection=_sql.conjunction(frm.where),
            group_by=group_by,
            order_by=order_by,
            limit=self.count(statement.limit),
            offset=self.count(statement.offset),
        )

    def count(self, value) -> dict | None:
        if value is None:
            return None
        if type(value) is ast.Parameter:
            return _sql.identifier(_sql.ident("$" + value.name, False, value.span))
        return _sql.number(value.text, value.span)

    # --- expressions -----------------------------------------------------------

    def binding_for(self, variable: ast.Variable, scope: dict, outer: dict | None) -> _Binding:
        if variable.name in scope:
            return scope[variable.name]
        if outer is not None and variable.name in outer:
            raise GqlNotSupportedError(
                f"this OPTIONAL MATCH refers to `{variable.name}`, which its own pattern "
                "doesn't mention; that is not supported yet",
                variable.span.line,
                variable.span.column,
            )
        raise GqlSemanticError(
            f"`{variable.name}` is not a variable here.{_suggest(variable.name, scope)}",
            variable.span.line,
            variable.span.column,
        )

    def expression(self, e, scope: dict, outer: dict | None = None) -> dict:
        kind = type(e)
        at = e.span
        if kind is ast.PropertyReference:
            if type(e.subject) is not ast.Variable:
                raise GqlNotSupportedError(
                    "property access on anything but an element variable is not supported yet",
                    at.line,
                    at.column,
                )
            binding = self.binding_for(e.subject, scope, outer)
            properties = dict(binding.element.properties)
            column = properties.get(e.property.name)
            if column is None:
                listed = ", ".join(f"`{p}`" for p in properties) or "none"
                raise GqlSemanticError(
                    f"`{binding.element.label}` has no property `{e.property.name}` "
                    f"(its properties: {listed}).{_suggest(e.property.name, properties)}",
                    e.property.span.line,
                    e.property.span.column,
                )
            return _column(binding, column, e.property.span)
        if kind is ast.Variable:
            self.binding_for(e, scope, outer)  # raises when it isn't an element variable
            raise GqlNotSupportedError(
                f"using the element variable `{e.name}` as a value is not supported yet; "
                "use one of its properties",
                at.line,
                at.column,
            )
        if kind is ast.BinaryOperation:
            return _sql.binary(
                self.expression(e.left, scope, outer),
                _BINARY_OPERATORS[e.operator],
                self.expression(e.right, scope, outer),
            )
        if kind is ast.UnaryOperation:
            return _sql.unary(
                _UNARY_OPERATORS[e.operator], self.expression(e.operand, scope, outer)
            )
        if kind is ast.IsNull:
            return _sql.is_null(self.expression(e.operand, scope, outer), e.negated)
        if (
            kind is ast.IntegerLiteral
            or kind is ast.ExactNumberLiteral
            or kind is ast.ApproximateNumberLiteral
        ):
            return _sql.number(e.text, at)
        if kind is ast.StringLiteral:
            return _sql.string(e.value, at)
        if kind is ast.BooleanLiteral:
            return _sql.boolean(e.value, at)
        if kind is ast.NullLiteral:
            return _sql.null(at)
        if kind is ast.Parameter:
            return _sql.identifier(_sql.ident("$" + e.name, False, at))
        if kind is ast.TemporalLiteral:
            if e.type_name == "DURATION":
                return self.duration(e)
            return _sql.cast_string(e.value, _temporal_type(e.type_name), at)
        if kind is ast.CurrentTemporal:
            return _sql.function(_sql.ident(e.name, False, at), None)
        if kind is ast.CountStar:
            return _sql.count_star(_sql.ident("COUNT", False, at))
        if kind is ast.FunctionCall:
            name = _RENAMED_FUNCTIONS.get(e.name.upper(), e.name)
            arguments = [self.expression(a, scope, outer) for a in e.arguments]
            return _sql.function(_sql.ident(name, False, at), arguments, e.distinct)
        if kind is ast.Case:
            operand = None if e.operand is None else self.expression(e.operand, scope, outer)
            conditions = [
                (
                    self.expression(w.condition, scope, outer),
                    self.expression(w.result, scope, outer),
                )
                for w in e.when_clauses
            ]
            otherwise = (
                None if e.else_result is None else self.expression(e.else_result, scope, outer)
            )
            return _sql.case(operand, conditions, otherwise, at)
        raise AssertionError(
            f"no lowering for {kind.__name__}"
        )  # every parser node is handled above

    def duration(self, e: ast.TemporalLiteral) -> dict:
        match = _DURATION.match(e.value)
        if match is None:
            raise GqlNotSupportedError(
                f"the duration '{e.value}' is not supported yet; only one whole-number unit "
                "is, such as 'P30D' or 'PT12H'",
                e.span.line,
                e.span.column,
            )
        date_amount, date_unit, time_amount, time_unit = match.groups()
        if date_unit is not None:
            amount = int(date_amount) * 7 if date_unit == "W" else int(date_amount)
            return _sql.interval(str(amount), _DATE_FIELDS[date_unit], e.span)
        return _sql.interval(time_amount, _TIME_FIELDS[time_unit], e.span)


def _has_aggregate(e) -> bool:
    kind = type(e)
    if kind is ast.CountStar:
        return True
    if kind is ast.FunctionCall:
        return e.name.upper() in _AGGREGATES or any(_has_aggregate(a) for a in e.arguments)
    if kind is ast.BinaryOperation:
        return _has_aggregate(e.left) or _has_aggregate(e.right)
    if kind is ast.UnaryOperation or kind is ast.IsNull:
        return _has_aggregate(e.operand)
    if kind is ast.Case:
        parts = [e.operand, e.else_result]
        for w in e.when_clauses:
            parts += [w.condition, w.result]
        return any(_has_aggregate(p) for p in parts if p is not None)
    return False
