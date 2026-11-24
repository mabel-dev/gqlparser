"""Builders for sqlparser-rs's serialised syntax tree (the release in SQLPARSER_FORMAT).

Each function returns the dict sqlparser-rs produces for that construct, so the
result can be rendered to SQL by sqlparser-rs itself (opteryx.compute.restore_ast,
sqloxide). Shapes were taken from sqlparser-rs's own output, not from its docs.
"""

from __future__ import annotations

from gqlparser.ast import Span

SQLPARSER_FORMAT = "0.63"

# Expressions whose rendering has no brackets of its own. Every one used as an
# operand is wrapped in Nested, so the SQL never depends on the target engine's
# operator precedence (which differs between engines, and from GQL's).
_COMPOUND = frozenset({"BinaryOp", "UnaryOp", "IsNull", "IsNotNull"})


def span(at: Span) -> dict:
    # sqlparser requires a span; a GQL Span knows its start only, so it is zero-width
    position = {"line": at.line, "column": at.column}
    return {"start": position, "end": dict(position)}


def ident(value: str, quoted: bool, at: Span) -> dict:
    return {"value": value, "quote_style": '"' if quoted else None, "span": span(at)}


def identifier(name: dict) -> dict:
    return {"Identifier": name}


def column(qualifier: dict, name: dict) -> dict:
    return {"CompoundIdentifier": [qualifier, name]}


def nest(expression: dict) -> dict:
    return {"Nested": expression} if next(iter(expression)) in _COMPOUND else expression


def _value(value, at: Span) -> dict:
    return {"Value": {"value": value, "span": span(at)}}


def number(text: str, at: Span) -> dict:
    return _value({"Number": [text, False]}, at)


def string(text: str, at: Span) -> dict:
    return _value({"SingleQuotedString": text}, at)


def boolean(value: bool, at: Span) -> dict:
    return _value({"Boolean": value}, at)


def null(at: Span) -> dict:
    return _value("Null", at)


def cast_string(text: str, data_type, at: Span) -> dict:
    """`CAST('text' AS type)`: standard SQL, and accepted where `DATE '...'` is not (Opteryx)."""
    return {
        "Cast": {"kind": "Cast", "expr": string(text, at), "data_type": data_type, "format": None}
    }


def interval(amount: str, field: str, at: Span) -> dict:
    return {
        "Interval": {
            "value": string(amount, at),
            "leading_field": field,
            "leading_precision": None,
            "last_field": None,
            "fractional_seconds_precision": None,
        }
    }


def binary(left: dict, operator: str, right: dict) -> dict:
    return {"BinaryOp": {"left": nest(left), "op": operator, "right": nest(right)}}


def conjunction(expressions: list[dict]) -> dict | None:
    if not expressions:
        return None
    result = expressions[0]
    for expression in expressions[1:]:
        result = binary(result, "And", expression)
    return result


def unary(operator: str, operand: dict) -> dict:
    return {"UnaryOp": {"op": operator, "expr": nest(operand)}}


def is_null(operand: dict, negated: bool) -> dict:
    return {"IsNotNull" if negated else "IsNull": nest(operand)}


def function(name: dict, arguments: list[dict] | None, distinct: bool = False) -> dict:
    """`arguments=None` renders with no brackets (`CURRENT_DATE`)."""
    if arguments is None:
        args = "None"
    else:
        args = {
            "List": {
                "duplicate_treatment": "Distinct" if distinct else None,
                "args": [{"Unnamed": {"Expr": a}} for a in arguments],
                "clauses": [],
            }
        }
    return {
        "Function": {
            "name": [identifier(name)],
            "uses_odbc_syntax": False,
            "parameters": "None",
            "args": args,
            "within_group": [],
            "filter": None,
            "null_treatment": None,
            "over": None,
        }
    }


def count_star(name: dict) -> dict:
    result = function(name, [])
    result["Function"]["args"]["List"]["args"] = [{"Unnamed": "Wildcard"}]
    return result


