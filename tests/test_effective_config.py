"""A setting a config asks for is transmitted, or the config is rejected.

`_build_target` read the keys it knew with `cfg.get(...)` and ignored the rest, so
three things were silent:

| config says | baseline |
|---|---|
| `top_p: 0.1` on an OpenAI target | never sent; receipt records `top_p: null` |
| `max_tokens: 64` on an OpenAI target | never sent; receipt records `max_tokens: null` |
| `tempreature: 0.9` beside `temperature: 0.0` | accepted and ignored |

The last one is the worst of the three: a misspelled key is indistinguishable from a
decision not to set it, and the run completes with a receipt that looks like it
describes what was asked for. This is the failure `policy.py` exists to prevent, one
layer out.
"""
from __future__ import annotations

import json

import pytest

from evalseal.adapters.recording import Cassette
from evalseal.adapters.target import ProviderCall
from evalseal.config import (
    AnthropicTargetConfig,
    ConfigError,
    OpenAITargetConfig,
    judge_config,
    load_scorer_config,
    load_target_config,
)


@pytest.fixture
def cassette(tmp_path):
    return Cassette(tmp_path / "c.json", record=True)


def _openai_target(cassette, cfg: dict):
    from evalseal.cli import _build_target

    return _build_target(cfg, cassette, max_retries=0, timeout=5.0)


def _capture(target):
    """Run one generate() and return (request body, TargetResponse)."""
    sent: dict = {}

    def fake_post(body):
        sent.update(body)
        return ProviderCall(
            {"choices": [{"message": {"content": "yes"}, "finish_reason": "stop"}],
             "model": body["model"]},
            attempts=1)

    target._post = fake_post
    return sent, target.generate("hello")


# --- the reproduction: controls reach the wire -----------------------------------------

def test_top_p_is_transmitted_and_recorded(cassette):
    target = _openai_target(cassette, {"model": "m", "top_p": 0.1})
    sent, response = _capture(target)
    assert sent["top_p"] == 0.1, "it must reach the provider"
    assert response.effective_params["top_p"] == 0.1, "and the receipt"


def test_max_tokens_is_transmitted_and_recorded(cassette):
    target = _openai_target(cassette, {"model": "m", "max_tokens": 64})
    sent, response = _capture(target)
    assert sent["max_tokens"] == 64
    assert response.effective_params["max_tokens"] == 64


def test_an_unset_control_is_not_sent(cassette):
    """Not setting one must stay different from setting it to a default."""
    target = _openai_target(cassette, {"model": "m"})
    sent, response = _capture(target)
    assert "top_p" not in sent
    assert "max_tokens" not in sent
    assert response.effective_params["top_p"] is None


def test_setting_only_top_p_counts_as_explicit(cassette):
    """`params_source` was derived from `temperature` alone, so a run that set only
    `top_p` was reported as inheriting every provider default."""
    target = _openai_target(cassette, {"model": "m", "top_p": 0.1})
    _, response = _capture(target)
    assert response.params_source == "explicit"


def test_setting_nothing_is_reported_as_inheriting(cassette):
    target = _openai_target(cassette, {"model": "m"})
    _, response = _capture(target)
    assert response.params_source == "provider_default"


# --- unknown keys fail -----------------------------------------------------------------

def test_a_misspelled_key_is_rejected_with_a_suggestion():
    with pytest.raises(ConfigError) as caught:
        load_target_config({"model": "m", "tempreature": 0.9})
    assert "tempreature" in str(caught.value)
    assert "did you mean 'temperature'" in str(caught.value)


def test_an_unknown_key_is_rejected_and_the_legal_ones_listed():
    with pytest.raises(ConfigError) as caught:
        load_target_config({"model": "m", "frequency_penalty": 1.0})
    message = str(caught.value)
    assert "frequency_penalty" in message
    assert "Supported:" in message and "top_p" in message


def test_the_cli_reports_an_unknown_key_as_bad_input(tmp_path, cassette):
    import typer

    with pytest.raises(typer.BadParameter, match="tempreature"):
        _openai_target(cassette, {"model": "m", "tempreature": 0.9})


def test_an_unknown_scorer_key_is_rejected():
    with pytest.raises(ConfigError, match="tollerance"):
        load_scorer_config({"type": "answer_match", "tollerance": 0.1})


# --- ranges are the provider's, and differ between providers ---------------------------

@pytest.mark.parametrize("value", [-0.1, 2.1])
def test_an_out_of_range_openai_temperature_is_rejected(value):
    with pytest.raises(ConfigError, match="temperature"):
        load_target_config({"model": "m", "temperature": value})


