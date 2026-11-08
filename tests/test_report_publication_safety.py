"""Regression tests for the report publication boundary (reporter/report_safety.py).

Origin: scripts/fetch_elasticache_price.sh once copied AWS CLI stderr -- which
contained an STS ARN -- into node_hourly_usd_reason, and an older renderer
put it into a ``title`` attribute of the hourly-price pill. These tests use that
case as the regression for the general rule:

    A report contains only explicitly allowed data, whatever went wrong on
    the way to it, and an error that prevents a valid report means no file
    is written and nothing is uploaded.

All data below is synthetic: the account id is all zeros, the ARN names a
role that does not exist. Nothing here reads results/ or talks to AWS.
"""

import importlib.util
import json
import math
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
REPORTER_DIR = str(ROOT / "reporter")
if REPORTER_DIR not in sys.path:
    sys.path.insert(0, REPORTER_DIR)


FAKE_ACCOUNT = "000000000000"
FAKE_ARN = f"arn:aws:sts::{FAKE_ACCOUNT}:assumed-role/SyntheticRole_0123456789abcdef/synthetic.user"
RAW_AWS_ERROR = (
    "aws pricing get-products failed: An error occurred (AccessDeniedException) when calling the "
    f"GetProducts operation: User: {FAKE_ARN} is not authorized to perform: pricing:GetProducts "
    "because no identity-based policy allows the pricing:GetProducts action"
)
STACK_TRACE = (
    "Traceback (most recent call last):\n"
    '  File "C:\\Users\\synthetic\\render.py", line 3, in <module>\n'
    "ValueError: synthetic-boom"
)
#: Fragments that must never appear anywhere in a published document.
LEAK_MARKERS = (
    FAKE_ARN, FAKE_ACCOUNT, "SyntheticRole", "synthetic.user", "assumed-role", "sts::",
    "AccessDenied", "An error occurred", "is not authorized", "GetProducts",
    "Traceback", "synthetic-boom", "C:\\Users", "aws pricing",
)

CLUSTER_ID = "cluster-a"
RUN_FOLDER = "20260501-000000-aa"


def skip_without_pandas(test):
    try:
        import pandas  # noqa: F401
        import plotly  # noqa: F401
    except ModuleNotFoundError as exc:
        test.skipTest(f"{exc.name} is not installed in this environment")


def assert_no_leaks(test, document, where="document"):
    for marker in LEAK_MARKERS:
        test.assertNotIn(marker, document, f"{marker!r} leaked into {where}")


# --------------------------------------------------------------------------- #
#  Synthetic run directory                                                     #
# --------------------------------------------------------------------------- #

def _stamp(minute, second=0):
    return f"2026-05-01T00:{minute:02d}:{second:02d}"


def synthetic_cluster_details(**elasticache_overrides):
    elasticache = {
        "engine": "valkey",
        "engine_version_configured": "8.0",
        "node_type": "cache.t4g.micro",
        "num_cache_nodes": 1,
        "cluster_mode_enabled": False,
        # The legacy defect: free text from a failed AWS call, stored verbatim.
        "node_hourly_usd": "",
        "node_hourly_usd_source": "unavailable",
        "node_hourly_usd_reason": RAW_AWS_ERROR,
    }
    elasticache.update(elasticache_overrides)
    return {
        "run": {"run_folder": RUN_FOLDER, "aws_region": "us-east-1", "cluster_id": CLUSTER_ID},
        "elasticache": elasticache,
        "memtier": {
            "task_count": 2, "clients": 4, "threads": 2, "pipeline": 1,
            "data_size_bytes": 32, "ratio": "1:10", "key_pattern": "R:R",
            "key_maximum_total": 100000, "test_time_seconds": 3600,
        },
        "ecs": {"fargate_cpu": 512, "fargate_memory": 1024},
    }


def _memtier_log(stream):
    lines = []
    for index, minute in enumerate((0, 1, 2)):
        ops = 100 * (index + 1)
        lines.append(
            f"[{_stamp(minute)}] [memtier/memtier/{stream}] [RUN #1 {index + 1}%, {index + 1} secs] "
            f"{ops} (avg: {ops}) ops/sec, 1KB/sec (avg: 1KB/sec), 1 (avg: 1) msec latency"
        )
        lines.append(
            f"[{_stamp(minute, 30)}] [memtier/memtier/{stream}] [RUN #1 {index + 1}%, {index + 1} secs] "
            f"{ops} (avg: {ops}) ops/sec, 1KB/sec (avg: 1KB/sec), 1 (avg: 1) msec latency"
        )
    lines.append(f"[{_stamp(1, 40)}] [memtier/memtier/{stream}] -OOM command not allowed")
    lines.append(f"[{_stamp(2, 5)}] [memtier/memtier/{stream}] -OOM command not allowed")
    lines.append(f"[{_stamp(2, 50)}] [memtier/memtier/{stream}] Totals 200 0 0 1 0.5 2 3 10")
    return "\n".join(lines) + "\n"


def synthetic_metrics_csv(extra_rows=()):
    header = "Timestamp,Namespace,MetricName,Stat,Value,Unit,Dimensions"
    dims = f"CacheClusterId={CLUSTER_ID}-001"
    rows = []
    for minute, errors in ((0, 2), (1, 3), (2, 2)):
        stamp = f"{_stamp(minute)}+00:00"
        rows.append(f"{stamp},AWS/ElastiCache,CacheHitRate,Average,{90 - minute},Percent,{dims}")
        rows.append(f"{stamp},AWS/ElastiCache,EngineCPUUtilization,Average,{20 + minute},Percent,{dims}")
        rows.append(f"{stamp},AWS/ElastiCache,ErrorCount,Sum,{errors},Count,{dims}")
        rows.append(f"{stamp},AWS/ElastiCache,Evictions,Sum,{minute},Count,{dims}")
        rows.append(f"{stamp},AWS/ElastiCache,CurrItems,Maximum,{1000 + minute},Count,{dims}")
    rows.extend(extra_rows)
    return header + "\n" + "\n".join(rows) + "\n"


def write_synthetic_run(run_dir, *, cluster_details="default", extra_metric_rows=()):
    """metrics/, logs/ and (optionally) cluster_details.json for one run."""
    run_dir = Path(run_dir)
    (run_dir / "metrics").mkdir(parents=True, exist_ok=True)
    (run_dir / "metrics" / f"{CLUSTER_ID}.csv").write_text(
        synthetic_metrics_csv(extra_metric_rows), encoding="utf-8"
    )
    stream_dir = run_dir / "logs" / "loadgen" / "memtier" / "memtier"
    stream_dir.mkdir(parents=True, exist_ok=True)
    for stream in ("stream-a", "stream-b"):
        (stream_dir / f"{stream}.txt").write_text(_memtier_log(stream), encoding="utf-8")
    if cluster_details == "default":
        cluster_details = synthetic_cluster_details()
    if cluster_details is not None:
        (run_dir / "cluster_details.json").write_text(json.dumps(cluster_details), encoding="utf-8")
    return run_dir


def generate_local(run_dir, output_dir=None):
    from report_generator import run_generate_report

    run_generate_report(str(run_dir), {}, output_dir=output_dir)
    out = Path(output_dir) if output_dir else Path(run_dir)
    return (out / "results_local.html").read_text(encoding="utf-8")


def assert_report_keeps_expected_data(test, document):
    """What a normal report must still say, with absolute timestamps."""
    test.assertIn("2026-05-01 00:00:00 UTC - 2026-05-01 00:02:50 UTC", document)
    test.assertIn("<div class='card-label'>Avg Throughput</div>", document)
    test.assertIn("<div class='card-label'>OOM Rejections</div>", document)
    test.assertIn(">4<span class='card-unit'></span>", document)  # 2 streams x 2 -OOM lines
    test.assertIn("<div class='card-label'>Total Evictions</div>", document)
    test.assertIn("Engine: <span>valkey</span>", document)
    test.assertIn("Node type: <span>cache.t4g.micro</span>", document)
    test.assertIn("Valkey hourly: <span>unavailable</span>", document)
    test.assertIn('title="AWS Price List lookup was unavailable for this run."', document)
    test.assertIn("<h2>Test Conditions</h2>", document)
    test.assertIn("us-east-1", document)
    # The benchmark-side error count (CloudWatch ErrorCount) keeps its own panel.
    test.assertIn("ErrorCount", document)


class PandasTempDirTestCase(unittest.TestCase):
    """Skips without pandas/plotly; gives each test a temporary directory."""

    def setUp(self):
        skip_without_pandas(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)


def load_exporter_harness():
    """tests/test_cluster_details_not_overwritten.py, which loads exporter.py
    with a fake boto3 and drives its main() against an in-memory S3."""
    spec = importlib.util.spec_from_file_location(
        "cluster_details_harness", ROOT / "tests" / "test_cluster_details_not_overwritten.py"
    )
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    return harness


