import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from phoenix_subagent_usage import build_economics, tso


class TimestampTransportTests(unittest.TestCase):
    def test_sqlite_launch_and_wire_delivery_share_a_comparable_clock(self):
        launch = tso("2026-11-04 02:08:12.534000")
        delivery = tso("2026-11-04T02:08:12.535Z")
        self.assertEqual(launch, tso("2026-11-04T02:08:12.534Z"))
        self.assertLess(launch, delivery)
        self.assertAlmostEqual((delivery - launch).total_seconds(), 0.001)

    def test_explicit_offsets_keep_the_same_instant(self):
        self.assertEqual(
            tso("2026-11-04T10:08:12.534+08:00"),
            tso("2026-11-04 02:08:12.534000"),
        )

    def test_missing_or_invalid_timestamp_remains_unknown(self):
        self.assertIsNone(tso(""))
        self.assertIsNone(tso("invalid"))


class ControlPlaneTests(unittest.TestCase):
    def test_result_request_that_only_returns_running_counts_as_polling(self):
        def status_span(result, params):
            return {
                "name": "execute_tool task_status",
                "spanKind": "tool",
                "startTime": "2026-11-04T02:00:00Z",
                "endTime": "2026-11-04T02:00:01Z",
                "attributes": json.dumps({
                    "session": {"id": "parent"},
                    "tool": {"name": "task_status", "parameters": params},
                    "output": {"value": json.dumps([result])},
                }),
            }

        spans = [
            status_span("sessionID: child\nstate: running\nWait for its result.",
                        {"sessionID": "child", "result": True}),
            status_span("Status: Completed\nVerification passed.",
                        {"sessionID": "child", "result": True}),
            status_span("state: running\nDiagnostic details.",
                        {"sessionID": "child", "diagnostic": True}),
        ]
        events = build_economics(spans, None, 2.35, 0)["task_events"]["parent"]
        self.assertEqual(events["polls"], 1)
        self.assertEqual(len(events["pulls"]), 1)
        self.assertIn("Verification passed", events["pulls"][0]["result"])
        self.assertEqual(events["diagnostic_reads"], 1)


if __name__ == "__main__":
    unittest.main()
