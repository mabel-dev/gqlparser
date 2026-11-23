"""End to end: GQL lowered by gqlparser, run on Opteryx, against hand-written SQL.

Needs `opteryx` importable and opteryx-core's `testdata/` (tpch_01, org), so run
it from an opteryx-core checkout where the engine is built:

    python ../gqlparser/dev/check_against_opteryx.py

Each case must return the same rows as its hand-written SQL (in order when the
GQL has ORDER BY, as a multiset otherwise). Exits non-zero on any difference.
"""

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
# The opteryx-core checkout this is run from. Python puts the script's own directory
# on sys.path, not the working directory, so without this an INSTALLED opteryx is used.
sys.path.insert(0, os.getcwd())
import opteryx
from opteryx.compute import restore_ast

import gqlparser

TPCH = """CREATE PROPERTY GRAPH tpch
  VERTEX TABLES (
    testdata.tpch_01.customer KEY (c_custkey) LABEL Customer
      PROPERTIES (c_custkey AS key, c_name AS name, c_acctbal AS balance, c_mktsegment AS segment),
    testdata.tpch_01.orders KEY (o_orderkey) LABEL "Order"
      PROPERTIES (o_orderkey AS key, o_totalprice AS total, o_orderdate AS date, o_orderstatus AS status),
    testdata.tpch_01.nation KEY (n_nationkey) LABEL Nation PROPERTIES (n_name AS name),
    testdata.tpch_01.region KEY (r_regionkey) LABEL Region PROPERTIES (r_name AS name),
    testdata.tpch_01.supplier KEY (s_suppkey) LABEL Supplier PROPERTIES (s_name AS name, s_acctbal AS balance),
    testdata.tpch_01.part KEY (p_partkey) LABEL Part PROPERTIES (p_partkey AS key, p_name AS name, p_size AS size)
  )
  EDGE TABLES (
    testdata.tpch_01.orders AS placed KEY (o_orderkey)
      SOURCE KEY (o_custkey) REFERENCES customer (c_custkey)
      DESTINATION KEY (o_orderkey) REFERENCES orders (o_orderkey)
      LABEL PLACED NO PROPERTIES,
    testdata.tpch_01.customer AS customer_in KEY (c_custkey)
      SOURCE KEY (c_custkey) REFERENCES customer (c_custkey)
      DESTINATION KEY (c_nationkey) REFERENCES nation (n_nationkey)
      LABEL IN_NATION NO PROPERTIES,
    testdata.tpch_01.nation AS nation_in KEY (n_nationkey)
      SOURCE KEY (n_nationkey) REFERENCES nation (n_nationkey)
      DESTINATION KEY (n_regionkey) REFERENCES region (r_regionkey)
      LABEL IN_REGION NO PROPERTIES,
    testdata.tpch_01.partsupp AS supplies KEY (ps_partkey, ps_suppkey)
      SOURCE KEY (ps_suppkey) REFERENCES supplier (s_suppkey)
      DESTINATION KEY (ps_partkey) REFERENCES part (p_partkey)
      LABEL SUPPLIES PROPERTIES (ps_availqty AS qty, ps_supplycost AS cost)
  )"""

ORG = """CREATE PROPERTY GRAPH org
  VERTEX TABLES (
    testdata.org.emp KEY (empno) LABEL Employee PROPERTIES (empno AS id, ename AS name, job, sal AS salary)
  )
  EDGE TABLES (
    testdata.org.emp AS reports KEY (empno)
      SOURCE KEY (empno) REFERENCES emp (empno)
      DESTINATION KEY (mgr) REFERENCES emp (empno)
      LABEL REPORTS_TO NO PROPERTIES
  )"""

