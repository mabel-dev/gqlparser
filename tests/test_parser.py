import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from gqlparser import GqlNotSupportedError
from gqlparser import GqlSyntaxError
from gqlparser import ast
from gqlparser import parse

D = ast.EdgeDirection


def fmt(e):
    """Compact, unambiguous rendering of an expression (test helper)."""
    name = type(e).__name__
    if name == "BinaryOperation":
        return f"({e.operator} {fmt(e.left)} {fmt(e.right)})"
    if name == "UnaryOperation":
        return f"({e.operator} {fmt(e.operand)})"
    if name == "Variable":
        return e.name
    if name == "PropertyReference":
        return f"{fmt(e.subject)}.{e.property.name}"
    if name in ("IntegerLiteral", "ExactNumberLiteral", "ApproximateNumberLiteral"):
        return f"{name[0]}:{e.text}"
    if name == "StringLiteral":
        return repr(e.value)
    if name == "BooleanLiteral":
        return str(e.value).upper()
    if name == "NullLiteral":
        return "NULL"
    if name == "IsNull":
        return f"({fmt(e.operand)} IS {'NOT ' if e.negated else ''}NULL)"
    if name == "Parameter":
        return f"${e.name}"
    if name == "FunctionCall":
        distinct = "DISTINCT " if e.distinct else ""
        return f"{e.name}({distinct}{', '.join(fmt(a) for a in e.arguments)})"
    if name == "CountStar":
        return "COUNT(*)"
    if name == "TemporalLiteral":
        return f"{e.type_name} {e.value!r}"
    if name == "CurrentTemporal":
        return e.name
    if name == "Case":
        operand = f" {fmt(e.operand)}" if e.operand is not None else ""
        whens = " ".join(f"WHEN {fmt(w.condition)} THEN {fmt(w.result)}" for w in e.when_clauses)
        else_ = f" ELSE {fmt(e.else_result)}" if e.else_result is not None else ""
        return f"(CASE{operand} {whens}{else_})"
    raise AssertionError(f"no format for {name}")


def expr(text):
    query = parse(f"MATCH (n) RETURN {text} AS x")
    return fmt(query.return_statement.items[0].expression)


def refused(text, error):
    with pytest.raises(error) as err:
        parse(text)
    return err.value


# --- a whole query -------------------------------------------------------------

EXPOSURE = """USE exposure_graph
MATCH (s:Supplier)-[:BILLED_BY]->(i:Invoice)
WHERE i.due_date < CURRENT_DATE + DURATION 'P30D'
MATCH (s)-[:ULTIMATELY_OWNED_BY]->(o:Person)
RETURN s.name AS supplier,
       SUM(i.amount) AS exposure,
       COLLECT_LIST(DISTINCT o.name) AS owners
GROUP BY supplier
ORDER BY exposure DESC
LIMIT 10"""


def test_exposure_query():
    query = parse(EXPOSURE)
    assert [part.name for part in query.graph] == ["exposure_graph"]
    first, second = query.match_statements
    assert not first.optional and not second.optional

    (path,) = first.paths
    supplier, billed_by, invoice = path.elements
    assert (supplier.variable.name, supplier.label.name) == ("s", "Supplier")
    assert billed_by.variable is None and billed_by.label.name == "BILLED_BY"
    assert billed_by.direction is D.POINTING_RIGHT
    assert (invoice.variable.name, invoice.label.name) == ("i", "Invoice")
    assert fmt(first.where) == "(< i.due_date (+ CURRENT_DATE DURATION 'P30D'))"

    supplier_again = second.paths[0].elements[0]
    assert supplier_again.variable.name == "s" and supplier_again.label is None

    result = query.return_statement
    assert [fmt(i.expression) for i in result.items] == [
        "s.name",
        "SUM(i.amount)",
        "COLLECT_LIST(DISTINCT o.name)",
    ]
    assert [i.alias.name for i in result.items] == ["supplier", "exposure", "owners"]
    assert [g.name for g in result.group_by] == ["supplier"]
    assert [(fmt(s.expression), s.descending) for s in result.order_by] == [("exposure", True)]
    assert result.offset is None and result.limit.text == "10"


