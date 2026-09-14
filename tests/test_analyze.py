from evalseal.analyze import analyze_case, flip_rate, classify_stability


def test_stable_case():
    s = analyze_case([1, 1, 1, 1, 1], binary=True)
    assert s.mean == 1.0
    assert s.flip_rate == 0.0
    assert s.stability == "STABLE"
    assert s.majority_verdict == 1


def test_borderline_case():
    # one dissenter in five -> 0.2 flip rate -> BORDERLINE
    s = analyze_case([1, 1, 1, 1, 0], binary=True)
    assert abs(s.flip_rate - 0.2) < 1e-9
    assert s.stability == "BORDERLINE"


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


def test_classify_boundaries():
    assert classify_stability(0.0) == "STABLE"
    assert classify_stability(0.20) == "BORDERLINE"
    assert classify_stability(0.21) == "UNSTABLE"