T = "testdata.tpch_01."
CASES = [
    (
        "multi-hop + second MATCH + GROUP BY",
        TPCH,
        """USE tpch MATCH (c:Customer)-[:IN_NATION]->(n:Nation)-[:IN_REGION]->(r:Region) WHERE r.name = 'EUROPE'
        MATCH (c)-[:PLACED]->(o:`Order`) RETURN n.name AS nation, COUNT(*) AS orders, SUM(o.total) AS total GROUP BY nation""",
        f"""SELECT n.n_name, COUNT(*), SUM(o.o_totalprice) FROM {T}customer c JOIN {T}nation n ON c.c_nationkey = n.n_nationkey
        JOIN {T}region r ON n.n_regionkey = r.r_regionkey JOIN {T}orders o ON o.o_custkey = c.c_custkey
        WHERE r.r_name = 'EUROPE' GROUP BY n.n_name""",
    ),
    (
        "left-pointing edge, composite key, edge property",
        TPCH,
        """USE tpch MATCH (p:Part)<-[s:SUPPLIES]-(x:Supplier) WHERE p.size = 15 RETURN x.name AS supplier, p.name AS part, s.cost AS cost""",
        f"""SELECT x.s_name, p.p_name, s.ps_supplycost FROM {T}part p JOIN {T}partsupp s ON s.ps_partkey = p.p_partkey
        JOIN {T}supplier x ON s.ps_suppkey = x.s_suppkey WHERE p.p_size = 15""",
    ),
    (
        "OPTIONAL MATCH keeps customers with no orders",
        TPCH,
        """USE tpch MATCH (c:Customer) WHERE c.balance > 9000 OPTIONAL MATCH (c)-[:PLACED]->(o:`Order`)
        RETURN c.key AS key, COUNT(o.key) AS orders GROUP BY key""",
        f"""SELECT c.c_custkey, COUNT(o.o_orderkey) FROM {T}customer c LEFT JOIN {T}orders o ON o.o_custkey = c.c_custkey
        WHERE c.c_acctbal > 9000 GROUP BY c.c_custkey""",
    ),
    (
        "OPTIONAL MATCH with a filter on the optional side (the LEFT JOIN trap)",
        TPCH,
        """USE tpch MATCH (c:Customer) WHERE c.balance > 9500 OPTIONAL MATCH (c)-[:PLACED]->(o:`Order`) WHERE o.status = 'F'
        RETURN c.key AS key, COUNT(o.key) AS finished GROUP BY key""",
        f"""SELECT c.c_custkey, COUNT(o.o_orderkey) FROM {T}customer c
        LEFT JOIN (SELECT * FROM {T}orders WHERE o_orderstatus = 'F') AS o ON o.o_custkey = c.c_custkey
        WHERE c.c_acctbal > 9500 GROUP BY c.c_custkey""",
    ),
    (
        "self-edge on its own vertex table",
        ORG,
        """USE org MATCH (e:Employee)-[:REPORTS_TO]->(m:Employee) RETURN e.name AS employee, m.name AS manager""",
        """SELECT e.ename, m.ename FROM testdata.org.emp e JOIN testdata.org.emp m ON e.mgr = m.empno""",
    ),
    (
        "two hops, labels inferred from the edges",
        ORG,
        """USE org MATCH (e:Employee)-[:REPORTS_TO]->(m)-[:REPORTS_TO]->(b) RETURN e.name AS employee, b.name AS boss""",
        """SELECT e.ename, b.ename FROM testdata.org.emp e JOIN testdata.org.emp m ON e.mgr = m.empno
        JOIN testdata.org.emp b ON m.mgr = b.empno""",
    ),
    (
        "DISTINCT, CASE, DATE + DURATION",
        TPCH,
        """USE tpch MATCH (o:`Order`) WHERE o.date >= DATE '1995-01-01' AND o.date < DATE '1995-01-01' + DURATION 'P30D'
        RETURN DISTINCT CASE WHEN o.total > 100000 THEN 'big' ELSE 'small' END AS size, o.status AS status""",
        f"""SELECT DISTINCT CASE WHEN o_totalprice > 100000 THEN 'big' ELSE 'small' END, o_orderstatus FROM {T}orders
        WHERE o_orderdate >= CAST('1995-01-01' AS DATE) AND o_orderdate < CAST('1995-01-01' AS DATE) + INTERVAL '30' DAY""",
    ),
    (
        "ORDER BY alias DESC + LIMIT (top 5 compared in order)",
        TPCH,
        """USE tpch MATCH (c:Customer)-[:PLACED]->(o:`Order`) RETURN c.key AS key, SUM(o.total) AS spend GROUP BY key ORDER BY spend DESC LIMIT 5""",
        f"""SELECT c.c_custkey, SUM(o.o_totalprice) AS spend FROM {T}customer c JOIN {T}orders o ON o.o_custkey = c.c_custkey
        GROUP BY c.c_custkey ORDER BY spend DESC LIMIT 5""",
    ),
]


def rows(sql):
    s = opteryx.session()
    out = []
    for m in s.execute_to_morsels(sql):
        cols = [m.column(n).to_pylist() for n in m.column_names]
        out.extend(zip(*cols))
    return out


graphs = {}
failed = 0
for name, ddl, gql, sql in CASES:
    graph = graphs.setdefault(ddl, gqlparser.parse_property_graph(ddl))
    lowered = restore_ast([gqlparser.lower(gqlparser.parse(gql), graph)])[0]
    got, want = rows(lowered), rows(sql)
    ordered = "ORDER BY" in gql
    same = (got == want) if ordered else (sorted(map(repr, got)) == sorted(map(repr, want)))
    failed += not same
    print(("PASS" if same else "FAIL"), f"{len(got):>6} rows  {name}")
    if not same:
        print("   lowered:", lowered)
        print("   got[:3] ", got[:3])
        print("   want[:3]", want[:3])

g = graphs[TPCH]
for bad in [
    "USE tpch MATCH (o:`Order`)-[:PLACED]->(c:Customer) RETURN c.name AS n",
    "USE tpch MATCH (c:Customer) RETURN c.nmae AS n",
    "USE tpch MATCH (c:Custmer) RETURN c.name AS n",
]:
    try:
        gqlparser.lower(gqlparser.parse(bad), g)
        print("NO ERROR", bad)
        failed += 1
    except gqlparser.GqlSemanticError as e:
        print("error ok:", e)
sys.exit(1 if failed else 0)