def test_query_span_covers_the_whole_text():
    query = parse(EXPOSURE)
    assert (query.span.start, query.span.end) == (0, len(EXPOSURE))


def test_node_and_edge_spans():
    text = "MATCH (a:L)-[e:R]->(b) RETURN a"
    path = parse(text).match_statements[0].paths[0]
    assert [text[el.span.start : el.span.end] for el in path.elements] == [
        "(a:L)",
        "-[e:R]->",
        "(b)",
    ]
    assert (path.elements[1].span.line, path.elements[1].span.column) == (1, 12)


# --- patterns -------------------------------------------------------------------


def test_left_pointing_edge():
    path = parse("MATCH (a)<-[e:R]-(b) RETURN a").match_statements[0].paths[0]
    assert path.elements[1].direction is D.POINTING_LEFT
    assert path.elements[1].variable.name == "e"


def test_empty_node_and_edge():
    path = parse("MATCH ()-[]->() RETURN 1 AS x").match_statements[0].paths[0]
    node, edge, _ = path.elements
    assert (node.variable, node.label, node.where) == (None, None, None)
    assert (edge.variable, edge.label, edge.where) == (None, None, None)


def test_long_path_alternates_nodes_and_edges():
    path = parse("MATCH (a)-[]->(b)<-[]-(c)-[]->(d) RETURN a").match_statements[0].paths[0]
    names = [type(el).__name__ for el in path.elements]
    assert names == ["NodePattern", "EdgePattern"] * 3 + ["NodePattern"]


def test_element_where():
    path = (
        parse("MATCH (a:L WHERE a.x > 1)-[e WHERE e.w = 2]->(b) RETURN a")
        .match_statements[0]
        .paths[0]
    )
    assert fmt(path.elements[0].where) == "(> a.x I:1)"
    assert fmt(path.elements[1].where) == "(= e.w I:2)"


def test_several_paths_and_optional_match():
    query = parse(
        "MATCH (a)-[]->(b), (a)-[]->(c) OPTIONAL MATCH (c)-[]->(d) WHERE d.x = 1 RETURN a"
    )
    first, second = query.match_statements
    assert len(first.paths) == 2
    assert second.optional and fmt(second.where) == "(= d.x I:1)"


def test_delimited_names():
    query = parse('USE `my graph` MATCH (`a b`:"Odd Label") RETURN `a b`.`x y` AS "out"')
    node = query.match_statements[0].paths[0].elements[0]
    assert [part.name for part in query.graph] == ["my graph"]
    assert (node.variable.name, node.label.name) == ("a b", "Odd Label")
    item = query.return_statement.items[0]
    assert fmt(item.expression) == "a b.x y" and item.alias.name == "out"


def test_keywords_are_case_insensitive_and_names_keep_case():
    query = parse("use G match (A:Person) return A.Name as N order by N asc skip 1 limit 2")
    assert [part.name for part in query.graph] == ["G"]
    assert query.match_statements[0].paths[0].elements[0].label.name == "Person"
    result = query.return_statement
    assert result.offset.text == "1" and result.limit.text == "2"
    assert result.order_by[0].descending is False


def test_label_may_be_any_word():
    node = parse("MATCH (o:Order) RETURN o").match_statements[0].paths[0].elements[0]
    assert node.label.name == "Order"


def test_no_use_clause():
    assert parse("MATCH (a) RETURN a").graph is None


def test_use_takes_a_qualified_name():
    graph = parse("USE ws.`my coll`.g MATCH (a) RETURN a").graph
    assert [part.name for part in graph] == ["ws", "my coll", "g"]
    assert (graph[1].span.line, graph[1].span.column) == (1, 8)


# --- RETURN ---------------------------------------------------------------------


def test_return_distinct_and_all():
    assert parse("MATCH (a) RETURN DISTINCT a").return_statement.distinct is True
    assert parse("MATCH (a) RETURN ALL a").return_statement.distinct is False


def test_return_item_without_alias():
    item = parse("MATCH (a) RETURN a.name").return_statement.items[0]
    assert item.alias is None


