"""WP4 tests 35-46: comparison_contract.build_comparison_contract.

See PLAN_2.md WP4. `invalid` is reserved for a defect in ONE run's own
data; two runs that individually look fine but differ on more than one
intended dimension are `conditional` (multi-factor design), never `invalid`
-- the plan's own corpus simulation found that every naive "two variables
differ" case was exactly this, not a data defect.
"""

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORTER_DIR = str(ROOT / "reporter")
if REPORTER_DIR not in sys.path:
    sys.path.insert(0, REPORTER_DIR)


def _control_cluster_details(**overrides):
    details = {
        "memtier": {
            "task_count": 6,
            "clients": 4,
            "threads": 2,
            "pipeline": 8,
            "data_size_bytes": 32,
            "ratio": "1:10",
            "key_pattern": "R:R",
            "key_maximum_total": 100000,
            "test_time_seconds": 3600,
        },
        "ecs": {"fargate_cpu": 512, "fargate_memory": 1024},
        "elasticache": {"cluster_mode_enabled": False, "num_cache_nodes": 1},
    }
    for path, value in overrides.items():
        section, field = path.split(".")
        details[section][field] = value
    return details


def _summary(
    node_type="cache.t4g.micro",
    engine="valkey",
    engine_version="9.0",
    engine_version_actual="9.0.1",
    status="ok",
    schema="2026-09-metrics-contract-v4",
    report_start="2026-08-10T12:00:00",
    report_end="2026-08-10T13:00:00",
    task_count_matches_request=True,
    loadgen_overrides=None,
):
    loadgen = {
        "diagnostic_status": status,
        "task_count_matches_request": task_count_matches_request,
    } if status is not None else {}
    if loadgen_overrides is not None:
        loadgen = loadgen_overrides
    return {
        "meta": {
            "node_type": node_type,
            "engine_type": engine,
            "engine_version": engine_version,
            "engine_version_actual": engine_version_actual,
            "generator_schema_version": schema,
            "report_start": report_start,
            "report_end": report_end,
        },
        "loadgen": loadgen,
    }


def _run(role, summary, cluster_details):
    from report_common import RunData

    return RunData(
        role=role,
        results_path=Path(f"results/{role.lower()}/results_{role.lower()}.json"),
        folder=role.lower(),
        summary=summary,
        cluster_details=cluster_details,
    )


