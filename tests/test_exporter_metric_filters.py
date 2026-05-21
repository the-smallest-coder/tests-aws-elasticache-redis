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


if __name__ == "__main__":
    unittest.main()
