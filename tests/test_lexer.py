import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from gqlparser import GqlNotSupportedError
from gqlparser import GqlSyntaxError
from gqlparser import TokenKind
from gqlparser import tokenize

K = TokenKind


def kinds_values(text):
    return [(t.kind, t.value) for t in tokenize(text)[:-1]]


def refused(text, error):
    with pytest.raises(error) as err:
        tokenize(text)
    return err.value.line, err.value.column


# --- words -------------------------------------------------------------------


def test_words_keep_their_case_and_are_not_classified():
    assert kinds_values("MATCH match Person _x café a1") == [
        (K.WORD, "MATCH"),
        (K.WORD, "match"),
        (K.WORD, "Person"),
        (K.WORD, "_x"),
        (K.WORD, "café"),
        (K.WORD, "a1"),
    ]


# --- quoted text ---------------------------------------------------------------


def test_three_quote_forms_are_distinct_kinds():
    assert kinds_values("'a' \"b\" `c`") == [
        (K.SINGLE_QUOTED, "a"),
        (K.DOUBLE_QUOTED, "b"),
        (K.ACCENT_QUOTED, "c"),
    ]


def test_doubled_quote_is_one_literal_quote():
    assert kinds_values("'it''s' \"say \"\"hi\"\"\" `a``b`") == [
        (K.SINGLE_QUOTED, "it's"),
        (K.DOUBLE_QUOTED, 'say "hi"'),
        (K.ACCENT_QUOTED, "a`b"),
    ]


def test_empty_quoted_text():
    assert kinds_values("''") == [(K.SINGLE_QUOTED, "")]


def test_quoted_text_may_span_lines():
    tokens = tokenize("'a\nb' x")
    assert tokens[0].value == "a\nb"
    assert (tokens[1].span.line, tokens[1].span.column) == (2, 4)


def test_unterminated_quote_points_at_the_opening_quote():
    assert refused("x = 'abc", GqlSyntaxError) == (1, 5)


def test_backslash_is_refused_not_taken_literally():
    assert refused("'a\\nb'", GqlNotSupportedError) == (1, 3)


def test_no_escape_prefix_is_refused():
    assert refused("@'a'", GqlNotSupportedError) == (1, 1)


# --- numbers -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, kind",
    [
        ("0", K.INTEGER),
        ("42", K.INTEGER),
        ("007", K.INTEGER),
        ("1.5", K.EXACT_NUMBER),
        (".5", K.EXACT_NUMBER),
        ("1.", K.EXACT_NUMBER),
        ("1e3", K.APPROXIMATE_NUMBER),
        ("1.5E-2", K.APPROXIMATE_NUMBER),
        ("1.e+5", K.APPROXIMATE_NUMBER),
    ],
)
def test_numbers(text, kind):
    assert kinds_values(text) == [(kind, text)]


@pytest.mark.parametrize("text", ["0x1F", "0b101", "0o17", "1_000", "1.5f", "2m", "3d"])
def test_valid_looking_number_forms_are_not_supported(text):
    refused(text, GqlNotSupportedError)


@pytest.mark.parametrize("text", ["1e", "12abc", "1.5x"])
def test_number_running_into_letters_is_a_syntax_error(text):
    refused(text, GqlSyntaxError)


def test_minus_is_not_part_of_a_number():
    assert kinds_values("-1") == [(K.PUNCTUATION, "-"), (K.INTEGER, "1")]


# --- parameters ----------------------------------------------------------------


def test_parameter():
    assert kinds_values("$since $_x") == [(K.PARAMETER, "since"), (K.PARAMETER, "_x")]


def test_substituted_parameter_is_refused():
    refused("$$x", GqlNotSupportedError)


def test_quoted_parameter_name_is_refused():
    refused('$"x"', GqlNotSupportedError)


@pytest.mark.parametrize("text", ["$", "$1", "$ x"])
def test_dollar_without_a_name_is_a_syntax_error(text):
    refused(text, GqlSyntaxError)


# --- punctuation ---------------------------------------------------------------


