# Lowering GQL to SQL

**Status:** BUILT, 2026-11-23, in 0.0.2 (unreleased). The body below is the
proposal as written. Where the build differs from it, the difference is
recorded here:

1. **The error type is `GqlSemanticError`**, not `GqlGraphError` (open
   question 1). It also covers errors that have nothing to do with the graph:
   an unknown variable, a `GROUP BY` name that matches no item, duplicate
   `RETURN` names, and a graph definition that contradicts itself.
2. **Unlabelled nodes are inferred, not refused.** A node without a label takes
   the vertex table its adjacent edges require. It is refused only when no
   edge implies a table, or when two edges imply different ones. Edges still
   need a label.
3. **Temporal literals lower to `CAST('...' AS DATE)`**, not `DATE '...'`.
   Opteryx refuses typed-string literals, and `CAST` is standard SQL.
4. **`OPTIONAL MATCH` refusals.** Two shapes are refused as not supported: an
   `OPTIONAL MATCH` that shares no variable with earlier statements (it would
   need `LEFT JOIN ... ON TRUE`, which Opteryx refuses), and one as the first
   statement.
5. **What an `OPTIONAL MATCH` projects.** Its derived table projects every
   column the graph knows for each newly bound element: keys, properties, and
   the columns edges reference. Later statements can therefore use any of
   them, and the engine's projection pushdown drops the ones they don't. A
   variable it shares with earlier statements is re-scanned inside the
   derived table and matched on its `KEY` (the lazy approach of D5). A
   `WHERE` that refers to a variable outside its own pattern is refused,
   because that would need `LATERAL`.
6. **Element variables as values** (`RETURN p`, `COUNT(p)`, `GROUP BY p`), and
   property access on anything but an element variable, are refused as not
   supported.
7. **Disconnected paths** (`MATCH (a), (b)`) lower to `CROSS JOIN`. Within one
   `MATCH`, relations are joined in an order that keeps each join connected
   to an earlier one wherever the pattern allows.
8. **Spans for generated identifiers** (open question 2) are zero-width, at
   the start of the GQL element that produced them. Every span is
   zero-width, because a GQL `Span` records only where it starts.
9. **End-to-end check:** 8 queries compared against hand-written SQL over
   opteryx-core's `testdata/tpch_01` and `testdata/org`, and all 8 return the
   same rows. They cover multi-hop paths, a composite key, edge properties,
   `OPTIONAL MATCH` including the filtered-optional-side trap, self-edges,
   inferred labels, `DISTINCT`, `CASE`, date arithmetic and ordered `LIMIT`.
   The check is `dev/check_against_opteryx.py`, run from an opteryx-core
   checkout. It belongs in opteryx-core's tests once the engine accepts the
   graph DDL.

The engine side (the property graph DDL, the catalog and
`SHOW CREATE PROPERTY GRAPH`) is designed in opteryx-core's
`docs/PROPERTY_GRAPH_DESIGN.md`. This document covers what gqlparser adds.

## What gqlparser adds

```python
graph = gqlparser.parse_property_graph(ddl_text)    # -> PropertyGraph
statement = gqlparser.lower(query, graph)            # -> sqlparser statement dict
```

- **`parse_property_graph(text)`:** reads a SQL/PGQ `CREATE PROPERTY GRAPH`
  statement into a `PropertyGraph` (plain frozen dataclasses: vertex tables,
  edge tables, keys, labels, properties).
- **`lower(query, graph)`:** turns a parsed GQL `Query` into one SQL `SELECT`,
  expressed as the statement dict that sqlparser-rs serialises. In Opteryx it
  is rendered with `opteryx.compute.restore_ast`. Other Python users can
  render it with sqloxide.

Both stay pure Python with no dependencies. gqlparser never imports sqlparser
or Opteryx: it builds the dict by hand.

**Rulings (architect, 2026-11-22 / 23).**
- **The output (D1):** the lowering emits the sqlparser dict, for
  portability.
- **Running it (2a):** the caller renders it to SQL text.
- **The glue (3):** the user's code fetches the definition and runs the SQL.
- **Match mode (D4):** repeatable elements.
- **Self-joins (D5):** an edge on a vertex table is a plain self-join.
- **Errors (D6):** a wrong-way edge gets a friendly error.

## 1. `parse_property_graph`

It accepts the DDL subset in opteryx-core's design §1. It reuses the GQL lexer:
SQL's tokens are a subset of the lexer's, and double-quoted text in a name
position is an identifier in both languages.

**It is a structural reader, not a validator.** It checks that:
- names are unique;
- `REFERENCES` targets are vertex elements of this graph;
- key arities match.

It cannot check that tables or columns exist, because it has no catalog.
That is the engine's job at CREATE time.

**Forms it cannot complete without a schema** (`PROPERTIES ALL COLUMNS`, an
absent `PROPERTIES` clause, foreign-key defaults) raise
`GqlNotSupportedError`. The error says to use the canonical text that
`SHOW CREATE PROPERTY GRAPH` returns, which always has these written out.

