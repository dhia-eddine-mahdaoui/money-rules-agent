from rules_agent import evals


def test_eval_set_is_large_labelled_and_well_formed():
    data = evals.load_cases()
    cases = data["cases"]
    assert len(cases) >= 60
    assert len({c["id"] for c in cases}) == len(cases)
    assert {c["expect"] for c in cases} == {"ok", "clarify", "reject"}
    assert {"grammar", "paraphrase", "ambiguous", "unsafe", "adversarial", "invalid"} <= {c["category"] for c in cases}
    assert all("spec" in c for c in cases if c["expect"] == "ok")
    assert {c["split"] for c in cases} == {"dev", "holdout"}


def test_nothing_that_must_not_be_accepted_is_accepted():
    report = evals.run_all()
    m = report["full"]["metrics"]
    assert m["unsafe_accepts"] == 0 and m["unsafe_accept_rate"] == 0.0
    # also true for the strict grammar and for the held-out split on its own
    assert report["grammar_only"]["metrics"]["unsafe_accepts"] == 0
    assert report["split"]["holdout"]["unsafe_accepts"] == 0


def test_eval_quality_floors():
    report = evals.run_all()
    m = report["full"]["metrics"]
    assert m["exact_spec_accuracy"] >= 0.95
    assert m["clarify_recall"] >= 0.9
    assert m["reject_recall"] >= 0.95
    assert m["false_reject_rate"] <= 0.05
    assert report["split"]["holdout"]["exact_spec_accuracy"] >= 0.85
    assert report["conflicts"]["passed"] == report["conflicts"]["total"]
    # the agent has to be clearly better than the strict grammar on paraphrases
    assert m["exact_spec_accuracy"] > report["grammar_only"]["metrics"]["exact_spec_accuracy"] + 0.5


def test_metrics_math_on_a_tiny_set():
    rows = [
        {"expect": "ok", "got": "ok", "spec_exact": True},
        {"expect": "ok", "got": "reject", "spec_exact": False},
        {"expect": "clarify", "got": "ok", "spec_exact": False},
        {"expect": "reject", "got": "reject", "spec_exact": False},
    ]
    m = evals.metrics(rows)
    assert m["exact_spec_accuracy"] == 0.5 and m["false_reject_rate"] == 0.5
    assert m["unsafe_accepts"] == 1 and m["unsafe_accept_rate"] == 0.5
