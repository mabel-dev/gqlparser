"""Property graph definitions: SQL/PGQ `CREATE PROPERTY GRAPH` text to a `PropertyGraph`.

This reads structure only. It cannot check that tables or columns exist (it has
no catalog), so forms that need a table's schema to complete (`PROPERTIES ALL
COLUMNS`, a missing `PROPERTIES` or `KEY` clause, foreign-key defaults) are
refused: use the canonical text an engine's `SHOW CREATE PROPERTY GRAPH` returns.
"""

from __future__ import annotations

from dataclasses import dataclass

from gqlparser.errors import GqlNotSupportedError
from gqlparser.errors import GqlSemanticError
from gqlparser.errors import GqlSyntaxError
from gqlparser.lexer import Token
from gqlparser.lexer import TokenKind
from gqlparser.lexer import tokenize


@dataclass(frozen=True, slots=True)
class SqlName:
    """A SQL identifier as written in the DDL: `quoted` when it was double-quoted."""

    value: str
    quoted: bool


@dataclass(frozen=True, slots=True)
class EdgeEndpoint:
    """`SOURCE KEY (key) REFERENCES vertex (references)`"""

    key: tuple[SqlName, ...]
    vertex: str  # the referenced vertex element's name
    references: tuple[SqlName, ...]


@dataclass(frozen=True, slots=True)
class VertexTable:
    name: str  # element name: the AS name, or the relation's last part
    relation: tuple[SqlName, ...]
    key: tuple[SqlName, ...]
    label: str
    properties: tuple[tuple[str, SqlName], ...]  # (property name, column)


@dataclass(frozen=True, slots=True)
class EdgeTable:
    name: str
    relation: tuple[SqlName, ...]
    key: tuple[SqlName, ...]
    source: EdgeEndpoint
    destination: EdgeEndpoint
    label: str
    properties: tuple[tuple[str, SqlName], ...]


@dataclass(frozen=True, slots=True)
class PropertyGraph:
    name: tuple[SqlName, ...]
    vertex_tables: tuple[VertexTable, ...]
    edge_tables: tuple[EdgeTable, ...]


def parse_property_graph(text: str) -> PropertyGraph:
    """Read one `CREATE PROPERTY GRAPH` statement."""
    reader = _DdlReader(tokenize(text))
    graph = reader.statement()
    _check(graph, reader.positions)
    return graph


def _check(graph: PropertyGraph, positions: dict) -> None:
    """Structural consistency: everything that can be checked without a catalog.

    `positions` maps an element name, or (edge name, "SOURCE"/"DESTINATION"), to the
    (line, column) where it was written, so each error points at the text at fault.
    """
    elements = [*graph.vertex_tables, *graph.edge_tables]
    names: set[str] = set()
    labels: set[str] = set()
    for element in elements:
        if element.name in names:
            raise GqlSemanticError(
                f"two element tables are both named `{element.name}`", *positions[element.name]
            )
        names.add(element.name)
        if element.label in labels:
            raise GqlSemanticError(
                f"the label `{element.label}` is on more than one element table, "
                "which is not supported yet",
                *positions[element.name],
            )
        labels.add(element.label)
        properties = [p for p, _ in element.properties]
        duplicate = next((p for p in properties if properties.count(p) > 1), None)
        if duplicate is not None:
            raise GqlSemanticError(
                f"`{element.name}` declares the property `{duplicate}` twice",
                *positions[element.name],
            )
    vertices = {v.name for v in graph.vertex_tables}
    for edge in graph.edge_tables:
        for end, endpoint in (("SOURCE", edge.source), ("DESTINATION", edge.destination)):
            if endpoint.vertex not in vertices:
                raise GqlSemanticError(
                    f"`{edge.name}` {end} references `{endpoint.vertex}`, "
                    "which is not a vertex table of this graph",
                    *positions[(edge.name, end)],
                )
            if len(endpoint.key) != len(endpoint.references):
                raise GqlSemanticError(
                    f"`{edge.name}` {end} KEY has {len(endpoint.key)} column(s) but "
                    f"REFERENCES has {len(endpoint.references)}",
                    *positions[(edge.name, end)],
                )


def _found(token: Token) -> str:
    return "the end of the statement" if token.kind is TokenKind.END else f"`{token.value}`"