# --------------------------------------------------------------------------- #
#  1. Legacy JSON with a raw AWS error -- local generation                     #
# --------------------------------------------------------------------------- #

class LegacyAwsErrorLocalGenerationTests(PandasTempDirTestCase):
    def setUp(self):
        super().setUp()
        self.run_dir = write_synthetic_run(Path(self.tmp.name) / RUN_FOLDER)

    def test_raw_aws_error_in_legacy_cluster_details_never_reaches_the_html(self):
        document = generate_local(self.run_dir, output_dir=Path(self.tmp.name) / "out")

        assert_no_leaks(self, document)
        assert_report_keeps_expected_data(self, document)

    def test_the_synthetic_input_really_carries_the_error(self):
        """Guard against a vacuous test: the input does contain the leak."""
        raw = (self.run_dir / "cluster_details.json").read_text(encoding="utf-8")
        self.assertIn(FAKE_ARN, raw)

    def test_the_error_does_not_reach_the_html_through_environment_config_either(self):
        from report_generator import run_generate_report

        out = Path(self.tmp.name) / "out"
        run_generate_report(
            str(self.run_dir),
            {"node_hourly_usd_source": "unavailable", "node_hourly_usd_reason": RAW_AWS_ERROR},
            output_dir=str(out),
        )
        assert_no_leaks(self, (out / "results_local.html").read_text(encoding="utf-8"))

    def test_normal_report_keeps_the_benchmark_error_count_in_the_summary(self):
        generate_local(self.run_dir, output_dir=Path(self.tmp.name) / "out")

        summary = json.loads((Path(self.tmp.name) / "out" / "results_local.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["errors"]["error_count_total"], 7)
        self.assertEqual(summary["oom"]["rejection_count"], 4)
        self.assertEqual(summary["meta"]["report_start"], "2026-05-01T00:00:00")


# --------------------------------------------------------------------------- #
#  2. Legacy JSON with a raw AWS error -- ECS / S3 generation                  #
# --------------------------------------------------------------------------- #

class FakeS3:
    def __init__(self):
        self.put_calls = []
        self.storage = {}

    def put_object(self, **params):
        body = params["Body"]
        self.put_calls.append(params["Key"])
        self.storage[params["Key"]] = body.decode("utf-8") if isinstance(body, bytes) else body


UPLOAD_ENV = {
    "S3_BUCKET": "test-bucket",
    "S3_PREFIX": "exports/",
    "REPORT_TIMESTAMP": RUN_FOLDER,
    "CLUSTER_ID": CLUSTER_ID,
    # What the ECS task definition passes down from Terraform (reporter.tf).
    "NODE_HOURLY_USD_SOURCE": "unavailable",
    "NODE_HOURLY_USD_REASON": RAW_AWS_ERROR,
}


def _patch_module(name, module):
    """Replace one entry of sys.modules for a test, restoring only that entry."""
    missing = object()

    class _Patcher:
        def start(self):
            self.saved = sys.modules.get(name, missing)
            sys.modules[name] = module

        def stop(self):
            if self.saved is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = self.saved

    return _Patcher()


class UploadedReportHarness(PandasTempDirTestCase):
    """Drives run_uploaded_report() against an in-memory 'S3' built from a
    synthetic local run, so the real parsing/rendering code runs end to end."""

    def _s3_objects(self, run_dir):
        """Local run files -> {s3 uri: text}, after the exporter's ETL step."""
        from memtier_etl import generate_memtier_artifacts

        generate_memtier_artifacts(Path(run_dir))
        base = f"s3://{UPLOAD_ENV['S3_BUCKET']}/{UPLOAD_ENV['S3_PREFIX']}{RUN_FOLDER}"
        objects = {}
        for path in Path(run_dir).rglob("*"):
            if path.is_file() and path.suffix in {".txt", ".csv", ".json"}:
                objects[f"{base}/{path.relative_to(run_dir).as_posix()}"] = path.read_text(encoding="utf-8")
        return objects

    def run_uploaded(self, run_dir, *, patches=(), env=None):
        import report_generator

        objects = self._s3_objects(run_dir)

        def fake_read_file_content(uri):
            if uri not in objects:
                raise RuntimeError(f"NoSuchKey: {uri}")
            return objects[uri]

        def fake_read_prefix(prefix_uri, suffixes):
            return sorted(
                (uri, content) for uri, content in objects.items()
                if uri.startswith(prefix_uri) and uri.endswith(tuple(suffixes))
            )

        fake_s3 = FakeS3()
        fake_boto3 = types.ModuleType("boto3")
        fake_boto3.client = mock.Mock(return_value=fake_s3)
        stack = [
            mock.patch.object(report_generator, "read_file_content", fake_read_file_content),
            mock.patch.object(report_generator, "_read_s3_prefix_contents", fake_read_prefix),
            _patch_module("boto3", fake_boto3),
            mock.patch.dict("os.environ", env if env is not None else UPLOAD_ENV, clear=True),
            *patches,
        ]
        for patcher in stack:
            patcher.start()
            self.addCleanup(patcher.stop)
        error = None
        try:
            report_generator.run_uploaded_report()
        except Exception as exc:  # the tests inspect it
            error = exc
        return fake_s3, error

    @property
    def html_key(self):
        return f"{UPLOAD_ENV['S3_PREFIX']}{RUN_FOLDER}/results_{RUN_FOLDER}.html"


class LegacyAwsErrorUploadedGenerationTests(UploadedReportHarness):
    def test_uploaded_html_has_no_diagnostics_and_keeps_expected_data(self):
        run_dir = write_synthetic_run(Path(self.tmp.name) / RUN_FOLDER)

        fake_s3, error = self.run_uploaded(run_dir)

        self.assertIsNone(error)
        self.assertEqual([key for key in fake_s3.put_calls if key.endswith(".html")], [self.html_key])
        document = fake_s3.storage[self.html_key]
        assert_no_leaks(self, document, "uploaded HTML")
        assert_report_keeps_expected_data(self, document)

    def test_env_only_config_without_cluster_details_is_also_clean(self):
        run_dir = write_synthetic_run(Path(self.tmp.name) / RUN_FOLDER, cluster_details=None)
        env = {**UPLOAD_ENV, "ENGINE_TYPE": "valkey", "NODE_TYPE": "cache.t4g.micro", "ENGINE_VERSION": "8.0"}

        fake_s3, error = self.run_uploaded(run_dir, env=env)

        self.assertIsNone(error)
        document = fake_s3.storage[self.html_key]
        assert_no_leaks(self, document, "uploaded HTML")
        self.assertIn("Node type: <span>cache.t4g.micro</span>", document)


# --------------------------------------------------------------------------- #
#  3. Unrelated errors while rendering -> failure, nothing written/uploaded    #
# --------------------------------------------------------------------------- #

def _exploding_renderer(*_args, **_kwargs):
    raise RuntimeError(f"synthetic-boom while rendering for {FAKE_ARN}")


class RenderFailureWritesNothingTests(PandasTempDirTestCase):
    def setUp(self):
        super().setUp()
        self.run_dir = write_synthetic_run(Path(self.tmp.name) / RUN_FOLDER)

    def _generate_expecting_failure(self, output_dir=None):
        import report_generator

        with mock.patch.object(report_generator, "render_html", _exploding_renderer):
            with self.assertLogs("reporter.diagnostics", level="ERROR") as logged:
                with self.assertRaises(report_generator.ReportGenerationError) as raised:
                    report_generator.run_generate_report(str(self.run_dir), {}, output_dir=output_dir)
        return raised.exception, "\n".join(logged.output)

    def test_unrelated_render_error_fails_and_creates_no_report_files(self):
        out = Path(self.tmp.name) / "out"

        error, log_text = self._generate_expecting_failure(output_dir=str(out))

        self.assertFalse((out / "results_local.html").exists())
        self.assertFalse((out / "results_local.json").exists())
        assert_no_leaks(self, str(error), "the exception message")
        # The detail is not lost -- it is in the private diagnostic log.
        self.assertIn("synthetic-boom", log_text)

    def test_unrelated_render_error_keeps_the_previous_valid_report_byte_for_byte(self):
        previous_html = "<!DOCTYPE html><html><body>previous valid report</body></html>"
        previous_json = '{"meta": {"previous": true}}'
        (self.run_dir / "results_local.html").write_text(previous_html, encoding="utf-8")
        (self.run_dir / "results_local.json").write_text(previous_json, encoding="utf-8")

        self._generate_expecting_failure()

        self.assertEqual((self.run_dir / "results_local.html").read_text(encoding="utf-8"), previous_html)
        self.assertEqual((self.run_dir / "results_local.json").read_text(encoding="utf-8"), previous_json)
        leftovers = [p.name for p in self.run_dir.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_html_that_fails_the_publication_check_is_not_written(self):
        """Simulates a future regression: a renderer starts echoing a diagnostic."""
        import report_generator

        def leaky_renderer(**kwargs):
            return f"<html><body><div title='{RAW_AWS_ERROR}'>x</div></body></html>"

        out = Path(self.tmp.name) / "out"
        with mock.patch.object(report_generator, "render_html", leaky_renderer):
            with self.assertRaises(report_generator.ReportGenerationError) as raised:
                report_generator.run_generate_report(str(self.run_dir), {}, output_dir=str(out))

        self.assertFalse((out / "results_local.html").exists())
        self.assertFalse((out / "results_local.json").exists())
        assert_no_leaks(self, str(raised.exception), "the exception message")

    def test_cli_exits_non_zero_when_the_report_cannot_be_produced(self):
        import report_generator

        argv = ["report_generator.py", "generate", str(self.run_dir), "--output-dir", str(Path(self.tmp.name) / "out")]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(report_generator, "render_html", _exploding_renderer), \
                self.assertLogs("reporter.diagnostics", level="ERROR"):
            with self.assertRaises(SystemExit) as raised:
                report_generator.main()

        self.assertEqual(raised.exception.code, 3)
        self.assertFalse((Path(self.tmp.name) / "out" / "results_local.html").exists())


class RenderFailureUploadsNothingTests(UploadedReportHarness):
    def test_unrelated_render_error_uploads_neither_html_nor_json(self):
        import report_generator

        run_dir = write_synthetic_run(Path(self.tmp.name) / RUN_FOLDER)

        with self.assertLogs("reporter.diagnostics", level="ERROR") as logged:
            fake_s3, error = self.run_uploaded(
                run_dir, patches=[mock.patch.object(report_generator, "render_html", _exploding_renderer)]
            )

        self.assertIsInstance(error, report_generator.ReportGenerationError)
        self.assertEqual(fake_s3.put_calls, [])
        assert_no_leaks(self, str(error), "the exception message")
        self.assertIn("synthetic-boom", "\n".join(logged.output))

    def test_html_failing_the_publication_check_is_not_uploaded(self):
        import report_generator

        run_dir = write_synthetic_run(Path(self.tmp.name) / RUN_FOLDER)
        leaky = mock.patch.object(
            report_generator, "render_html",
            lambda **kwargs: f"<html><body><!-- {FAKE_ARN} --></body></html>",
        )

        fake_s3, error = self.run_uploaded(run_dir, patches=[leaky])

        self.assertIsInstance(error, report_generator.ReportGenerationError)
        self.assertEqual(fake_s3.put_calls, [])

    def _exporter_status_after_report_failure(self, failure):
        """Drive exporter.main() with run_uploaded_report raising ``failure``."""
        harness = load_exporter_harness()
        real_load = harness._load_exporter

        def load_failing_exporter():
            exporter = real_load()
            exporter.run_uploaded_report = mock.Mock(side_effect=failure)
            return exporter

        fake_s3 = harness.FakeS3()
        with mock.patch.object(harness, "_load_exporter", load_failing_exporter):
            with self.assertRaises(type(failure)):
                harness._run_main_with_fakes(fake_s3)
        key = f"{harness.ENV['S3_PREFIX']}{harness.ENV['REPORT_TIMESTAMP']}/report_status.json"
        return json.loads(fake_s3.storage[key])

    def test_exporter_marks_the_run_incomplete_when_the_report_cannot_be_produced(self):
        """exporter.main() must not leave report_status.json saying complete."""
        from report_safety import ReportGenerationError

        status = self._exporter_status_after_report_failure(ReportGenerationError("fixed text"))

        self.assertIs(status["complete"], False)
        self.assertEqual(status["report_generation"], {"complete": False, "error_type": "ReportGenerationError"})
        self.assertNotIn("report", status)

    def test_exporter_marks_the_run_incomplete_when_the_report_exits(self):
        """run_uploaded_report's sys.exit(2) paths are report failures too."""
        status = self._exporter_status_after_report_failure(SystemExit(2))

        self.assertIs(status["complete"], False)
        self.assertEqual(status["report_generation"]["error_type"], "SystemExit")


# --------------------------------------------------------------------------- #
#  4. Unexpected input fields                                                  #
# --------------------------------------------------------------------------- #

class UnexpectedInputFieldsTests(PandasTempDirTestCase):
    def test_unknown_and_malformed_cluster_details_fields_are_not_rendered(self):
        details = synthetic_cluster_details(
            debug_stderr=RAW_AWS_ERROR,
            stack_trace=STACK_TRACE,
            node_type=f"cache.t4g.micro {FAKE_ARN}",
            engine_version_configured=RAW_AWS_ERROR,
        )
        details["memtier"]["note"] = STACK_TRACE
        details["memtier"]["ratio"] = RAW_AWS_ERROR
        details["run"]["aws_region"] = FAKE_ARN
        details["run"]["operator_note"] = RAW_AWS_ERROR
        details["unexpected_section"] = {"stderr": RAW_AWS_ERROR}
        run_dir = write_synthetic_run(Path(self.tmp.name) / RUN_FOLDER, cluster_details=details)

        document = generate_local(run_dir, output_dir=Path(self.tmp.name) / "out")

        assert_no_leaks(self, document)
        self.assertIn("Engine: <span>valkey</span>", document)  # valid fields survive
        self.assertNotIn("Node type:", document)  # the malformed one is absent, not echoed
        self.assertNotIn("SET:GET ratio", document)

    def test_metric_rows_with_free_text_labels_are_withheld_but_real_rows_stay(self):
        hostile_rows = [
            f"{_stamp(1)}+00:00,AWS/ElastiCache,Throttled: {FAKE_ARN},Sum,9,Count,CacheClusterId={CLUSTER_ID}-001",
            f"{_stamp(1)}+00:00,AWS/ElastiCache,CmdBlocked,Sum,9,Not authorized: {FAKE_ARN},CacheClusterId={CLUSTER_ID}-001",
            f"{_stamp(1)}+00:00,AWS/ElastiCache,CmdOther,Sum,9,Count,\"CacheClusterId={CLUSTER_ID}-001;Note={FAKE_ARN}\"",
        ]
        run_dir = write_synthetic_run(Path(self.tmp.name) / RUN_FOLDER, extra_metric_rows=hostile_rows)

        document = generate_local(run_dir, output_dir=Path(self.tmp.name) / "out")

        assert_no_leaks(self, document)
        assert_report_keeps_expected_data(self, document)
        self.assertNotIn("CmdBlocked", document)
        self.assertNotIn("CmdOther", document)

    def test_a_label_shaped_diagnostic_in_metric_data_fails_closed(self):
        """Allowlisting is shape-based; the marker scan catches the rest."""
        sneaky = f"{_stamp(1)}+00:00,AWS/ElastiCache,AccessDeniedException,Sum,9,Count,CacheClusterId={CLUSTER_ID}-001"
        run_dir = write_synthetic_run(Path(self.tmp.name) / RUN_FOLDER, extra_metric_rows=[sneaky])
        out = Path(self.tmp.name) / "out"

        from report_generator import ReportGenerationError

        with self.assertRaises(ReportGenerationError):
            generate_local(run_dir, output_dir=out)
        self.assertFalse((out / "results_local.html").exists())


# --------------------------------------------------------------------------- #
#  Comparison and aggregate reports                                            #
# --------------------------------------------------------------------------- #

def _write_compare_run(root, name, *, avg_ops, ratio="1:10", poison=True):
    """A run folder holding only what compare/aggregate read: JSON files."""
    from report_common import GENERATOR_SCHEMA_VERSION

    run_dir = Path(root) / name
    run_dir.mkdir(parents=True)
    meta = {
        "cluster_id": CLUSTER_ID,
        "time_range": "2026-05-01 00:00:00 UTC - 2026-05-01 01:00:00 UTC",
        "report_start": "2026-05-01T00:00:00",
        "report_end": "2026-05-01T01:00:00",
        "engine_type": "valkey", "engine_version": "8.0", "node_type": "cache.t4g.micro",
        "node_count": "1", "cluster_mode": "false", "node_hourly_usd": "0.016",
        "generator_schema_version": GENERATOR_SCHEMA_VERSION,
        "source_mode": "local", "memtier_window_source": "memtier_log_messages",
        "artifact_source": "generated",
    }
    summary = {
        "meta": meta,
        "benchmark": {"avg_ops": avg_ops, "avg_latency_ms": 1.5},
        "cache_efficiency": {"total_ops": 3_600_000, "avg_hit_rate_pct": 91.5},
        "oom": {"rejection_count": 4},
        "errors": {"error_count_total": 5},
        "loadgen": {"diagnostic_status": "ok"},
    }
    details = synthetic_cluster_details()
    details["memtier"]["ratio"] = ratio
    if poison:
        meta["node_hourly_usd_reason"] = RAW_AWS_ERROR
        meta["notes"] = STACK_TRACE
        summary["benchmark"]["p99_latency_ms"] = RAW_AWS_ERROR  # free text where a number belongs
        summary["loadgen"] = {"diagnostic_status": "warning", "warning_reasons": [FAKE_ARN]}
        details["memtier"]["note"] = STACK_TRACE
    (run_dir / "results_local.json").write_text(json.dumps(summary), encoding="utf-8")
    (run_dir / "cluster_details.json").write_text(json.dumps(details), encoding="utf-8")
    return run_dir


class ComparisonReportTests(PandasTempDirTestCase):
    def setUp(self):
        super().setUp()
        self.baseline = _write_compare_run(self.tmp.name, "20260501-000000-aa", avg_ops=1000)
        # A free-text control variable that differs: it would be printed in the
        # comparability contract as "baseline vs candidate".
        self.candidate = _write_compare_run(self.tmp.name, "20260501-010000-ab", avg_ops=1200, ratio=RAW_AWS_ERROR)
        self.output = Path(self.tmp.name) / "comparisons" / "cmp.html"

    def _compare(self):
        from report_compare import run_compare_report

        run_compare_report(str(self.baseline), str(self.candidate), str(self.output))
        return self.output.read_text(encoding="utf-8")

    def test_comparison_with_legacy_raw_errors_everywhere_is_clean_and_complete(self):
        # A status file that cannot be read: its parse error must stay in the log.
        (self.baseline / "report_status.json").write_text("{not json", encoding="utf-8")
        import report_common

        real_load_json = report_common.load_json

        def load_json(path):
            if Path(path).name == "report_status.json":
                raise OSError(f"cannot read {path}: {RAW_AWS_ERROR}")
            return real_load_json(path)

        with mock.patch.object(report_common, "load_json", load_json):
            document = self._compare()

        assert_no_leaks(self, document)
        # Expected data survives, with the absolute timestamps.
        self.assertIn("Avg Throughput", document)
        self.assertIn("1,000 ops/sec", document)
        self.assertIn("1,200 ops/sec", document)
        self.assertIn("OOM Rejections", document)
        self.assertIn("2026-05-01T00:00:00", document)
        self.assertIn("2026-05-01 00:00:00 UTC - 2026-05-01 01:00:00 UTC", document)
        self.assertIn("report_status.json unreadable.", document)
        self.assertIn("withheld", document)  # the free-text control variable
        self.assertIn("other ECS task constraint", document)

    def test_comparison_shows_file_names_not_local_paths(self):
        document = self._compare()

        self.assertIn("results_local.json", document)
        self.assertNotIn(self.tmp.name, document)
        self.assertNotIn(str(self.baseline), document)

    def test_non_numeric_metric_values_render_as_unavailable(self):
        document = self._compare()

        # benchmark.p99_latency_ms held free text in both runs.
        row = re.search(r"P99 Latency.*?</tr>", document, re.S).group(0)
        self.assertEqual(row.count("n/a"), 3)
        assert_no_leaks(self, row)

    def test_render_failure_leaves_an_existing_comparison_untouched(self):
        import report_compare

        self.output.parent.mkdir(parents=True)
        self.output.write_text("<html>previous valid comparison</html>", encoding="utf-8")

        with mock.patch.object(report_compare, "render_report", _exploding_renderer):
            with self.assertLogs("reporter.diagnostics", level="ERROR"):
                with self.assertRaises(report_compare.ReportGenerationError) as raised:
                    report_compare.run_compare_report(str(self.baseline), str(self.candidate), str(self.output))

        self.assertEqual(self.output.read_text(encoding="utf-8"), "<html>previous valid comparison</html>")
        assert_no_leaks(self, str(raised.exception), "the exception message")

    def test_render_failure_creates_no_comparison_file(self):
        import report_compare

        with mock.patch.object(report_compare, "render_report", _exploding_renderer):
            with self.assertLogs("reporter.diagnostics", level="ERROR"):
                with self.assertRaises(report_compare.ReportGenerationError):
                    report_compare.run_compare_report(str(self.baseline), str(self.candidate), str(self.output))

        self.assertFalse(self.output.exists())


class AggregateReportTests(PandasTempDirTestCase):
    def setUp(self):
        super().setUp()
        self.runs = [
            str(_write_compare_run(self.tmp.name, "20260501-000000-aa", avg_ops=1000)),
            str(_write_compare_run(self.tmp.name, "20260501-010000-ab", avg_ops=1100)),
        ]
        self.out = Path(self.tmp.name) / "aggregates"

    def test_aggregate_with_legacy_raw_errors_is_clean_and_keeps_error_adjusted_cost(self):
        from aggregator import run_aggregate_report

        run_aggregate_report(self.runs, str(self.out))

        (document_path,) = list(self.out.glob("*.html"))
        document = document_path.read_text(encoding="utf-8")
        assert_no_leaks(self, document)
        self.assertIn("Avg Throughput", document)
        self.assertIn("Cost per successful operation", document)
        self.assertNotIn("errors_unknown", document)
        # (3,600,000 ops - 5 errors) over 3,600 s at $0.016/h: the benchmark
        # error count is used for the figure, not dropped. The HTML rounds to
        # 6 decimals, so check the exact value in the JSON written beside it.
        (json_path,) = list(self.out.glob("*.json"))
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        for row in payload["cost_per_successful_operation"]["per_run"]:
            self.assertTrue(math.isclose(row["value"], 0.016 / 3_599_995, rel_tol=1e-9), row)
            self.assertFalse(math.isclose(row["value"], 0.016 / 3_600_000, rel_tol=1e-9), row)
            self.assertFalse(row["errors_unknown"])

    def test_aggregate_flags_unknown_error_counts_without_inventing_zero(self):
        from aggregator import run_aggregate_report

        for run in self.runs:
            path = Path(run) / "results_local.json"
            summary = json.loads(path.read_text(encoding="utf-8"))
            summary["errors"] = {}
            path.write_text(json.dumps(summary), encoding="utf-8")

        run_aggregate_report(self.runs, str(self.out))

        (document_path,) = list(self.out.glob("*.html"))
        self.assertIn("(errors_unknown)", document_path.read_text(encoding="utf-8"))

    def test_aggregate_render_failure_writes_nothing(self):
        import template
        from aggregator import run_aggregate_report
        from report_safety import ReportGenerationError

        with mock.patch.object(template, "render_aggregate_report", _exploding_renderer):
            with self.assertLogs("reporter.diagnostics", level="ERROR"):
                with self.assertRaises(ReportGenerationError):
                    run_aggregate_report(self.runs, str(self.out))

        self.assertEqual(list(self.out.glob("*")), [])


# --------------------------------------------------------------------------- #
#  The module itself                                                           #
# --------------------------------------------------------------------------- #

class FieldContractTests(unittest.TestCase):
    def test_run_config_carries_no_price_reason_and_no_unknown_keys(self):
        from report_safety import public_run_config

        config = public_run_config({
            "engine_type": "Valkey", "engine_version": "8.0", "node_type": "cache.t4g.micro",
            "node_count": "3", "cluster_mode": "true", "node_hourly_usd": "0.016",
            "node_memory_bytes": "6850472837", "node_hourly_usd_source": "unavailable",
            "node_hourly_usd_reason": RAW_AWS_ERROR, "stderr": RAW_AWS_ERROR, "git_sha": "abc",
        })

        self.assertEqual(config, {
            "engine_type": "valkey", "engine_version": "8.0", "node_type": "cache.t4g.micro",
            "node_count": "3", "cluster_mode": "true", "node_hourly_usd": "0.016",
            "node_memory_bytes": "6850472837", "node_hourly_usd_source": "unavailable",
        })

    def test_run_config_drops_values_that_do_not_match_their_contract(self):
        from report_safety import public_run_config

        config = public_run_config({
            "engine_type": FAKE_ARN, "engine_version": RAW_AWS_ERROR, "node_type": "cache.<script>",
            "node_count": "-3", "cluster_mode": "maybe", "node_hourly_usd": "free",
            "node_memory_bytes": float("nan"),
        })

        self.assertEqual(config, {})

    def test_unknown_price_source_is_reported_as_unavailable_not_echoed(self):
        from report_safety import public_run_config

        config = public_run_config({"node_hourly_usd_source": RAW_AWS_ERROR})

        self.assertEqual(config, {"node_hourly_usd_source": "unavailable"})

    def test_cluster_details_view_keeps_only_workload_settings(self):
        from report_safety import public_cluster_details

        view = public_cluster_details({
            "run": {"aws_region": "eu-west-1", "operator_note": RAW_AWS_ERROR},
            "memtier": {"task_count": "4", "ratio": "1:10", "threads": 2, "clients": RAW_AWS_ERROR,
                        "data_size_bytes": 4096, "note": STACK_TRACE, "pipeline": True},
            "elasticache": {"node_hourly_usd_reason": RAW_AWS_ERROR},
        })

        self.assertEqual(view, {
            "run": {"aws_region": "eu-west-1"},
            "memtier": {"task_count": 4, "ratio": "1:10", "threads": 2, "data_size_bytes": 4096},
        })

    def test_loadgen_view_drops_rows_with_malformed_task_ids_and_blanks_bad_zones(self):
        from report_safety import public_loadgen_view

        view = public_loadgen_view({
            "generator_cpu_p95_pct": 41.5,
            "elasticache_availability_zone": FAKE_ARN,
            "generator_cpu_p95_by_task": [
                {"task_id": "3313bd2138b5450fb05ac921c10ba5cd", "task_index": 1, "availability_zone": "us-east-1e", "p95_pct": 40.0},
                {"task_id": FAKE_ARN, "task_index": 2, "availability_zone": "us-east-1e", "p95_pct": 41.0},
                {"task_id": "abc", "task_index": 3, "availability_zone": RAW_AWS_ERROR, "p95_pct": 42.0},
            ],
            "throughput_by_az": [{"availability_zone": STACK_TRACE, "task_count": 2, "median_ops_sec": 10, "p90_to_p10": 1.1}],
            "unexpected": RAW_AWS_ERROR,
        })

        self.assertEqual([row["task_id"] for row in view["generator_cpu_p95_by_task"]],
                         ["3313bd2138b5450fb05ac921c10ba5cd", "abc"])
        self.assertEqual(view["generator_cpu_p95_by_task"][1]["availability_zone"], "unknown")
        self.assertEqual(view["throughput_by_az"][0]["availability_zone"], "unknown")
        # Dropped, not "unknown": that would match the unknown-AZ task bucket.
        self.assertIsNone(view["elasticache_availability_zone"])
        self.assertNotIn("unexpected", view)
        self.assertEqual(view["generator_cpu_p95_pct"], 41.5)

    def test_metrics_frame_keeps_real_labels_including_error_count(self):
        try:
            import pandas as pd
        except ModuleNotFoundError as exc:
            self.skipTest(f"{exc.name} is not installed in this environment")
        from report_safety import ReportGenerationError, sanitize_metrics_frame

        base = {"Timestamp": pd.Timestamp("2026-05-01"), "Namespace": "AWS/ElastiCache", "Stat": "Sum",
                "Value": 1.0, "Unit": "Count", "Dimensions": "CacheClusterId=a-001"}
        frame = pd.DataFrame([
            {**base, "MetricName": "ErrorCount"},
            {**base, "MetricName": f"Bad {FAKE_ARN}"},
            {**base, "MetricName": "Ok", "Unit": STACK_TRACE},
            {**base, "MetricName": "Ok", "Dimensions": f"CacheClusterId=a-001;Note={RAW_AWS_ERROR}"},
        ])

        with self.assertLogs("reporter.diagnostics", level="WARNING"):
            cleaned = sanitize_metrics_frame(frame, "test metrics")

        self.assertEqual(cleaned["MetricName"].tolist(), ["ErrorCount"])
        with self.assertLogs("reporter.diagnostics", level="WARNING"):
            with self.assertRaises(ReportGenerationError):
                sanitize_metrics_frame(frame.iloc[[1, 2, 3]], "test metrics")


class StoredReasonsAreFixedCodesTests(unittest.TestCase):
    """Where stored JSON (cluster_details.json, results_*.json,
    report_status.json) records why something failed, it holds a fixed code,
    not tool output. Other free text in a summary is caught by
    assert_publishable_json before it is written or uploaded."""

    def test_price_script_only_ever_emits_literal_reason_codes(self):
        from report_safety import PRICE_REASON_CODES

        script = (ROOT / "scripts" / "fetch_elasticache_price.sh").read_text(encoding="utf-8")
        emitted = set(re.findall(r'_unavailable "([^"]*)"', script))
        emitted |= set(re.findall(r'"reason":"([^"]*)"', script))

        self.assertTrue(emitted)
        self.assertEqual(emitted - PRICE_REASON_CODES, set(), "script emits a reason that is not a fixed code")
        # No reason is built from command output or interpolated input.
        self.assertNotIn("REASON=", script)
        self.assertNotRegex(script, r'_unavailable "[^"]*[$`]')

    def test_a_failed_aws_call_yields_a_fixed_reason_and_no_arn_on_stdout(self):
        import os
        import shutil
        import subprocess

        if not shutil.which("jq") or not shutil.which("bash"):
            self.skipTest("jq and bash are required to run the price script")
        with tempfile.TemporaryDirectory() as bin_dir:
            stub = Path(bin_dir) / "aws"
            stub.write_text(
                "#!/usr/bin/env bash\n"
                f"echo 'An error occurred (AccessDeniedException) when calling GetProducts: User: {FAKE_ARN}' >&2\n"
                "exit 254\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)
            env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
            completed = subprocess.run(
                ["bash", str(ROOT / "scripts" / "fetch_elasticache_price.sh")],
                input=json.dumps({"node_type": "cache.t4g.micro", "engine_type": "valkey", "aws_region": "us-east-1"}),
                env=env, text=True, capture_output=True, check=True,
            )

        result = json.loads(completed.stdout)
        self.assertEqual(result, {"hourly_usd": "", "source": "unavailable", "reason": "pricing_request_failed"})
        assert_no_leaks(self, completed.stdout, "script stdout")

    def test_legacy_free_text_reason_is_reduced_when_summaries_are_built_or_enriched(self):
        from report_common import enrich_summary_meta
        from report_safety import public_price_reason

        summary = {"meta": {"node_hourly_usd_reason": RAW_AWS_ERROR}}
        enrich_summary_meta(summary, synthetic_cluster_details())

        self.assertEqual(summary["meta"]["node_hourly_usd_reason"], "unspecified")
        self.assertEqual(public_price_reason("pricing_request_failed"), "pricing_request_failed")
        self.assertEqual(public_price_reason(""), "")
        assert_no_leaks(self, json.dumps(summary))

    def test_generated_summary_json_does_not_carry_the_legacy_reason(self):
        skip_without_pandas(self)
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = write_synthetic_run(Path(tmp) / RUN_FOLDER)
            generate_local(run_dir, output_dir=Path(tmp) / "out")
            raw = (Path(tmp) / "out" / "results_local.json").read_text(encoding="utf-8")

        assert_no_leaks(self, raw, "results_local.json")
        self.assertEqual(json.loads(raw)["meta"]["node_hourly_usd_reason"], "unspecified")

    def test_metric_export_errors_are_stored_as_codes_not_messages(self):
        exporter = load_exporter_harness()._load_exporter()

        class ClientErrorLike(Exception):
            response = {"Error": {"Code": "AccessDenied", "Message": f"User: {FAKE_ARN} is not authorized"}}

        self.assertEqual(exporter._error_code(ClientErrorLike(FAKE_ARN)), "AccessDenied")
        self.assertEqual(exporter._error_code(RuntimeError(FAKE_ARN)), "RuntimeError")
        self.assertEqual(exporter._error_code(Exception("Throttling: rate exceeded")), "Exception")


class PublicationCheckTests(unittest.TestCase):
    PAGE = "<!DOCTYPE html><html><body>{}</body></html>"

    def test_each_diagnostic_shape_is_rejected_without_echoing_it(self):
        from report_safety import ReportGenerationError, assert_publishable_html

        samples = {
            "aws-arn": f"<div title='{FAKE_ARN}'>x</div>",
            "aws-error-banner": "<p>An error occurred (Throttling) when calling</p>",
            "aws-error-code": "<p>AccessDeniedException</p>",
            "aws-not-authorized": "<p>User is not authorized to perform: x</p>",
            "aws-identity": "<p>assumed-role/SyntheticRole/x</p>",
            "aws-credentials": "<p>AKIAABCDEFGHIJKLMNOP</p>",
            "python-traceback": f"<pre>{STACK_TRACE}</pre>",
            "python-frame": '<pre>File "x.py", line 3</pre>',
            "exception-line": "<p>KeyError: 'x'</p>",
            "process-output": "<p>stderr: boom</p>",
            "local-path": "<p>C:\\Users\\someone\\file</p>",
            "html-comment": "<!-- anything -->",
        }
        for marker, fragment in samples.items():
            with self.subTest(marker=marker):
                with self.assertLogs("reporter.diagnostics", level="ERROR"):
                    with self.assertRaises(ReportGenerationError) as raised:
                        assert_publishable_html(self.PAGE.format(fragment))
                self.assertIn(marker, str(raised.exception))
                for secret in ("SyntheticRole", "someone", "boom", "AKIAABCDEFGHIJKLMNOP"):
                    self.assertNotIn(secret, str(raised.exception))

    def test_markers_are_found_in_attributes_scripts_and_chart_data_not_just_text(self):
        from report_safety import ReportGenerationError, assert_publishable_html

        for fragment in (
            f"<span title=\"{RAW_AWS_ERROR}\">ok</span>",
            f"<script>Plotly.newPlot('x', [{{name: '{FAKE_ARN}'}}])</script>",
            f"<div data-note='{FAKE_ARN}'></div>",
        ):
            with self.subTest(fragment=fragment[:40]):
                with self.assertLogs("reporter.diagnostics", level="ERROR"):
                    with self.assertRaises(ReportGenerationError):
                        assert_publishable_html(self.PAGE.format(fragment))

    def test_ordinary_report_vocabulary_is_not_mistaken_for_a_diagnostic(self):
        from report_safety import assert_publishable_html

        page = self.PAGE.format(
            "<h2>ErrorCount</h2><div>Errors: 0</div><div>error count 7</div>"
            "<div class='card-label'>OOM Rejections</div><p>-OOM command not allowed</p>"
            "<span title='Count of memtier log events containing -OOM command not allowed.'>x</span>"
            "<p>Benchmark errors: 3</p><p>ECS Service CPU</p><p>2026-05-01 00:00:00 UTC</p>"
        )

        self.assertEqual(assert_publishable_html(page), page)

    def test_incomplete_or_non_text_documents_are_rejected(self):
        from report_safety import ReportGenerationError, assert_publishable_html

        for document in ("", "<html><body>cut off", None, b"<html></html>"):
            with self.subTest(document=document):
                with self.assertRaises(ReportGenerationError):
                    assert_publishable_html(document)


class LeakAndRegressionFixTests(PandasTempDirTestCase):
    """Defects found in review of the publication boundary."""

    def test_free_text_in_the_memtier_minute_series_never_reaches_chart_data(self):
        import report_generator

        secret = "lookup denied by policy for account 980101524250"
        real_load = report_generator._load_memtier_artifacts

        def poisoned_load(entries):
            minute_df, totals_df = real_load(entries)
            minute_df = minute_df.astype({column: object for column in minute_df.columns if column != "Timestamp"})
            for column in minute_df.columns:
                if column != "Timestamp":
                    minute_df.loc[0, column] = secret
            return minute_df, totals_df

        run_dir = write_synthetic_run(Path(self.tmp.name) / RUN_FOLDER)
        with mock.patch.object(report_generator, "_load_memtier_artifacts", poisoned_load):
            document = generate_local(run_dir, output_dir=Path(self.tmp.name) / "out")

        self.assertNotIn("980101524250", document)
        self.assertIn("<div class='card-label'>Avg Throughput</div>", document)

    def test_withheld_metric_labels_do_not_change_summary_numbers(self):
        hostile_peak = f"{_stamp(1)}+00:00,AWS/ElastiCache,EngineCPUUtilization,Average,95,Not authorized,CacheClusterId={CLUSTER_ID}-001"
        clean_dir = write_synthetic_run(Path(self.tmp.name) / "clean" / RUN_FOLDER)
        hostile_dir = write_synthetic_run(Path(self.tmp.name) / "hostile" / RUN_FOLDER, extra_metric_rows=[hostile_peak])

        generate_local(clean_dir, output_dir=Path(self.tmp.name) / "clean-out")
        document = generate_local(hostile_dir, output_dir=Path(self.tmp.name) / "hostile-out")

        clean = json.loads((Path(self.tmp.name) / "clean-out" / "results_local.json").read_text(encoding="utf-8"))
        hostile = json.loads((Path(self.tmp.name) / "hostile-out" / "results_local.json").read_text(encoding="utf-8"))
        # The row's label is withheld from the page, but its number still counts.
        self.assertNotIn("Not authorized", document)
        self.assertEqual(hostile["engine_cpu"]["max_pct"], 95)
        self.assertNotEqual(clean["engine_cpu"]["max_pct"], 95)

    def test_a_run_folder_outside_the_label_contract_is_withheld_not_fatal(self):
        details = synthetic_cluster_details()
        del details["run"]["run_folder"]
        run_dir = write_synthetic_run(Path(self.tmp.name) / "run#2", cluster_details=details)

        document = generate_local(run_dir, output_dir=Path(self.tmp.name) / "out")

        self.assertNotIn("run#2", document)
        self.assertIn("withheld", document)

    def test_a_duplicate_metrics_file_does_not_replace_the_cluster_id(self):
        run_dir = write_synthetic_run(Path(self.tmp.name) / RUN_FOLDER)
        original = run_dir / "metrics" / f"{CLUSTER_ID}.csv"
        (run_dir / "metrics" / f"{CLUSTER_ID} (1).csv").write_text(original.read_text(encoding="utf-8"), encoding="utf-8")

        document = generate_local(run_dir, output_dir=Path(self.tmp.name) / "out")

        self.assertIn(CLUSTER_ID, document)

    def test_aggregate_label_and_folders_come_from_field_contracts(self):
        from aggregator import run_aggregate_report

        secret = "lookup denied by policy for account 980101524250"
        runs = [
            _write_compare_run(self.tmp.name, "20260501-000000-aa", avg_ops=1000),
            _write_compare_run(self.tmp.name, "run#2", avg_ops=1100),
        ]
        for run in runs:
            path = run / "results_local.json"
            summary = json.loads(path.read_text(encoding="utf-8"))
            summary["meta"]["engine_version"] = secret
            path.write_text(json.dumps(summary), encoding="utf-8")
        out = Path(self.tmp.name) / "aggregates"

        run_aggregate_report([str(run) for run in runs], str(out))

        (document_path,) = list(out.glob("*.html"))
        (json_path,) = list(out.glob("*.json"))
        for text in (document_path.name, document_path.read_text(encoding="utf-8"), json_path.read_text(encoding="utf-8")):
            self.assertNotIn("980101524250", text)
            self.assertNotIn("run#2", text)
        self.assertIn("cache.t4g.micro-valkey-withheld", document_path.read_text(encoding="utf-8"))

    def test_aggregate_cost_uses_the_price_contract(self):
        from aggregator import cost_per_successful_operation
        from report_common import load_run

        run_dir = _write_compare_run(self.tmp.name, "20260501-000000-aa", avg_ops=1000)
        run = load_run("Baseline", str(run_dir))
        for price in ("nan", "inf", "0", "-1"):
            with self.subTest(price=price):
                run.summary["meta"]["node_hourly_usd"] = price
                cost = cost_per_successful_operation(run)
                self.assertIsNone(cost["value"])
                self.assertEqual(cost["reason"], "node_hourly_usd_unavailable")

    def test_task_count_mismatch_reason_keeps_its_meaning_in_comparisons(self):
        from report_compare import collect_takeaways
        from report_common import load_run

        baseline = load_run("Baseline", str(_write_compare_run(self.tmp.name, "20260501-000000-aa", avg_ops=1000, poison=False)))
        candidate = load_run("Candidate", str(_write_compare_run(self.tmp.name, "20260501-010000-ab", avg_ops=1100, poison=False)))
        candidate.summary["loadgen"] = {"diagnostic_status": "warning", "warning_reasons": ["task_count_mismatch_with_request"]}

        text = json.dumps(collect_takeaways(baseline, candidate))

        self.assertIn("observed ECS task count differs from the request", text)
        self.assertNotIn("other ECS task constraint", text)


class RemainingFindingsTests(PandasTempDirTestCase):
    """The second round of review fixes."""

    def test_a_non_numeric_value_row_is_withheld_not_fatal(self):
        bad_value = f"{_stamp(1)}+00:00,AWS/ElastiCache,StringBasedCmdsLatency,Average,An error occurred,Microseconds,CacheClusterId={CLUSTER_ID}-001"
        run_dir = write_synthetic_run(Path(self.tmp.name) / RUN_FOLDER, extra_metric_rows=[bad_value])

        with self.assertLogs("reporter.diagnostics", level="WARNING"):
            document = generate_local(run_dir, output_dir=Path(self.tmp.name) / "out")

        assert_no_leaks(self, document)
        assert_report_keeps_expected_data(self, document)

    def test_free_text_summary_fields_are_withheld_by_shape(self):
        from report_generator import run_generate_report

        out = Path(self.tmp.name) / "out"
        run_dir = write_synthetic_run(Path(self.tmp.name) / RUN_FOLDER)

        run_generate_report(str(run_dir), {"git_sha": RAW_AWS_ERROR, "loadgen_image": FAKE_ARN}, output_dir=str(out))

        raw = (out / "results_local.json").read_text(encoding="utf-8")
        assert_no_leaks(self, raw, "results_local.json")
        meta = json.loads(raw)["meta"]
        self.assertEqual((meta["git_sha"], meta["loadgen_image"]), ("withheld", "withheld"))

    def test_free_text_elsewhere_in_the_summary_json_fails_closed(self):
        from report_generator import ReportGenerationError, run_generate_report

        out = Path(self.tmp.name) / "out"
        run_dir = write_synthetic_run(Path(self.tmp.name) / RUN_FOLDER)

        with self.assertLogs("reporter.diagnostics", level="ERROR"):
            with self.assertRaises(ReportGenerationError):
                run_generate_report(str(run_dir), {"node_hourly_usd": RAW_AWS_ERROR}, output_dir=str(out))
        self.assertFalse((out / "results_local.json").exists())
        self.assertFalse((out / "results_local.html").exists())

    def test_null_cluster_details_sections_do_not_crash_a_comparison(self):
        from report_compare import run_compare_report

        runs = []
        for name, avg_ops in (("20260501-000000-aa", 1000), ("20260501-010000-ab", 1100)):
            run = _write_compare_run(self.tmp.name, name, avg_ops=avg_ops, poison=False)
            details = json.loads((run / "cluster_details.json").read_text(encoding="utf-8"))
            details.update({"reporter": None, "elasticache": None, "ecs": [1]})
            (run / "cluster_details.json").write_text(json.dumps(details), encoding="utf-8")
            runs.append(run)
        output = Path(self.tmp.name) / "cmp.html"

        run_compare_report(str(runs[0]), str(runs[1]), str(output))

        self.assertIn("Avg Throughput", output.read_text(encoding="utf-8"))

    def test_takeaways_show_missing_values_as_unavailable_not_zero(self):
        from report_compare import collect_takeaways
        from report_common import load_run

        baseline = load_run("Baseline", str(_write_compare_run(self.tmp.name, "20260501-000000-aa", avg_ops=100, poison=False)))
        candidate = load_run("Candidate", str(_write_compare_run(self.tmp.name, "20260501-010000-ab", avg_ops=100, poison=False)))
        del candidate.summary["benchmark"]["avg_ops"]

        tradeoff = collect_takeaways(baseline, candidate)[0]

        self.assertIn("100 -> n/a ops/sec", tradeoff["text"])
        self.assertNotIn("-> 0 ops/sec", tradeoff["text"])
        self.assertNotEqual(tradeoff["tone"], "worse")

    def test_summary_is_uploaded_before_the_page(self):
        harness = UploadedReportHarness("run_uploaded")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        run_dir = write_synthetic_run(Path(harness.tmp.name) / RUN_FOLDER)

        fake_s3, error = harness.run_uploaded(run_dir)

        self.assertIsNone(error)
        self.assertEqual([key.rsplit(".", 1)[1] for key in fake_s3.put_calls], ["json", "html"])


class ExporterFailureNotificationTests(unittest.TestCase):
    def test_a_report_failure_sends_a_fixed_text_email_and_exits_3(self):
        from report_safety import ReportGenerationError

        harness = load_exporter_harness()
        sent = []
        real_load = harness._load_exporter

        def load_failing_exporter():
            exporter = real_load()
            exporter.run_uploaded_report = mock.Mock(side_effect=ReportGenerationError("fixed text"))
            exporter.send_report_failed_email = lambda *args: sent.append(args)
            return exporter

        with mock.patch.object(harness, "_load_exporter", load_failing_exporter):
            with self.assertRaises(ReportGenerationError):
                harness._run_main_with_fakes(harness.FakeS3())
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][-1], "ReportGenerationError")

        exporter = real_load()
        exporter.main = mock.Mock(side_effect=ReportGenerationError("fixed text"))
        with self.assertLogs("reporter.diagnostics", level="ERROR"):
            with self.assertRaises(SystemExit) as exited:
                exporter._run_main()
        self.assertEqual(exited.exception.code, 3)


class ContractEdgeCaseTests(unittest.TestCase):
    def test_version_strings_stay_text_in_comparison_reasons(self):
        from report_safety import WITHHELD, public_scalar

        self.assertEqual(public_scalar("7.10"), "7.10")
        self.assertEqual(public_scalar("8.0"), "8.0")
        self.assertEqual(public_scalar(4), 4)
        self.assertEqual(public_scalar(True), True)
        self.assertEqual(public_scalar(RAW_AWS_ERROR), WITHHELD)

    def test_engine_version_x_form_and_memory_bound(self):
        from report_safety import public_run_config

        config = public_run_config({"engine_version": "6.x", "node_memory_bytes": "1" * 400})

        self.assertEqual(config.get("engine_version"), "6.x")
        self.assertNotIn("node_memory_bytes", config)

    def test_numeric_text_is_formatted_like_a_number(self):
        from report_common import MetricSpec, bytes_to_mb, display_value

        plain = MetricSpec("benchmark", "Value", ("x",), unit="ops/sec", decimals=0)
        swap = MetricSpec("memory", "Swap", ("x",), unit="MB", decimals=0, normalizer=bytes_to_mb)

        self.assertEqual(display_value(plain, "1.2e+06"), "1,200,000 ops/sec")
        self.assertEqual(display_value(plain, "1,234"), "1,234 ops/sec")
        self.assertEqual(display_value(swap, "1048576"), display_value(swap, 1048576))
        self.assertEqual(display_value(plain, float("inf")), plain.none_label)
        self.assertEqual(display_value(plain, "fast"), plain.none_label)

    def test_marker_gaps_are_closed(self):
        from report_safety import find_diagnostic_markers

        for fragment in ("Error: connection refused", "Exception: boom", r"C:\work\repo\x",
                         "/tmp/x", "/opt/app", "/c/Users/alice/x", "/home/x"):
            with self.subTest(fragment=fragment):
                with self.assertLogs("reporter.diagnostics", level="ERROR"):
                    self.assertNotEqual(find_diagnostic_markers(f"<p>{fragment}</p>"), [])
        for fragment in ("https://cdn.plot.ly/plotly-2.35.2.min.js", "https://example.com/opt/app", "ErrorCount", "Errors"):
            with self.subTest(fragment=fragment):
                self.assertEqual(find_diagnostic_markers(f"<p>{fragment}</p>"), [])

    def test_a_colon_arn_metric_name_is_withheld_not_kept(self):
        try:
            import pandas as pd
        except ModuleNotFoundError:
            self.skipTest("pandas is not installed in this environment")
        from report_safety import sanitize_metrics_frame

        frame = pd.DataFrame([
            {"Namespace": "AWS/ElastiCache", "MetricName": "CacheHits", "Stat": "Sum", "Unit": "Count", "Dimensions": "CacheClusterId=a"},
            {"Namespace": "AWS/ElastiCache", "MetricName": "arn:aws:elasticache:us-east-1:000000000000:cluster:x",
             "Stat": "Sum", "Unit": "Count", "Dimensions": "CacheClusterId=a"},
        ])
        with self.assertLogs("reporter.diagnostics", level="WARNING"):
            kept = sanitize_metrics_frame(frame, "metrics")
        self.assertEqual(list(kept["MetricName"]), ["CacheHits"])


class ViewAndScanRegressionTests(unittest.TestCase):
    def test_an_empty_loadgen_summary_renders_no_quality_section(self):
        from cards import loadgen_quality_html
        from report_safety import public_loadgen_view

        self.assertEqual(public_loadgen_view({}), {})
        self.assertEqual(loadgen_quality_html(public_loadgen_view({})), "")

    def test_a_malformed_cache_az_is_not_matched_to_unknown_tasks(self):
        from cards import loadgen_quality_html
        from report_safety import public_loadgen_view

        view = public_loadgen_view({
            "elasticache_availability_zone": "use1-az6",
            "throughput_by_az": [{"availability_zone": None, "task_count": 2, "median_ops_sec": 10}],
        })

        self.assertNotIn("ElastiCache AZ", loadgen_quality_html(view))

    def test_missing_az_values_show_a_fixed_status_not_zero_or_none(self):
        from cards import loadgen_quality_html
        from report_safety import public_loadgen_view

        view = public_loadgen_view({
            "throughput_by_az": [{"availability_zone": "us-east-1a", "task_count": "x y", "median_ops_sec": float("nan")}],
        })
        row = re.search(r"<tr><td>us-east-1a</td>.*?</tr>", loadgen_quality_html(view)).group(0)

        self.assertNotIn("None", row)
        self.assertNotIn("<td>0</td>", row)
        self.assertEqual(row.count("n/a"), 3)

    def test_real_ecs_stream_dimensions_are_kept_and_an_optional_frame_may_end_empty(self):
        try:
            import pandas as pd
        except ModuleNotFoundError:
            self.skipTest("pandas is not installed in this environment")
        from report_safety import sanitize_metrics_frame

        frame = pd.DataFrame([{
            "Namespace": "ECS/Memtier", "MetricName": "ClientLatency", "Stat": "p99", "Unit": "Milliseconds",
            "Dimensions": "Stream=memtier/memtier/627f2842e42a48b294f7e9a55d4f015f",
        }])
        self.assertEqual(len(sanitize_metrics_frame(frame, "ECS metrics")), 1)

        hostile = frame.assign(Unit="Not authorized")
        with self.assertLogs("reporter.diagnostics", level="WARNING"):
            self.assertTrue(sanitize_metrics_frame(hostile, "ECS metrics", required=False).empty)

    def test_markers_are_found_inside_real_plotly_output(self):
        try:
            import plotly.graph_objects as go
        except ModuleNotFoundError:
            self.skipTest("plotly is not installed in this environment")
        from report_safety import find_diagnostic_markers

        for name in ("/home/alice/secret/", "assumed-role/x", "<!-- x -->", 'File "/a.py", line 3'):
            with self.subTest(name=name):
                fragment = go.Figure(go.Scatter(x=[1], y=[1], name=name)).to_html(full_html=False, include_plotlyjs=False)
                with self.assertLogs("reporter.diagnostics", level="ERROR") as logs:
                    self.assertNotEqual(find_diagnostic_markers(fragment), [])
                self.assertNotIn(name, "\n".join(logs.output))  # names and offsets only

    def test_names_that_merely_contain_marker_words_are_not_diagnostics(self):
        from report_safety import find_diagnostic_markers

        for name in ("fix-stdout", "stdout-cluster-valkey-1", "asiapacificloadtest2-valkey-12345678"):
            with self.subTest(name=name):
                self.assertEqual(find_diagnostic_markers(f"<html><h1>{name}</h1></html>"), [])

    def test_a_huge_numeric_string_is_withheld_not_raised(self):
        from report_safety import public_number, public_run_config

        self.assertIsNone(public_number("9" * 5000))
        self.assertNotIn("node_count", public_run_config({"node_count": "9" * 5000}))


class ExporterLogExportTests(unittest.TestCase):
    def _exporter(self):
        return load_exporter_harness()._load_exporter()

    def test_the_reporter_stream_is_never_exported_as_run_data(self):
        exporter = self._exporter()
        exported = []
        exporter._list_log_stream_names = lambda group: ["memtier/memtier/a", "reporter/reporter/b"]
        exporter._export_log_streams_to_s3 = lambda group, bucket, prefix, streams: (
            exported.extend(streams) or [{"stream": stream} for stream in streams]
        )

        status = exporter.export_loadgen_logs_to_s3("group", "bucket", "prefix/logs/loadgen")

        self.assertEqual(exported, ["memtier/memtier/a"])
        self.assertTrue(status["complete"])

    def test_error_code_tolerates_malformed_responses(self):
        exporter = self._exporter()
        for response in ({"Error": None}, {"Error": "x"}, None, {"Error": {"Code": "AccessDenied"}}):
            with self.subTest(response=response):
                error = RuntimeError("message with arn:aws:sts::000000000000:assumed-role/x")
                error.response = response
                code = exporter._error_code(error)
                self.assertIn(code, {"RuntimeError", "AccessDenied"})


class AtomicWriteTests(unittest.TestCase):
    def test_all_files_are_written(self):
        from report_safety import write_files_atomically

        with tempfile.TemporaryDirectory() as tmp:
            write_files_atomically({Path(tmp) / "a.html": "A", Path(tmp) / "sub" / "b.json": "B"})

            self.assertEqual((Path(tmp) / "a.html").read_text(encoding="utf-8"), "A")
            self.assertEqual((Path(tmp) / "sub" / "b.json").read_text(encoding="utf-8"), "B")
            self.assertEqual([p.name for p in Path(tmp).rglob("*.tmp")], [])

    def test_a_failure_while_staging_replaces_nothing_and_leaves_no_temp_files(self):
        from report_safety import ReportGenerationError, write_files_atomically

        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "results.html"
            existing.write_text("previous valid", encoding="utf-8")
            blocker = Path(tmp) / "blocker"
            blocker.write_text("a file where a directory is needed", encoding="utf-8")

            with self.assertLogs("reporter.diagnostics", level="ERROR"):
                with self.assertRaises(ReportGenerationError):
                    write_files_atomically({existing: "new", blocker / "second.json": "x"})

            self.assertEqual(existing.read_text(encoding="utf-8"), "previous valid")
            self.assertEqual([p.name for p in Path(tmp).iterdir() if p.name.endswith(".tmp")], [])

    def test_a_failure_while_moving_into_place_restores_every_file(self):
        """A later move failing must not leave new JSON beside old HTML."""
        from report_safety import ReportGenerationError, write_files_atomically

        with tempfile.TemporaryDirectory() as tmp:
            first, second, fresh = Path(tmp) / "a.json", Path(tmp) / "b.html", Path(tmp) / "c.json"
            first.write_text("old A", encoding="utf-8")
            second.write_text("old B", encoding="utf-8")
            real_replace = os.replace

            def replace(src, dst):
                if Path(dst).name == "b.html":
                    raise PermissionError("locked by a reader")
                return real_replace(src, dst)

            with mock.patch("report_safety.os.replace", side_effect=replace):
                with self.assertLogs("reporter.diagnostics", level="ERROR"):
                    with self.assertRaises(ReportGenerationError):
                        write_files_atomically({first: "new A", fresh: "new C", second: "new B"})

            self.assertEqual(first.read_text(encoding="utf-8"), "old A")
            self.assertEqual(second.read_text(encoding="utf-8"), "old B")
            self.assertFalse(fresh.exists())
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["a.json", "b.html"])

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_written_files_keep_normal_permissions(self):
        from report_safety import write_files_atomically

        with tempfile.TemporaryDirectory() as tmp:
            existing, fresh = Path(tmp) / "kept.html", Path(tmp) / "new.json"
            existing.write_text("old", encoding="utf-8")
            os.chmod(existing, 0o640)
            umask = os.umask(0o022)
            try:
                write_files_atomically({existing: "new", fresh: "new"})
            finally:
                os.umask(umask)

            self.assertEqual(existing.stat().st_mode & 0o777, 0o640)
            self.assertEqual(fresh.stat().st_mode & 0o777, 0o644)


class ModulePackagingTests(unittest.TestCase):
    """ECS downloads a fixed list of reporter modules from S3 (reporter.tf).
    A module that is imported but not on the list fails the task at import."""

    @staticmethod
    def _shipped_modules():
        reporter_tf = (ROOT / "reporter.tf").read_text(encoding="utf-8")
        block = re.search(r"reporter_modules = toset\(\[(.*?)\]\)", reporter_tf, re.S).group(1)
        return reporter_tf, set(re.findall(r'"([a-z_]+)\.py"', block))

    def test_the_task_downloads_exactly_the_uploaded_module_list(self):
        reporter_tf, shipped = self._shipped_modules()

        self.assertIn("report_safety", shipped)
        self.assertIn("modules = ${jsonencode(sort(tolist(local.reporter_modules)))}", reporter_tf)
        # No second hand-kept list for the two to drift apart.
        self.assertEqual(reporter_tf.count('"report_safety.py"'), 1)

    def test_every_shipped_module_exists(self):
        _reporter_tf, shipped = self._shipped_modules()

        for module in sorted(shipped):
            with self.subTest(module=module):
                self.assertTrue((ROOT / "reporter" / f"{module}.py").exists())

    def test_every_top_level_local_import_of_a_shipped_module_is_itself_shipped(self):
        _reporter_tf, shipped = self._shipped_modules()
        local_modules = {path.stem for path in (ROOT / "reporter").glob("*.py")}
        self.assertIn("report_safety", shipped)

        for module in sorted(shipped):
            source = (ROOT / "reporter" / f"{module}.py").read_text(encoding="utf-8")
            for imported in re.findall(r"^(?:from|import) ([a-z_]+)", source, re.M):
                if imported in local_modules and imported != module:
                    with self.subTest(module=module, imports=imported):
                        self.assertIn(imported, shipped)

    def test_report_safety_does_not_need_pandas_to_import(self):
        saved = {name: sys.modules.get(name) for name in ("report_safety", "pandas")}
        sys.modules.pop("report_safety", None)
        sys.modules["pandas"] = None
        try:
            import importlib

            importlib.import_module("report_safety")
        finally:
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module


if __name__ == "__main__":
    unittest.main()
