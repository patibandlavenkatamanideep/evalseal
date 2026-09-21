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
    AnthropicTarget,
    LocalCallableTarget,
    OpenAICompatibleTarget,
    Target,
    TargetResponse,
    extract_text,
)
from .analyze import CaseStats, analyze_case, classify_stability, flip_rate, wilson_ci
from .decompose import CaseDecomposition, Decomposition, decompose
from .diffing import (
    DiffResult,
    diff_records,
    load_receipt,
    mean_flip_rate,
    per_case_halfwidth,
)
from .executor import run_eval
from .htmlreport import diff_to_html, to_html, write_diff_html, write_html
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
from .paired import (
    PairedComparison,
    compare_runs,
    exact_mcnemar,
    paired_bootstrap_ci,
    paired_permutation_p,
)
from .policy import (
    Check,
    Policy,
    PolicyError,
    PolicyResult,
    evaluate,
    load_policy,
)
from .power import PowerEstimate, estimate_from_record, min_discordant_items
from .provenance import file_hash, git_provenance, text_hash
from .report import render_diff, to_junit, to_markdown
from .signing import generate_keypair, sign_head, verify_signatures

__version__ = "2.0.0"

__all__ = [
    "Aggregate", "Case", "CaseResult", "CaseStats", "Cassette", "Dataset",
    "AnswerMatchScorer", "AnthropicTarget", "ExactMatchScorer", "LLMJudgeScorer",
    "LocalCallableTarget", "extract_text",
    "OpenAICompatibleTarget",
    "DiffResult", "ProvenanceManifest", "RegexScorer", "RunRecord", "ScoreResult",
    "Scorer", "Target",
    "TargetResponse", "__version__", "analyze_case", "classify_stability",
    "config_fingerprint", "diff_records", "evaluator_fingerprint",
    "extract_answer", "file_hash", "load_receipt", "render_diff",
    "flip_rate", "git_provenance", "mean_flip_rate", "per_case_halfwidth",
    "PairedComparison", "compare_runs", "exact_mcnemar",
    "CaseDecomposition", "Decomposition", "decompose",
    "PowerEstimate", "estimate_from_record", "min_discordant_items",
    "paired_bootstrap_ci", "paired_permutation_p",
    "diff_to_html", "to_html", "write_diff_html", "write_html",
    "Check", "Policy", "PolicyError", "PolicyResult", "evaluate", "load_policy",
    "ledger_lock", "strip_decoration", "text_hash", "wilson_ci",
    "last_hash", "load_all", "run_eval", "seal_and_append", "slot", "to_junit",
    "to_markdown", "generate_keypair", "sign_head", "verify_chain",
    "verify_signatures",
]
