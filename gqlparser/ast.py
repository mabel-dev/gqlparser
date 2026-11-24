"""Syntax tree nodes. Public API: every change is listed in CHANGELOG.md.

Every node is a frozen dataclass whose last field is its `span`. Numbers keep
their source text (`IntegerLiteral.text`), so the tree never rounds or
converts a value: that is the consumer's decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True, slots=True)
class Span:
    """Where a node sits in the source text.

    `start` and `end` are character offsets (end exclusive); `line` and
    `column` are 1-based and describe `start`.
    """

    start: int
    end: int
    line: int
    column: int


# --- names --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Identifier:
    """A name as written: a graph, label, property, variable or alias."""

    name: str
    span: Span


# --- expressions ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IntegerLiteral:
    text: str
    span: Span


@dataclass(frozen=True, slots=True)
class ExactNumberLiteral:
    """`1.5`, `.5`, `1.`"""

    text: str
    span: Span


@dataclass(frozen=True, slots=True)
class ApproximateNumberLiteral:
    """`1e3`, `1.5E-2`"""

    text: str
    span: Span


@dataclass(frozen=True, slots=True)
class StringLiteral:
    value: str
    span: Span


@dataclass(frozen=True, slots=True)
class BooleanLiteral:
    value: bool
    span: Span


@dataclass(frozen=True, slots=True)
class NullLiteral:
    span: Span


@dataclass(frozen=True, slots=True)
class TemporalLiteral:
    """`DATE '2026-01-01'`, `DURATION 'P30D'`: `type_name` is upper case."""

    type_name: str
    value: str
    span: Span


@dataclass(frozen=True, slots=True)
class CurrentTemporal:
    """`CURRENT_DATE`, `CURRENT_TIME`, `CURRENT_TIMESTAMP`: `name` is upper case."""

    name: str
    span: Span


@dataclass(frozen=True, slots=True)
class Parameter:
    """`$name`; `name` excludes the `$`."""

    name: str
    span: Span


@dataclass(frozen=True, slots=True)
class Variable:
    """A reference to a variable bound by a pattern, or to a `RETURN` alias."""

    name: str
    span: Span


@dataclass(frozen=True, slots=True)
class PropertyReference:
    """`subject.property`"""

    subject: Expression
    property: Identifier
    span: Span


@dataclass(frozen=True, slots=True)
class UnaryOperation:
    """`operator` is `NOT`, `-` or `+`."""

    operator: str
    operand: Expression
    span: Span


@dataclass(frozen=True, slots=True)
class BinaryOperation:
    """`operator` is one of `OR XOR AND = <> < <= > >= || + - * / %` (words upper case)."""

    operator: str
    left: Expression
    right: Expression
    span: Span


@dataclass(frozen=True, slots=True)
class IsNull:
    """`operand IS NULL`, or `operand IS NOT NULL` when `negated`."""

    operand: Expression
    negated: bool
    span: Span


@dataclass(frozen=True, slots=True)
class FunctionCall:
    """`name(arguments)`. `name` is as written; the parser does not check it
    against GQL's built-in functions, and allows `DISTINCT` on any call."""

    name: str
    arguments: tuple[Expression, ...]
    distinct: bool
    span: Span


@dataclass(frozen=True, slots=True)
class CountStar:
    """`COUNT(*)`"""

    span: Span


@dataclass(frozen=True, slots=True)
class WhenClause:
    """In a simple `CASE`, `condition` is the value compared with the operand."""

    condition: Expression
    result: Expression
    span: Span


@dataclass(frozen=True, slots=True)
class Case:
    """Simple `CASE x WHEN ...` when `operand` is set, searched `CASE WHEN ...` otherwise."""

    operand: Expression | None
    when_clauses: tuple[WhenClause, ...]
    else_result: Expression | None
    span: Span


Expression = (
    IntegerLiteral
    | ExactNumberLiteral
    | ApproximateNumberLiteral
    | StringLiteral
    | BooleanLiteral
    | NullLiteral
    | TemporalLiteral
    | CurrentTemporal
    | Parameter
    | Variable
    | PropertyReference
    | UnaryOperation
    | BinaryOperation
    | IsNull
    | FunctionCall
    | CountStar
    | Case
)


# --- patterns -----------------------------------------------------------------


class EdgeDirection(Enum):
    POINTING_RIGHT = "pointing_right"  # (a)-[e]->(b)
    POINTING_LEFT = "pointing_left"  # (a)<-[e]-(b)


@dataclass(frozen=True, slots=True)
class NodePattern:
    """`(variable:label WHERE where)`; each part optional."""

    variable: Identifier | None
    label: Identifier | None
    where: Expression | None
    span: Span


@dataclass(frozen=True, slots=True)
class Quantifier:
    """`{minimum, maximum}` after an edge pattern: the edge repeats `minimum` to `maximum`
    times in a row. `{n}` reads as `{n, n}`. Only bounded quantifiers with `minimum >= 1`
    are parsed."""

    minimum: int
    maximum: int
    span: Span


@dataclass(frozen=True, slots=True)
class EdgePattern:
    """`-[variable:label WHERE where]->` or `<-[...]-`, then an optional quantifier
    (`->{1,3}`); each part optional. A quantified edge's `where` applies to each repeat."""

    variable: Identifier | None
    label: Identifier | None
    where: Expression | None
    direction: EdgeDirection
    quantifier: Quantifier | None
    span: Span


@dataclass(frozen=True, slots=True)
class PathPattern:
    """Alternating node and edge patterns, starting and ending with a node."""

    elements: tuple[NodePattern | EdgePattern, ...]
    span: Span


# --- statements ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MatchStatement:
    """`[OPTIONAL] MATCH path, path, ... [WHERE where]`"""

    optional: bool
    paths: tuple[PathPattern, ...]
    where: Expression | None
    span: Span


@dataclass(frozen=True, slots=True)
class ReturnItem:
    expression: Expression
    alias: Identifier | None
    span: Span


@dataclass(frozen=True, slots=True)
class SortSpecification:
    expression: Expression
    descending: bool
    span: Span


@dataclass(frozen=True, slots=True)
class ReturnStatement:
    """`RETURN [DISTINCT] items [GROUP BY ...] [ORDER BY ...] [OFFSET n] [LIMIT n]`

    `offset` and `limit` are an `IntegerLiteral` or a `Parameter`. `SKIP` is
    read as `OFFSET`.
    """

    distinct: bool
    items: tuple[ReturnItem, ...]
    group_by: tuple[Identifier, ...]
    order_by: tuple[SortSpecification, ...]
    offset: IntegerLiteral | Parameter | None
    limit: IntegerLiteral | Parameter | None
    span: Span


@dataclass(frozen=True, slots=True)
class Query:
    """`[USE graph] match_statements RETURN ...`"""

    graph: tuple[Identifier, ...] | None  # the graph name's parts: `USE ws.coll.g` gives three
    match_statements: tuple[MatchStatement, ...]
    return_statement: ReturnStatement
    span: Span
