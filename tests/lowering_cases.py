"""Lowering cases: each GQL query with the SQL it must lower to, written by hand.

`dev/make_lowering_fixtures.py` parses each `sql` with sqlparser-rs (through
Opteryx) into tests/fixtures/lowering.json; test_lowering.py checks that
`lower()` produces exactly that dict. Run the script after changing a case.

The expected SQL follows the lowering's conventions: GQL names (variables,
output aliases, generated names) are double-quoted, DDL names keep the DDL's
quoting, and every compound operand is parenthesised.
"""

from typing import NamedTuple


class Case(NamedTuple):
    name: str
    ddl: str
    gql: str
    sql: str


PEOPLE = """CREATE PROPERTY GRAPH g
  VERTEX TABLES (
    db.people KEY (id) LABEL Person PROPERTIES (id, name, age, born),
    db.companies KEY (id) LABEL Company PROPERTIES (id, legal_name AS name)
  )
  EDGE TABLES (
    db.employment KEY (person_id, company_id)
      SOURCE KEY (person_id) REFERENCES people (id)
      DESTINATION KEY (company_id) REFERENCES companies (id)
      LABEL WORKS_AT PROPERTIES (since),
    db.knows KEY (a, b)
      SOURCE KEY (a) REFERENCES people (id)
      DESTINATION KEY (b) REFERENCES people (id)
      LABEL KNOWS NO PROPERTIES,
    db.people AS managed KEY (id)
      SOURCE KEY (id) REFERENCES people (id)
      DESTINATION KEY (manager_id) REFERENCES people (id)
      LABEL MANAGED_BY NO PROPERTIES
  )"""

ODD = """CREATE PROPERTY GRAPH "My Graph"
  VERTEX TABLES (
    "Odd Schema"."Weird Table" AS w KEY ("Key Col") LABEL Thing PROPERTIES ("Key Col" AS key, "Value" AS value)
  )"""

LINKS = """CREATE PROPERTY GRAPH g
  VERTEX TABLES (
    db.resources KEY (id) LABEL Resource PROPERTIES (id, name)
  )
  EDGE TABLES (
    db.links KEY (src, dst, relation)
      SOURCE KEY (src) REFERENCES resources (id)
      DESTINATION KEY (dst) REFERENCES resources (id)
      LABEL LINK PROPERTIES (relation)
  )"""