class ComparisonContractTests(unittest.TestCase):
    def _load(self):
        try:
            from comparison_contract import build_comparison_contract
        except ModuleNotFoundError as exc:
            self.skipTest(f"{exc.name} is not installed in this environment")
        return build_comparison_contract

    def test_identical_runs_with_different_node_type_are_comparable(self):
        build = self._load()
        baseline = _run("Baseline", _summary(node_type="cache.t4g.micro"), _control_cluster_details())
        candidate = _run("Candidate", _summary(node_type="cache.m5.large"), _control_cluster_details())

        contract = build(baseline, candidate)

        self.assertEqual(contract["verdict"], "comparable")
        self.assertEqual(contract["reasons"], [])

    def test_two_intended_dimensions_differing_is_conditional_not_invalid(self):
        build = self._load()
        baseline = _run(
            "Baseline",
            _summary(node_type="cache.t4g.micro", engine_version="7.2", engine_version_actual="7.2.0"),
            _control_cluster_details(),
        )
        candidate = _run(
            "Candidate",
            _summary(node_type="cache.m5.large", engine_version="9.0", engine_version_actual="9.0.1"),
            _control_cluster_details(),
        )

        contract = build(baseline, candidate)

        self.assertEqual(contract["verdict"], "conditional")
        codes = [r["code"] for r in contract["reasons"]]
        self.assertIn("multiple_intended_dimensions_differ", codes)
        reason = next(r for r in contract["reasons"] if r["code"] == "multiple_intended_dimensions_differ")
        self.assertEqual(set(reason["fields"]), {"node_type", "engine_version"})

    def test_invalid_diagnostic_status_makes_the_pair_invalid(self):
        build = self._load()
        baseline = _run("Baseline", _summary(status="invalid", task_count_matches_request=None), _control_cluster_details())
        candidate = _run("Candidate", _summary(), _control_cluster_details())

        contract = build(baseline, candidate)

        self.assertEqual(contract["verdict"], "invalid")
        self.assertTrue(any(r["code"] == "diagnostic_status_invalid" for r in contract["reasons"]))

    def test_run_until_stopped_sentinel_does_not_false_positive_as_truncated(self):
        """Regression: ecs.tf's loadgen_memtier_test_time == 0 default (run
        until the shutdown Lambda ends it) is written to cluster_details.json
        as test_time_seconds = 2147483647, not 0 -- memtier's --test-time
        needs a positive integer. _is_truncated_run used to check
        test_time_seconds == 0, a value this rig can never actually produce,
        so truncated_run fired on every real comparison regardless of how
        long the run actually ran. duration_label == "until stopped" is the
        real signal.
        """
        build = self._load()
        overrides = {"memtier.test_time_seconds": 2147483647, "memtier.duration_label": "until stopped"}
        baseline = _run(
            "Baseline",
            _summary(report_start="2026-08-10T12:00:00", report_end="2026-08-10T13:00:00"),
            _control_cluster_details(**overrides),
        )
        candidate = _run(
            "Candidate",
            _summary(node_type="cache.m5.large", report_start="2026-08-10T12:00:00", report_end="2026-08-10T13:00:00"),
            _control_cluster_details(**overrides),
        )

        contract = build(baseline, candidate)

        self.assertEqual(contract["verdict"], "comparable")
        self.assertEqual(contract["reasons"], [])

    def test_short_observed_window_against_a_real_configured_duration_is_still_truncated(self):
        """The until-stopped exemption must not swallow real truncation
        detection: a run configured for a fixed duration that ends far short
        of it (crash, early kill, task replaced mid-run) still has to fire.
        """
        build = self._load()
        baseline = _run(
            "Baseline",
            _summary(report_start="2026-08-10T12:00:00", report_end="2026-08-10T12:10:00"),
            _control_cluster_details(**{"memtier.test_time_seconds": 3600}),
        )
        candidate = _run("Candidate", _summary(), _control_cluster_details())

        contract = build(baseline, candidate)

        self.assertEqual(contract["verdict"], "invalid")
        self.assertTrue(any(r["code"] == "truncated_run" and r["role"] == "Baseline" for r in contract["reasons"]))

    def test_matching_configured_engine_version_with_differing_actual_is_conditional(self):
        build = self._load()
        baseline = _run(
            "Baseline",
            _summary(engine_version="9.0", engine_version_actual="9.0.1"),
            _control_cluster_details(),
        )
        candidate = _run(
            "Candidate",
            _summary(engine_version="9.0", engine_version_actual="9.0.3"),
            _control_cluster_details(),
        )

        contract = build(baseline, candidate)

        self.assertEqual(contract["verdict"], "conditional")
        self.assertTrue(any(r["code"] == "engine_version_actual_differs" for r in contract["reasons"]))

    def test_only_engine_version_differs_with_full_control_variables_is_comparable(self):
        build = self._load()
        baseline = _run(
            "Baseline", _summary(engine_version="7.2", engine_version_actual="7.2.0"), _control_cluster_details(),
        )
        candidate = _run(
            "Candidate", _summary(engine_version="9.0", engine_version_actual="9.0.1"), _control_cluster_details(),
        )

        contract = build(baseline, candidate)

        self.assertEqual(contract["verdict"], "comparable")

    def test_differing_pipeline_is_conditional_and_names_the_field(self):
        build = self._load()
        baseline = _run("Baseline", _summary(), _control_cluster_details(**{"memtier.pipeline": 8}))
        candidate = _run("Candidate", _summary(), _control_cluster_details(**{"memtier.pipeline": 16}))

        contract = build(baseline, candidate)

        self.assertEqual(contract["verdict"], "conditional")
        diff = next(r for r in contract["reasons"] if r["code"] == "control_variable_differs")
        self.assertEqual(diff["field"], "memtier.pipeline")
        self.assertEqual(diff["baseline"], 8)
        self.assertEqual(diff["candidate"], 16)
        self.assertIn(diff, contract["control_diffs"])

    def test_num_cache_nodes_string_vs_int_is_not_a_diff(self):
        build = self._load()
        baseline = _run("Baseline", _summary(), _control_cluster_details(**{"elasticache.num_cache_nodes": "1"}))
        candidate = _run("Candidate", _summary(), _control_cluster_details(**{"elasticache.num_cache_nodes": 1}))

        contract = build(baseline, candidate)

        self.assertEqual(contract["verdict"], "comparable")

    def test_diagnostic_warning_status_is_conditional(self):
        build = self._load()
        baseline = _run("Baseline", _summary(status="warning"), _control_cluster_details())
        candidate = _run("Candidate", _summary(), _control_cluster_details())

        contract = build(baseline, candidate)

        self.assertEqual(contract["verdict"], "conditional")
        self.assertTrue(any(r["code"] == "diagnostic_warning" for r in contract["reasons"]))

    def test_missing_loadgen_block_is_conditional_not_comparable(self):
        build = self._load()
        baseline = _run("Baseline", _summary(loadgen_overrides={}), _control_cluster_details())
        candidate = _run("Candidate", _summary(), _control_cluster_details())

        contract = build(baseline, candidate)

        self.assertEqual(contract["verdict"], "conditional")
        self.assertTrue(any(r["code"] == "loadgen_unknown" for r in contract["reasons"]))

    def test_thin_cluster_details_with_only_task_count_is_control_variables_unknown(self):
        """The real case for 81 of 82 pre-WP0 runs."""
        build = self._load()
        thin = {"memtier": {"task_count": 6}}
        baseline = _run("Baseline", _summary(), thin)
        candidate = _run("Candidate", _summary(), thin)

        contract = build(baseline, candidate)

        self.assertEqual(contract["verdict"], "conditional")
        self.assertTrue(any(r["code"] == "control_variables_unknown" for r in contract["reasons"]))

    def test_empty_string_control_field_is_unknown_not_silently_matching(self):
        """Regression: control_known used to check the raw value
        (control_variable_value(...) is not None), but "" is not None even
        though coerce_control_value("") == None -- a run with a present-but-
        empty control field (not a missing key -- the thin-artifact case
        above) slipped past the knownness gate, then both sides coerced to
        None and compared equal, so it silently produced neither
        control_variables_unknown nor control_variable_differs.
        """
        build = self._load()
        baseline = _run("Baseline", _summary(), _control_cluster_details(**{"memtier.pipeline": ""}))
        candidate = _run("Candidate", _summary(), _control_cluster_details(**{"memtier.pipeline": ""}))

        contract = build(baseline, candidate)

        self.assertEqual(contract["verdict"], "conditional")
        self.assertTrue(any(r["code"] == "control_variables_unknown" for r in contract["reasons"]))
        self.assertFalse(any(r["code"] == "control_variable_differs" for r in contract["reasons"]))

    def test_different_schema_version_is_conditional_not_invalid(self):
        build = self._load()
        baseline = _run("Baseline", _summary(schema="2026-05-loadgen-quality-v1"), _control_cluster_details())
        candidate = _run("Candidate", _summary(schema="2026-08-metrics-contract-v3"), _control_cluster_details())

        contract = build(baseline, candidate)

        self.assertEqual(contract["verdict"], "conditional")
        self.assertTrue(any(r["code"] == "schema_version_differs" for r in contract["reasons"]))
        self.assertNotEqual(contract["verdict"], "invalid")

    def test_invalid_verdict_still_produces_a_written_report_with_the_badge(self):
        """D2, pinned at the comparison-report level: run_compare_report must
        still write HTML with the contract badge when verdict == invalid.
        """
        try:
            from report_compare import build_compare_payload
            from template import render_report
        except ModuleNotFoundError as exc:
            self.skipTest(f"{exc.name} is not installed in this environment")

        baseline = _run(
            "Baseline", _summary(status="invalid", task_count_matches_request=None), _control_cluster_details(),
        )
        candidate = _run("Candidate", _summary(), _control_cluster_details())

        payload = build_compare_payload(baseline, candidate)
        self.assertEqual(payload["contract"]["verdict"], "invalid")

        html = render_report(payload)

        self.assertIn("contract-invalid", html)
        self.assertIn("INVALID", html)

    def test_invalid_verdict_still_reports_control_diffs_and_other_reasons(self):
        """Regression (code review): invalid used to short-circuit and
        return before computing any conditional-tier reason, so control_diffs
        was always [] and every other finding was silently dropped -- a
        reader debugging "why invalid" lost "and what else differs" in the
        same view. All reasons are always computed now; verdict is just the
        most severe classification present.
        """
        build = self._load()
        baseline = _run(
            "Baseline",
            _summary(status="invalid", task_count_matches_request=None),
            _control_cluster_details(**{"memtier.pipeline": 8}),
        )
        candidate = _run(
            "Candidate", _summary(), _control_cluster_details(**{"memtier.pipeline": 16}),
        )

        contract = build(baseline, candidate)

        self.assertEqual(contract["verdict"], "invalid")
        self.assertTrue(any(r["code"] == "diagnostic_status_invalid" for r in contract["reasons"]))
        diff = next(r for r in contract["reasons"] if r["code"] == "control_variable_differs")
        self.assertEqual(diff["field"], "memtier.pipeline")
        self.assertIn(diff, contract["control_diffs"])

    def test_int_and_float_control_values_do_not_register_as_a_diff(self):
        """Regression (code review): coerce_control_value returned int for
        "1" but float for JSON 1.0; 1 == 1.0 so the direct comparison here
        was fine, but repr(1) != repr(1.0), which broke aggregator.py's
        repr()-based fingerprint hash for otherwise-identical runs.
        """
        build = self._load()
        baseline = _run(
            "Baseline", _summary(), _control_cluster_details(**{"elasticache.num_cache_nodes": 1}),
        )
        candidate = _run(
            "Candidate", _summary(), _control_cluster_details(**{"elasticache.num_cache_nodes": 1.0}),
        )

        contract = build(baseline, candidate)

        self.assertEqual(contract["verdict"], "comparable")

    def test_coerce_control_value_normalizes_whole_number_float_to_int(self):
        try:
            from comparison_contract import coerce_control_value
        except ModuleNotFoundError as exc:
            self.skipTest(f"{exc.name} is not installed in this environment")

        self.assertEqual(coerce_control_value("num_cache_nodes", 1.0), 1)
        self.assertEqual(coerce_control_value("num_cache_nodes", 1), 1)
        self.assertEqual(coerce_control_value("num_cache_nodes", "1"), 1)
        self.assertEqual(
            repr(coerce_control_value("num_cache_nodes", 1.0)),
            repr(coerce_control_value("num_cache_nodes", "1")),
        )


