"""The native Anthropic adapter: a second wire format, not a shim over the first."""
from __future__ import annotations

import httpx
import pytest
from conftest import make_dataset
from typer.testing import CliRunner

from evalseal.adapters.recording import Cassette
from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import AnthropicTarget, OpenAICompatibleTarget, extract_text
from evalseal.cli import app
from evalseal.executor import run_eval

runner = CliRunner()


def _reply(text="yes", *, stop_reason="end_turn", model="claude-haiku-4-5-20251001"):
    return {
        "id": "msg_01", "type": "message", "role": "assistant", "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 9, "output_tokens": 2},
    }


def _capture(monkeypatch, replies):
    """Record every outgoing request so the wire format itself can be asserted on."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-key-value")
    queue = list(replies)
    sent: list[dict] = []

    def fake_post(self, url, headers=None, json=None):
        sent.append({"url": url, "headers": headers or {}, "body": json})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    return sent


def _target(tmp_path, **kw):
    return AnthropicTarget(
        model="claude-haiku-4-5-20251001",
        cassette=Cassette(tmp_path / "c.json", record=True),
        sleep=lambda _: None,
        **kw,
    )


# --- the wire format --------------------------------------------------------------

def test_it_calls_messages_not_chat_completions(tmp_path, monkeypatch):
    sent = _capture(monkeypatch, [_reply()])
    _target(tmp_path).generate("hello")
    assert sent[0]["url"] == "https://api.anthropic.com/v1/messages"


def test_it_sends_the_anthropic_headers_not_a_bearer_token(tmp_path, monkeypatch):
    sent = _capture(monkeypatch, [_reply()])
    _target(tmp_path).generate("hello")
    headers = sent[0]["headers"]
    assert headers["x-api-key"] == "secret-key-value"
    assert headers["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in headers


def test_max_tokens_is_always_sent_because_the_api_requires_it(tmp_path, monkeypatch):
    sent = _capture(monkeypatch, [_reply()])
    _target(tmp_path).generate("hello")
    assert sent[0]["body"]["max_tokens"] == 1024


def test_system_is_a_top_level_field_not_a_message(tmp_path, monkeypatch):
    sent = _capture(monkeypatch, [_reply()])
    _target(tmp_path, system="Be terse.").generate("hello")
    body = sent[0]["body"]
    assert body["system"] == "Be terse."
    assert [m["role"] for m in body["messages"]] == ["user"]


def test_unset_parameters_are_omitted_rather_than_guessed(tmp_path, monkeypatch):
    sent = _capture(monkeypatch, [_reply()])
    _target(tmp_path).generate("hello")
    body = sent[0]["body"]
    assert "temperature" not in body and "top_p" not in body
    assert "seed" not in body           # the Messages API has no seed


# --- reading the reply ------------------------------------------------------------

def test_text_is_joined_from_the_content_blocks():
    raw = {"content": [{"type": "text", "text": "he"}, {"type": "text", "text": "llo"}]}
    assert extract_text(raw) == "hello"


def test_a_leading_non_text_block_does_not_swallow_the_answer():
    """`content[0]` works until the first reply that opens with a thinking block."""
    raw = {"content": [
        {"type": "thinking", "thinking": "hmm"},
        {"type": "text", "text": "the answer"},
    ]}
    assert extract_text(raw) == "the answer"


@pytest.mark.parametrize("raw", [{}, {"content": None}, {"content": []}])
def test_a_reply_with_no_text_is_empty_not_an_exception(raw):
    assert extract_text(raw) == ""


def test_served_model_is_recorded_and_fingerprint_is_honestly_none(tmp_path, monkeypatch):
    _capture(monkeypatch, [_reply(model="claude-haiku-4-5-20251001")])
    resp = _target(tmp_path).generate("hello")
    assert resp.served_model == "claude-haiku-4-5-20251001"
    # Anthropic publishes no backend identifier; inventing one would fake a pin.
    assert resp.system_fingerprint is None


def test_effective_params_record_max_tokens_and_no_seed(tmp_path, monkeypatch):
    _capture(monkeypatch, [_reply()])
    resp = _target(tmp_path, temperature=0.0, top_p=0.9, max_tokens=64).generate("hi")
    assert resp.effective_params == {
        "temperature": 0.0, "seed": None, "top_p": 0.9, "max_tokens": 64}
    assert resp.params_source == "explicit"


def test_temperature_left_unset_is_reported_as_a_provider_default(tmp_path, monkeypatch):
    _capture(monkeypatch, [_reply()])
    assert _target(tmp_path).generate("hi").params_source == "provider_default"


# --- truncation -------------------------------------------------------------------

def test_a_reply_cut_off_at_the_token_limit_is_flagged(tmp_path, monkeypatch):
    _capture(monkeypatch, [_reply("the answer is", stop_reason="max_tokens")])
    assert _target(tmp_path, max_tokens=8).generate("hi").truncated is True


def test_the_openai_shape_reports_truncation_too(tmp_path, monkeypatch):
    monkeypatch.setenv("EVALSEAL_API_KEY", "k")
    monkeypatch.setattr(httpx.Client, "post", lambda self, url, **k: httpx.Response(
        200, request=httpx.Request("POST", url), json={
            "model": "m",
            "choices": [{"message": {"content": "cut"}, "finish_reason": "length"}]}))
    target = OpenAICompatibleTarget(
        model="m", cassette=Cassette(tmp_path / "c.json", record=True))
    assert target.generate("hi").truncated is True


def test_truncation_becomes_a_run_warning(tmp_path, monkeypatch):
    """Scored naively a cut-off answer looks like the model being wrong."""
    _capture(monkeypatch, [_reply("y", stop_reason="max_tokens") for _ in range(5)])
    target = _target(tmp_path, max_tokens=4)
    record = run_eval(make_dataset("p"), target, RegexScorer(r"^yes$"), n_repeats=5)

    warning = next(w for w in record.aggregate.warnings if "TRUNCATED" in w)
    assert "5 of 5 call(s) hit the token limit" in warning
    assert "incomplete, not incorrect" in warning


def test_no_truncation_warning_when_nothing_was_cut_off(tmp_path, monkeypatch):
    _capture(monkeypatch, [_reply("yes") for _ in range(5)])
    record = run_eval(make_dataset("p"), _target(tmp_path), RegexScorer(r"^yes$"), n_repeats=5)
    assert not [w for w in record.aggregate.warnings if "TRUNCATED" in w]


# --- retries and recording --------------------------------------------------------

def test_529_overloaded_is_retried(tmp_path, monkeypatch):
    """Anthropic signals overload with 529, which is not in anyone's default retry set."""
    overloaded = httpx.Response(
        529, json={"type": "error", "error": {"type": "overloaded_error"}},
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    sent = _capture(monkeypatch, [overloaded, _reply("yes")])

    assert _target(tmp_path).generate("hi").text == "yes"
    assert len(sent) == 2


def test_the_api_key_never_reaches_the_cassette(tmp_path, monkeypatch):
    _capture(monkeypatch, [_reply()])
    path = tmp_path / "c.json"
    AnthropicTarget(model="m", cassette=Cassette(path, record=True),
                    sleep=lambda _: None).generate("hi")
    assert "secret-key-value" not in path.read_text(encoding="utf-8")


def test_replay_needs_no_key(tmp_path, monkeypatch):
    _capture(monkeypatch, [_reply("recorded answer")])
    path = tmp_path / "c.json"
    AnthropicTarget(model="m", cassette=Cassette(path, record=True),
                    sleep=lambda _: None).generate("hi")

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(httpx.Client, "post", _no_network)
    replayed = AnthropicTarget(model="m", cassette=Cassette(path, record=False)).generate("hi")
    assert replayed.text == "recorded answer"


def _no_network(self, url, **kwargs):  # pragma: no cover - asserts it is never called
    raise AssertionError("replay must not touch the network")


# --- configuration ----------------------------------------------------------------

def _cfg(tmp_path, target_cfg, scorer_cfg='{"type": "regex", "pattern": "yes"}'):
    (tmp_path / "ds.jsonl").write_text('{"case_id": "only", "prompt": "p"}\n')
    (tmp_path / "t.json").write_text(target_cfg)
    (tmp_path / "s.json").write_text(scorer_cfg)
    return ["run", "--dataset", str(tmp_path / "ds.jsonl"),
            "--target-config", str(tmp_path / "t.json"),
            "--scorer-config", str(tmp_path / "s.json"),
            "--cassette", str(tmp_path / "c.json"), "--n", "5",
            "--concurrency", "1", "--fail-on", "none"]


def test_cli_builds_an_anthropic_target_from_the_provider_key(tmp_path, monkeypatch):
    _capture(monkeypatch, [_reply("yes") for _ in range(5)])
    monkeypatch.setenv("EVALSEAL_RECORD", "1")
    result = runner.invoke(app, _cfg(
        tmp_path, '{"provider": "anthropic", "model": "claude-haiku-4-5-20251001"}'))
    assert result.exit_code == 0, result.output
    assert "claude-haiku-4-5-20251001" in result.output


def test_cli_rejects_an_unknown_provider(tmp_path):
    result = runner.invoke(app, _cfg(tmp_path, '{"provider": "gopher", "model": "m"}'))
    assert result.exit_code == 2
    assert "unknown provider 'gopher'" in result.output


def test_a_config_without_a_provider_still_means_openai(tmp_path, monkeypatch):
    """Every suite file written before this adapter existed must keep working."""
    monkeypatch.setenv("EVALSEAL_RECORD", "1")
    monkeypatch.setenv("EVALSEAL_API_KEY", "k")
    monkeypatch.setattr(httpx.Client, "post", lambda self, url, **k: httpx.Response(
        200, request=httpx.Request("POST", url),
        json={"model": "m", "choices": [{"message": {"content": "yes"}}]}))
    result = runner.invoke(app, _cfg(tmp_path, '{"model": "m"}'))
    assert result.exit_code == 0, result.output
    assert "/chat/completions" not in result.output      # nothing leaked into the report
