import pytest

# The dogfood package lives at the repo root (not under src) and pulls the
# optional [dogfood] extra (requests/tiktoken). CI installs only .[dev], so skip
# cleanly there instead of erroring at collection. Runs locally with the extra.
_dogfood = pytest.importorskip(
    "dogfood.consumer_dogfood",
    reason="install the [dogfood] extra and run from repo root to exercise these",
)
arm_order = _dogfood.arm_order
score_answer = _dogfood.score_answer


def test_score_answer_requires_each_concept_group():
    criteria = [["working set", "pack"], ["ordered", "exact"]]

    assert score_answer("Use an exact ordered working set pack.", criteria)
    assert not score_answer("Use a working set.", criteria)


def test_arm_order_alternates_to_reduce_order_bias():
    assert arm_order(0, 4000, 1000) == [("control", 4000), ("candidate", 1000)]
    assert arm_order(1, 4000, 1000) == [("candidate", 1000), ("control", 4000)]
