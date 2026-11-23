"""Regenerate tests/fixtures/lowering.json from tests/lowering_cases.py.

Each case's hand-written SQL is parsed by sqlparser-rs, through Opteryx, so the
fixtures are sqlparser's own output and not the lowering's. Needs `opteryx`
importable (run it where opteryx-core is built); gqlparser itself never does.

    python dev/make_lowering_fixtures.py
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from lowering_cases import CASES
from opteryx.compute import parse_sql


def normalise(tree):
    """Spans removed (they are positions in different texts); tuples become lists."""
    if type(tree) is dict:
        return {k: normalise(v) for k, v in tree.items() if k != "span"}
    if type(tree) in (list, tuple):
        return [normalise(v) for v in tree]
    return tree


fixtures = {}
for case in CASES:
    statements = parse_sql(case.sql, "opteryx")
    if len(statements) != 1:
        raise SystemExit(f"{case.name}: expected one statement, got {len(statements)}")
    fixtures[case.name] = normalise(statements[0])

target = ROOT / "tests" / "fixtures" / "lowering.json"
target.parent.mkdir(exist_ok=True)
target.write_text(json.dumps(fixtures, indent=1, sort_keys=True) + "\n")
print(f"wrote {len(fixtures)} fixtures to {target.relative_to(ROOT)}")