def test_two_character_punctuation_is_one_token():
    assert [v for _, v in kinds_values("<= >= <> || -> <-")] == ["<=", ">=", "<>", "||", "->", "<-"]


def test_single_character_punctuation():
    text = "( ) [ ] { } , . : ; = < > + - * / % | & ! ? ~ ^"
    assert [v for _, v in kinds_values(text)] == text.split()


def test_edge_punctuation_lexes_in_pieces():
    assert [v for _, v in kinds_values("(a)-[e:L]->(b)<-[f]-(c)")] == [
        "(", "a", ")", "-", "[", "e", ":", "L", "]", "->", "(", "b", ")",
        "<-", "[", "f", "]", "-", "(", "c", ")",
    ]  # fmt: skip


def test_less_than_minus_is_a_left_arrow():
    # longest match: `a<-1` is `a`, `<-`, `1`; `a < -1` needs the space
    assert [v for _, v in kinds_values("a<-1")] == ["a", "<-", "1"]
    assert [v for _, v in kinds_values("a < -1")] == ["a", "<", "-", "1"]


@pytest.mark.parametrize("text, position", [("a # b", (1, 3)), ("a\n  @x", (2, 3))])
def test_unexpected_character(text, position):
    assert refused(text, GqlSyntaxError) == position


# --- comments and whitespace ---------------------------------------------------


def test_line_comment_runs_to_end_of_line():
    assert kinds_values("a // b c\nd") == [(K.WORD, "a"), (K.WORD, "d")]


def test_block_comment_may_span_lines():
    tokens = tokenize("a /* b\n c */ d")
    assert [t.value for t in tokens[:-1]] == ["a", "d"]
    assert (tokens[1].span.line, tokens[1].span.column) == (2, 7)


def test_unterminated_block_comment():
    assert refused("a /* b", GqlSyntaxError) == (1, 3)


def test_double_minus_comment_is_refused():
    assert refused("MATCH (a)\n  --(b)", GqlNotSupportedError) == (2, 3)


def test_cypher_arrow_is_refused_not_commented_out():
    # `-->` must not silently drop the rest of the line as a `--` comment
    assert refused("MATCH (a)-->(b)", GqlNotSupportedError) == (1, 10)


# --- positions -----------------------------------------------------------------


@pytest.mark.parametrize("break_", ["\n", "\r\n", "\r", " "])
def test_line_breaks(break_):
    tokens = tokenize(f"a{break_}  b")
    assert (tokens[1].span.line, tokens[1].span.column) == (2, 3)


def test_span_covers_raw_source_text():
    text = "MATCH (s:Supplier) WHERE s.name = 'it''s' AND s.x >= 1.5e3 RETURN $p"
    for token in tokenize(text)[:-1]:
        raw = text[token.span.start : token.span.end]
        if token.kind is K.SINGLE_QUOTED:
            assert raw == "'it''s'"
        elif token.kind is K.PARAMETER:
            assert raw == "$" + token.value
        else:
            assert raw == token.value


def test_end_token_sits_at_end_of_input():
    tokens = tokenize("a\nbc ")
    assert tokens[-1].kind is K.END
    assert (tokens[-1].span.start, tokens[-1].span.line, tokens[-1].span.column) == (5, 2, 4)


def test_empty_input_is_just_end():
    assert [t.kind for t in tokenize("")] == [K.END]


# --- a whole query -------------------------------------------------------------


def test_exposure_query():
    text = """USE exposure_graph
MATCH (s:Supplier)-[:BILLED_BY]->(i:Invoice)
WHERE i.due_date < CURRENT_DATE + DURATION 'P30D'
RETURN s.name AS supplier, SUM(i.amount) AS exposure
ORDER BY exposure DESC LIMIT 10"""
    values = [t.value for t in tokenize(text)[:-1]]
    assert values[:6] == ["USE", "exposure_graph", "MATCH", "(", "s", ":"]
    assert "->" in values and "P30D" in values
    assert values[-2:] == ["LIMIT", "10"]
