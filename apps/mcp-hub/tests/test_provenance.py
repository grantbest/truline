import pytest

from tools.provenance import build_provenance, prompt_ref_for


# --- build_provenance: the six keys, nothing else ---------------------------


def test_returns_exactly_the_six_keys():
    result = build_provenance(worker="w", model="gemini-flash", prompt_ref="p")
    assert set(result) == {"worker", "model", "prompt_ref", "tokens", "cost_usd", "duration_s"}


# --- rule (i): worker/prompt_ref non-empty after strip() --------------------


def test_empty_worker_raises():
    with pytest.raises(ValueError):
        build_provenance(worker="   ", model="gemini-flash", prompt_ref="p")


def test_empty_prompt_ref_raises():
    with pytest.raises(ValueError):
        build_provenance(worker="w", model="gemini-flash", prompt_ref="  ")


# --- rule (ii): model None/'' becomes 'none' --------------------------------


def test_model_none_becomes_none_literal():
    result = build_provenance(worker="w", model=None, prompt_ref="p")
    assert result["model"] == "none"


def test_model_empty_string_becomes_none_literal():
    result = build_provenance(worker="w", model="", prompt_ref="p")
    assert result["model"] == "none"


def test_real_model_is_kept_verbatim():
    result = build_provenance(worker="w", model="gemini-flash", prompt_ref="p")
    assert result["model"] == "gemini-flash"


# --- rule (iii): tokens/cost_usd defaults depend on model -------------------


def test_none_model_defaults_tokens_and_cost_to_zero():
    result = build_provenance(worker="w", model=None, prompt_ref="p")
    assert result["tokens"] == 0
    assert result["cost_usd"] == 0.0


def test_real_model_defaults_tokens_and_cost_to_none():
    result = build_provenance(worker="w", model="gemini-flash", prompt_ref="p")
    assert result["tokens"] is None
    assert result["cost_usd"] is None


def test_caller_supplied_tokens_and_cost_are_kept_regardless_of_model():
    none_model = build_provenance(worker="w", model=None, prompt_ref="p", tokens=5, cost_usd=1.5)
    assert none_model["tokens"] == 5
    assert none_model["cost_usd"] == 1.5

    real_model = build_provenance(worker="w", model="gemini-flash", prompt_ref="p", tokens=5, cost_usd=1.5)
    assert real_model["tokens"] == 5
    assert real_model["cost_usd"] == 1.5


def test_caller_supplied_zero_is_kept_not_overridden_by_default():
    result = build_provenance(worker="w", model="gemini-flash", prompt_ref="p", tokens=0, cost_usd=0.0)
    assert result["tokens"] == 0
    assert result["cost_usd"] == 0.0


# --- rule (iv): duration_s None becomes 0.0 ---------------------------------


def test_duration_none_becomes_zero():
    result = build_provenance(worker="w", model="gemini-flash", prompt_ref="p")
    assert result["duration_s"] == 0.0


def test_duration_supplied_is_kept():
    result = build_provenance(worker="w", model="gemini-flash", prompt_ref="p", duration_s=1.25)
    assert result["duration_s"] == 1.25


# --- rule (v): negative numeric fields raise --------------------------------


def test_negative_tokens_raises():
    with pytest.raises(ValueError):
        build_provenance(worker="w", model="gemini-flash", prompt_ref="p", tokens=-1)


def test_negative_cost_usd_raises():
    with pytest.raises(ValueError):
        build_provenance(worker="w", model="gemini-flash", prompt_ref="p", cost_usd=-0.01)


def test_negative_duration_raises():
    with pytest.raises(ValueError):
        build_provenance(worker="w", model="gemini-flash", prompt_ref="p", duration_s=-1.0)


# --- prompt_ref_for ----------------------------------------------------------


def test_prompt_ref_for_uses_hash_when_present():
    assert prompt_ref_for("deadbeef", "fallback/ref") == "prompt-sha256:deadbeef"


def test_prompt_ref_for_uses_fallback_when_hash_is_none():
    assert prompt_ref_for(None, "fallback/ref") == "fallback/ref"


def test_prompt_ref_for_uses_fallback_when_hash_is_empty():
    assert prompt_ref_for("", "fallback/ref") == "fallback/ref"