class ReportCompareIndependentClientLatencyRowsTests(unittest.TestCase):
    """EMF-based and memtier-Totals-based client latency are two independent
    measurements (schema v4), not a rename pair -- both get their own row.
    """

    def test_emf_and_task_median_p99_are_separate_rows_with_independent_values(self):
        try:
            from report_common import RunData
            from report_compare import metric_rows
        except ModuleNotFoundError as exc:
            self.skipTest(f"{exc.name} is not installed in this environment")

        baseline = RunData(
            role="Baseline", results_path=Path("results/b/results_b.json"), folder="b",
            summary={"client_latency": {"p99_ms": 5.0, "task_median_p99_ms": 4.8}}, cluster_details=None,
        )
        candidate = RunData(
            role="Candidate", results_path=Path("results/c/results_c.json"), folder="c",
            summary={"client_latency": {"p99_ms": 5.5, "task_median_p99_ms": 5.1}}, cluster_details=None,
        )

        rows = metric_rows(baseline, candidate)
        task_median_row = next(r for r in rows if r["label"] == "ECS Task Latency p99 (Totals)")
        emf_row = next(r for r in rows if r["label"] == "ECS Task Latency p99 (EMF)")

        self.assertIn("4.8", task_median_row["baseline"])
        self.assertIn("5", emf_row["baseline"])


if __name__ == "__main__":
    unittest.main()