def test_offset_and_limit_take_parameters():
    result = parse("MATCH (a) RETURN a OFFSET $o LIMIT $l").return_statement
    assert (result.offset.name, result.limit.name) == ("o", "l")


def test_order_by_several_keys():
    result = parse("MATCH (a) RETURN a ORDER BY a.x DESCENDING, a.y, a.z ASC").return_statement
    assert [s.descending for s in result.order_by] == [True, False, False]


# --- expressions ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("a OR b AND c", "(OR a (AND b c))"),
        ("a AND b OR c", "(OR (AND a b) c)"),
        ("a XOR b OR c", "(OR (XOR a b) c)"),
        ("a AND b XOR c", "(XOR (AND a b) c)"),
        ("NOT a AND b", "(AND (NOT a) b)"),
        ("NOT NOT a", "(NOT (NOT a))"),
        ("NOT a = b", "(NOT (= a b))"),
        ("a = b AND c <> d", "(AND (= a b) (<> c d))"),
        ("a + b * c", "(+ a (* b c))"),
        ("a - b - c", "(- (- a b) c)"),
        ("a * b / c % d", "(% (/ (* a b) c) d)"),
        ("(a + b) * c", "(* (+ a b) c)"),
        ("-a * b", "(* (- a) b)"),
        ("- -a", "(- (- a))"),
        ("+a", "(+ a)"),
        ("a || b || c", "(|| (|| a b) c)"),
        ("a || b + c", "(|| a (+ b c))"),
        ("a + 1 < b || c", "(< (+ a I:1) (|| b c))"),
        ("a.b.c", "a.b.c"),
        ("-a.b", "(- a.b)"),
        ("a IS NULL", "(a IS NULL)"),
        ("a.x + 1 IS NOT NULL", "((+ a.x I:1) IS NOT NULL)"),
        ("NOT a IS NULL", "(NOT (a IS NULL))"),
        ("a <= b", "(<= a b)"),
        ("a >= b", "(>= a b)"),
    ],
)
def test_precedence(text, expected):
    assert expr(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("42", "I:42"),
        ("1.5", "E:1.5"),
        ("1e3", "A:1e3"),
        ("'it''s'", '"it\'s"'),
        ("TRUE", "TRUE"),
        ("false", "FALSE"),
        ("NULL", "NULL"),
        ("$p", "$p"),
        ("DATE '2026-01-01'", "DATE '2026-01-01'"),
        ("timestamp '2026-01-01 10:00:00'", "TIMESTAMP '2026-01-01 10:00:00'"),
        ("DURATION 'P30D'", "DURATION 'P30D'"),
        ("CURRENT_DATE", "CURRENT_DATE"),
        ("current_timestamp", "CURRENT_TIMESTAMP"),
    ],
)
def test_literals(text, expected):
    assert expr(text) == expected


def test_numbers_keep_their_source_text():
    assert expr("007") == "I:007"
    assert expr("1.50") == "E:1.50"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("COUNT(*)", "COUNT(*)"),
        ("count(*)", "COUNT(*)"),
        ("COUNT(a)", "COUNT(a)"),
        ("COUNT(DISTINCT a.x)", "COUNT(DISTINCT a.x)"),
        ("SUM(ALL a.x)", "SUM(a.x)"),
        ("coalesce(a.x, a.y, 0)", "coalesce(a.x, a.y, I:0)"),
        ("now()", "now()"),
        ("DATE(a.s)", "DATE(a.s)"),
        ("UPPER(a.x || 'y')", "UPPER((|| a.x 'y'))"),
    ],
)
def test_function_calls(text, expected):
    assert expr(text) == expected


def test_function_name_is_kept_as_written():
    call = parse("MATCH (a) RETURN Sum(a.x) AS s").return_statement.items[0].expression
    assert call.name == "Sum"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("CASE WHEN a > 1 THEN 'x' ELSE 'y' END", "(CASE WHEN (> a I:1) THEN 'x' ELSE 'y')"),
        ("CASE a.k WHEN 1 THEN 'one' WHEN 2 THEN 'two' END", "(CASE a.k WHEN I:1 THEN 'one' WHEN I:2 THEN 'two')"),
        ("CASE WHEN a THEN b END", "(CASE WHEN a THEN b)"),
    ],
)  # fmt: skip
def test_case(text, expected):
    assert expr(text) == expected


