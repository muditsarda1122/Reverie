"""Guard for the compound-rule fix: the extractor prompt must keep telling the model to split a
message that states several independent rules into one item each (measured: 6 of 30 real runs
produced one compound item before; 0 of 30 after), while keeping a rule with its own reason."""

from reverie import typed_extractor


def test_prompt_splits_independent_rules_but_keeps_a_reason_with_its_rule():
    text = typed_extractor.load_prompt()
    assert "ONE ITEM PER RULE" in text and "Same rules as always" in text
    assert "THREE directive items" in text
    assert "Only a reason stays with its own rule" in text
    assert 'Keep one fact in ONE item: a rule together with its reason' in text
