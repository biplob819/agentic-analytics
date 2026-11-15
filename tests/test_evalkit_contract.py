"""HAR-06: every EvalKit symbol this project imports, listed here independently (EK L-10)."""

import importlib
import inspect
import re
import tomllib

import pytest

from harness_lib import ROOT

USED = {
    "evalkit.scorers": ["sql_match"],
    "evalkit.schemas": ["ResultSet", "Interval"],
    "evalkit.stats": ["Resamples", "interval", "interval_from", "paired", "holm", "percentile_ms"],
    "evalkit.demo.guard": ["Guard", "GuardRefused", "PostgresSpend", "SqliteSpend"],
    "evalkit.cost": ["Price", "call_cost", "load_prices", "price_for", "UnknownPrice"],
    "evalkit.retry": ["with_retries", "is_retryable"],
}


@pytest.mark.contract
@pytest.mark.parametrize("module", sorted(USED))
def test_every_used_evalkit_symbol_imports(module):
    mod = importlib.import_module(module)
    missing = [name for name in USED[module] if not hasattr(mod, name)]
    assert not missing, f"{module} lost {missing}"


@pytest.mark.contract
def test_sql_match_keyword_contract():
    from evalkit.scorers import sql_match

    params = inspect.signature(sql_match).parameters
    for name in ("rel_tol", "abs_tol", "allow_extra_columns"):
        assert name in params and params[name].kind is inspect.Parameter.KEYWORD_ONLY


def test_sql_match_extra_columns_and_tolerance():
    from evalkit.schemas import ResultSet
    from evalkit.scorers import sql_match

    gold = ResultSet(columns=("town", "median_price"), rows=(("LEEDS", 250000),))
    extra = ResultSet(columns=("town", "median_price", "sales"), rows=(("LEEDS", 250000, 812),))
    assert sql_match(extra, gold).value == 0.0
    assert sql_match(extra, gold, allow_extra_columns=True).value == 1.0
    near = ResultSet(columns=("town", "median_price"), rows=(("LEEDS", 251000.0),))
    assert sql_match(near, gold).value == 0.0
    assert sql_match(near, gold, rel_tol=0.005).value == 1.0


def test_evalkit_pin_is_an_exact_commit():
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "dependencies"
    ]
    pin = next(d for d in deps if d.startswith("evalkit-harness"))
    assert re.fullmatch(
        r"evalkit-harness @ git\+https://github\.com/biplob819/evalkit@[0-9a-f]{40}", pin
    ), pin