def keyword_token(word: str, at: Span) -> dict:
    return {
        "token": {"Word": {"value": word, "quote_style": None, "keyword": word}},
        "span": span(at),
    }


def case(operand, conditions: list[tuple[dict, dict]], else_result, at: Span) -> dict:
    return {
        "Case": {
            "case_token": keyword_token("CASE", at),
            "end_token": keyword_token("END", at),
            "operand": operand,
            "conditions": [{"condition": c, "result": r} for c, r in conditions],
            "else_result": else_result,
        }
    }


def _alias(name: dict) -> dict:
    return {"explicit": True, "name": name, "columns": [], "at": None}


def table(name: list[dict], alias: dict) -> dict:
    return {
        "Table": {
            "name": [identifier(part) for part in name],
            "alias": _alias(alias),
            "args": None,
            "with_hints": [],
            "version": None,
            "with_ordinality": False,
            "partitions": [],
            "json_path": None,
            "sample": None,
            "index_hints": [],
        }
    }


def derived(subquery: dict, alias: dict) -> dict:
    return {
        "Derived": {"lateral": False, "subquery": subquery, "alias": _alias(alias), "sample": None}
    }


def inner_join(relation: dict, on: dict) -> dict:
    return {"relation": relation, "global": False, "join_operator": {"Inner": {"On": on}}}


def left_join(relation: dict, on: dict) -> dict:
    return {"relation": relation, "global": False, "join_operator": {"Left": {"On": on}}}


def cross_join(relation: dict) -> dict:
    return {"relation": relation, "global": False, "join_operator": {"CrossJoin": "None"}}


def select_item(expression: dict, alias: dict) -> dict:
    return {"ExprWithAlias": {"expr": expression, "alias": alias}}


def sort_item(expression: dict, descending: bool) -> dict:
    return {
        "expr": expression,
        "options": {"sort": "Desc" if descending else None, "nulls_first": None},
        "with_fill": None,
    }


def union_all(queries: list[dict]) -> dict:
    """The bodies of `queries` joined by `UNION ALL`, left to right, as one query."""
    body = queries[0]["body"]
    for next_query in queries[1:]:
        body = {
            "SetOperation": {
                "op": "Union",
                "set_quantifier": "All",
                "left": body,
                "right": next_query["body"],
            }
        }
    return {**queries[0], "body": body}


def query(
    *,
    at: Span,
    distinct: bool,
    projection: list[dict],
    relation: dict,
    joins: list[dict],
    selection: dict | None,
    group_by: list[dict],
    order_by: list[dict],
    limit: dict | None,
    offset: dict | None,
) -> dict:
    select = {
        "select_token": keyword_token("SELECT", at),
        "optimizer_hints": [],
        "distinct": "Distinct" if distinct else None,
        "select_modifiers": None,
        "top": None,
        "top_before_distinct": False,
        "projection": projection,
        "exclude": None,
        "into": None,
        "from": [{"relation": relation, "joins": joins}],
        "lateral_views": [],
        "prewhere": None,
        "selection": selection,
        "connect_by": [],
        "group_by": {"Expressions": [group_by, []]},
        "cluster_by": [],
        "distribute_by": [],
        "sort_by": [],
        "having": None,
        "named_window": [],
        "qualify": None,
        "window_before_qualify": False,
        "value_table_mode": None,
        "flavor": "Standard",
    }
    limit_clause = None
    if limit is not None or offset is not None:
        limit_clause = {
            "LimitOffset": {
                "limit": limit,
                "offset": None if offset is None else {"value": offset, "rows": "None"},
                "limit_by": [],
            }
        }
    return {
        "with": None,
        "body": {"Select": select},
        "order_by": {"kind": {"Expressions": order_by}, "interpolate": None} if order_by else None,
        "limit_clause": limit_clause,
        "fetch": None,
        "locks": [],
        "for_clause": None,
        "settings": None,
        "format_clause": None,
        "pipe_operators": [],
    }
