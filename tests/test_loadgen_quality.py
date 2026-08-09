import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORTER_DIR = str(ROOT / "reporter")
if REPORTER_DIR not in sys.path:
    sys.path.insert(0, REPORTER_DIR)


class LoadgenQualityTests(unittest.TestCase):
    def test_cluster_details_availability_zone_reaches_report_config(self):
        try:
            from report_generator import _config_from_cluster_details
        except ModuleNotFoundError as exc:
            self.skipTest(f"{exc.name} is not installed in this environment")

        config = _config_from_cluster_details({
            "elasticache": {"availability_zone": "us-east-1f"},
        })

        self.assertEqual(config["elasticache_availability_zone"], "us-east-1f")

    def test_visible_ecs_metrics_are_neutral(self):
        try:
            from cards import loadgen_quality_html
        except ModuleNotFoundError as exc:
            self.skipTest(f"{exc.name} is not installed in this environment")

        html = loadgen_quality_html({
            "validation_status": "invalid",
            "invalid_reasons": ["generator_cpu_p95_above_85_pct"],
            "unknown_reasons": [],
            "expected_task_count": 2,
            "generator_cpu_limited": True,
            "generator_cpu_p95_by_task": [
                {"task_id": "a", "p95_pct": 98.0},
                {"task_id": "b", "p95_pct": 50.0},
            ],
            "throughput_median_by_task": [],
            "throughput_by_az": [],
        })

        self.assertEqual(html.count("<table>"), 2)
        self.assertIn("<th>#</th>", html)
        self.assertIn("<th>ECS Task ID</th>", html)
        self.assertNotIn("ECS Task Metrics", html)
        self.assertNotIn("ECS Task Diagnostics", html)
        self.assertNotIn("<h3", html)
        self.assertNotIn("group-header", html)
        self.assertNotIn("Invalid", html)
        self.assertNotIn("Validity", html)
        self.assertNotIn("warning", html.lower())
        self.assertNotIn("quality-status", html)
        self.assertNotIn("Expected ECS tasks", html)
        self.assertNotIn("complete minutes", html)
        self.assertNotIn("fleet p90/p10", html)
        self.assertNotIn("Load Generator", html)
        self.assertNotIn("per task + AZ", html)

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
        self.assertEqual(result["diagnostic_status"], "warning")
        self.assertNotIn("validation_status", result)
        self.assertNotIn("invalid_reasons", result)
        self.assertIn("generator_cpu_p95_above_85_pct", result["warning_reasons"])
        self.assertEqual(
            [row["sample_count"] for row in result["generator_cpu_p95_by_task"]],
            [1, 1, 1, 1],
        )
        self.assertEqual(
            {row["task_id"]: row["task_index"] for row in result["generator_cpu_p95_by_task"]},
            {"a": 1, "b": 2, "c": 3, "d": 4},
        )
        self.assertGreater(result["throughput_skew_within_az_max"], 1.3)
        self.assertEqual(
            {row["task_id"]: row["median_ops_sec"] for row in result["throughput_median_by_task"]},
            {"a": 100.0, "b": 200.0, "c": 50.0, "d": 52.0},
        )
        self.assertEqual(
            {row["task_id"]: row["task_index"] for row in result["throughput_median_by_task"]},
            {"a": 1, "b": 2, "c": 3, "d": 4},
        )

    def test_task_cpu_falls_back_to_ecs_dimensions_when_container_insights_is_empty(self):
        """When Container Insights EMF data is unavailable, per-task CPU
        falls back to the raw ECS CloudWatch metrics CSV (Dimensions string
        column, e.g. "ClusterName=c;ServiceName=s;TaskId=a"). That fallback
        shares helpers.ecs_task_metric_rows's dimension-set dedup rule
        (prefer the row carrying ServiceName over a bare TaskId-only row for
        the same Timestamp/TaskId) rather than a second copy of that rule --
        this pins the shared behavior so a future edit to one doesn't
        silently diverge from the other.
        """
        try:
            import pandas as pd

            from loadgen_analysis import build_loadgen_summary
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

        samples = pd.DataFrame([
            {"Timestamp": "2026-08-01T00:01:10Z", "Stream": "a", "Ops/sec": 100},
            {"Timestamp": "2026-08-01T00:01:10Z", "Stream": "b", "Ops/sec": 200},
        ])
        # Report window must wholly contain the 00:01:00 minute bucket, or
        # build_loadgen_summary discards it as a partial boundary minute and
        # every per-task vector -- CPU included -- comes back empty.
        minutes = pd.DataFrame([
            {"Timestamp": "2026-08-01T00:01:00Z", "task_count_present": 2},
        ])

        def _metric(metric_name, value, dimensions):
            return {
                "Timestamp": "2026-08-01T00:01:00Z",
                "Namespace": "AWS/ECS",
                "MetricName": metric_name,
                "Stat": "Average",
                "Value": value,
                "Unit": "Percent",
                "Dimensions": dimensions,
            }

        ecs_df = pd.DataFrame([
            # Task "a" has two CpuUtilized dimension sets for the same
            # Timestamp/TaskId; the bare one must lose to the ServiceName one.
            _metric("CpuUtilized", 999, "ClusterName=c;TaskId=a"),
            _metric("CpuUtilized", 60, "ClusterName=c;ServiceName=s;TaskId=a"),
            _metric("CpuReserved", 100, "ClusterName=c;ServiceName=s;TaskId=a"),
            _metric("CpuUtilized", 40, "ClusterName=c;ServiceName=s;TaskId=b"),
            _metric("CpuReserved", 100, "ClusterName=c;ServiceName=s;TaskId=b"),
        ])

        result = build_loadgen_summary(
            samples,
            minutes,
            ecs_df,
            pd.DataFrame(),
            pd.DataFrame(),
            pd.Timestamp("2026-08-01T00:01:00"),
            pd.Timestamp("2026-08-01T00:02:00"),
        )

        self.assertEqual(
            {row["task_id"]: row["p95_pct"] for row in result["generator_cpu_p95_by_task"]},
            {"a": 60.0, "b": 40.0},
        )

    def test_task_indexes_are_deterministic_for_shuffled_input(self):
        try:
            from helpers import ecs_task_index_map
        except ModuleNotFoundError as exc:
            self.skipTest(f"{exc.name} is not installed in this environment")

        task_az = {
            "task-d": "us-east-1f",
            "task-b": "us-east-1e",
            "task-c": "us-east-1f",
            "task-a": "us-east-1e",
            "task-z": "unknown",
        }

        forward = ecs_task_index_map(
            ["task-d", "task-b", "task-z", "task-a", "task-c"],
            task_az,
            "us-east-1f",
        )
        shuffled = ecs_task_index_map(
            ["task-c", "task-a", "task-d", "task-z", "task-b"],
            dict(reversed(list(task_az.items()))),
            "us-east-1f",
        )

        self.assertEqual(forward, shuffled)
        self.assertEqual(
            forward,
            {
                "task-c": 1,
                "task-d": 2,
                "task-a": 3,
                "task-b": 4,
                "task-z": 5,
            },
        )

    def test_elasticache_az_tasks_receive_first_indexes(self):
        try:
            from helpers import ecs_task_index_map
        except ModuleNotFoundError as exc:
            self.skipTest(f"{exc.name} is not installed in this environment")

        indexes = ecs_task_index_map(
            ["task-a", "task-b", "task-c", "task-d"],
            {
                "task-a": "us-east-1e",
                "task-b": "us-east-1f",
                "task-c": "us-east-1e",
                "task-d": "us-east-1f",
            },
            "us-east-1f",
        )

        self.assertEqual([indexes["task-b"], indexes["task-d"]], [1, 2])
        self.assertEqual([indexes["task-a"], indexes["task-c"]], [3, 4])

    def test_missing_elasticache_az_is_inferred_from_task_latency_and_marked(self):
        try:
            import pandas as pd

            from cards import loadgen_quality_html
            from helpers import elasticache_availability_zone
            from loadgen_analysis import build_loadgen_summary
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

        # Fixture captured verbatim from a real downloaded run
        # (results/20260802-012513-ab): 6 ECS tasks split 3/3 across two
        # AZs. Container Insights' TaskId and memtier's totals.json
        # stream_id both name the same 32-character ECS task ID (the
        # memtier side carries a realistic "memtier/memtier/" log-stream
        # prefix, exercising the prefix strip in `_task_latency_map`).
        # A prior version of this fallback joined on the ECS ClientLatency
        # EMF metric instead, whose TaskId dimension is a self-generated
        # UUID in a completely different namespace (also captured from the
        # same run, see test_elasticache_az_inference_ignores_disjoint_ids
        # below) - that join always failed silently on real data.
        task_az = {
            "2e5ee144bb604580a48b378e5300d81c": "us-east-1f",
            "e4e8bfbb425541d092c39868432d77ea": "us-east-1f",
            "e6404c50f8864d66bac89d7286f37302": "us-east-1f",
            "6f768707472b447f929a82ca7ffc3b60": "us-east-1e",
            "84c52bc998d64e21837d5641d56cac19": "us-east-1e",
            "d137d22478864d45a79f314f4a5667a3": "us-east-1e",
        }
        task_p50 = {
            "2e5ee144bb604580a48b378e5300d81c": 0.231,
            "e4e8bfbb425541d092c39868432d77ea": 0.207,
            "e6404c50f8864d66bac89d7286f37302": 0.159,
            "6f768707472b447f929a82ca7ffc3b60": 0.615,
            "84c52bc998d64e21837d5641d56cac19": 0.559,
            "d137d22478864d45a79f314f4a5667a3": 0.599,
        }

        inferred = elasticache_availability_zone(
            task_latency_map=task_p50,
            task_az_map=task_az,
        )

        self.assertEqual(inferred["availability_zone"], "us-east-1f")
        self.assertEqual(inferred["source"], "inferred_from_memtier_task_p50_latency")

        samples = pd.DataFrame([
            {
                "Timestamp": "2026-08-01T00:01:10Z",
                "Stream": f"memtier/memtier/{task_id}",
                "Ops/sec": 100,
            }
            for task_id in task_az
        ])
        minutes = pd.DataFrame([
            {"Timestamp": "2026-08-01T00:00:00Z", "task_count_present": 6},
            {"Timestamp": "2026-08-01T00:01:00Z", "task_count_present": 6},
            {"Timestamp": "2026-08-01T00:02:00Z", "task_count_present": 6},
        ])
        ci_tasks = pd.DataFrame([
            {
                "Timestamp": "2026-08-01T00:01:00Z",
                "TaskId": task_id,
                "AvailabilityZone": az,
                "CpuUtilized": 50,
                "CpuReserved": 100,
                "Stat": "Average",
            }
            for task_id, az in task_az.items()
        ])
        ci_service = pd.DataFrame([{
            "Timestamp": "2026-08-01T00:01:00Z",
            "RunningTaskCount": 6,
        }])
        # "source" is the *.totals.json artifact path exactly as produced by
        # a local `report_generator.py generate` run - not the payload's own
        # (prefixed) stream_id field. Getting this shape wrong is what broke
        # the fix on the first pass: the ".totals.json" suffix survived
        # normalization and no longer matched task_az_map's bare task IDs.
        memtier_totals_df = pd.DataFrame([
            {
                "source": f"/run/logs/loadgen/memtier/memtier/{task_id}.totals.json",
                "p50_latency_ms": p50,
            }
            for task_id, p50 in task_p50.items()
        ])
        result = build_loadgen_summary(
            samples,
            minutes,
            pd.DataFrame(),
            ci_tasks,
            ci_service,
            pd.Timestamp("2026-08-01T00:00:10"),
            pd.Timestamp("2026-08-01T00:02:10"),
            memtier_totals_df=memtier_totals_df,
        )

        self.assertEqual(result["elasticache_availability_zone"], "us-east-1f")
        self.assertEqual(
            result["elasticache_availability_zone_source"],
            "inferred_from_memtier_task_p50_latency",
        )
        # us-east-1f (the inferred ElastiCache AZ) must rank first.
        self.assertEqual(
            [row["task_id"] for row in result["throughput_median_by_task"]],
            [
                "2e5ee144bb604580a48b378e5300d81c",
                "e4e8bfbb425541d092c39868432d77ea",
                "e6404c50f8864d66bac89d7286f37302",
                "6f768707472b447f929a82ca7ffc3b60",
                "84c52bc998d64e21837d5641d56cac19",
                "d137d22478864d45a79f314f4a5667a3",
            ],
        )
        self.assertEqual(
            [row["task_index"] for row in result["throughput_median_by_task"]],
            [1, 2, 3, 4, 5, 6],
        )

        html = loadgen_quality_html(result)
        self.assertIn(
            "us-east-1f — ElastiCache AZ (inferred from memtier task latency)",
            html,
        )

    def test_task_latency_map_normalizes_both_totals_source_shapes(self):
        """`memtier_totals_df["source"]` takes two different real shapes:
        a *.totals.json artifact path (local/uploaded sidecar files) or a
        bare stream_id (in-memory reconstruction from raw logs). Both must
        normalize to the same bare ECS task ID that Container Insights uses.
        """
        try:
            import pandas as pd

            from loadgen_analysis import _task_latency_map
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

        totals_df = pd.DataFrame([
            {
                "source": "/run/logs/loadgen/memtier/memtier/2e5ee144bb604580a48b378e5300d81c.totals.json",
                "p50_latency_ms": 0.231,
            },
            {
                "source": "memtier/memtier/e4e8bfbb425541d092c39868432d77ea",
                "p50_latency_ms": 0.207,
            },
        ])

        self.assertEqual(
            _task_latency_map(totals_df),
            {
                "2e5ee144bb604580a48b378e5300d81c": 0.231,
                "e4e8bfbb425541d092c39868432d77ea": 0.207,
            },
        )

    def test_elasticache_az_inference_ignores_disjoint_client_latency_uuids(self):
        """Regression: the legacy ECS-ClientLatency-based fallback joined on
        the wrong ID namespace and always failed closed on real data. A
        latency map keyed by those same (real, captured) ClientLatency
        TaskId UUIDs must not coincidentally match the 32-hex ECS task IDs
        in task_az_map - the function should report 'unavailable' rather
        than silently misattributing the AZ.
        """
        try:
            from helpers import elasticache_availability_zone
        except ModuleNotFoundError as exc:
            self.skipTest(f"{exc.name} is not installed in this environment")

        task_az = {
            "2e5ee144bb604580a48b378e5300d81c": "us-east-1f",
            "e4e8bfbb425541d092c39868432d77ea": "us-east-1f",
            "e6404c50f8864d66bac89d7286f37302": "us-east-1f",
            "6f768707472b447f929a82ca7ffc3b60": "us-east-1e",
            "84c52bc998d64e21837d5641d56cac19": "us-east-1e",
            "d137d22478864d45a79f314f4a5667a3": "us-east-1e",
        }
        # Real ECS ClientLatency EMF TaskId dimension values from the same
        # run (results/20260802-012513-ab) - a self-generated UUID
        # namespace, unrelated to the ECS task IDs above.
        client_latency_uuids = [
            "09af642d-6e33-4309-8c89-c429aa459579",
            "46859b77-03d7-4e69-a820-8dbcea3877e2",
            "5a04a81c-6b30-44cc-af9b-e81b73e58916",
            "5d07550b-b9bc-453d-83d4-c2e1c9d43a64",
            "6b9a5e02-efc4-4ee7-aef9-5706211362e8",
            "949de4b1-fdae-4ccb-aa05-78f6221f7817",
        ]

        result = elasticache_availability_zone(
            task_latency_map=dict.fromkeys(client_latency_uuids, 0.2),
            task_az_map=task_az,
        )

        self.assertIsNone(result["availability_zone"])
        self.assertEqual(result["source"], "unavailable")

    def test_elasticache_az_inference_is_ambiguous_when_azs_are_close(self):
        """Two AZs within noise of each other (here ~1.05x apart) must not
        produce a confident winner - that would be exactly as overconfident
        as the alphabetical-fallback bug this whole fix replaced, just
        dressed up with real-looking numbers.
        """
        try:
            from helpers import elasticache_availability_zone
        except ModuleNotFoundError as exc:
            self.skipTest(f"{exc.name} is not installed in this environment")

        task_az = {
            "task-a": "us-east-1e",
            "task-b": "us-east-1e",
            "task-c": "us-east-1f",
            "task-d": "us-east-1f",
        }
        task_p50 = {"task-a": 0.20, "task-b": 0.20, "task-c": 0.21, "task-d": 0.21}

        result = elasticache_availability_zone(
            task_latency_map=task_p50,
            task_az_map=task_az,
        )

        self.assertIsNone(result["availability_zone"])
        self.assertEqual(result["source"], "ambiguous")

    def test_elasticache_az_inference_resolves_at_the_separation_threshold(self):
        """The runner-up clears AZ_INFERENCE_MIN_SEPARATION_RATIO (1.5x) -
        at/above that separation counts as resolvable, not ambiguous.
        (0.32 rather than an exact 0.30 to avoid float-precision noise
        right at the 1.5x boundary itself.)
        """
        try:
            from helpers import elasticache_availability_zone
        except ModuleNotFoundError as exc:
            self.skipTest(f"{exc.name} is not installed in this environment")

        result = elasticache_availability_zone(
            task_latency_map={"task-a": 0.20, "task-b": 0.32},
            task_az_map={"task-a": "us-east-1e", "task-b": "us-east-1f"},
        )

        self.assertEqual(result["availability_zone"], "us-east-1e")
        self.assertEqual(result["source"], "inferred_from_memtier_task_p50_latency")

    def test_zero_latency_does_not_choose_an_alphabetical_az(self):
        try:
            from helpers import elasticache_availability_zone
        except ModuleNotFoundError as exc:
            self.skipTest(f"{exc.name} is not installed in this environment")

        result = elasticache_availability_zone(
            task_latency_map={"task-a": 0.0, "task-b": 0.2},
            task_az_map={"task-a": "us-east-1e", "task-b": "us-east-1f"},
        )

        self.assertIsNone(result["availability_zone"])
        self.assertEqual(result["source"], "ambiguous")

    def test_ambiguous_elasticache_az_is_reported_in_unknown_reasons(self):
        try:
            import pandas as pd

            from loadgen_analysis import build_loadgen_summary
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

        task_az = {
            "task-a": "us-east-1e",
            "task-b": "us-east-1e",
            "task-c": "us-east-1f",
            "task-d": "us-east-1f",
        }
        samples = pd.DataFrame([
            {"Timestamp": "2026-08-01T00:01:10Z", "Stream": task_id, "Ops/sec": 100}
            for task_id in task_az
        ])
        minutes = pd.DataFrame([
            {"Timestamp": "2026-08-01T00:00:00Z", "task_count_present": 4},
            {"Timestamp": "2026-08-01T00:01:00Z", "task_count_present": 4},
            {"Timestamp": "2026-08-01T00:02:00Z", "task_count_present": 4},
        ])
        ci_tasks = pd.DataFrame([
            {
                "Timestamp": "2026-08-01T00:01:00Z",
                "TaskId": task_id,
                "AvailabilityZone": az,
                "CpuUtilized": 50,
                "CpuReserved": 100,
                "Stat": "Average",
            }
            for task_id, az in task_az.items()
        ])
        ci_service = pd.DataFrame([{"Timestamp": "2026-08-01T00:01:00Z", "RunningTaskCount": 4}])
        # ~1.05x apart - within the noise band, must not resolve a winner.
        memtier_totals_df = pd.DataFrame([
            {"source": "task-a", "p50_latency_ms": 0.20},
            {"source": "task-b", "p50_latency_ms": 0.20},
            {"source": "task-c", "p50_latency_ms": 0.21},
            {"source": "task-d", "p50_latency_ms": 0.21},
        ])

        result = build_loadgen_summary(
            samples,
            minutes,
            pd.DataFrame(),
            ci_tasks,
            ci_service,
            pd.Timestamp("2026-08-01T00:00:10"),
            pd.Timestamp("2026-08-01T00:02:10"),
            memtier_totals_df=memtier_totals_df,
        )

        self.assertIsNone(result["elasticache_availability_zone"])
        self.assertEqual(result["elasticache_availability_zone_source"], "ambiguous")
        self.assertIn("elasticache_availability_zone_ambiguous", result["unknown_reasons"])

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

        self.assertEqual(result["diagnostic_status"], "unknown")
        self.assertEqual(
            result["unknown_reasons"],
            ["availability_zone_missing", "elasticache_availability_zone_unavailable"],
        )
        self.assertNotIn("throughput_skew_within_az_above_1_3", result["warning_reasons"])
        self.assertIsNone(result["throughput_skew_within_az_max"])


if __name__ == "__main__":
    unittest.main()
