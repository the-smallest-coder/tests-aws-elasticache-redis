import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class _FakeEcs:
    def describe_services(self, cluster, services):
        return {"services": [{"desiredCount": 1, "runningCount": 1}]}


class _FakeEvents:
    def __init__(self):
        self.rules = []

    def describe_rule(self, Name):
        return {"ScheduleExpression": ""}

    def put_rule(self, **kwargs):
        self.rules.append(kwargs)
        return {}


def _load_schedule_shutdown_module():
    fake_boto3 = types.SimpleNamespace(client=lambda *args, **kwargs: object())
    module_path = ROOT / "lambda" / "schedule_shutdown.py"
    spec = importlib.util.spec_from_file_location(
        "schedule_shutdown_email_test_subject", module_path
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"boto3": fake_boto3}):
        spec.loader.exec_module(module)
    return module


class ScheduleShutdownEmailTests(unittest.TestCase):
    def test_started_email_escapes_env_values_in_html_body(self):
        module = _load_schedule_shutdown_module()
        module.ecs = _FakeEcs()
        module.events = _FakeEvents()
        captured = {}

        def capture_email(subject, body_text, body_html=None):
            captured["subject"] = subject
            captured["body_text"] = body_text
            captured["body_html"] = body_html
            return True

        module._send_email = capture_email

        env = {
            "CLUSTER_ID": "cluster</span><script>",
            "ECS_CLUSTER": "ecs-cluster",
            "ECS_SERVICE": "svc<script>",
            "SHUTDOWN_RULE_NAME": "shutdown-rule",
            "TEST_DURATION_MINUTES": "5",
            "VERIFY_DELAY_MINUTES": "10",
            "ENGINE_TYPE": "redis<script>",
            "ENGINE_VERSION": "7&\"'",
            "NODE_TYPE": "cache.m7g.large</td>",
            "NODE_COUNT": "3",
            "LOADGEN_TASK_COUNT": "2",
            "AWS_REGION_NAME": "us-east-1<script>",
        }
        with patch.dict(os.environ, env, clear=False):
            result = module.handler({}, None)

        self.assertTrue(result["scheduled"])
        html = captured["body_html"]
        self.assertIn("cluster&lt;/span&gt;&lt;script&gt;", html)
        self.assertIn("Redis&lt;script&gt; 7&amp;&quot;&#x27;", html)
        self.assertIn("cache.m7g.large&lt;/td&gt;", html)
        self.assertIn("us-east-1&lt;script&gt;", html)
        self.assertNotIn("cluster</span><script>", html)
        self.assertNotIn("Redis<script>", html)
        self.assertNotIn("cache.m7g.large</td>", html)
        self.assertNotIn("us-east-1<script>", html)


if __name__ == "__main__":
    unittest.main()
