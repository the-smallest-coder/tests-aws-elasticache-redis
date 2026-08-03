import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORTER_DIR = str(ROOT / "reporter")
if REPORTER_DIR not in sys.path:
    sys.path.insert(0, REPORTER_DIR)


class LoadgenQualityTests(unittest.TestCase):
    def test_summary_migration_renames_service_cpu_and_fills_task_count(self):
        from report_common import enrich_summary_meta

        summary = {
            "ecs": {"avg_cpu_pct": 70.0, "max_cpu_pct": 90.0, "task_count": ""},
            "loadgen": {"expected_task_count": 6},
        }

        enrich_summary_meta(summary, None)

        self.assertEqual(summary["ecs"]["service_cpu_time_avg_pct"], 70.0)
        self.assertEqual(summary["ecs"]["service_cpu_time_peak_pct"], 90.0)
        self.assertEqual(summary["ecs"]["task_count"], 6)
        self.assertNotIn("avg_cpu_pct", summary["ecs"])
        self.assertNotIn("max_cpu_pct", summary["ecs"])

    def test_empty_legacy_memtier_task_count_is_not_copied_to_ecs(self):
        from report_common import enrich_summary_meta

        summary = {"ecs": {}, "loadgen": {}}
        cluster_details = {"memtier": {"task_count": ""}}

        enrich_summary_meta(summary, cluster_details)

        self.assertNotIn("task_count", summary["ecs"])

    def test_per_task_cpu_and_within_az_skew_ignore_incomplete_and_zero_minutes(self):
        try:
            import pandas as pd

            from loadgen_analysis import build_loadgen_summary
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

        sample_rows = []
        for timestamp, values in (
            ("2026-08-01T00:00:10Z", {"a": 999}),
            ("2026-08-01T00:01:10Z", {"a": 100, "b": 200, "c": 50, "d": 52}),
            ("2026-08-01T00:02:10Z", {"a": 100, "b": 200, "c": 50, "d": 52}),
            ("2026-08-01T00:03:10Z", {"a": 999, "b": 999, "c": 999, "d": 999}),
        ):
            for task_id, ops in values.items():
                sample_rows.append({"Timestamp": timestamp, "Stream": task_id, "Ops/sec": ops})
        samples = pd.DataFrame(sample_rows)

        minutes = pd.DataFrame([
            {"Timestamp": "2026-08-01T00:00:00Z", "task_count_present": 1},
            {"Timestamp": "2026-08-01T00:01:00Z", "task_count_present": 4},
            {"Timestamp": "2026-08-01T00:02:00Z", "task_count_present": 3},
            {"Timestamp": "2026-08-01T00:03:00Z", "task_count_present": 4},
        ])
        ci_rows = []
        for timestamp, cpus in (
            ("2026-08-01T00:01:00Z", {"a": 90, "b": 80, "c": 50, "d": 50}),
            ("2026-08-01T00:02:00Z", {"a": 0, "b": 0, "c": 0, "d": 0}),
        ):
            for task_id, utilized in cpus.items():
                ci_rows.append({
                    "Timestamp": timestamp,
                    "TaskId": task_id,
                    "AvailabilityZone": "az-1" if task_id in {"a", "b"} else "az-2",
                    "CpuUtilized": utilized,
                    "CpuReserved": 100,
                    "Stat": "Average",
                })
        ci_tasks = pd.DataFrame(ci_rows)
        ci_service = pd.DataFrame([
            {"Timestamp": "2026-08-01T00:01:00Z", "RunningTaskCount": 4},
        ])

        result = build_loadgen_summary(
            samples,
            minutes,
            pd.DataFrame(),
            ci_tasks,
            ci_service,
            pd.Timestamp("2026-08-01T00:00:10"),
            pd.Timestamp("2026-08-01T00:03:10"),
        )

        self.assertEqual(result["expected_task_count"], 4)
        self.assertEqual(result["complete_minute_count"], 1)
        self.assertEqual(result["discarded_incomplete_minute_count"], 3)
        self.assertEqual(result["discarded_partial_boundary_minute_count"], 2)
        self.assertEqual(result["discarded_missing_task_minute_count"], 1)
        self.assertEqual(result["minutes_below_expected_task_count"], 2)
        self.assertEqual(result["generator_cpu_p95_pct"], 90.0)
        self.assertEqual(
            result["generator_cpu_across_tasks"],
            {"min": 50.0, "median": 65.0, "max": 90.0},
        )
        self.assertTrue(result["generator_cpu_limited"])
        self.assertFalse(result["latency_tail_valid"])
        self.assertEqual(
            [row["sample_count"] for row in result["generator_cpu_p95_by_task"]],
            [1, 1, 1, 1],
        )
        self.assertGreater(result["throughput_skew_within_az_max"], 1.3)
        self.assertEqual(
            {row["task_id"]: row["median_ops_sec"] for row in result["throughput_median_by_task"]},
            {"a": 100.0, "b": 200.0, "c": 50.0, "d": 52.0},
        )

    def test_expected_task_count_uses_mode_not_transient_maximum(self):
        try:
            import pandas as pd

            from loadgen_analysis import _running_task_count
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

        service = pd.DataFrame([
            {"Timestamp": "2026-08-01T00:00:00Z", "RunningTaskCount": 6},
            {"Timestamp": "2026-08-01T00:01:00Z", "RunningTaskCount": 7},
            {"Timestamp": "2026-08-01T00:02:00Z", "RunningTaskCount": 6},
        ])

        count = _running_task_count(
            pd.DataFrame(),
            service,
            pd.Timestamp("2026-08-01T00:00:00"),
            pd.Timestamp("2026-08-01T00:02:00"),
        )

        self.assertEqual(count, 6)

    def test_container_insights_parser_keeps_actual_reservation_and_az(self):
        try:
            from parsers import parse_container_insights_logs
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

        content = (
            '[2026-08-01T00:01:00] [telemetry] '
            '{"Type":"Task","Timestamp":1785542460000,"TaskId":"task-a",'
            '"AvailabilityZone":"us-east-1f","CpuUtilized":245,"CpuReserved":256}\n'
            '[2026-08-01T00:01:00] [service] '
            '{"Type":"Service","Timestamp":1785542460000,"RunningTaskCount":6}\n'
        )

        tasks, services = parse_container_insights_logs(content)

        self.assertEqual(tasks.iloc[0]["TaskId"], "task-a")
        self.assertEqual(tasks.iloc[0]["AvailabilityZone"], "us-east-1f")
        self.assertEqual(float(tasks.iloc[0]["CpuReserved"]), 256.0)
        self.assertEqual(tasks.iloc[0]["Stat"], "Average")
        self.assertEqual(int(services.iloc[0]["RunningTaskCount"]), 6)

    def test_comparison_rows_warn_when_either_run_crosses_a_loadgen_gate(self):
        from report_common import RunData
        from report_compare import metric_rows

        baseline = RunData(
            "Baseline",
            Path("baseline/results_local.json"),
            "baseline",
            {
                "loadgen": {
                    "generator_cpu_across_tasks": {"min": 50.0, "median": 70.0, "max": 90.0},
                    "throughput_skew_within_az_max": 1.1,
                    "latency_tail_valid": False,
                },
                "client_latency": {"p99_ms": 9.0},
            },
            None,
        )
        candidate = RunData(
            "Candidate",
            Path("candidate/results_local.json"),
            "candidate",
            {
                "loadgen": {
                    "generator_cpu_across_tasks": {"min": 40.0, "median": 60.0, "max": 80.0},
                    "throughput_skew_within_az_max": 1.4,
                    "latency_tail_valid": True,
                },
                "client_latency": {"p99_ms": 2.0},
            },
            None,
        )

        rows = {row["path"]: row for row in metric_rows(baseline, candidate)}

        self.assertEqual(
            rows[("loadgen", "generator_cpu_across_tasks", "max")]["tone"],
            "warning",
        )
        self.assertEqual(rows[("loadgen", "throughput_skew_within_az_max")]["tone"], "warning")
        self.assertEqual(rows[("client_latency", "p99_ms")]["tone"], "warning")

    def test_missing_az_disables_within_az_gate_and_reports_unknown_reason(self):
        try:
            import pandas as pd

            from loadgen_analysis import build_loadgen_summary
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

        samples = pd.DataFrame([
            {"Timestamp": "2026-08-01T00:01:10Z", "Stream": "a", "Ops/sec": 100},
            {"Timestamp": "2026-08-01T00:01:10Z", "Stream": "b", "Ops/sec": 300},
        ])
        minutes = pd.DataFrame([
            {"Timestamp": "2026-08-01T00:00:00Z", "task_count_present": 2},
            {"Timestamp": "2026-08-01T00:01:00Z", "task_count_present": 2},
            {"Timestamp": "2026-08-01T00:02:00Z", "task_count_present": 2},
        ])
        ci_tasks = pd.DataFrame([
            {
                "Timestamp": "2026-08-01T00:01:00Z",
                "TaskId": task_id,
                "AvailabilityZone": "unknown",
                "CpuUtilized": 50,
                "CpuReserved": 100,
                "Stat": "Average",
            }
            for task_id in ("a", "b")
        ])
        ci_service = pd.DataFrame([
            {"Timestamp": "2026-08-01T00:01:00Z", "RunningTaskCount": 2},
        ])

        result = build_loadgen_summary(
            samples,
            minutes,
            pd.DataFrame(),
            ci_tasks,
            ci_service,
            pd.Timestamp("2026-08-01T00:00:10"),
            pd.Timestamp("2026-08-01T00:02:10"),
        )

        self.assertEqual(result["validation_status"], "unknown")
        self.assertEqual(result["unknown_reasons"], ["availability_zone_missing"])
        self.assertNotIn("throughput_skew_within_az_above_1_3", result["invalid_reasons"])
        self.assertIsNone(result["throughput_skew_within_az_max"])


if __name__ == "__main__":
    unittest.main()