CASES = [
    Case(
        "single node with a filter",
        PEOPLE,
        "USE g MATCH (p:Person) WHERE p.age > 30 RETURN p.name",
        'SELECT "p".name AS "name" FROM db.people AS "p" WHERE "p".age > 30',
    ),
    Case(
        "right-pointing edge with a variable, an edge property and a renamed property",
        PEOPLE,
        "USE g MATCH (p:Person)-[w:WORKS_AT]->(c:Company) RETURN p.name AS person, c.name AS company, w.since",
        'SELECT "p".name AS "person", "c".legal_name AS "company", "w".since AS "since" '
        'FROM db.people AS "p" '
        'INNER JOIN db.employment AS "w" ON "w".person_id = "p".id '
        'INNER JOIN db.companies AS "c" ON "w".company_id = "c".id',
    ),
    Case(
        "left-pointing anonymous edge",
        PEOPLE,
        "USE g MATCH (c:Company)<-[:WORKS_AT]-(p:Person) RETURN c.name AS company",
        'SELECT "c".legal_name AS "company" FROM db.companies AS "c" '
        'INNER JOIN db.employment AS "$e1" ON "$e1".company_id = "c".id '
        'INNER JOIN db.people AS "p" ON "$e1".person_id = "p".id',
    ),
    Case(
        "two paths sharing a variable, with labels inferred from edges",
        PEOPLE,
        "USE g MATCH (a:Person)-[:KNOWS]->(b), (b)-[:WORKS_AT]->(c) RETURN a.name AS a, c.name AS c",
        'SELECT "a".name AS "a", "c".legal_name AS "c" FROM db.people AS "a" '
        'INNER JOIN db.knows AS "$e1" ON "$e1".a = "a".id '
        'INNER JOIN db.people AS "b" ON "$e1".b = "b".id '
        'INNER JOIN db.employment AS "$e2" ON "$e2".person_id = "b".id '
        'INNER JOIN db.companies AS "c" ON "$e2".company_id = "c".id',
    ),
    Case(
        "edge stored on its vertex table, DISTINCT, ORDER BY an alias, OFFSET and a LIMIT parameter",
        PEOPLE,
        "USE g MATCH (e:Person)-[:MANAGED_BY]->(m) RETURN DISTINCT m.name AS manager "
        "ORDER BY manager DESC OFFSET 2 LIMIT $n",
        'SELECT DISTINCT "m".name AS "manager" FROM db.people AS "e" '
        'INNER JOIN db.people AS "$e1" ON "$e1".id = "e".id '
        'INNER JOIN db.people AS "m" ON "$e1".manager_id = "m".id '
        'ORDER BY "manager" DESC LIMIT $n OFFSET 2',
    ),
    Case(
        "aggregates, GROUP BY, COLLECT_LIST, COUNT(*) and NOT",
        PEOPLE,
        "USE g MATCH (p:Person)-[w:WORKS_AT]->(c:Company) WHERE w.since >= 2020 AND NOT p.age < 18 "
        "RETURN c.name AS company, COUNT(*) AS staff, COLLECT_LIST(DISTINCT p.name) AS names, "
        "AVG(p.age) + 1 AS older GROUP BY company",
        'SELECT "c".legal_name AS "company", COUNT(*) AS "staff", ARRAY_AGG(DISTINCT "p".name) AS "names", '
        'AVG("p".age) + 1 AS "older" FROM db.people AS "p" '
        'INNER JOIN db.employment AS "w" ON "w".person_id = "p".id '
        'INNER JOIN db.companies AS "c" ON "w".company_id = "c".id '
        'WHERE ("w".since >= 2020) AND (NOT ("p".age < 18)) GROUP BY "c".legal_name',
    ),
    Case(
        "OPTIONAL MATCH with a filter on the optional side",
        PEOPLE,
        "USE g MATCH (p:Person) OPTIONAL MATCH (p)-[w:WORKS_AT]->(c:Company) WHERE w.since > 2000 "
        "RETURN p.name AS person, c.name AS company",
        'SELECT "p".name AS "person", "$o1"."$c6" AS "company" FROM db.people AS "p" '
        'LEFT JOIN (SELECT "p".id AS "$c1", "w".person_id AS "$c2", "w".company_id AS "$c3", '
        '"w".since AS "$c4", "c".id AS "$c5", "c".legal_name AS "$c6" FROM db.people AS "p" '
        'INNER JOIN db.employment AS "w" ON "w".person_id = "p".id '
        'INNER JOIN db.companies AS "c" ON "w".company_id = "c".id '
        'WHERE "w".since > 2000) AS "$o1" ON "$o1"."$c1" = "p".id',
    ),
    Case(
        "literals and operators",
        PEOPLE,
        "USE g MATCH (p:Person) WHERE p.name <> 'it''s' AND p.born < DATE '2000-01-01' + DURATION 'P2W' "
        "RETURN CASE WHEN p.age % 2 = 0 THEN 'even' ELSE 'odd' END AS parity, -p.age AS neg, "
        "p.name || '!' AS shout, (p.age > 1) XOR (p.age < 5) AS x, p.born IS NOT NULL AS known, "
        "CURRENT_DATE AS today, 1.5 AS e, 2e3 AS a, TRUE AS t, NULL AS n, "
        "TIMESTAMP '2020-01-01 00:00:00' AS ts",
        "SELECT CASE WHEN (\"p\".age % 2) = 0 THEN 'even' ELSE 'odd' END AS \"parity\", "
        '-"p".age AS "neg", "p".name || \'!\' AS "shout", ("p".age > 1) <> ("p".age < 5) AS "x", '
        '"p".born IS NOT NULL AS "known", CURRENT_DATE AS "today", 1.5 AS "e", 2e3 AS "a", '
        'TRUE AS "t", NULL AS "n", CAST(\'2020-01-01 00:00:00\' AS TIMESTAMP) AS "ts" '
        'FROM db.people AS "p" '
        "WHERE (\"p\".name <> 'it''s') AND (\"p\".born < (CAST('2000-01-01' AS DATE) + INTERVAL '14' DAY))",
    ),
    Case(
        "quantified edge: one derived table of the chains of 1 and 2 repeats",
        PEOPLE,
        "USE g MATCH (a:Person)-[:KNOWS]->{1,2}(b:Person) WHERE a.name = 'x' RETURN b.name AS reached",
        'SELECT "b".name AS "reached" FROM db.people AS "a" '
        'INNER JOIN (SELECT "$e2".a AS "$c1", "$e2".b AS "$c2" FROM db.knows AS "$e2" '
        'UNION ALL SELECT "$e3".a AS "$c1", "$e4".b AS "$c2" FROM db.knows AS "$e3" '
        'INNER JOIN db.people AS "$v5" ON "$e3".b = "$v5".id '
        'INNER JOIN db.knows AS "$e4" ON "$e4".a = "$v5".id) AS "$q1" ON "$q1"."$c1" = "a".id '
        'INNER JOIN db.people AS "b" ON "$q1"."$c2" = "b".id '
        "WHERE \"a\".name = 'x'",
    ),
    Case(
        "left-pointing quantified edge with an exact count",
        PEOPLE,
        "USE g MATCH (a:Person)<-[:KNOWS]-{2}(b:Person) RETURN a.name AS a, b.name AS b",
        'SELECT "a".name AS "a", "b".name AS "b" FROM db.people AS "a" '
        'INNER JOIN (SELECT "$e2".a AS "$c1", "$e3".b AS "$c2" FROM db.knows AS "$e2" '
        'INNER JOIN db.people AS "$v4" ON "$e2".b = "$v4".id '
        'INNER JOIN db.knows AS "$e3" ON "$e3".a = "$v4".id) AS "$q1" ON "$q1"."$c2" = "a".id '
        'INNER JOIN db.people AS "b" ON "$q1"."$c1" = "b".id',
    ),
    Case(
        "quantified edge whose WHERE applies to every repeat, with an aggregate",
        LINKS,
        "USE g MATCH (s:Resource)-[r:LINK WHERE r.relation = 'impersonates']->{1,2}(t:Resource) "
        "WHERE s.name = 'web' RETURN t.name AS reached, COUNT(*) AS walks GROUP BY reached",
        'SELECT "t".name AS "reached", COUNT(*) AS "walks" FROM db.resources AS "s" '
        'INNER JOIN (SELECT "$e2".src AS "$c1", "$e2".dst AS "$c2" FROM db.links AS "$e2" '
        "WHERE \"$e2\".relation = 'impersonates' "
        'UNION ALL SELECT "$e3".src AS "$c1", "$e4".dst AS "$c2" FROM db.links AS "$e3" '
        'INNER JOIN db.resources AS "$v5" ON "$e3".dst = "$v5".id '
        'INNER JOIN db.links AS "$e4" ON "$e4".src = "$v5".id '
        "WHERE (\"$e3\".relation = 'impersonates') AND (\"$e4\".relation = 'impersonates')) "
        'AS "$q1" ON "$q1"."$c1" = "s".id '
        'INNER JOIN db.resources AS "t" ON "$q1"."$c2" = "t".id '
        "WHERE \"s\".name = 'web' GROUP BY \"t\".name",
    ),
    Case(
        "quoted DDL names and disconnected paths",
        ODD,
        "USE `My Graph` MATCH (a:Thing), (b:Thing) WHERE a.value = b.value RETURN a.key AS k",
        'SELECT "a"."Key Col" AS "k" FROM "Odd Schema"."Weird Table" AS "a" '
        'CROSS JOIN "Odd Schema"."Weird Table" AS "b" WHERE "a"."Value" = "b"."Value"',
    ),
]
