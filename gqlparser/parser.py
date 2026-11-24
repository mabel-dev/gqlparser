"""GQL parser: tokens to a `Query` syntax tree, by recursive descent.

Constructs this release recognises but does not support raise
`GqlNotSupportedError` naming the construct. Anything else the parser cannot
read raises `GqlSyntaxError`.
"""

from __future__ import annotations

from gqlparser import ast
from gqlparser.ast import Span
from gqlparser.errors import GqlNotSupportedError
from gqlparser.errors import GqlSyntaxError
from gqlparser.lexer import Token
from gqlparser.lexer import TokenKind
from gqlparser.lexer import tokenize

# Words with a meaning inside expressions: never read as a variable or alias name.
_EXPRESSION_KEYWORDS = frozenset(
    {"AND", "OR", "XOR", "NOT", "IS", "NULL", "TRUE", "FALSE", "UNKNOWN", "CASE", "WHEN",
     "THEN", "ELSE", "END", "DISTINCT", "ALL", "AS", "EXISTS", "WHERE"}
)  # fmt: skip
_TEMPORAL_LITERAL_TYPES = frozenset({"DATE", "TIME", "DATETIME", "TIMESTAMP", "DURATION"})
_CURRENT_TEMPORAL = frozenset({"CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP"})
# GQL statements that may appear where this release expects MATCH or RETURN.
_STATEMENTS_NOT_SUPPORTED = frozenset(
    {"LET", "FILTER", "FOR", "ORDER", "OFFSET", "SKIP", "LIMIT", "CALL", "OPTIONAL", "USE",
     "SELECT", "INSERT", "SET", "REMOVE", "DELETE", "DETACH", "NODETACH", "CREATE", "DROP",
     "SESSION", "START", "COMMIT", "ROLLBACK", "FINISH", "AT", "YIELD"}
)  # fmt: skip
_COMPOSITION_NOT_SUPPORTED = frozenset({"NEXT", "UNION", "EXCEPT", "INTERSECT", "OTHERWISE"})
_PATH_PREFIXES_NOT_SUPPORTED = frozenset(
    {"ANY", "ALL", "SHORTEST", "WALK", "TRAIL", "SIMPLE", "ACYCLIC"}
)
_MATCH_MODES_NOT_SUPPORTED = frozenset({"DIFFERENT", "REPEATABLE"})
_COMPARISON_OPERATORS = frozenset({"=", "<>", "<", "<=", ">", ">="})
_QUANTIFIER_STARTS = frozenset({"{", "*", "+", "?"})
_NAME_KINDS = frozenset({TokenKind.WORD, TokenKind.ACCENT_QUOTED, TokenKind.DOUBLE_QUOTED})
# Each nested expression costs ~18 Python frames (one per precedence level), so refuse
# cleanly at 32 (~600 frames), leaving room under Python's default limit (1000) for the
# caller's own stack.
# shortcut: one function per precedence level caps nesting at 32; upgrade to precedence
# climbing (~3 frames per level) if real queries nest deeper.
_MAX_NESTING = 32
# A quantified edge lowers to one chain of joins per length, so the SQL grows with the square
# of the upper bound.
# shortcut: bounds above 16 are refused; upgrade with a native path operator rather than more joins.
_MAX_QUANTIFIER = 16


def parse(text: str) -> ast.Query:
    """Parse one GQL query into a syntax tree."""
    return _Parser(tokenize(text)).query()


def _found(token: Token) -> str:
    kind = token.kind
    if kind is TokenKind.END:
        return "the end of the query"
    if kind is TokenKind.SINGLE_QUOTED:
        return f"'{token.value}'"
    if kind is TokenKind.DOUBLE_QUOTED:
        return f'"{token.value}"'
    if kind is TokenKind.PARAMETER:
        return f"`${token.value}`"
    return f"`{token.value}`"


