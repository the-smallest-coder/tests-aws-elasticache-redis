"""WP0 — cluster_details.json must survive a run untouched.

node_details.tf uploads a rich artifact at apply time. Before this fix,
exporter.py's main() clobbered the same S3 key with a thin, env-derived
subset just before reading it back four lines later in report_generator.py.
See PLAN_2.md WP0.
"""

import importlib.util
import io
import json
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
REPORTER_DIR = str(ROOT / "reporter")
if REPORTER_DIR not in sys.path:
    sys.path.insert(0, REPORTER_DIR)


def _load_exporter():
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = mock.Mock(side_effect=AssertionError("unexpected boto3 client"))
    fake_report_generator = types.ModuleType("report_generator")
    fake_report_generator.run_uploaded_report = mock.Mock(return_value={"present": True})

    module_name = "exporter_cluster_details_test_subject"
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "reporter" / "exporter.py")
    exporter = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"boto3": fake_boto3, "report_generator": fake_report_generator}):
        spec.loader.exec_module(exporter)
    return exporter


class FakeS3:
    """Records every put_object key/body; serves get_object from the same store."""

    def __init__(self, initial=None):
        self.storage = {key: _as_bytes(value) for key, value in (initial or {}).items()}
        self.put_calls = []

    def put_object(self, **params):
        key = params["Key"]
        self.put_calls.append(key)
        self.storage[key] = _as_bytes(params["Body"])

    def get_object(self, **params):
        return {"Body": io.BytesIO(self.storage[params["Key"]])}


def _as_bytes(value):
    return value.encode("utf-8") if isinstance(value, str) else value


ENV = {
    "CLUSTER_ID": "elasticache-perf-test-valkey-20260810-al",
    "ECS_CLUSTER": "loadgen-cluster",
    "ECS_SERVICE": "loadgen-service",
    "S3_BUCKET": "test-bucket",
    "S3_PREFIX": "exports/",
    "REPORT_TIMESTAMP": "20260810-125015-al",
}
CLUSTER_DETAILS_KEY = f"{ENV['S3_PREFIX']}{ENV['REPORT_TIMESTAMP']}/cluster_details.json"
RICH_ARTIFACT = json.dumps({
    "run": {"cluster_id": ENV["CLUSTER_ID"]},
    "elasticache": {"availability_zone": "us-east-1f", "engine": "valkey"},
    "memtier": {"task_count": 6},
}).encode("utf-8")


DEFAULT_ELASTICACHE_MANIFEST = {
    "uri": "s3://test-bucket/metrics.csv",
    "discovered": ["CacheHits"],
    "exported": 1,
    "zero_datapoints": [],
    "missing_from_discovery": [],
    "errors": [],
    "rows_written": 10,
    "complete": True,
    "missing": [],
}


def _run_main_with_fakes(fake_s3: FakeS3, elasticache_manifest: dict | None = None):
    exporter = _load_exporter()
    exporter.export_loadgen_logs_to_s3 = lambda *a, **k: {
        "complete": True,
        "first_message_ts": datetime(2026, 8, 10, 12, 50, tzinfo=timezone.utc),
        "last_message_ts": datetime(2026, 8, 10, 13, 50, tzinfo=timezone.utc),
        "files": [],
    }
    exporter._generate_and_upload_memtier_etl = lambda *a, **k: None
    exporter.export_elasticache_metrics_to_s3 = lambda *a, **k: (
        elasticache_manifest if elasticache_manifest is not None else dict(DEFAULT_ELASTICACHE_MANIFEST)
    )
    exporter.export_logs_to_s3 = lambda *a, **k: None
    exporter._task_metadata_from_container_insights_object = lambda *a, **k: {}
    exporter.export_ecs_metrics_to_s3 = lambda *a, **k: "s3://test-bucket/ecs.csv"
    exporter.send_report_ready_email = lambda *a, **k: True
    exporter.s3 = fake_s3

    with mock.patch.dict("os.environ", ENV, clear=False):
        exporter.main()
    return exporter


