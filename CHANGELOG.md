# Changelog

Every change to the syntax tree (a node added, renamed or reshaped) is listed
here, because the tree is public API.

## 0.0.1 (unreleased)

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
