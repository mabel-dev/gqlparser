import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

import gqlparser


def test_the_two_errors_are_distinguishable():
    # "not valid GQL" and "not supported yet" must never be caught as each other
    assert not issubclass(gqlparser.GqlNotSupportedError, gqlparser.GqlSyntaxError)
    assert not issubclass(gqlparser.GqlSyntaxError, gqlparser.GqlNotSupportedError)


def test_error_message_carries_position():
    err = gqlparser.GqlSyntaxError("expected ')'", 3, 14)
    assert str(err) == "expected ')' (line 3, column 14)"
    assert err.message == "expected ')'"


def test_span_is_frozen():
    span = gqlparser.Span(start=0, end=5, line=1, column=1)
    with pytest.raises(AttributeError):
        span.start = 1


def test_package_is_dependency_free():
    # importing gqlparser in a clean interpreter may load only itself and the standard library
    probe = (
        "import sys; sys.path.insert(0, sys.argv[1]); before = set(sys.modules); import gqlparser; "
        "new = {m.split('.')[0] for m in set(sys.modules) - before}; "
        "print(sorted(n for n in new if n != 'gqlparser' and n not in sys.stdlib_module_names))"
    )
    root = os.path.join(os.path.dirname(__file__), "..")
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, root], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"