class _DdlReader:
    __slots__ = ("index", "positions", "tokens")

    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.index = 0
        self.positions: dict = {}

    def peek(self) -> Token:
        return self.tokens[self.index]

    def advance(self) -> Token:
        token = self.tokens[self.index]
        if token.kind is not TokenKind.END:
            self.index += 1
        return token

    def is_word(self, word: str) -> bool:
        token = self.peek()
        return token.kind is TokenKind.WORD and token.value.upper() == word

    def is_punct(self, punctuation: str) -> bool:
        token = self.peek()
        return token.kind is TokenKind.PUNCTUATION and token.value == punctuation

    def accept_word(self, word: str) -> bool:
        if self.is_word(word):
            self.advance()
            return True
        return False

    def accept_punct(self, punctuation: str) -> bool:
        if self.is_punct(punctuation):
            self.advance()
            return True
        return False

    def expect_word(self, word: str) -> None:
        if not self.accept_word(word):
            self.syntax(f"expected `{word}`, found {_found(self.peek())}")

    def expect_punct(self, punctuation: str) -> None:
        if not self.accept_punct(punctuation):
            self.syntax(f"expected `{punctuation}`, found {_found(self.peek())}")

    def syntax(self, message: str):
        token = self.peek()
        raise GqlSyntaxError(message, token.span.line, token.span.column)

    def unsupported(self, message: str):
        token = self.peek()
        raise GqlNotSupportedError(message, token.span.line, token.span.column)

    # --- names -----------------------------------------------------------------

    def name(self, what: str) -> SqlName:
        token = self.peek()
        if token.kind is TokenKind.WORD:
            self.advance()
            return SqlName(token.value, False)
        if token.kind is TokenKind.DOUBLE_QUOTED:
            self.advance()
            return SqlName(token.value, True)
        if token.kind is TokenKind.PARAMETER:
            # `$planets`: the lexer reads `$name` as a parameter; in a DDL name it is a relation name
            self.advance()
            return SqlName("$" + token.value, False)
        self.syntax(f"expected {what}, found {_found(token)}")

    def qualified_name(self, what: str) -> tuple[SqlName, ...]:
        parts = [self.name(what)]
        while self.accept_punct("."):
            parts.append(self.name(what))
        return tuple(parts)

    def column_list(self) -> tuple[SqlName, ...]:
        self.expect_punct("(")
        columns = [self.name("a column name")]
        while self.accept_punct(","):
            columns.append(self.name("a column name"))
        self.expect_punct(")")
        return tuple(columns)

    # --- statement -------------------------------------------------------------

    def statement(self) -> PropertyGraph:
        self.expect_word("CREATE")
        if self.accept_word("OR"):
            self.expect_word("REPLACE")
        self.expect_word("PROPERTY")
        self.expect_word("GRAPH")
        if self.accept_word("IF"):
            self.expect_word("NOT")
            self.expect_word("EXISTS")
        name = self.qualified_name("a graph name")

        self.expect_word("VERTEX")
        self.expect_word("TABLES")
        self.expect_punct("(")
        vertices = [self.element(edge=False)]
        while self.accept_punct(","):
            vertices.append(self.element(edge=False))
        self.expect_punct(")")

        edges = []
        if self.accept_word("EDGE"):
            self.expect_word("TABLES")
            self.expect_punct("(")
            edges.append(self.element(edge=True))
            while self.accept_punct(","):
                edges.append(self.element(edge=True))
            self.expect_punct(")")

        self.accept_punct(";")
        if self.peek().kind is not TokenKind.END:
            self.syntax(f"expected the end of the statement, found {_found(self.peek())}")
        return PropertyGraph(name, tuple(vertices), tuple(edges))

    def element(self, edge: bool):
        start = self.peek().span
        relation = self.qualified_name("a table name")
        element_name = relation[-1].value
        if self.accept_word("AS"):
            element_name = self.name("an element name").value
        # the first element of a name keeps its position, so a duplicate points at the original
        self.positions.setdefault(element_name, (start.line, start.column))
        if not self.is_word("KEY"):
            self.unsupported(
                f"`{element_name}` has no KEY clause; a default key needs the table's "
                "schema, which is not supported yet. Use the definition from "
                "SHOW CREATE PROPERTY GRAPH"
            )
        self.advance()
        key = self.column_list()

        source = destination = None
        if edge:
            source = self.endpoint("SOURCE", element_name)
            destination = self.endpoint("DESTINATION", element_name)

        if self.is_word("DEFAULT"):
            self.unsupported("`DEFAULT LABEL` is not supported yet")
        label = element_name  # SQL/PGQ: without a LABEL clause the label is the element name
        properties = None
        if self.accept_word("LABEL"):
            label = self.name("a label").value
            properties = self.properties(element_name)
        if self.is_word("LABEL"):
            self.unsupported("more than one label on an element table is not supported yet")
        if properties is None:
            properties = self.properties(element_name)

        if edge:
            return EdgeTable(element_name, relation, key, source, destination, label, properties)
        return VertexTable(element_name, relation, key, label, properties)

    def endpoint(self, end: str, element_name: str) -> EdgeEndpoint:
        self.expect_word(end)
        if not self.is_word("KEY"):
            self.unsupported(
                f"`{element_name}` {end} has no KEY ... REFERENCES; a default needs a "
                "foreign key from the schema, which is not supported yet. Use the "
                "definition from SHOW CREATE PROPERTY GRAPH"
            )
        self.advance()
        key = self.column_list()
        self.expect_word("REFERENCES")
        at = self.peek().span
        self.positions[(element_name, end)] = (at.line, at.column)
        vertex = self.name("a vertex table name").value
        references = self.column_list()
        return EdgeEndpoint(key, vertex, references)

    def properties(self, element_name: str) -> tuple[tuple[str, SqlName], ...]:
        if self.accept_word("NO"):
            self.expect_word("PROPERTIES")
            return ()
        if not self.is_word("PROPERTIES"):
            self.unsupported(
                f"`{element_name}` has no PROPERTIES clause, which means all columns; "
                "that needs the table's schema, which is not supported yet. Use the "
                "definition from SHOW CREATE PROPERTY GRAPH"
            )
        self.advance()
        if self.is_word("ALL"):
            self.unsupported(
                "`PROPERTIES ALL COLUMNS` needs the table's schema, which is not supported "
                "yet. Use the definition from SHOW CREATE PROPERTY GRAPH"
            )
        if self.is_word("ARE"):
            self.unsupported("`PROPERTIES ARE ...` is not supported yet")
        self.expect_punct("(")
        properties = [self.property()]
        while self.accept_punct(","):
            properties.append(self.property())
        self.expect_punct(")")
        return tuple(properties)

    def property(self) -> tuple[str, SqlName]:
        column = self.name("a column name")
        if not (self.is_punct(",") or self.is_punct(")") or self.is_word("AS")):
            self.unsupported("expression properties are not supported yet; use a column")
        name = column.value
        if self.accept_word("AS"):
            name = self.name("a property name").value
        return name, column