## 2. `lower`

### Errors

A third error type, **`GqlGraphError`**, a sibling of `GqlSyntaxError` and
`GqlNotSupportedError` under `GqlError`. It covers a query that is valid GQL
but doesn't fit the graph:
- the `USE` name differs from `graph.name` (or the query has no `USE`);
- an unknown label, listing the graph's labels with a closest-match suggestion;
- a property not declared on its label, naming the label and its properties;
- a variable used with two different labels;
- **a wrong-way edge (D6)**, for example "`BILLED_BY` points from `Supplier`
  to `Invoice`; write `(s:Supplier)-[:BILLED_BY]->(i:Invoice)`";
- an unlabelled element. Every element needs a label until label unions
  exist.

Each error carries the line and column of the GQL text at fault.

### Rules

| GQL | SQL |
|---|---|
| first binding of `(v:L)` | the relation of `L`, aliased `v` |
| `(a)-[e:L]->(b)` | the relation of `L` aliased `e`, joined `e.<src key> = a.<key>`; `b` joined `e.<dst key> = b.<key>` |
| `(a)<-[e:L]-(b)` | the same with source and destination swapped |
| composite keys | AND of the column equalities, in declaration order |
| a variable bound again | join predicates only, no new relation |
| anonymous element | a generated alias `$e1`, `$v1`, ... (cannot collide with a GQL variable) |
| element `WHERE`, `MATCH ... WHERE` | ANDed into that `MATCH`'s filter |
| successive `MATCH` | inner joins on shared variables |
| `OPTIONAL MATCH` block | the block lowered **as one derived table** (its joins and its `WHERE`), `LEFT JOIN`ed on the shared variables |
| `v.p` | `v.<column of p>` |
| `RETURN` items, `DISTINCT` | the `SELECT` list, `SELECT DISTINCT` |
| `GROUP BY name` | `GROUP BY` the *expression* that `name` aliases (not the alias itself, for portability) |
| `ORDER BY`, `OFFSET` / `SKIP`, `LIMIT` | `ORDER BY` (aliases allowed, as in standard SQL), `OFFSET`, `LIMIT` |
| `COUNT(*)`, `COUNT/SUM/AVG/MIN/MAX` | the same |
| `COLLECT_LIST` | `ARRAY_AGG` |
| other function calls | passed through by name; the target engine resolves them |
| `XOR` | `<>` between the two boolean operands (same three-valued result) |
| `DURATION 'P30D'` | `INTERVAL '30' DAY`; durations with more than one unit are refused as not supported |
| `DATE '...'`, `TIMESTAMP '...'`, `CURRENT_*` | the same |
| `$name` | a placeholder `$name`; binding values is the caller's job |

**Match mode (D4).** The default is repeatable elements, so the lowering emits
no edge-inequality predicates. An explicit `DIFFERENT EDGES` is already refused
by the parser.

**Self-joins (D5).** An edge stored on a vertex table (a foreign key on the
row) lowers to a plain self-join on that table's key. Reading the table once
for several scans is left to the engine's optimizer.

**Output names.** An item without `AS` is named after its property (`v.p` is
`p`). Any other expression without `AS` is refused, because naming
conventions for computed columns differ between engines.

### Output format

**Spans.** sqlparser's dict requires a `span` on every identifier, and
rendering fails without one. The lowering fills spans with the GQL source
positions of the construct that produced each identifier. Generated
identifiers (anonymous aliases, key columns) take the span of the pattern
element they came from.

**Version coupling.** The dict shape is that of a specific sqlparser-rs
release. gqlparser declares the release it targets, as
`gqlparser.SQLPARSER_FORMAT` (initially `"0.63"`, the release Opteryx uses).
When Opteryx moves to a sqlparser release whose dict shape differs, gqlparser
ships a release that targets it. The README states the pairing.

## 3. Tests

- **Lowering shape:** each GQL query has an expected SQL text. A dev script
  (not a test dependency) converts the expected SQL to a dict with
  `opteryx.compute.parse_sql` and stores it as a JSON fixture. The test
  compares `lower()`'s dict with the fixture, spans excluded. gqlparser's
  tests stay dependency-free.
- **`parse_property_graph`:** every DDL form, plus refusals of schema-needing
  forms.
- **`GqlGraphError`:** one test per error in §2, each checking the message and
  position.
- **The `OPTIONAL MATCH` trap:** a block with a filter on an inner element must
  lower to a derived table, not to a chain of LEFT JOINs.
- **End to end:** results compared with hand-written SQL. These live in
  opteryx-core's tests, which install gqlparser from PyPI.

## Open questions

1. **The third error type.** Is `GqlGraphError` the right name? Should it
   exist at all, rather than reusing `GqlSyntaxError`? I recommend a separate
   type: a query can be perfectly good GQL and still not fit a particular
   graph.
2. **Spans for generated identifiers.** These take the source element's span.
   An alternative is a zero-width span at the start of the query. Nothing
   depends on the choice today.
