"""Splitting the prompts into editable guidance and a fixed output contract
must not change what the model is sent."""

import hashlib

from app.services.apply import prompts as apply_prompts
from app.services.review import prompts as review_prompts

# sha256 of each full system prompt as it was before the split.
REVIEW_BEFORE = "77e31c5af2bbe6aff7f595d77abf5568adc577dcdaf4ee528d52dae931e716bd"
APPLY_BEFORE = "90261b350e47bb93f095727943a9268324ea701c884b475e0648479a218cc008"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_review_prompt_is_unchanged_by_the_split():
    assert _sha(review_prompts.SYSTEM_RULES.strip()) == REVIEW_BEFORE


def test_apply_prompt_is_unchanged_by_the_split():
    assert _sha(apply_prompts.build_system_prompt()) == APPLY_BEFORE


def test_the_output_contract_is_not_part_of_the_editable_guidance():
    for module in (review_prompts, apply_prompts):
        assert "Return JSON only" not in module.GUIDANCE
        assert module.OUTPUT_CONTRACT.startswith("Return JSON only")
