import json
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import pytest
from lowering_cases import CASES
from lowering_cases import PEOPLE

import gqlparser
from gqlparser import GqlNotSupportedError
from gqlparser import GqlSemanticError
from gqlparser import GqlSyntaxError
from gqlparser.graph import EdgeEndpoint
from gqlparser.graph import SqlName

with open(os.path.join(HERE, "fixtures", "lowering.json")) as f:
    FIXTURES = json.load(f)


def normalise(tree):
    if type(tree) is dict:
        return {k: normalise(v) for k, v in tree.items() if k != "span"}
    if type(tree) in (list, tuple):
        return [normalise(v) for v in tree]
    return tree


def lower(gql, ddl=PEOPLE):
    return gqlparser.lower(gqlparser.parse(gql), gqlparser.parse_property_graph(ddl))


def refused(gql, error, ddl=PEOPLE):
    with pytest.raises(error) as err:
        lower(gql, ddl)
    return err.value


# --- lowering matches sqlparser's own parse of the expected SQL ---------------------


def test_every_case_has_a_fixture():
    assert sorted(FIXTURES) == sorted(c.name for c in CASES), "run dev/make_lowering_fixtures.py"


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_lowering_matches_fixture(case):
    assert normalise(lower(case.gql, case.ddl)) == FIXTURES[case.name]


def test_format_version_is_declared():
    assert gqlparser.SQLPARSER_FORMAT == "0.63"


def test_every_identifier_value_and_token_has_a_span():
    # sqlparser refuses to render a tree with any of these missing
    missing = []

    def walk(tree, path):
        if type(tree) is dict:
            is_word = "keyword" in tree  # a token's Word carries no span; its AttachedToken does
            needs_span = not is_word and (
                "quote_style" in tree or "token" in tree or (path and path[-1] == "Value")
            )
            if needs_span and "span" not in tree:
                missing.append(path)
            for key, value in tree.items():
                walk(value, path + [key])
        elif type(tree) in (list, tuple):
            for value in tree:
                walk(value, path)

    for case in CASES:
        walk(lower(case.gql, case.ddl), [])
    assert missing == []


def test_spans_carry_gql_positions():
    statement = lower("USE g MATCH (p:Person)\n  WHERE p.age > 30 RETURN p.name")
    where = statement["Query"]["body"]["Select"]["selection"]["BinaryOp"]
    age = where["left"]["CompoundIdentifier"][1]
    assert (age["span"]["start"]["line"], age["span"]["start"]["column"]) == (2, 11)


def test_lowering_is_repeatable_and_returns_fresh_trees():
    first, second = lower(CASES[1].gql), lower(CASES[1].gql)
    assert first == second and first is not second


# --- semantic errors -------------------------------------------------------------


@pytest.mark.parametrize(
    "gql, message",
    [
        ("MATCH (p:Person) RETURN p.name", "has no USE; start it with `USE g`"),
        ("USE other MATCH (p:Person) RETURN p.name", "uses `other`, but the graph is `g`"),
        ("USE g MATCH (p:Persn) RETURN p.name", "no label `Persn`. Did you mean `Person`?"),
        ("USE g MATCH (p:KNOWS) RETURN p.name", "`KNOWS` is an edge label, used here on a node"),
        ("USE g MATCH (p)-[:Person]->(q) RETURN p.name", "`Person` is a node label, used here on an edge"),
        ("USE g MATCH (p:Person) RETURN p.nmae", "no property `nmae`"),
        ("USE g MATCH (c:Company)-[:WORKS_AT]->(p:Person) RETURN c.name",
         "`WORKS_AT` points from `Person` to `Company`, but here it points from `Company` to `Person`; reverse the arrow"),
        ("USE g MATCH (a:Person)-[:KNOWS]->(c:Company) RETURN a.name",
         "`KNOWS` connects `Person` to `Person`, not `Person` to `Company`"),
        ("USE g MATCH (p:Person) MATCH (p:Company) RETURN p.name", "`p` is already a `Person`"),
        ("USE g MATCH (x) RETURN x.name", "this node needs a label"),
        ("USE g MATCH (a:Person)-[]->(b:Person) RETURN a.name", "this edge needs a label"),
        ("USE g MATCH (a)-[:KNOWS]->(b)-[:WORKS_AT]->(c), (c)-[:KNOWS]->(d) RETURN a.name",
         "`KNOWS` connects `Person` to `Person`, not `Company` to `Person`"),
        ("USE g MATCH (a:Person)-[e:KNOWS]->(b) MATCH (e)-[:KNOWS]->(b) RETURN a.name",
         "`e` is an edge variable, used here on a node"),
        ("USE g MATCH (p:Person) WHERE q.age > 1 RETURN p.name", "`q` is not a variable here"),
        ("USE g MATCH (p:Person) RETURN p.name AS n GROUP BY m", "`GROUP BY m` names no RETURN item"),
        ("USE g MATCH (p:Person) RETURN p.name, p.age AS name", "two RETURN items are both named `name`"),
    ],
)  # fmt: skip
def test_semantic_errors(gql, message):
    err = refused(gql, GqlSemanticError)
    assert message in err.message, err.message