def test_accent_quoted_variable_in_expression():
    assert expr("`weird name`.x") == "weird name.x"


def test_binary_operation_span_covers_both_operands():
    text = "MATCH (n) RETURN a.x + b.y AS s"
    expression = parse(text).return_statement.items[0].expression
    assert text[expression.span.start : expression.span.end] == "a.x + b.y"


# --- not supported: valid GQL outside this release ------------------------------


@pytest.mark.parametrize(
    "text, construct",
    [
        ("MATCH (a)-[e]-(b) RETURN a", "any direction"),
        ("MATCH (a)<-[e]->(b) RETURN a", "left or right"),
        ("MATCH (a)->(b) RETURN a", "abbreviated"),
        ("MATCH (a)<-(b) RETURN a", "abbreviated"),
        ("MATCH (a)-(b) RETURN a", "abbreviated"),
        ("MATCH (a)~[e]~(b) RETURN a", "undirected"),
        ("MATCH (a)-[e]->*(b) RETURN a", "the quantifier `*`"),
        ("MATCH (a)-[e]->+(b) RETURN a", "the quantifier `+`"),
        ("MATCH (a)-[e]->?(b) RETURN a", "the quantifier `?`"),
        ("MATCH (a)-[e]->{1,}(b) RETURN a", "open-ended quantifiers"),
        ("MATCH (a)-[e]->{,3}(b) RETURN a", "without a lower bound"),
        ("MATCH (a)-[e]->{0,3}(b) RETURN a", "zero repeats"),
        ("MATCH (a)-[e]->{0}(b) RETURN a", "zero repeats"),
        ("MATCH (a)-[e]->{1,17}(b) RETURN a", "bounds above 16"),
        ("MATCH ((a)-[e]->(b)) RETURN a", "parenthesized path"),
        ("MATCH p = (a)-[e]->(b) RETURN a", "path variables"),
        ("MATCH ANY SHORTEST (a)-[e]->(b) RETURN a", "path search prefixes"),
        ("MATCH TRAIL (a)-[e]->(b) RETURN a", "path search prefixes"),
        ("MATCH DIFFERENT EDGES (a)-[e]->(b) RETURN a", "match modes"),
        ("MATCH (a:A|B) RETURN a", "label expressions"),
        ("MATCH (a:A&B) RETURN a", "label expressions"),
        ("MATCH (a:!A) RETURN a", "label expressions"),
        ("MATCH (a:%) RETURN a", "label expressions"),
        ("MATCH (a IS A) RETURN a", "`IS` label expressions"),
        ("MATCH (a:A:B) RETURN a", "more than one label"),
        ("MATCH (a {name: 'x'}) RETURN a", "property specifications"),
        ("MATCH (a) YIELD a RETURN a", "`YIELD`"),
        ("MATCH (a) KEEP TRAIL RETURN a", "`KEEP`"),
        ("OPTIONAL { MATCH (a) } RETURN a", "`OPTIONAL` blocks"),
        ("RETURN 1 AS x", "without `MATCH`"),
        ("MATCH (a) LET x = 1 RETURN x", "`LET`"),
        ("MATCH (a) FILTER a.x > 1 RETURN a", "`FILTER`"),
        ("MATCH (a) ORDER BY a.x RETURN a", "`ORDER`"),
        ("MATCH (a) INSERT (b) RETURN a", "`INSERT`"),
        ("MATCH (a) RETURN a NEXT MATCH (b) RETURN b", "composite"),
        ("MATCH (a) RETURN a UNION MATCH (b) RETURN b", "composite"),
        ("MATCH (a) RETURN *", "`RETURN *`"),
        ("MATCH (a) RETURN a.x AS k, COUNT(*) AS n GROUP BY ()", "empty grouping set"),
        ("MATCH (a) RETURN a ORDER BY a.x NULLS LAST", "`NULLS"),
        ("MATCH (a) RETURN a LIMIT 1 OFFSET 2", "`OFFSET` after `LIMIT`"),
        ("MATCH (a) RETURN a <> b = c AS x", "chained comparisons"),
        ("MATCH (a) RETURN a = b IS NULL AS x", "chained comparisons"),
        ("MATCH (a) RETURN a != b AS x", "`!=`"),
        ("MATCH (a) RETURN a IS TRUE AS x", "`IS TRUE`"),
        ("MATCH (a) RETURN a IS NOT LABELED A AS x", "`IS NOT LABELED`"),
        ("MATCH (a) RETURN [1, 2] AS x", "list literals"),
        ("MATCH (a) RETURN {k: 1} AS x", "record literals"),
        ('MATCH (a) RETURN "x" AS x', "double-quoted text"),
        ("MATCH (a) RETURN UNKNOWN AS x", "`UNKNOWN`"),
        ("MATCH (a) RETURN EXISTS { MATCH (b) } AS x", "`EXISTS`"),
        ("MATCH (a) RETURN CASE a WHEN > 1 THEN 1 END AS x", "comparisons in a simple"),
        ("MATCH (a) RETURN CASE a WHEN 1, 2 THEN 1 END AS x", "several values"),
    ],
)
def test_not_supported(text, construct):
    err = refused(text, GqlNotSupportedError)
    assert construct in err.message
    assert "not supported yet" in err.message


