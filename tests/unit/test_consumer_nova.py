from __future__ import annotations

import sys
from pathlib import Path

_DOGFOOD = Path(__file__).parents[2] / "dogfood"
if str(_DOGFOOD) not in sys.path:
    sys.path.insert(0, str(_DOGFOOD))

import consumer_nova as cn  # noqa: E402


def test_score_blind_requires_every_group():
    crit = [["pin_working_set"], ["fetch_working_set"], ["ordered", "exact ordered"]]
    assert cn.score_blind("use pin_working_set and fetch_working_set on the ordered set", crit)
    # Missing pin -> fail (the gemma-style incomplete answer).
    assert not cn.score_blind("just call fetch_working_set on the ordered set", crit)


def test_score_blind_accepts_any_synonym_in_group():
    crit = [["harness-permissions", "harness permissions"]]
    assert cn.score_blind("added harness permissions to the pack", crit)
    assert not cn.score_blind("unrelated text", crit)


def test_arm_order_alternates_and_covers_both_arms():
    even = cn.arm_order(0)
    odd = cn.arm_order(1)
    assert [a for a, _ in even] == ["control", "candidate"]
    assert [a for a, _ in odd] == ["candidate", "control"]
    assert dict(even) == {"control": cn.CONTROL_BUDGET, "candidate": cn.CANDIDATE_BUDGET}


def test_build_prompt_contains_only_supplied_summaries():
    p = cn.build_prompt("do X", ["alpha fact", "beta fact"])
    assert "alpha fact" in p and "beta fact" in p
    assert "do X" in p
    assert "ONLY the memory summaries" in p