def test_inferred_labels_that_conflict():
    err = refused(
        "USE g MATCH (c:Company)<-[:WORKS_AT]-(x)<-[:WORKS_AT]-(p:Person) RETURN c.name",
        GqlSemanticError,
    )
    assert "would have to be both" in err.message


def test_wrong_way_edge_points_at_the_edge():
    err = refused(
        "USE g MATCH (c:Company)\n  -[:WORKS_AT]->(p:Person) RETURN c.name", GqlSemanticError
    )
    assert (err.line, err.column) == (2, 3)


# --- not supported -----------------------------------------------------------------


@pytest.mark.parametrize(
    "gql, message",
    [
        ("USE g OPTIONAL MATCH (p:Person) RETURN p.name", "starts with OPTIONAL MATCH"),
        ("USE g MATCH (p:Person) OPTIONAL MATCH (c:Company) RETURN p.name", "shares no variable"),
        ("USE g MATCH (p:Person), (q:Person) OPTIONAL MATCH (p)-[:KNOWS]->(r) WHERE r.age > q.age RETURN p.name",
         "refers to `q`, which its own pattern doesn't mention"),
        ("USE g MATCH (p:Person) RETURN p", "using the element variable `p` as a value"),
        ("USE g MATCH (p:Person) RETURN COUNT(p) AS n", "using the element variable `p` as a value"),
        ("USE g MATCH (p:Person) RETURN p.age + 1", "isn't a property needs `AS name`"),
        ("USE g MATCH (p:Person) RETURN p.name AS n, COUNT(*) AS c", "mixes aggregates"),
        ("USE g MATCH (p:Person) RETURN COUNT(*) AS c GROUP BY p", "grouping by an element variable"),
        ("USE g MATCH (p:Person) RETURN p.name.first AS f", "property access on anything but an element variable"),
        ("USE g MATCH (p:Person) WHERE p.born < CURRENT_DATE + DURATION 'P1Y2M' RETURN p.name", "the duration 'P1Y2M'"),
        ("USE g MATCH (`$x`:Person) RETURN `$x`.name AS n", "variable names starting with `$`"),
    ],
)  # fmt: skip
def test_not_supported(gql, message):
    err = refused(gql, GqlNotSupportedError)
    assert message in err.message, err.message
    assert "not supported yet" in err.message


def test_all_aggregates_need_no_group_by():
    statement = lower("USE g MATCH (p:Person) RETURN COUNT(*) AS c, MAX(p.age) AS oldest")
    assert statement["Query"]["body"]["Select"]["group_by"] == {"Expressions": [[], []]}


def test_use_names_a_qualified_graph_in_full_or_by_its_last_part():
    ddl = PEOPLE.replace("PROPERTY GRAPH g", "PROPERTY GRAPH ws.coll.g")
    assert lower("USE ws.coll.g MATCH (p:Person) RETURN p.name", ddl)
    assert lower("USE g MATCH (p:Person) RETURN p.name", ddl)


@pytest.mark.parametrize("use", ["coll.g", "other.coll.g", "`ws.coll.g`"])
def test_use_refuses_any_other_name_for_a_qualified_graph(use):
    ddl = PEOPLE.replace("PROPERTY GRAPH g", "PROPERTY GRAPH ws.coll.g")
    err = refused(f"USE {use} MATCH (p:Person) RETURN p.name", GqlSemanticError, ddl)
    assert "but the graph is `ws.coll.g`" in err.message


@pytest.mark.parametrize("duration, amount, field", [("P3D", "3", "Day"), ("P2W", "14", "Day"),
                         ("P1Y", "1", "Year"), ("P6M", "6", "Month"), ("PT12H", "12", "Hour"),
                         ("PT5M", "5", "Minute"), ("PT30S", "30", "Second")])  # fmt: skip
def test_durations(duration, amount, field):
    statement = lower(f"USE g MATCH (p:Person) WHERE p.born > DURATION '{duration}' RETURN p.name")
    interval = statement["Query"]["body"]["Select"]["selection"]["BinaryOp"]["right"]["Interval"]
    assert interval["value"]["Value"]["value"] == {"SingleQuotedString": amount}
    assert interval["leading_field"] == field


# --- parse_property_graph ------------------------------------------------------------


def test_parse_property_graph_structure():
    graph = gqlparser.parse_property_graph(PEOPLE)
    assert graph.name == (SqlName("g", False),)
    people, companies = graph.vertex_tables
    assert people.name == "people" and people.label == "Person"
    assert people.relation == (SqlName("db", False), SqlName("people", False))
    assert people.key == (SqlName("id", False),)
    assert companies.properties == (
        ("id", SqlName("id", False)),
        ("name", SqlName("legal_name", False)),
    )
    employment, knows, managed = graph.edge_tables
    assert employment.source == EdgeEndpoint(
        (SqlName("person_id", False),), "people", (SqlName("id", False),)
    )
    assert employment.properties == (("since", SqlName("since", False)),)
    assert knows.properties == ()
    assert managed.name == "managed" and managed.relation[-1].value == "people"


