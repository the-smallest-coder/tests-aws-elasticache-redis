import importlib.util
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def _load_exporter():
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = mock.Mock(side_effect=AssertionError("unexpected boto3 client"))
    fake_report_generator = types.ModuleType("report_generator")
    fake_report_generator.run_uploaded_report = mock.Mock()

    module_name = "exporter_metric_filter_test_subject"
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "reporter" / "exporter.py")
    exporter = importlib.util.module_from_spec(spec)
    with mock.patch.dict(
        sys.modules,
        {
            "boto3": fake_boto3,
            "report_generator": fake_report_generator,
        },
    ):
        spec.loader.exec_module(exporter)
    return exporter


class MetricExportFilterTests(unittest.TestCase):
    def test_metric_source_export_skips_unrequested_discovered_metrics(self):
        exporter = _load_exporter()

        class FakeCloudWatch:
            def __init__(self):
                self.statistics_requests = []

            def list_metrics(self, **_params):
                return {
                    "Metrics": [
                        {
                            "MetricName": "WantedMetric",
                            "Dimensions": [
                                {"Name": "ClusterName", "Value": "test-cluster"},
                                {"Name": "TaskId", "Value": "task-a"},
                            ],
                        },
                        {
                            "MetricName": "UnrelatedMetric",
                            "Dimensions": [
                                {"Name": "ClusterName", "Value": "test-cluster"},
                                {"Name": "TaskId", "Value": "task-a"},
                            ],
                        },
                    ]
                }

            def get_metric_statistics(self, **params):
                self.statistics_requests.append(params)
                return {"Datapoints": []}

        class FakeS3:
            def put_object(self, **_params):
                return {}

        fake_cloudwatch = FakeCloudWatch()
        original_cloudwatch = exporter.cloudwatch
        original_s3 = exporter.s3
        exporter.cloudwatch = fake_cloudwatch
        exporter.s3 = FakeS3()
        try:
            with mock.patch("builtins.print"):
                exporter.export_metric_sources_to_s3(
                    [
                        {
                            "namespace": "AWS/Test",
                            "dimensions": [{"Name": "ClusterName", "Value": "test-cluster"}],
                            "metric_names": ["WantedMetric"],
                        }
                    ],
                    "bucket",
                    "metrics.csv",
                    datetime(2026, 5, 21, tzinfo=timezone.utc),
                    datetime(2026, 5, 21, 0, 1, tzinfo=timezone.utc),
                )
        finally:
            exporter.cloudwatch = original_cloudwatch
            exporter.s3 = original_s3

        self.assertEqual(
            {request["MetricName"] for request in fake_cloudwatch.statistics_requests},
            {"WantedMetric"},
        )
        self.assertEqual(len(fake_cloudwatch.statistics_requests), 2)

    def test_ecs_client_latency_exports_percentile_rows(self):
        exporter = _load_exporter()

        class FakeCloudWatch:
            def list_metrics(self, **_params):
                return {
                    "Metrics": [
                        {
                            "MetricName": "ClientLatency",
                            "Dimensions": [
                                {"Name": "ClusterName", "Value": "cluster-a"},
                                {"Name": "ServiceName", "Value": "service-a"},
                                {"Name": "TaskId", "Value": "task-a"},
                            ],
                        }
                    ]
                }

            def get_metric_statistics(self, **params):
                self.request = params
                return {
                    "Datapoints": [
                        {
                            "Timestamp": datetime(2026, 5, 21, tzinfo=timezone.utc),
                            "Unit": "Milliseconds",
                            "ExtendedStatistics": {"p50": 1.5, "p99": 9.9, "p99.9": 19.9},
                        }
                    ]
                }

        class FakeS3:
            def put_object(self, **params):
                self.body = params["Body"]

        fake_cloudwatch = FakeCloudWatch()
        fake_s3 = FakeS3()
        original_cloudwatch = exporter.cloudwatch
        original_s3 = exporter.s3
        exporter.cloudwatch = fake_cloudwatch
        exporter.s3 = fake_s3
        try:
            with mock.patch("builtins.print") as fake_print:
                exporter.export_metric_sources_to_s3(
                    [
                        {
                            "namespace": exporter.ECS_CLIENT_LATENCY_METRIC["namespace"],
                            "dimensions": [
                                {"Name": "ClusterName", "Value": "cluster-a"},
                                {"Name": "ServiceName", "Value": "service-a"},
                            ],
                            "metric_names": [exporter.ECS_CLIENT_LATENCY_METRIC["metric_name"]],
                            "optional_metric_names": [exporter.ECS_CLIENT_LATENCY_METRIC["metric_name"]],
                            "statistics": [],
                            "extended_statistics": exporter.ECS_CLIENT_LATENCY_METRIC["stats"],
                            "label": "ECS client latency metric discovery",
                        }
                    ],
                    "bucket",
                    "metrics.csv",
                    datetime(2026, 5, 21, tzinfo=timezone.utc),
                    datetime(2026, 5, 21, 0, 1, tzinfo=timezone.utc),
                )
        finally:
            exporter.cloudwatch = original_cloudwatch
            exporter.s3 = original_s3

        self.assertEqual(fake_cloudwatch.request["ExtendedStatistics"], ["p50", "p99", "p99.9"])
        printed = "\n".join(str(call.args[0]) for call in fake_print.call_args_list if call.args)
        self.assertIn("ECS client latency metric discovery", printed)
        self.assertIn("Namespace=ElastiCache/LoadGenerator", printed)
        self.assertIn("MetricName=ClientLatency", printed)
        self.assertIn("RequestedDimensions=ClusterName=cluster-a;ServiceName=service-a", printed)
        self.assertIn("TaskId=task-a", printed)
        csv_text = fake_s3.body
        self.assertIn("ClientLatency,p50,1.5,Milliseconds", csv_text)
        self.assertIn("ClientLatency,p99,9.9,Milliseconds", csv_text)
        self.assertIn("ClientLatency,p99.9,19.9,Milliseconds", csv_text)

    def test_optional_cpu_credit_metrics_are_discovery_only(self):
        exporter = _load_exporter()

        class FakeCloudWatch:
            def __init__(self, discovered):
                self.discovered = discovered
                self.statistics_requests = []

            def list_metrics(self, **_params):
                return {"Metrics": self.discovered}

            def get_metric_statistics(self, **params):
                self.statistics_requests.append(params)
                return {"Datapoints": []}

        class FakeS3:
            def put_object(self, **_params):
                return {}

        def run(discovered):
            fake_cloudwatch = FakeCloudWatch(discovered)
            original_cloudwatch = exporter.cloudwatch
            original_s3 = exporter.s3
            exporter.cloudwatch = fake_cloudwatch
            exporter.s3 = FakeS3()
            try:
                with mock.patch("builtins.print"):
                    exporter.export_metric_sources_to_s3(
                        [
                            {
                                "namespace": "AWS/ElastiCache",
                                "dimensions": [{"Name": "CacheClusterId", "Value": "cluster-a-001"}],
                                "metric_names": ["EngineCPUUtilization", "CPUCreditBalance", "CPUCreditUsage"],
                                "optional_metric_names": ["CPUCreditBalance", "CPUCreditUsage"],
                            }
                        ],
                        "bucket",
                        "metrics.csv",
                        datetime(2026, 5, 21, tzinfo=timezone.utc),
                        datetime(2026, 5, 21, 0, 1, tzinfo=timezone.utc),
                    )
            finally:
                exporter.cloudwatch = original_cloudwatch
                exporter.s3 = original_s3
            return {request["MetricName"] for request in fake_cloudwatch.statistics_requests}

        absent = run([])
        present = run([
            {
                "MetricName": "CPUCreditBalance",
                "Dimensions": [{"Name": "CacheClusterId", "Value": "cluster-a-001"}],
            }
        ])

        self.assertEqual(absent, {"EngineCPUUtilization"})
        self.assertEqual(present, {"EngineCPUUtilization", "CPUCreditBalance"})


if __name__ == "__main__":
    unittest.main()