class ExporterNeverWritesClusterDetailsTests(unittest.TestCase):
    def test_main_does_not_put_object_to_cluster_details_key(self):
        fake_s3 = FakeS3(initial={CLUSTER_DETAILS_KEY: RICH_ARTIFACT})

        _run_main_with_fakes(fake_s3)

        self.assertNotIn(CLUSTER_DETAILS_KEY, fake_s3.put_calls)

    def test_existing_rich_artifact_survives_a_run_byte_for_byte(self):
        fake_s3 = FakeS3(initial={CLUSTER_DETAILS_KEY: RICH_ARTIFACT})

        _run_main_with_fakes(fake_s3)

        self.assertEqual(fake_s3.storage[CLUSTER_DETAILS_KEY], RICH_ARTIFACT)


class MergeMissingConfigPrecedenceTests(unittest.TestCase):
    """run_uploaded_report composes _merge_missing_config(cluster_details, env) —
    the Terraform artifact is the base and env only fills what it lacks."""

    def _load(self):
        try:
            from report_generator import _config_from_cluster_details, _config_from_env, _merge_missing_config
        except ModuleNotFoundError as exc:
            self.skipTest(f"{exc.name} is not installed in this environment")
        return _config_from_cluster_details, _config_from_env, _merge_missing_config

    def test_rich_artifact_fields_reach_report_config_env_fills_gaps_only(self):
        _config_from_cluster_details, _config_from_env, _merge_missing_config = self._load()

        cluster_details = {
            "elasticache": {
                "engine": "valkey",
                "node_type": "cache.t4g.micro",
                "availability_zone": "us-east-1f",
            },
        }
        with mock.patch.dict("os.environ", {"ENGINE_TYPE": "valkey", "NODE_TYPE": "cache.t4g.micro"}, clear=True):
            report_config = _merge_missing_config(
                _config_from_cluster_details(cluster_details),
                _config_from_env(),
            )

        self.assertEqual(report_config["engine_type"], "valkey")
        self.assertEqual(report_config["node_type"], "cache.t4g.micro")
        # Only the rich artifact carries this field; env has no equivalent key at all.
        self.assertEqual(report_config["elasticache_availability_zone"], "us-east-1f")

    def test_terraform_value_wins_over_a_different_env_value(self):
        _config_from_cluster_details, _config_from_env, _merge_missing_config = self._load()

        cluster_details = {"elasticache": {"node_hourly_usd": "0.226"}}
        with mock.patch.dict("os.environ", {"NODE_HOURLY_USD": "0.156"}, clear=True):
            report_config = _merge_missing_config(
                _config_from_cluster_details(cluster_details),
                _config_from_env(),
            )

        self.assertEqual(report_config["node_hourly_usd"], "0.226")


