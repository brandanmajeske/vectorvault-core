from dogfood.consumer_dogfood import arm_order, score_answer


def test_score_answer_requires_each_concept_group():
    criteria = [["working set", "pack"], ["ordered", "exact"]]

    assert score_answer("Use an exact ordered working set pack.", criteria)
    assert not score_answer("Use a working set.", criteria)


def test_arm_order_alternates_to_reduce_order_bias():
    assert arm_order(0, 4000, 1000) == [("control", 4000), ("candidate", 1000)]
    assert arm_order(1, 4000, 1000) == [("candidate", 1000), ("control", 4000)]
