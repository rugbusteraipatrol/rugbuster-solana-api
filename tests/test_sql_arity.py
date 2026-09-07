"""Every SQL statement must be passed exactly as many parameters as it has
placeholders.

Written after a near miss: adding a version filter to fetch_live_cache put the
extra parameter on fetch_latest_scan instead, leaving one query with two
placeholders and one parameter and the other with one placeholder and two.
Both would have raised at runtime on the first request -- taking the service
down -- and no existing test touches these functions, because they need a
database.

This checks the source, not the database, so it runs anywhere.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app.py"


def _execute_calls():
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            if getattr(call.func, "attr", "") != "execute":
                continue
            if not call.args or not isinstance(call.args[0], ast.Constant):
                continue
            sql = call.args[0].value
            if not isinstance(sql, str):
                continue
            params = call.args[1] if len(call.args) > 1 else None
            yield node.name, sql, params


def test_placeholder_count_matches_parameter_count():
    checked = 0
    for name, sql, params in _execute_calls():
        placeholders = sql.count("%s")
        if placeholders == 0:
            continue
        assert params is not None, f"{name}: {placeholders} placeholders but no parameters passed"
        if not isinstance(params, ast.Tuple):
            continue  # a variable or comprehension; cannot count statically
        assert len(params.elts) == placeholders, (
            f"{name}: {placeholders} placeholders but {len(params.elts)} parameters"
        )
        checked += 1
    assert checked >= 3, f"expected to check several statements, only saw {checked}"
