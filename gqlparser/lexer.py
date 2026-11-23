"""GQL lexer: text to tokens, each with its source position.

The lexer knows GQL's lexical elements, not its grammar, so it leaves two
questions to the parser:

- whether a word is a keyword: keywords are case-insensitive, and the parser
  knows which word it expects where;
- what double-quoted text means: GQL uses it for both strings and delimited
  identifiers, so the token records only the quote form it saw.

Anything the lexer does not handle yet raises `GqlNotSupportedError`. It never
skips text it does not understand.
"""

from dataclasses import dataclass
from enum import Enum

from gqlparser.ast import Span
from gqlparser.errors import GqlNotSupportedError
from gqlparser.errors import GqlSyntaxError


class TokenKind(Enum):
    WORD = "word"  # a regular identifier or a keyword, as written
    SINGLE_QUOTED = "single_quoted"  # '...'
    DOUBLE_QUOTED = "double_quoted"  # "...": a string or an identifier, by context
    ACCENT_QUOTED = "accent_quoted"  # `...`
    INTEGER = "integer"  # 42
    EXACT_NUMBER = "exact_number"  # 1.5  .5  1.
    APPROXIMATE_NUMBER = "approximate_number"  # 1e3  1.5E-2
    PARAMETER = "parameter"  # $name
    PUNCTUATION = "punctuation"  # ( -> <= ...
    END = "end"  # end of input


@dataclass(frozen=True, slots=True)
class Token:
    """One token.

    `value` is decoded: the word as written, quoted text with its doubled
    quotes collapsed, the number's digits, a parameter's name without the `$`,
    or the punctuation itself. `span` covers the token's raw source text.
    """

    kind: TokenKind
    value: str
    span: Span


_DIGITS = "0123456789"
_LINE_BREAKS = "\n\r\u0085  "
_PUNCTUATION_2 = ("<=", ">=", "<>", "||", "->", "<-")
_PUNCTUATION_1 = "()[]{},.:;=<>+-*/%|&!?~^"
_QUOTES = {
    "'": (TokenKind.SINGLE_QUOTED, "single-quoted text"),
    '"': (TokenKind.DOUBLE_QUOTED, "double-quoted text"),
    "`": (TokenKind.ACCENT_QUOTED, "accent-quoted text"),
}


def _is_identifier_start(ch: str) -> bool:
    # shortcut: Python's identifier rules (XID_Start, plus `_`), so a GQL identifier
    # starting with connector punctuation other than `_` (e.g. U+203F) is refused;
    # upgrade if anyone writes one.
    return ch.isidentifier()


def _is_identifier_part(ch: str) -> bool:
    return ("a" + ch).isidentifier()


def tokenize(text: str) -> list[Token]:
    """Return the tokens of `text`, ending with a single END token."""
    return _Lexer(text).run()