def test_label_defaults_to_the_element_name():
    graph = gqlparser.parse_property_graph(
        "CREATE PROPERTY GRAPH g VERTEX TABLES (db.people KEY (id) PROPERTIES (id))"
    )
    assert graph.vertex_tables[0].label == "people"


def test_quoting_and_dollar_names_are_kept():
    graph = gqlparser.parse_property_graph(
        'CREATE OR REPLACE PROPERTY GRAPH IF NOT EXISTS "G" VERTEX TABLES '
        '($planets AS p KEY ("Id") LABEL Planet PROPERTIES ("Id" AS id, name));'
    )
    planet = graph.vertex_tables[0]
    assert graph.name == (SqlName("G", True),)
    assert planet.relation == (SqlName("$planets", False),)
    assert planet.properties == (("id", SqlName("Id", True)), ("name", SqlName("name", False)))


@pytest.mark.parametrize(
    "ddl, message",
    [
        ("CREATE PROPERTY GRAPH g VERTEX TABLES (t LABEL A PROPERTIES (x))", "no KEY clause"),
        ("CREATE PROPERTY GRAPH g VERTEX TABLES (t KEY (x) LABEL A)", "no PROPERTIES clause"),
        ("CREATE PROPERTY GRAPH g VERTEX TABLES (t KEY (x) LABEL A PROPERTIES ALL COLUMNS)", "ALL COLUMNS"),
        ("CREATE PROPERTY GRAPH g VERTEX TABLES (t KEY (x) DEFAULT LABEL)", "`DEFAULT LABEL`"),
        ("CREATE PROPERTY GRAPH g VERTEX TABLES (t KEY (x) LABEL A PROPERTIES (x) LABEL B PROPERTIES (x))",
         "more than one label"),
        ("CREATE PROPERTY GRAPH g VERTEX TABLES (t KEY (x) LABEL A PROPERTIES (x * 2 AS y))", "expression properties"),
        (("CREATE PROPERTY GRAPH g VERTEX TABLES (t KEY (x) LABEL A PROPERTIES (x)) EDGE TABLES "
         "(e KEY (k) SOURCE t DESTINATION t LABEL E NO PROPERTIES)"), "no KEY ... REFERENCES"),
    ],
)  # fmt: skip
def test_graph_forms_needing_a_schema_are_not_supported(ddl, message):
    with pytest.raises(GqlNotSupportedError) as err:
        gqlparser.parse_property_graph(ddl)
    assert message in err.value.message


@pytest.mark.parametrize(
    "ddl, message, position",
    [
        ("CREATE PROPERTY GRAPH g VERTEX TABLES (t KEY (x) LABEL A PROPERTIES (x),\n  t KEY (x) LABEL B PROPERTIES (x))",
         "two element tables are both named `t`", (1, 40)),
        ("CREATE PROPERTY GRAPH g VERTEX TABLES (t KEY (x) LABEL A PROPERTIES (x), u KEY (x) LABEL A PROPERTIES (x))",
         "the label `A` is on more than one element table", (1, 74)),
        ("CREATE PROPERTY GRAPH g VERTEX TABLES (t KEY (x) LABEL A PROPERTIES (x, x))",
         "declares the property `x` twice", (1, 40)),
        (("CREATE PROPERTY GRAPH g VERTEX TABLES (t KEY (x) LABEL A PROPERTIES (x)) EDGE TABLES "
         "(e KEY (k) SOURCE KEY (a) REFERENCES nope (x) DESTINATION KEY (b) REFERENCES t (x) LABEL E NO PROPERTIES)"),
         "SOURCE references `nope`", (1, 123)),
        (("CREATE PROPERTY GRAPH g VERTEX TABLES (t KEY (x) LABEL A PROPERTIES (x)) EDGE TABLES "
         "(e KEY (k) SOURCE KEY (a, b) REFERENCES t (x) DESTINATION KEY (b) REFERENCES t (x) LABEL E NO PROPERTIES)"),
         "SOURCE KEY has 2 column(s) but REFERENCES has 1", (1, 126)),
    ],
)  # fmt: skip
def test_graph_definitions_that_contradict_themselves(ddl, message, position):
    with pytest.raises(GqlSemanticError) as err:
        gqlparser.parse_property_graph(ddl)
    assert message in err.value.message, err.value.message
    assert (err.value.line, err.value.column) == position


@pytest.mark.parametrize(
    "ddl, message",
    [
        ("CREATE GRAPH g", "expected `PROPERTY`"),
        ("CREATE PROPERTY GRAPH g EDGE TABLES ()", "expected `VERTEX`"),
        ("CREATE PROPERTY GRAPH g VERTEX TABLES (t KEY x LABEL A PROPERTIES (x))", "expected `(`"),
        (
            "CREATE PROPERTY GRAPH g VERTEX TABLES (t KEY (x) LABEL A PROPERTIES (x)) extra",
            "expected the end",
        ),
    ],
)
def test_graph_syntax_errors(ddl, message):
    with pytest.raises(GqlSyntaxError) as err:
        gqlparser.parse_property_graph(ddl)
    assert message in err.value.message