class MissingClusterDetailsIsAVisibleDefectTests(unittest.TestCase):
    def test_missing_artifact_still_generates_report_but_flags_the_gap(self):
        try:
            import report_generator
        except ModuleNotFoundError as exc:
            self.skipTest(f"{exc.name} is not installed in this environment")
        try:
            import pandas as pd
        except ModuleNotFoundError:
            self.skipTest("pandas is not installed in this environment")

        cluster_details_uri = (
            f"s3://{ENV['S3_BUCKET']}/{ENV['S3_PREFIX']}{ENV['REPORT_TIMESTAMP']}/cluster_details.json"
        )

        def fake_read_file_content(uri):
            if uri == cluster_details_uri:
                raise RuntimeError("NoSuchKey: the artifact does not exist")
            return ""

        fake_s3 = FakeS3()
        fake_boto3 = types.ModuleType("boto3")
        fake_boto3.client = mock.Mock(return_value=fake_s3)

        originals = {
            name: getattr(report_generator, name)
            for name in (
                "read_file_content",
                "parse_metrics_csv",
                "_warn_if_cache_hit_rate_missing",
                "_read_uploaded_log_contents",
                "_parse_memtier_log_entries",
                "_read_uploaded_container_insights_contents",
                "_parse_container_insights_entries",
                "_read_uploaded_memtier_artifact_contents",
                "_load_memtier_artifacts",
                "create_report",
            )
        }
        report_generator.read_file_content = fake_read_file_content
        report_generator.parse_metrics_csv = lambda content: pd.DataFrame()
        report_generator._warn_if_cache_hit_rate_missing = lambda *a, **k: None
        report_generator._read_uploaded_log_contents = lambda prefix: [("s3://bucket/loadgen.log", "log")]
        report_generator._parse_memtier_log_entries = lambda entries: (
            pd.DataFrame(),
            {
                "first_message_ts": datetime(2026, 8, 10, 12, 50, tzinfo=timezone.utc),
                "last_message_ts": datetime(2026, 8, 10, 13, 50, tzinfo=timezone.utc),
            },
        )
        report_generator._read_uploaded_container_insights_contents = lambda prefix: []
        report_generator._parse_container_insights_entries = lambda entries: (None, None)
        report_generator._read_uploaded_memtier_artifact_contents = lambda prefix: [("s3://bucket/a.totals.json", "{}")]
        report_generator._load_memtier_artifacts = lambda entries: (None, None)
        report_generator.create_report = lambda **kwargs: ("<html></html>", json.dumps({"meta": {}}))

        try:
            with mock.patch.dict("sys.modules", {"boto3": fake_boto3}):
                with mock.patch.dict("os.environ", ENV, clear=False):
                    result = report_generator.run_uploaded_report()
        finally:
            for name, value in originals.items():
                setattr(report_generator, name, value)

        self.assertEqual(result["present"], False)
        self.assertIn("NoSuchKey", result["reason"])

        json_key = f"{ENV['S3_PREFIX']}{ENV['REPORT_TIMESTAMP']}/results_{ENV['REPORT_TIMESTAMP']}.json"
        uploaded_summary = json.loads(fake_s3.storage[json_key])
        warnings = uploaded_summary["meta"]["warnings"]
        self.assertTrue(
            any("cluster_details.json" in warning for warning in warnings),
            warnings,
        )


class TaskCountEnvVarNotIntroducedTests(unittest.TestCase):
    def test_reporter_tf_does_not_define_a_task_count_env_var(self):
        reporter_tf = (ROOT / "reporter.tf").read_text(encoding="utf-8")
        self.assertNotIn('"TASK_COUNT"', reporter_tf)