def test_openai_allows_a_temperature_above_one():
    assert load_target_config({"model": "m", "temperature": 1.5}).temperature == 1.5


def test_anthropic_rejects_a_temperature_above_one():
    """Anthropic's documented range is 0..1. Enforced here rather than by an HTTP 400
    partway through a run."""
    with pytest.raises(ConfigError, match="temperature"):
        load_target_config({"provider": "anthropic", "model": "m", "temperature": 1.5})


@pytest.mark.parametrize("value", [0, -1])
def test_a_non_positive_max_tokens_is_rejected(value):
    with pytest.raises(ConfigError, match="max_tokens"):
        load_target_config({"model": "m", "max_tokens": value})


def test_an_unknown_provider_is_named():
    with pytest.raises(ConfigError, match="unknown provider 'gopher'"):
        load_target_config({"provider": "gopher", "model": "m"})


# --- the provider discriminates which fields exist -------------------------------------

def test_both_providers_accept_a_system_prompt():
    """Both transmit it, so both accept it.

    C1a asserted the opposite - that `system` belongs to Anthropic and is rejected for
    an OpenAI target - and that was wrong in the same way C1-2 was wrong: the OpenAI
    adapter had no field for it, so the key was accepted at the scorer layer, dropped
    on the way to the judge, and nothing said so. Rejecting it would have been one
    honest answer; transmitting it as a leading system message is the better one,
    because that is how this wire format carries a system prompt.
    """
    assert load_target_config(
        {"provider": "anthropic", "model": "m", "system": "be terse"}).system == "be terse"
    assert load_target_config({"model": "m", "system": "be terse"}).system == "be terse"


def test_seed_belongs_to_openai_only():
    assert load_target_config({"model": "m", "seed": 7}).seed == 7
    with pytest.raises(ConfigError, match="seed"):
        load_target_config({"provider": "anthropic", "model": "m", "seed": 7})


def test_a_config_without_a_provider_is_openai():
    """Every suite written before the Anthropic adapter existed keeps working."""
    assert isinstance(load_target_config({"model": "m"}), OpenAITargetConfig)


def test_anthropic_defaults_max_tokens_because_the_api_requires_one():
    cfg = load_target_config({"provider": "anthropic", "model": "m"})
    assert isinstance(cfg, AnthropicTargetConfig)
    assert cfg.max_tokens == 1024


# --- the judge is configured by the same schema ---------------------------------------

def test_the_judge_is_validated_as_a_target():
    scorer = load_scorer_config({
        "type": "llm_judge", "rubric": "r", "judge_model": "j",
        "judge_temperature": 0.0, "judge_top_p": 0.5})
    cfg = judge_config(scorer)
    validated = load_target_config(cfg, what="the judge config")
    assert validated.model == "j"
    assert validated.top_p == 0.5


def test_a_misspelled_judge_key_is_rejected():
    with pytest.raises(ConfigError, match="judge_temprature"):
        load_scorer_config({"type": "llm_judge", "rubric": "r", "judge_model": "j",
                            "judge_temprature": 0.0})


def test_an_anthropic_judge_keeps_its_system_prompt():
    scorer = load_scorer_config({
        "type": "llm_judge", "rubric": "r", "judge_model": "j",
        "judge_provider": "anthropic", "system": "grade strictly"})
    cfg = judge_config(scorer)
    assert cfg["system"] == "grade strictly"
    assert load_target_config(cfg).system == "grade strictly"


def test_an_openai_judge_receives_its_system_prompt():
    """A system prompt steers a judge's verdicts, so dropping it changes the scores."""
    scorer = load_scorer_config({
        "type": "llm_judge", "rubric": "r", "judge_model": "j",
        "system": "grade strictly"})
    cfg = judge_config(scorer)
    assert cfg["system"] == "grade strictly"
    validated = load_target_config(cfg)
    assert validated.model == "j"
    assert validated.system == "grade strictly"


# --- the existing configs in this repository still load --------------------------------

def test_every_target_config_in_the_repository_validates():
    """A schema that rejects the project's own examples is the wrong schema."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    checked = 0
    for path in sorted(root.glob("examples/**/target*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        load_target_config(data, what=str(path))
        checked += 1
    assert checked, "no example target configs were found to check"


def test_every_scorer_config_in_the_repository_validates():
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    checked = 0
    for path in sorted(root.glob("examples/**/scorer*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        load_scorer_config(data, what=str(path))
        checked += 1
    assert checked, "no example scorer configs were found to check"