class _Parser:
    __slots__ = ("depth", "index", "previous_end", "tokens")

    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.index = 0
        self.previous_end = 0
        self.depth = 0

    # --- token helpers ---------------------------------------------------------

    def peek(self, offset: int = 0) -> Token:
        return self.tokens[min(self.index + offset, len(self.tokens) - 1)]

    def advance(self) -> Token:
        token = self.tokens[self.index]
        if token.kind is not TokenKind.END:
            self.index += 1
            self.previous_end = token.span.end
        return token

    def is_word(self, word: str, offset: int = 0) -> bool:
        token = self.peek(offset)
        return token.kind is TokenKind.WORD and token.value.upper() == word

    def is_punct(self, punctuation: str, offset: int = 0) -> bool:
        token = self.peek(offset)
        return token.kind is TokenKind.PUNCTUATION and token.value == punctuation

    def accept_word(self, word: str) -> Token | None:
        return self.advance() if self.is_word(word) else None

    def accept_punct(self, punctuation: str) -> Token | None:
        return self.advance() if self.is_punct(punctuation) else None

    def expect_word(self, word: str) -> Token:
        if not self.is_word(word):
            self.syntax(f"expected `{word}`, found {_found(self.peek())}")
        return self.advance()

    def expect_punct(self, punctuation: str, purpose: str = "") -> Token:
        if not self.is_punct(punctuation):
            self.syntax(f"expected `{punctuation}`{purpose}, found {_found(self.peek())}")
        return self.advance()

    def span_from(self, start: Span) -> Span:
        return Span(start.start, self.previous_end, start.line, start.column)

    def syntax(self, message: str, token: Token | None = None):
        token = token or self.peek()
        raise GqlSyntaxError(message, token.span.line, token.span.column)

    def unsupported(self, message: str, token: Token | None = None):
        token = token or self.peek()
        raise GqlNotSupportedError(message, token.span.line, token.span.column)

    def require_adjacent(self, first: Token, second: Token) -> None:
        if first.span.end != second.span.start:
            self.syntax(f"write `{first.value}{second.value}` without spaces", second)

    # --- names -----------------------------------------------------------------

    def name(self, what: str) -> ast.Identifier:
        """A graph, label or property name: any word or delimited identifier."""
        token = self.peek()
        if token.kind not in _NAME_KINDS:
            self.syntax(f"expected {what}, found {_found(token)}")
        self.advance()
        return ast.Identifier(token.value, token.span)

    def binding_name(self, what: str) -> ast.Identifier:
        """A variable or alias: a word that is not an expression keyword, or a delimited identifier."""
        token = self.peek()
        if token.kind is TokenKind.WORD and token.value.upper() in _EXPRESSION_KEYWORDS:
            self.syntax(
                f"`{token.value}` is a reserved word and cannot be {what}; "
                f"quote it as `{token.value}` with accents (`` ` ``)"
            )
        return self.name(what)

    # --- query -----------------------------------------------------------------

    def query(self) -> ast.Query:
        first = self.peek()
        graph = None
        if self.accept_word("USE"):
            graph = [self.name("a graph name")]
            while self.accept_punct("."):
                graph.append(self.name("a graph name"))
            graph = tuple(graph)
        statements = []
        while not self.is_word("RETURN"):
            if self.is_word("MATCH") or (self.is_word("OPTIONAL") and self.is_word("MATCH", 1)):
                statements.append(self.match_statement())
            else:
                self.refuse_statement()
        if not statements:
            self.unsupported("queries without `MATCH` are not supported yet")
        return_statement = self.return_statement()
        token = self.peek()
        if token.kind is not TokenKind.END:
            if token.kind is TokenKind.WORD and token.value.upper() in _COMPOSITION_NOT_SUPPORTED:
                self.unsupported(
                    f"`{token.value.upper()}` (composite queries) is not supported yet"
                )
            self.syntax(f"expected the end of the query, found {_found(token)}")
        return ast.Query(graph, tuple(statements), return_statement, self.span_from(first.span))

    def refuse_statement(self):
        token = self.peek()
        if token.kind is TokenKind.WORD:
            word = token.value.upper()
            if word == "OPTIONAL":
                if self.is_punct("{", 1) or self.is_punct("(", 1):
                    self.unsupported(
                        "`OPTIONAL` blocks are not supported yet; write `OPTIONAL MATCH`"
                    )
                self.syntax(f"expected `MATCH` or `RETURN`, found {_found(token)}")
            if word in _STATEMENTS_NOT_SUPPORTED:
                self.unsupported(f"the `{word}` statement here is not supported yet")
        self.syntax(f"expected `MATCH` or `RETURN`, found {_found(token)}")

    # --- MATCH -----------------------------------------------------------------

    def match_statement(self) -> ast.MatchStatement:
        first = self.peek()
        optional = self.accept_word("OPTIONAL") is not None
        self.expect_word("MATCH")
        token = self.peek()
        if token.kind is TokenKind.WORD and token.value.upper() in _MATCH_MODES_NOT_SUPPORTED:
            self.unsupported(
                "match modes (`DIFFERENT EDGES`, `REPEATABLE ELEMENTS`) are not supported yet"
            )
        paths = [self.path_pattern()]
        while self.accept_punct(","):
            paths.append(self.path_pattern())
        if self.is_word("KEEP"):
            self.unsupported("`KEEP` is not supported yet")
        where = self.expression() if self.accept_word("WHERE") else None
        if self.is_word("YIELD"):
            self.unsupported("`YIELD` is not supported yet")
        return ast.MatchStatement(optional, tuple(paths), where, self.span_from(first.span))

    def path_pattern(self) -> ast.PathPattern:
        first = self.peek()
        if first.kind in _NAME_KINDS and self.is_punct("=", 1):
            self.unsupported("path variables (`p = ...`) are not supported yet")
        if first.kind is TokenKind.WORD and first.value.upper() in _PATH_PREFIXES_NOT_SUPPORTED:
            self.unsupported(
                f"path search prefixes and path modes (`{first.value.upper()}`) are not supported yet"
            )
        elements: list[ast.NodePattern | ast.EdgePattern] = [self.node_pattern()]
        while (edge := self.edge_pattern_or_none()) is not None:
            elements.append(edge)
            elements.append(self.node_pattern())
        return ast.PathPattern(tuple(elements), self.span_from(first.span))

    def node_pattern(self) -> ast.NodePattern:
        first = self.expect_punct("(", " to start a node pattern")
        if self.is_punct("("):
            self.unsupported("parenthesized path patterns are not supported yet")
        variable, label, where = self.element_filler()
        self.expect_punct(")", " to close the node pattern")
        return ast.NodePattern(variable, label, where, self.span_from(first.span))

    def element_filler(self):
        """The inside of `( )` or `[ ]`: [variable] [:label] [WHERE expression]."""
        variable = None
        token = self.peek()
        if token.kind in _NAME_KINDS and not (self.is_word("WHERE") or self.is_word("IS")):
            variable = self.binding_name("a variable name")
        if self.is_word("IS"):
            self.unsupported("`IS` label expressions are not supported yet; write `:Label`")
        label = None
        if self.accept_punct(":"):
            if self.is_punct("!") or self.is_punct("%") or self.is_punct("("):
                self.unsupported("label expressions are not supported yet")
            label = self.name("a label")
            if self.is_punct("|") or self.is_punct("&"):
                self.unsupported("label expressions are not supported yet")
            if self.is_punct(":"):
                self.unsupported("more than one label on an element is not supported yet")
        if self.is_punct("{"):
            self.unsupported("property specifications (`{key: value}`) are not supported yet")
        where = self.expression() if self.accept_word("WHERE") else None
        return variable, label, where

    def edge_pattern_or_none(self) -> ast.EdgePattern | None:
        first = self.peek()
        if self.is_punct("-"):
            if not self.is_punct("[", 1):
                self.unsupported("abbreviated edge patterns (`-`) are not supported yet")
            pointing_right = True
        elif self.is_punct("<-"):
            if not self.is_punct("[", 1):
                self.unsupported("abbreviated edge patterns (`<-`, `<->`) are not supported yet")
            pointing_right = False
        elif self.is_punct("->"):
            self.unsupported("abbreviated edge patterns (`->`) are not supported yet")
        elif self.is_punct("~") or (self.is_punct("<") and self.is_punct("~", 1)):
            self.unsupported("undirected edge patterns (`~`) are not supported yet")
        else:
            return None
        self.require_adjacent(first, self.peek(1))
        self.advance()
        self.advance()
        variable, label, where = self.element_filler()
        close = self.expect_punct("]", " to close the edge pattern")
        arrow = self.peek()
        if pointing_right:
            if self.is_punct("-"):
                self.unsupported("edges in any direction (`-[...]-`) are not supported yet")
            if not self.is_punct("->"):
                self.syntax(f"expected `]->` to close the edge pattern, found {_found(arrow)}")
            direction = ast.EdgeDirection.POINTING_RIGHT
        else:
            if self.is_punct("->"):
                self.unsupported("edges pointing left or right (`<-[...]->`) are not supported yet")
            if not self.is_punct("-"):
                self.syntax(f"expected `]-` to close the edge pattern, found {_found(arrow)}")
            direction = ast.EdgeDirection.POINTING_LEFT
        self.require_adjacent(close, arrow)
        self.advance()
        quantifier = self.quantifier_or_none()
        return ast.EdgePattern(
            variable, label, where, direction, quantifier, self.span_from(first.span)
        )

    def quantifier_or_none(self) -> ast.Quantifier | None:
        """`{m}` or `{m,n}` after an edge. The rest of GQL's quantifiers are unbounded or can
        be zero-length, which need more than a fixed set of joins, so they are refused."""
        following = self.peek()
        if following.kind is not TokenKind.PUNCTUATION or following.value not in _QUANTIFIER_STARTS:
            return None
        if following.value != "{":
            self.unsupported(
                f"the quantifier `{following.value}` is not supported yet; "
                "only bounded `{m}` and `{m,n}` quantifiers are"
            )
        opening = self.advance()
        minimum = self.quantifier_bound(allow_missing=True)
        if self.accept_punct(","):
            if self.is_punct("}"):
                self.unsupported("open-ended quantifiers (`{m,}`) are not supported yet")
            maximum = self.quantifier_bound()
        else:
            maximum = minimum
        if minimum is None:
            self.unsupported("quantifiers without a lower bound (`{,n}`) are not supported yet", opening)
        self.expect_punct("}", " to close the quantifier")
        if minimum < 1:
            self.unsupported(
                "quantifiers that allow zero repeats (`{0,n}`) are not supported yet", opening
            )
        if maximum < minimum:
            self.syntax(f"the quantifier's upper bound {maximum} is below its lower bound {minimum}", opening)
        if maximum > _MAX_QUANTIFIER:
            self.unsupported(
                f"quantifier bounds above {_MAX_QUANTIFIER} are not supported yet", opening
            )
        return ast.Quantifier(minimum, maximum, self.span_from(opening.span))

    def quantifier_bound(self, allow_missing: bool = False) -> int | None:
        token = self.peek()
        if token.kind is TokenKind.INTEGER:
            self.advance()
            return int(token.value)
        if allow_missing and self.is_punct(","):
            return None
        self.syntax(f"expected a whole number, found {_found(token)}")

    # --- RETURN ----------------------------------------------------------------

    def return_statement(self) -> ast.ReturnStatement:
        first = self.expect_word("RETURN")
        distinct = self.accept_word("DISTINCT") is not None
        if not distinct:
            self.accept_word("ALL")  # ALL is the default
        if self.is_punct("*"):
            self.unsupported("`RETURN *` is not supported yet")
        items = [self.return_item()]
        while self.accept_punct(","):
            items.append(self.return_item())

        group_by = []
        if self.accept_word("GROUP"):
            self.expect_word("BY")
            if self.is_punct("("):
                self.unsupported("the empty grouping set `GROUP BY ()` is not supported yet")
            group_by.append(self.binding_name("a grouping name"))
            while self.accept_punct(","):
                group_by.append(self.binding_name("a grouping name"))

        order_by = []
        if self.accept_word("ORDER"):
            self.expect_word("BY")
            order_by.append(self.sort_specification())
            while self.accept_punct(","):
                order_by.append(self.sort_specification())

        offset = limit = None
        if self.accept_word("OFFSET") or self.accept_word("SKIP"):
            offset = self.count()
        if self.accept_word("LIMIT"):
            limit = self.count()
        if self.is_word("OFFSET") or self.is_word("SKIP"):
            self.unsupported("`OFFSET` after `LIMIT` is not supported yet")

        return ast.ReturnStatement(
            distinct,
            tuple(items),
            tuple(group_by),
            tuple(order_by),
            offset,
            limit,
            self.span_from(first.span),
        )

    def return_item(self) -> ast.ReturnItem:
        first = self.peek()
        expression = self.expression()
        alias = self.binding_name("an alias") if self.accept_word("AS") else None
        return ast.ReturnItem(expression, alias, self.span_from(first.span))

    def sort_specification(self) -> ast.SortSpecification:
        first = self.peek()
        expression = self.expression()
        descending = False
        if self.accept_word("DESC") or self.accept_word("DESCENDING"):
            descending = True
        else:
            self.accept_word("ASC") or self.accept_word("ASCENDING")
        if self.is_word("NULLS"):
            self.unsupported("`NULLS FIRST` / `NULLS LAST` is not supported yet")
        return ast.SortSpecification(expression, descending, self.span_from(first.span))

    def count(self) -> ast.IntegerLiteral | ast.Parameter:
        token = self.peek()
        if token.kind is TokenKind.INTEGER:
            self.advance()
            return ast.IntegerLiteral(token.value, token.span)
        if token.kind is TokenKind.PARAMETER:
            self.advance()
            return ast.Parameter(token.value, token.span)
        self.syntax(f"expected a whole number or a parameter, found {_found(token)}")

    # --- expressions, lowest precedence first ----------------------------------

    def nest(self) -> None:
        """Count one level of expression nesting; the caller decrements on the way out."""
        self.depth += 1
        if self.depth > _MAX_NESTING:
            self.unsupported(
                f"expressions nested more than {_MAX_NESTING} deep are not supported yet"
            )

    def expression(self) -> ast.Expression:
        self.nest()
        result = self.disjunction()
        self.depth -= 1
        return result

    def binary_level(self, words: tuple[str, ...], operand) -> ast.Expression:
        left = operand()
        while True:
            word = next((w for w in words if self.is_word(w)), None)
            if word is None:
                return left
            self.advance()
            right = operand()
            left = ast.BinaryOperation(word, left, right, self.span_from(left.span))

    def disjunction(self) -> ast.Expression:
        return self.binary_level(("OR",), self.exclusive_disjunction)

    def exclusive_disjunction(self) -> ast.Expression:
        return self.binary_level(("XOR",), self.conjunction)

    def conjunction(self) -> ast.Expression:
        return self.binary_level(("AND",), self.negation)

    def negation(self) -> ast.Expression:
        first = self.accept_word("NOT")
        if first is None:
            return self.comparison()
        self.nest()
        operand = self.negation()
        self.depth -= 1
        return ast.UnaryOperation("NOT", operand, self.span_from(first.span))

    def comparison(self) -> ast.Expression:
        left = self.concatenation()
        if self.is_word("IS"):
            return self.is_predicate(left)
        if self.is_punct("!") and self.is_punct("=", 1):
            self.unsupported("`!=` is not supported yet; write `<>`")
        operator = self.peek()
        if (
            operator.kind is not TokenKind.PUNCTUATION
            or operator.value not in _COMPARISON_OPERATORS
        ):
            return left
        self.advance()
        right = self.concatenation()
        following = self.peek()
        if self.is_word("IS") or (
            following.kind is TokenKind.PUNCTUATION and following.value in _COMPARISON_OPERATORS
        ):
            self.unsupported("chained comparisons are not supported yet; add parentheses")
        return ast.BinaryOperation(operator.value, left, right, self.span_from(left.span))

    def is_predicate(self, operand: ast.Expression) -> ast.Expression:
        self.advance()  # IS
        negated = self.accept_word("NOT") is not None
        if self.accept_word("NULL"):
            return ast.IsNull(operand, negated, self.span_from(operand.span))
        token = self.peek()
        if token.kind is TokenKind.WORD:
            not_ = "NOT " if negated else ""
            self.unsupported(f"`IS {not_}{token.value.upper()}` is not supported yet")
        self.syntax(f"expected `NULL` after `IS`, found {_found(token)}")

    def concatenation(self) -> ast.Expression:
        return self.punct_level(("||",), self.additive)

    def additive(self) -> ast.Expression:
        return self.punct_level(("+", "-"), self.multiplicative)

    def multiplicative(self) -> ast.Expression:
        return self.punct_level(("*", "/", "%"), self.unary)

    def punct_level(self, operators: tuple[str, ...], operand) -> ast.Expression:
        left = operand()
        while True:
            token = self.peek()
            if token.kind is not TokenKind.PUNCTUATION or token.value not in operators:
                return left
            self.advance()
            right = operand()
            left = ast.BinaryOperation(token.value, left, right, self.span_from(left.span))

    def unary(self) -> ast.Expression:
        token = self.peek()
        if token.kind is TokenKind.PUNCTUATION and token.value in ("-", "+"):
            self.advance()
            self.nest()
            operand = self.unary()
            self.depth -= 1
            return ast.UnaryOperation(token.value, operand, self.span_from(token.span))
        return self.property_access()

    def property_access(self) -> ast.Expression:
        subject = self.primary()
        while self.accept_punct("."):
            prop = self.name("a property name")
            subject = ast.PropertyReference(subject, prop, self.span_from(subject.span))
        return subject

    def primary(self) -> ast.Expression:
        token = self.peek()
        kind = token.kind
        if kind is TokenKind.INTEGER:
            self.advance()
            return ast.IntegerLiteral(token.value, token.span)
        if kind is TokenKind.EXACT_NUMBER:
            self.advance()
            return ast.ExactNumberLiteral(token.value, token.span)
        if kind is TokenKind.APPROXIMATE_NUMBER:
            self.advance()
            return ast.ApproximateNumberLiteral(token.value, token.span)
        if kind is TokenKind.SINGLE_QUOTED:
            self.advance()
            return ast.StringLiteral(token.value, token.span)
        if kind is TokenKind.DOUBLE_QUOTED:
            self.unsupported(
                "double-quoted text in an expression is not supported yet, because it can be "
                "a string or a name; write '...' for a string or `...` for a name"
            )
        if kind is TokenKind.ACCENT_QUOTED:
            self.advance()
            return ast.Variable(token.value, token.span)
        if kind is TokenKind.PARAMETER:
            self.advance()
            return ast.Parameter(token.value, token.span)
        if self.is_punct("("):
            self.advance()
            inner = self.expression()
            self.expect_punct(")", " to close the parenthesis")
            return inner
        if self.is_punct("["):
            self.unsupported("list literals are not supported yet")
        if self.is_punct("{"):
            self.unsupported("record literals are not supported yet")
        if kind is TokenKind.WORD:
            return self.word_primary(token)
        self.syntax(f"expected an expression, found {_found(token)}")

    def word_primary(self, token: Token) -> ast.Expression:
        word = token.value.upper()
        if word in ("TRUE", "FALSE"):
            self.advance()
            return ast.BooleanLiteral(word == "TRUE", token.span)
        if word == "NULL":
            self.advance()
            return ast.NullLiteral(token.span)
        if word == "UNKNOWN":
            self.unsupported("the `UNKNOWN` literal is not supported yet")
        if word == "EXISTS":
            self.unsupported("`EXISTS` is not supported yet")
        if word == "CASE":
            return self.case()
        if word in _TEMPORAL_LITERAL_TYPES and self.peek(1).kind is TokenKind.SINGLE_QUOTED:
            self.advance()
            value = self.advance()
            return ast.TemporalLiteral(word, value.value, self.span_from(token.span))
        if word in _EXPRESSION_KEYWORDS:
            self.syntax(f"expected an expression, found {_found(token)}")
        if self.is_punct("(", 1):
            return self.function_call()
        if word in _CURRENT_TEMPORAL:
            self.advance()
            return ast.CurrentTemporal(word, token.span)
        if word in _TEMPORAL_LITERAL_TYPES:
            self.syntax(f"expected a quoted value after `{word}`, found {_found(self.peek(1))}")
        self.advance()
        return ast.Variable(token.value, token.span)

    def function_call(self) -> ast.Expression:
        name = self.advance()
        self.advance()  # (
        if name.value.upper() == "COUNT" and self.is_punct("*"):
            self.advance()
            self.expect_punct(")", " to close `COUNT(*)`")
            return ast.CountStar(self.span_from(name.span))
        distinct = self.accept_word("DISTINCT") is not None
        if not distinct:
            self.accept_word("ALL")  # ALL is the default
        arguments = []
        if not self.is_punct(")"):
            arguments.append(self.expression())
            while self.accept_punct(","):
                arguments.append(self.expression())
        elif distinct:
            self.syntax("`DISTINCT` needs an argument")
        self.expect_punct(")", f" to close the call to `{name.value}`")
        return ast.FunctionCall(name.value, tuple(arguments), distinct, self.span_from(name.span))

    def case(self) -> ast.Case:
        first = self.advance()  # CASE
        if self.is_word("END"):
            self.syntax("expected `WHEN`, found `END`")
        operand = None if self.is_word("WHEN") else self.expression()
        when_clauses = []
        while self.is_word("WHEN"):
            when = self.advance()
            following = self.peek()
            if operand is not None and (
                self.is_word("IS")
                or (
                    following.kind is TokenKind.PUNCTUATION
                    and following.value in _COMPARISON_OPERATORS
                )
            ):
                self.unsupported("comparisons in a simple `CASE` `WHEN` are not supported yet")
            condition = self.expression()
            if operand is not None and self.is_punct(","):
                self.unsupported("several values in one `WHEN` are not supported yet")
            self.expect_word("THEN")
            result = self.expression()
            when_clauses.append(ast.WhenClause(condition, result, self.span_from(when.span)))
        if not when_clauses:
            self.syntax(f"expected `WHEN`, found {_found(self.peek())}")
        else_result = self.expression() if self.accept_word("ELSE") else None
        self.expect_word("END")
        return ast.Case(operand, tuple(when_clauses), else_result, self.span_from(first.span))