class PresentButEmptyClusterDetailsTests(unittest.TestCase):
    """Regression (code review, post-WP0-6): a cluster_details.json that
    parses successfully but is falsy ({}, null, []) used to raise KeyError
    on cluster_details_status['reason'] -- that key only exists on the
    except/failure branch, but the code branched on cluster_details' own
    truthiness, which is False for a present-but-empty body too.
    """

    def test_empty_object_cluster_details_does_not_raise(self):
        try:
            import report_generator
        except ModuleNotFoundError as exc:
            self.skipTest(f"{exc.name} is not installed in this environment")
        try:
            import pandas as pd
        except ModuleNotFoundError:
            self.skipTest("pandas is not installed in this environment")

        cluster_details_uri = (
            f"s3://{ENV['S3_BUCKET']}/{ENV['S3_PREFIX']}{ENV['REPORT_TIMESTAMP']}/cluster_details.json"
        )

        def fake_read_file_content(uri):
            if uri == cluster_details_uri:
                return "{}"  # parses fine, but is falsy
            return ""

        fake_s3 = FakeS3()
        fake_boto3 = types.ModuleType("boto3")
        fake_boto3.client = mock.Mock(return_value=fake_s3)

        originals = {
            name: getattr(report_generator, name)
            for name in (
                "read_file_content",
                "parse_metrics_csv",
                "_warn_if_cache_hit_rate_missing",
                "_read_uploaded_log_contents",
                "_parse_memtier_log_entries",
                "_read_uploaded_container_insights_contents",
                "_parse_container_insights_entries",
                "_read_uploaded_memtier_artifact_contents",
                "_load_memtier_artifacts",
                "create_report",
            )
        }
        report_generator.read_file_content = fake_read_file_content
        report_generator.parse_metrics_csv = lambda content: pd.DataFrame()
        report_generator._warn_if_cache_hit_rate_missing = lambda *a, **k: None
        report_generator._read_uploaded_log_contents = lambda prefix: [("s3://bucket/loadgen.log", "log")]
        report_generator._parse_memtier_log_entries = lambda entries: (
            pd.DataFrame(),
            {
                "first_message_ts": datetime(2026, 8, 10, 12, 50, tzinfo=timezone.utc),
                "last_message_ts": datetime(2026, 8, 10, 13, 50, tzinfo=timezone.utc),
            },
        )
        report_generator._read_uploaded_container_insights_contents = lambda prefix: []
        report_generator._parse_container_insights_entries = lambda entries: (None, None)
        report_generator._read_uploaded_memtier_artifact_contents = lambda prefix: [("s3://bucket/a.totals.json", "{}")]
        report_generator._load_memtier_artifacts = lambda entries: (None, None)
        report_generator.create_report = lambda **kwargs: ("<html></html>", json.dumps({"meta": {}}))

        try:
            with mock.patch.dict("sys.modules", {"boto3": fake_boto3}):
                with mock.patch.dict("os.environ", ENV, clear=False):
                    result = report_generator.run_uploaded_report()  # must not raise KeyError
        finally:
            for name, value in originals.items():
                setattr(report_generator, name, value)

        self.assertEqual(result["present"], True)
        json_key = f"{ENV['S3_PREFIX']}{ENV['REPORT_TIMESTAMP']}/results_{ENV['REPORT_TIMESTAMP']}.json"
        self.assertIn(json_key, fake_s3.storage)


class MetricsHardAbortGateTests(unittest.TestCase):
    """Regression (code review): the hard RuntimeError abort in main() used
    to be tied to the wide, topology-conditional completeness check
    (elasticache_manifest["complete"], ~30 names including 4 unconfirmed for
    redis). One optional metric absent for an unrelated reason (ListMetrics
    propagation lag, an engine that doesn't publish it) would abort report
    generation entirely, after the cluster is already torn down. The gate
    must instead be narrow: did the export come back with real rows at all.
    """

    def test_incomplete_wide_contract_with_real_rows_does_not_abort(self):
        fake_s3 = FakeS3(initial={CLUSTER_DETAILS_KEY: RICH_ARTIFACT})
        manifest = dict(DEFAULT_ELASTICACHE_MANIFEST)
        manifest["complete"] = False
        manifest["missing"] = ["CPUCreditBalance", "CPUCreditUsage"]
        manifest["missing_from_discovery"] = ["CPUCreditBalance", "CPUCreditUsage"]
        manifest["rows_written"] = 500

        _run_main_with_fakes(fake_s3, elasticache_manifest=manifest)  # must not raise

        status = json.loads(fake_s3.storage[f"{ENV['S3_PREFIX']}{ENV['REPORT_TIMESTAMP']}/report_status.json"])
        self.assertTrue(status["complete"])
        self.assertTrue(status["checks"]["metrics"]["complete"])
        self.assertFalse(status["metric_export"]["complete"])  # wide check stays visible, just not a gate

    def test_zero_rows_written_still_aborts(self):
        fake_s3 = FakeS3(initial={CLUSTER_DETAILS_KEY: RICH_ARTIFACT})
        manifest = dict(DEFAULT_ELASTICACHE_MANIFEST)
        manifest["rows_written"] = 0
        manifest["exported"] = 0

        with self.assertRaises(RuntimeError):
            _run_main_with_fakes(fake_s3, elasticache_manifest=manifest)


if __name__ == "__main__":
    unittest.main()
