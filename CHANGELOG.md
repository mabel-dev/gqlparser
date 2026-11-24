# Changelog

Every change to the syntax tree (a node added, renamed or reshaped) is listed
here, because the tree is public API.

## 0.0.3 (2026-11-24)

- Bounded quantified edges: `-[e:L]->{m}` and `{m,n}` (`1 <= m <= n <= 16`). **Syntax tree
  change:** new node `Quantifier(minimum, maximum, span)`, and `EdgePattern` gains a
  `quantifier` field (`None` when absent) before `span`. `lower` turns a quantified edge
  into a derived table, the `UNION ALL` of the chains of `m` to `n` repeats. `*`, `+`, `?`,
  `{m,}`, `{,n}`, `{0,n}` and bounds above 16 raise `GqlNotSupportedError`.

## 0.0.2 (2026-11-23)

- Lowering to SQL: `lower(query, graph)` returns a sqlparser-rs statement dict,
  in the format of the release named by `SQLPARSER_FORMAT` (`"0.63"`).
- Graph definitions: `parse_property_graph(text)` reads a SQL/PGQ
  `CREATE PROPERTY GRAPH` into a `PropertyGraph`. The model in `gqlparser.graph`
  is `PropertyGraph`, `VertexTable`, `EdgeTable`, `EdgeEndpoint` and `SqlName`.
- A third error, `GqlSemanticError`: well-formed text that means nothing here.
- `USE` takes a qualified graph name. **Syntax tree change:** `Query.graph` is
  now a tuple of `Identifier`s, one per part, instead of one `Identifier`.
  `lower` accepts the graph's full name or its last part; a delimited name
  that contains dots (`` `ws.coll.g` ``) is one part, so it no longer matches.

## 0.0.1 (2026-11-23)

- Scaffold: package layout, `GqlSyntaxError` / `GqlNotSupportedError`, `Span`,
  and a `parse` that supports no constructs yet.
- Lexer: `tokenize`, `Token` and `TokenKind`, covering the lexical subset
  listed in the README.
- Parser: `parse` returns a `Query`. New syntax tree nodes in `gqlparser.ast`:
  - **names:** `Identifier`;
  - **query structure:** `Query`, `MatchStatement`, `PathPattern`,
    `NodePattern`, `EdgePattern` (with `EdgeDirection`), `ReturnStatement`,
    `ReturnItem`, `SortSpecification`;
  - **literals:** `IntegerLiteral`, `ExactNumberLiteral`,
    `ApproximateNumberLiteral`, `StringLiteral`, `BooleanLiteral`,
    `NullLiteral`, `TemporalLiteral`, `CurrentTemporal`;
  - **other expressions:** `Parameter`, `Variable`, `PropertyReference`,
    `UnaryOperation`, `BinaryOperation`, `IsNull`, `FunctionCall`,
    `CountStar`, `Case`, `WhenClause`;
  - the `Expression` union.
