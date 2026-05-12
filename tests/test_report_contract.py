import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OLD_RUN = ROOT / "results" / "20260227-140039"
GOOD_RUN = ROOT / "results" / "20260501-063922"
BAD_RUN = ROOT / "results" / "20260501-083934"


def card_labels(run_dir: Path) -> set[str]:
    html = (run_dir / "results_local.html").read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r"<div class='card-label'>(.*?)</div>", html))


def summary(run_dir: Path) -> dict:
    return json.loads((run_dir / "results_local.json").read_text(encoding="utf-8"))


class ReportContractTests(unittest.TestCase):
    def test_good_report_keeps_old_card_contract(self):
        old_labels = card_labels(OLD_RUN)
        good_labels = card_labels(GOOD_RUN)

        self.assertLessEqual(old_labels, good_labels)

    def test_good_report_keeps_old_json_contract(self):
        old = summary(OLD_RUN)
        good = summary(GOOD_RUN)

        for section, values in old.items():
            if not isinstance(values, dict):
                continue
            self.assertIn(section, good)
            self.assertLessEqual(set(values), set(good[section]), section)

    def test_missing_benchmark_fixture_shows_current_failure(self):
        bad = summary(BAD_RUN)

        self.assertFalse(bad["benchmark"])
        self.assertFalse((BAD_RUN / "logs").exists())
        self.assertNotIn("Avg Throughput", card_labels(BAD_RUN))


class MemtierParserTests(unittest.TestCase):
    def test_parser_handles_carriage_return_progress_records(self):
        try:
            from reporter.parsers import parse_memtier_logs
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed in this environment")
            raise

        content = (
            "[2026-05-01T00:00:10] [memtier/task-a] "
            "[RUN #1 10%, 1 secs] 100.00 (avg: 100.00) ops/sec, "
            "1.00 (avg: 1.00) msec latency, 1.00KB/sec (avg: 1.00KB/sec)\r"
            "[RUN #1 20%, 2 secs] 200.00 (avg: 150.00) ops/sec, "
            "2.00 (avg: 1.50) msec latency, 2.00KB/sec (avg: 1.50KB/sec)\n"
        )

        parsed = parse_memtier_logs(content)

        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed["Ops/sec"].tolist(), [100.0, 150.0])
        self.assertEqual(parsed["Latency (ms)"].tolist(), [1.0, 1.5])


if __name__ == "__main__":
    unittest.main()