def test_quantified_edges_parse():
    for text, bounds in (("{1,3}", (1, 3)), ("{2}", (2, 2)), ("{ 1 , 16 }", (1, 16))):
        edge = parse(f"MATCH (a)-[e:E WHERE e.x = 1]->{text}(b) RETURN a.x AS x").match_statements[0].paths[0].elements[1]
        assert (edge.quantifier.minimum, edge.quantifier.maximum) == bounds
        assert edge.where is not None and edge.variable.name == "e"
    left = parse("MATCH (a)<-[:E]-{2}(b) RETURN a.x AS x").match_statements[0].paths[0].elements[1]
    assert left.direction is D.POINTING_LEFT and left.quantifier.minimum == 2


def test_an_edge_without_a_quantifier_has_none():
    edge = parse("MATCH (a)-[:E]->(b) RETURN a.x AS x").match_statements[0].paths[0].elements[1]
    assert edge.quantifier is None


def test_the_quantifier_span_is_inside_the_edge_span():
    text = "MATCH (a)-[:E]->{1,3}(b) RETURN a.x AS x"
    edge = parse(text).match_statements[0].paths[0].elements[1]
    assert text[edge.quantifier.span.start : edge.quantifier.span.end] == "{1,3}"
    assert text[edge.span.start : edge.span.end] == "-[:E]->{1,3}"


def test_not_supported_points_at_the_construct():
    err = refused("MATCH (a)\n  -[e]-(b) RETURN a", GqlNotSupportedError)
    assert (err.line, err.column) == (2, 7)


# --- syntax errors: not GQL -----------------------------------------------------