class _Lexer:
    __slots__ = ("text", "pos", "line", "column", "tokens")

    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.line = 1
        self.column = 1
        self.tokens: list[Token] = []

    def run(self) -> list[Token]:
        text = self.text
        while True:
            self._skip_whitespace_and_comments()
            if self.pos >= len(text):
                end = len(text)
                self.tokens.append(Token(TokenKind.END, "", Span(end, end, self.line, self.column)))
                return self.tokens

            ch = text[self.pos]
            following = text[self.pos + 1] if self.pos + 1 < len(text) else ""

            if ch in _QUOTES:
                self._quoted(ch)
            elif ch in _DIGITS or (ch == "." and following in _DIGITS and following):
                self._number()
            elif ch == "$":
                self._parameter()
            elif ch == "@" and following in _QUOTES:
                raise GqlNotSupportedError(
                    "the no-escape prefix `@` on quoted text is not supported yet",
                    self.line,
                    self.column,
                )
            elif _is_identifier_start(ch):
                self._word()
            elif text.startswith(_PUNCTUATION_2, self.pos):
                self._punctuation(2)
            elif ch in _PUNCTUATION_1:
                self._punctuation(1)
            else:
                raise GqlSyntaxError(f"unexpected character {ch!r}", self.line, self.column)

    def _advance(self, count: int) -> None:
        """Move forward `count` characters, keeping line and column current."""
        text = self.text
        end = self.pos + count
        while self.pos < end:
            ch = text[self.pos]
            self.pos += 1
            if ch == "\r" and self.pos < len(text) and text[self.pos] == "\n":
                continue  # \r\n is one line break: the \n completes it
            if ch in _LINE_BREAKS:
                self.line += 1
                self.column = 1
            else:
                self.column += 1

    def _emit(self, kind: TokenKind, value: str, start: int, line: int, column: int) -> None:
        self.tokens.append(Token(kind, value, Span(start, self.pos, line, column)))

    def _skip_whitespace_and_comments(self) -> None:
        text = self.text
        while self.pos < len(text):
            ch = text[self.pos]
            if ch.isspace():
                self._advance(1)
            elif text.startswith("//", self.pos):
                end = self.pos
                while end < len(text) and text[end] not in _LINE_BREAKS:
                    end += 1
                self._advance(end - self.pos)
            elif text.startswith("/*", self.pos):
                close = text.find("*/", self.pos + 2)
                if close < 0:
                    raise GqlSyntaxError("unterminated `/*` comment", self.line, self.column)
                self._advance(close + 2 - self.pos)
            elif text.startswith("--", self.pos):
                # GQL reads `--` as a comment, so Cypher's `-->` would silently drop the
                # rest of the line. Refused until supported deliberately.
                raise GqlNotSupportedError(
                    "`--` comments are not supported yet; use `//` or `/* */`. "
                    "(An edge is written `->` or `-[...]->`, not `-->`.)",
                    self.line,
                    self.column,
                )
            else:
                return

    def _quoted(self, quote: str) -> None:
        kind, description = _QUOTES[quote]
        start, line, column = self.pos, self.line, self.column
        text = self.text
        chars = []
        i = self.pos + 1
        while True:
            if i >= len(text):
                raise GqlSyntaxError(f"unterminated {description}", line, column)
            ch = text[i]
            if ch == quote:
                if i + 1 < len(text) and text[i + 1] == quote:
                    chars.append(quote)  # a doubled quote is one literal quote
                    i += 2
                    continue
                i += 1
                break
            if ch == "\\":
                self._advance(i - self.pos)
                raise GqlNotSupportedError(
                    f"backslash escapes in {description} are not supported yet",
                    self.line,
                    self.column,
                )
            chars.append(ch)
            i += 1
        self._advance(i - self.pos)
        self._emit(kind, "".join(chars), start, line, column)

    def _digits_end(self, i: int) -> int:
        text = self.text
        while i < len(text) and text[i] in _DIGITS:
            i += 1
        return i

    def _number(self) -> None:
        start, line, column = self.pos, self.line, self.column
        text = self.text
        kind = TokenKind.INTEGER
        i = self._digits_end(self.pos)
        if i < len(text) and text[i] == ".":
            kind = TokenKind.EXACT_NUMBER
            i = self._digits_end(i + 1)
        if i < len(text) and text[i] in "eE":
            j = i + 1
            if j < len(text) and text[j] in "+-":
                j += 1
            if j < len(text) and text[j] in _DIGITS:
                kind = TokenKind.APPROXIMATE_NUMBER
                i = self._digits_end(j)
        if i < len(text) and _is_identifier_part(text[i]):
            self._refuse_number_tail(text[start:i], i)
        self._advance(i - self.pos)
        self._emit(kind, text[start:i], start, line, column)

    def _refuse_number_tail(self, number: str, i: int) -> None:
        """A number runs straight into identifier characters: say why it is refused."""
        text = self.text
        tail = text[i]
        tail_is_one_letter = i + 1 >= len(text) or not _is_identifier_part(text[i + 1])
        self._advance(i - self.pos)
        if tail == "_":
            reason = "digit separators (`_`) in numbers are not supported yet"
        elif number == "0" and tail in "xXoObB":
            reason = "hexadecimal, octal and binary numbers are not supported yet"
        elif tail in "mMfFdD" and tail_is_one_letter:
            reason = f"the number suffix `{tail}` is not supported yet"
        else:
            raise GqlSyntaxError(
                f"a number cannot run straight into {tail!r}", self.line, self.column
            )
        raise GqlNotSupportedError(reason, self.line, self.column)

    def _parameter(self) -> None:
        start, line, column = self.pos, self.line, self.column
        text = self.text
        i = self.pos + 1
        if i < len(text) and text[i] == "$":
            raise GqlNotSupportedError(
                "substituted parameters (`$$name`) are not supported yet", line, column
            )
        if i < len(text) and text[i] in _QUOTES:
            raise GqlNotSupportedError(
                "quoted parameter names are not supported yet", line, column
            )
        if i >= len(text) or not _is_identifier_start(text[i]):
            raise GqlSyntaxError("expected a parameter name after `$`", line, column)
        j = i + 1
        while j < len(text) and _is_identifier_part(text[j]):
            j += 1
        self._advance(j - self.pos)
        self._emit(TokenKind.PARAMETER, text[i:j], start, line, column)

    def _word(self) -> None:
        start, line, column = self.pos, self.line, self.column
        text = self.text
        i = self.pos + 1
        while i < len(text) and _is_identifier_part(text[i]):
            i += 1
        self._advance(i - self.pos)
        self._emit(TokenKind.WORD, text[start:i], start, line, column)

    def _punctuation(self, length: int) -> None:
        start, line, column = self.pos, self.line, self.column
        self._advance(length)
        self._emit(TokenKind.PUNCTUATION, self.text[start : self.pos], start, line, column)
