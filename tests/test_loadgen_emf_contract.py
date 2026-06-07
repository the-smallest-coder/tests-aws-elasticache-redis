import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LoadgenEmfContractTests(unittest.TestCase):
    def test_loadgen_stdout_publishes_client_latency_emf(self):
        ecs_tf = (ROOT / "ecs.tf").read_text(encoding="utf-8")

        self.assertIn("ElastiCache/LoadGenerator", ecs_tf)
        self.assertIn("ClientLatency", ecs_tf)
        self.assertIn("Milliseconds", ecs_tf)
        self.assertIn("StorageResolution", ecs_tf)
        self.assertIn("ClusterName", ecs_tf)
        self.assertIn("ServiceName", ecs_tf)
        self.assertIn("TaskId", ecs_tf)
        self.assertIn("sprintf", ecs_tf)
        self.assertIn("systime() * 1000", ecs_tf)
        self.assertIn('msec latency', ecs_tf)

    def test_emf_path_does_not_require_put_metric_data(self):
        iam_tf = (ROOT / "ecs_iam.tf").read_text(encoding="utf-8")
        ecs_tf = (ROOT / "ecs.tf").read_text(encoding="utf-8")

        self.assertNotIn("cloudwatch:PutMetricData", iam_tf)
        self.assertNotIn("put-metric-data", ecs_tf)


if __name__ == "__main__":
    unittest.main()
