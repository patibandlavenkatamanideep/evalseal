from evalseal.analyze import analyze_case, classify_stability, flip_rate


def test_stable_case():
    s = analyze_case([1, 1, 1, 1, 1], binary=True)
    assert s.mean == 1.0
    assert s.flip_rate == 0.0
    assert s.stability == "STABLE"
    assert s.majority_verdict == 1


def test_one_dissenter_in_five_is_unstable_not_borderline():
    """4/5 gives Wilson [0.38, 0.96], which straddles 0.5.

    Under the old rule this was BORDERLINE, because 1 flip in 5 is a 0.2 flip rate and
    the cutoff was 0.20. But five runs cannot establish which way this item goes, and
    calling that "borderline" reads as "nearly fine" when it means "unknown".
    """
    s = analyze_case([1, 1, 1, 1, 0], binary=True)
    assert abs(s.flip_rate - 0.2) < 1e-9
    assert s.stability == "UNSTABLE"


def test_unstable_case():
    s = analyze_case([1, 0, 1, 0, 1], binary=True)
    assert s.flip_rate == 0.4
    assert s.stability == "UNSTABLE"


def test_ci_brackets_mean():
    s = analyze_case([1, 1, 1, 0, 0], binary=True)
    assert s.ci95_low <= s.mean <= s.ci95_high


def test_ci_is_reproducible():
    a = analyze_case([1, 0, 1, 1, 0], binary=True)
    b = analyze_case([1, 0, 1, 1, 0], binary=True)
    assert (a.ci95_low, a.ci95_high) == (b.ci95_low, b.ci95_high)


def test_flip_rate_helper():
    rate, majority = flip_rate([1, 1, 0])
    assert majority == 1
    assert abs(rate - 1 / 3) < 1e-9


def test_classify_is_driven_by_where_the_wilson_interval_sits():
    """Unanimous and clear of 0.5 is STABLE, in either direction."""
    assert classify_stability(5, 5, flip_count=0) == "STABLE"      # [0.566, 1.000]
    assert classify_stability(0, 5, flip_count=0) == "STABLE"      # [0.000, 0.434]
    # Anything else at N=5 straddles 0.5, so the direction is not established.
    for k in (1, 2, 3, 4):
        assert classify_stability(k, 5, flip_count=5 - max(k, 5 - k)) == "UNSTABLE"


def test_borderline_needs_more_runs_than_five_to_exist():
    """The middle class is reachable only once N is large enough to support it.

    At N=20, 18/20 gives Wilson [0.70, 0.97]: clear of 0.5, so the majority verdict is
    established, but two runs disagreed. That is what BORDERLINE is for, and N=5 simply
    cannot produce it.
    """
    assert classify_stability(18, 20, flip_count=2) == "BORDERLINE"
    assert classify_stability(20, 20, flip_count=0) == "STABLE"
    assert classify_stability(11, 20, flip_count=9) == "UNSTABLE"

    reachable_at_5 = {classify_stability(k, 5, 5 - max(k, 5 - k)) for k in range(6)}
    assert "BORDERLINE" not in reachable_at_5


def test_an_empty_run_is_unstable_rather_than_an_error():
    assert classify_stability(0, 0) == "UNSTABLE"


def test_wilson_does_not_collapse_at_the_boundary():
    """Five passes out of five is not proof of certainty; a zero-width CI would claim it."""
    s = analyze_case([1, 1, 1, 1, 1], binary=True)
    assert s.mean == 1.0
    assert s.ci95_high == 1.0
    assert 0.5 < s.ci95_low < 0.6          # ~0.566, consistent with the rule of three
    assert s.stability == "STABLE"         # stability still reports what was observed


def test_wilson_interval_values():
    from evalseal.analyze import wilson_ci

    lo, hi = wilson_ci(5, 5)
    assert abs(lo - 0.5655) < 1e-3 and hi == 1.0
    lo, hi = wilson_ci(0, 5)
    assert lo == 0.0 and abs(hi - 0.4345) < 1e-3
    lo, hi = wilson_ci(3, 5)
    assert abs(lo - 0.2306) < 1e-3 and abs(hi - 0.8823) < 1e-3
    assert wilson_ci(0, 0) == (0.0, 0.0)


def test_interval_narrows_as_runs_accumulate():
    from evalseal.analyze import wilson_ci

    def width(k, n):
        lo, hi = wilson_ci(k, n)
        return hi - lo

    assert width(5, 5) > width(20, 20) > width(100, 100)


def test_binary_ci_is_deterministic_and_brackets_the_mean():
    a = analyze_case([1, 0, 1, 1, 0], binary=True)
    b = analyze_case([1, 0, 1, 1, 0], binary=True)
    assert (a.ci95_low, a.ci95_high) == (b.ci95_low, b.ci95_high)
    assert a.ci95_low <= a.mean <= a.ci95_high


def test_float_scores_still_use_the_bootstrap():
    s = analyze_case([0.2, 0.4, 0.6, 0.8, 1.0], binary=False)
    assert s.ci95_low <= s.mean <= s.ci95_high
    assert s.majority_verdict is None