@pytest.mark.parametrize(
    "text, message, position",
    [
        ("", "expected `MATCH` or `RETURN`, found the end of the query", (1, 1)),
        ("MATCH (a)", "expected `MATCH` or `RETURN`, found the end of the query", (1, 10)),
        ("MATCH a RETURN a", "expected `(` to start a node pattern, found `a`", (1, 7)),
        ("MATCH (a RETURN a", "expected `)` to close the node pattern, found `RETURN`", (1, 10)),
        ("MATCH (a)-[e(b) RETURN a", "expected `]` to close the edge pattern, found `(`", (1, 13)),
        ("MATCH (a)-[e]>(b) RETURN a", "expected `]->` to close the edge pattern, found `>`", (1, 14)),
        ("MATCH (a)<-[e]>(b) RETURN a", "expected `]-` to close the edge pattern, found `>`", (1, 15)),
        ("MATCH (a)- [e]->(b) RETURN a", "write `-[` without spaces", (1, 12)),
        ("MATCH (a)-[e] ->(b) RETURN a", "write `]->` without spaces", (1, 15)),
        ("MATCH (a)-[e]->", "expected `(` to start a node pattern, found the end of the query", (1, 16)),
        ("MATCH (a)-[e]->{}(b) RETURN a", "expected a whole number, found `}`", (1, 17)),
        ("MATCH (a)-[e]->{x}(b) RETURN a", "expected a whole number, found `x`", (1, 17)),
        ("MATCH (a)-[e]->{1,2(b) RETURN a", "expected `}` to close the quantifier, found `(`", (1, 20)),
        ("MATCH (a)-[e]->{3,2}(b) RETURN a", "upper bound 2 is below its lower bound 3", (1, 16)),
        ("MATCH (a) RETURN", "expected an expression, found the end of the query", (1, 17)),
        ("MATCH (a) RETURN a,", "expected an expression, found the end of the query", (1, 20)),
        ("MATCH (a) RETURN a AS", "expected an alias, found the end of the query", (1, 22)),
        ("MATCH (a) RETURN a a", "expected the end of the query, found `a`", (1, 20)),
        ("MATCH (a) RETURN a;", "expected the end of the query, found `;`", (1, 19)),
        ("MATCH (a) RETURN (a", "expected `)` to close the parenthesis", (1, 20)),
        ("MATCH (a) RETURN a +", "expected an expression, found the end of the query", (1, 21)),
        ("MATCH (a) RETURN AND", "expected an expression, found `AND`", (1, 18)),
        ("MATCH (a) RETURN AND(1)", "expected an expression, found `AND`", (1, 18)),
        ("MATCH (a) RETURN a.", "expected a property name", (1, 20)),
        ("MATCH (a) RETURN a IS", "expected `NULL` after `IS`", (1, 22)),
        ("MATCH (a) RETURN DATE", "expected a quoted value after `DATE`", (1, 18)),
        ("MATCH (a) RETURN COUNT(DISTINCT)", "`DISTINCT` needs an argument", (1, 32)),
        ("MATCH (a) RETURN f(a", "expected `)` to close the call to `f`", (1, 21)),
        ("MATCH (a) RETURN CASE END", "expected `WHEN`, found `END`", (1, 23)),
        ("MATCH (a) RETURN CASE WHEN a 1 END", "expected `THEN`, found `1`", (1, 30)),
        ("MATCH (a) RETURN CASE WHEN a THEN 1", "expected `END`", (1, 36)),
        ("MATCH (a) RETURN a GROUP a", "expected `BY`, found `a`", (1, 26)),
        ("MATCH (a) RETURN a LIMIT x", "expected a whole number or a parameter, found `x`", (1, 26)),
        ("MATCH (a) RETURN a LIMIT 1.5", "expected a whole number or a parameter", (1, 26)),
        ("MATCH (null) RETURN 1", "`null` is a reserved word and cannot be a variable name", (1, 8)),
        ("MATCH (a) RETURN a AS not", "`not` is a reserved word and cannot be an alias", (1, 23)),
        ("MATCH (a) RETURN a GROUP BY true", "`true` is a reserved word", (1, 29)),
        ("OPTIONAL a", "expected `MATCH` or `RETURN`, found `OPTIONAL`", (1, 1)),
    ],
)  # fmt: skip
def test_syntax_errors(text, message, position):
    err = refused(text, GqlSyntaxError)
    assert message in err.message, err.message
    assert (err.line, err.column) == position


@pytest.mark.parametrize(
    "inner",
    [
        "(" * 200 + "1" + ")" * 200,
        "NOT " * 2000 + "a",
        "- " * 2000 + "1",
        "f(" * 200 + "1" + ")" * 200,
    ],
)
def test_deep_nesting_is_refused_cleanly(inner):
    err = refused(f"MATCH (a) RETURN {inner} AS x", GqlNotSupportedError)
    assert "nested more than 32 deep" in err.message


def test_nesting_up_to_the_limit_parses():
    depth = 31
    assert expr("(" * depth + "1" + ")" * depth) == "I:1"
    assert expr("NOT " * depth + "a").count("NOT") == depth


def test_lexer_errors_surface_through_parse():
    refused("MATCH (a) RETURN 'x", GqlSyntaxError)
    refused("MATCH (a)-->(b) RETURN a", GqlNotSupportedError)
