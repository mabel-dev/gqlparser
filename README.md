# gqlparser

A parser for **GQL** (ISO/IEC 39075), the ISO graph query language, written in
pure Python with no dependencies.

`gqlparser` turns GQL text into a syntax tree. Given a property graph
definition (SQL/PGQ `CREATE PROPERTY GRAPH`), it can also lower a query to
one SQL `SELECT` over the tables behind the graph, so that any SQL engine can
run it. It does not execute queries itself. It was written for
[Opteryx](https://github.com/mabel-dev/opteryx-core).

> Not GraphQL. GQL is the ISO standard graph query language
> (`MATCH (a:Person)-[:KNOWS]->(b) RETURN b.name`).

## Status

**Early.**
- `parse` reads one query made of fixed-length `MATCH` patterns followed by
  `RETURN` (the subset below).
- `parse_property_graph` reads a graph definition.
- `lower` turns a query into SQL.
- `tokenize` exposes the lexer on its own.

## Usage

```python
import gqlparser

query = gqlparser.parse("""
    USE social
    MATCH (a:Person)-[:KNOWS]->(b:Person)
    WHERE a.age > 30
    RETURN b.name AS friend, COUNT(*) AS n
    GROUP BY friend
    ORDER BY n DESC
    LIMIT 10
""")

query.match_statements[0].paths[0].elements[1].label.name   # 'KNOWS'
query.return_statement.limit.text                           # '10'
```

The node types are in `gqlparser.ast`. A `Query` holds an optional `graph`,
one or more `MatchStatement`s, and a `ReturnStatement`.

### Lowering to SQL

```python
graph = gqlparser.parse_property_graph("""
    CREATE PROPERTY GRAPH social
      VERTEX TABLES (db.people KEY (id) LABEL Person PROPERTIES (id, name, age))
      EDGE TABLES (db.knows KEY (a, b)
        SOURCE KEY (a) REFERENCES people (id)
        DESTINATION KEY (b) REFERENCES people (id)
        LABEL KNOWS NO PROPERTIES)
""")
statement = gqlparser.lower(query, graph)   # a sqlparser-rs statement dict

from opteryx.compute import restore_ast    # or sqloxide's restore_ast
sql = restore_ast([statement])[0]
```

`lower` returns the statement dict that
[sqlparser-rs](https://github.com/apache/datafusion-sqlparser-rs) serialises.
gqlparser builds the dict itself and does not depend on sqlparser. The dict's
shape belongs to one sqlparser-rs release, given by `gqlparser.SQLPARSER_FORMAT`
(currently `"0.63"`); render it with a matching release.

The SQL it produces:
- **Joins:** each element variable is a table alias. Each edge joins its
  endpoints on the `SOURCE` / `DESTINATION` `KEY ... REFERENCES` columns.
- **Successive `MATCH` statements** are inner joins. An `OPTIONAL MATCH` is a
  derived table that is `LEFT JOIN`ed on the variables it shares with earlier
  statements.
- **Names:** GQL names are double-quoted. Table and column names keep the
  quoting the DDL used.
- **Precedence:** every compound operand is parenthesised, so the result
  never depends on the target engine's operator precedence.
- **Temporal values:** temporal literals become `CAST('...' AS DATE)` (or
  `TIME` / `TIMESTAMP`), `DURATION 'P30D'` becomes `INTERVAL '30' DAY`, and
  `COLLECT_LIST` becomes `ARRAY_AGG`.
- **Labels and match mode:** a node without a label takes it from an adjacent
  edge when the graph makes that unambiguous. The match mode is repeatable
  elements, so no edge-inequality predicates are added.

`parse_property_graph` reads structure only. A definition that needs a
table's schema to complete (`PROPERTIES ALL COLUMNS`, a missing `PROPERTIES`
or `KEY` clause, foreign-key defaults) is refused with `GqlNotSupportedError`.
Use the fully written-out definition an engine's
`SHOW CREATE PROPERTY GRAPH` returns.

### Tokens

Each `Token` has a `kind` (`TokenKind`), a decoded `value` and a `Span`. The
lexer leaves two decisions to the parser:
- **Keywords:** every word is a `WORD`, because which words are keywords
  depends on where they appear.
- **Double-quoted text:** GQL uses `"..."` for both strings and delimited
  identifiers, so it is lexed as `DOUBLE_QUOTED` and the context decides
  which.

## Contract

- **Input:** GQL text. **Output:** a syntax tree of frozen dataclasses. Every
  node carries a `Span` giving its position in the source.
- **Three errors.** Each carries the line and column where the problem starts.
  They are siblings under `GqlError`, so none is a subclass of another.
  - `GqlSemanticError`: the text is well-formed but means nothing here. This
    covers an unknown label, property or variable, an edge written against
    its declared direction, a query whose `USE` names a different graph, and
    a graph definition that contradicts itself. Raised by `lower` and
    `parse_property_graph`.
  - `GqlNotSupportedError`: the text uses a GQL construct that this release
    recognises but doesn't support yet. The error names the construct. Every
    construct in the "not supported" lists below raises this error.
  - `GqlSyntaxError`: the parser could not read the text. Usually that means
    the text is not GQL. But the parser does not know all of GQL, so **valid
    GQL it doesn't recognise at all is also reported here**. The "not
    supported" lists grow as more constructs are recognised.
- **Leniency.** The parser enforces only part of GQL's reserved-word list. It
  refuses the expression keywords as variable and alias names, but accepts
  any word as a label, property or graph name (`:Order`). It also accepts
  `DISTINCT` in any function call and doesn't check function names. These
  leniencies accept some text that GQL itself would reject, but they never
  change what valid text means.
- **No approximation.** A construct is either parsed fully or refused. Nothing
  is skipped, and no query parses into a tree that means something else.
- **The syntax tree is public API**, versioned under semver. Before 1.0, node
  names and fields may change; every change is listed in
  [CHANGELOG.md](CHANGELOG.md).

## Supported subset

The full 39075 grammar is very large, so `gqlparser` declares the subset it
supports, and that subset grows release by release.

**Lexical elements**

| Construct | Since |
|---|---|
| words (regular identifiers and keywords) | 0.0.1 |
| `'...'`, `"..."` and `` `...` `` quoted text, with a doubled quote as the only escape | 0.0.1 |
| unsigned integers (`42`), exact numbers (`1.5`, `.5`, `1.`) and approximate numbers (`1e3`, `1.5E-2`) | 0.0.1 |
| `$name` parameters | 0.0.1 |
| punctuation: `( ) [ ] { } , . : ; = < > + - * / % \| & ! ? ~ ^` and `<= >= <> \|\| -> <-` | 0.0.1 |
| `//` and `/* */` comments | 0.0.1 |

**Lexical elements not supported yet.** Each raises `GqlNotSupportedError`
naming the construct:
- backslash escapes in quoted text, and the `@` no-escape prefix;
- `--` comments. GQL reads `--` as a comment, which would make Cypher's
  `-->` silently drop the rest of the line, so it is refused instead;
- hexadecimal, octal and binary numbers, `_` digit separators, and number
  suffixes (`1.5f`, `2m`);
- substituted parameters (`$$name`) and quoted parameter names (`$"name"`);
- identifiers that start with connector punctuation other than `_`.

**Queries**

| Construct | Since |
|---|---|
| `[USE graph] MATCH ... [MATCH ...] RETURN ...`, a single query | 0.0.1 |
| a qualified graph name in `USE` (`USE ws.coll.g`) | 0.0.2 |
| `MATCH` and `OPTIONAL MATCH`, with comma-separated paths and a `WHERE` | 0.0.1 |
| node patterns `(v:Label WHERE ...)`; each part optional | 0.0.1 |
| full edge patterns `-[e:Label WHERE ...]->` and `<-[e:Label WHERE ...]-`; each part optional | 0.0.1 |
| `RETURN [DISTINCT \| ALL] expr [AS name], ...` | 0.0.1 |
| `GROUP BY name, ...` | 0.0.1 |
| `ORDER BY expr [ASC \| ASCENDING \| DESC \| DESCENDING], ...` | 0.0.1 |
| `OFFSET n` (or `SKIP n`), then `LIMIT n`, where `n` is a whole number or a `$parameter` | 0.0.1 |

**Expressions** (precedence lowest first: `OR`, `XOR`, `AND`, `NOT`, comparison and
`IS [NOT] NULL`, `\|\|`, `+ -`, `* / %`, unary `+ -`, property access)

| Construct | Since |
|---|---|
| literals: numbers (with their source text kept), `'strings'`, `TRUE`, `FALSE`, `NULL` | 0.0.1 |
| `DATE`, `TIME`, `DATETIME`, `TIMESTAMP` and `DURATION '...'` literals | 0.0.1 |
| `CURRENT_DATE`, `CURRENT_TIME`, `CURRENT_TIMESTAMP` | 0.0.1 |
| `$parameters`, variables (including `` `quoted` `` ones), `v.property` | 0.0.1 |
| `OR XOR AND NOT`, `= <> < <= > >=`, `IS [NOT] NULL`, `\|\| + - * / %`, unary `+ -` | 0.0.1 |
| function calls `name([DISTINCT \| ALL] args)` and `COUNT(*)` | 0.0.1 |
| simple and searched `CASE ... END` | 0.0.1 |

**Not supported yet.** Each raises `GqlNotSupportedError` naming the construct:
- **edges:** abbreviated edges (`->`, `<-`, `-`), any-direction edges
  (`-[...]-`), `<-[...]->`, and undirected `~` edges;
- **paths:** quantified paths (`{m,n}`, `*`, `+`, `?`), parenthesized path
  patterns, path variables (`p = ...`), search prefixes and path modes
  (`ANY`, `SHORTEST`, `TRAIL`, ...), match modes (`DIFFERENT EDGES`, ...),
  `KEEP` and `YIELD`;
- **labels:** label expressions (`|`, `&`, `!`, `%`), `IS` labels, more than one
  label, and property specifications (`{key: value}`);
- **statements:** a query without `MATCH`, `OPTIONAL { }` blocks, `LET`,
  `FILTER`, `FOR`, `CALL`, a standalone `ORDER BY` / `OFFSET` / `LIMIT`, data
  modification (`INSERT`, `SET`, `REMOVE`, `DELETE`), session and catalog
  statements, and composite queries (`NEXT`, `UNION`, `EXCEPT`, `INTERSECT`,
  `OTHERWISE`);
- **RETURN:** `RETURN *`, `GROUP BY ()`, `NULLS FIRST` / `NULLS LAST`, and
  `OFFSET` after `LIMIT`;
- **expressions:** chained comparisons (`a < b < c`), `!=` (write `<>`),
  `IS TRUE` and the other `IS` predicates, list and record literals,
  double-quoted text (ambiguous: a string or a name), `UNKNOWN`, `EXISTS`,
  comparisons and several values in a simple `CASE` `WHEN`, and expressions
  nested more than 32 deep.

**Not supported yet by `lower`.** These queries parse, but `lower` raises
`GqlNotSupportedError` for them:
- **`OPTIONAL MATCH`:** as the first statement; one that shares no variable
  with earlier statements; or one whose `WHERE` refers to a variable its own
  pattern doesn't mention;
- **element variables as values:** `RETURN p`, `COUNT(p)`, `GROUP BY p`;
  property access on anything but an element variable (`p.a.b`);
- **RETURN:** an item that isn't a property and has no `AS`; aggregates mixed
  with other items without `GROUP BY`;
- **durations:** anything but one whole-number unit (`'P1Y2M'`, `'PT1.5S'`);
- **variable names:** names starting with `$`.

The SQL that `lower` produces is checked two ways. The tests compare it,
construct by construct, with sqlparser-rs's own parse of hand-written SQL
(`tests/lowering_cases.py`; regenerate the fixtures with
`dev/make_lowering_fixtures.py`). It has also been run against Opteryx and
compared with hand-written SQL over real data.

## Development

```bash
python -m pytest tests
```

CI (`.github/workflows/tests.yaml`) runs the tests on Python 3.11 to 3.14 for
every push and pull request, and checks that the package builds.

## Releasing

1. Set the version in `gqlparser/__version__.py` and move the CHANGELOG entry
   from "unreleased" to that version.
2. Push a tag `gqlparser-<version>`, for example `gqlparser-0.0.1`.

`.github/workflows/release.yaml` then:
- runs the tests;
- refuses the release if the tag doesn't match `__version__`;
- publishes the wheel and sdist to PyPI.

A version can be published only once.

## License

Apache License 2.0. See [LICENSE](LICENSE).
