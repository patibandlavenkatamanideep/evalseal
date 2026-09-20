"""EvalSeal: reproducibility and provenance receipts for LLM evaluations."""

from .adapters.dataset import Case, Dataset
from .adapters.recording import Cassette, slot
from .adapters.scorer import (
    AnswerMatchScorer,
    ExactMatchScorer,
    LLMJudgeScorer,
    RegexScorer,
    Scorer,
    ScoreResult,
    extract_answer,
    strip_decoration,
)
from .adapters.target import (
    LocalCallableTarget,
    OpenAICompatibleTarget,
    Target,
    TargetResponse,
)
from .analyze import CaseStats, analyze_case, classify_stability, flip_rate, wilson_ci
from .diffing import DiffResult, diff_records, load_receipt
from .executor import run_eval
from .ledger import (
    config_fingerprint,
    evaluator_fingerprint,
    last_hash,
    load_all,
    seal_and_append,
    verify_chain,
)
from .locking import ledger_lock
from .models import Aggregate, CaseResult, ProvenanceManifest, RunRecord
from .provenance import file_hash, git_provenance, text_hash
from .report import render_diff, to_junit, to_markdown
from .signing import generate_keypair, sign_head, verify_signatures

__version__ = "1.4.0"

__all__ = [
    "Aggregate", "Case", "CaseResult", "CaseStats", "Cassette", "Dataset",
    "AnswerMatchScorer", "ExactMatchScorer", "LLMJudgeScorer", "LocalCallableTarget",
    "OpenAICompatibleTarget",
    "DiffResult", "ProvenanceManifest", "RegexScorer", "RunRecord", "ScoreResult",
    "Scorer", "Target",
    "TargetResponse", "__version__", "analyze_case", "classify_stability",
    "config_fingerprint", "diff_records", "evaluator_fingerprint",
    "extract_answer", "file_hash", "load_receipt", "render_diff",
    "flip_rate", "git_provenance",
    "ledger_lock", "strip_decoration", "text_hash", "wilson_ci",
    "last_hash", "load_all", "run_eval", "seal_and_append", "slot", "to_junit",
    "to_markdown", "generate_keypair", "sign_head", "verify_chain",
    "verify_signatures",
]
