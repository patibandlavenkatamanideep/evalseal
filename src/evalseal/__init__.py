"""EvalSeal: reproducibility and provenance receipts for LLM evaluations."""

from .adapters.dataset import Case, Dataset
from .adapters.recording import Cassette, slot
from .adapters.scorer import ExactMatchScorer, LLMJudgeScorer, RegexScorer, Scorer, ScoreResult
from .adapters.target import (
    LocalCallableTarget,
    OpenAICompatibleTarget,
    Target,
    TargetResponse,
)
from .analyze import CaseStats, analyze_case, classify_stability, flip_rate
from .executor import run_eval
from .ledger import last_hash, load_all, seal_and_append, verify_chain
from .models import Aggregate, CaseResult, ProvenanceManifest, RunRecord
from .report import to_junit, to_markdown

__version__ = "0.3.0"

__all__ = [
    "Aggregate", "Case", "CaseResult", "CaseStats", "Cassette", "Dataset",
    "ExactMatchScorer", "LLMJudgeScorer", "LocalCallableTarget", "OpenAICompatibleTarget",
    "ProvenanceManifest", "RegexScorer", "RunRecord", "ScoreResult", "Scorer", "Target",
    "TargetResponse", "__version__", "analyze_case", "classify_stability", "flip_rate",
    "last_hash", "load_all", "run_eval", "seal_and_append", "slot", "to_junit",
    "to_markdown", "verify_chain",
]
