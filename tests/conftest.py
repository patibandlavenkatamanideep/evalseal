from __future__ import annotations

import itertools

import pytest

from evalseal.adapters.dataset import Case, Dataset


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    """Tests never record and never touch the real ledger in the repo."""
    monkeypatch.delenv("EVALSEAL_RECORD", raising=False)
    monkeypatch.delenv("EVALSEAL_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)


def scripted(outputs_by_prompt: dict[str, list[str]]):
    """A fn for LocalCallableTarget that cycles through fixed outputs per prompt."""
    cycles = {p: itertools.cycle(outs) for p, outs in outputs_by_prompt.items()}
    return lambda prompt: next(cycles[prompt])


def make_dataset(*prompts: str) -> Dataset:
    return Dataset([Case(f"c{i}", p) for i, p in enumerate(prompts)], hash="sha256:test")
