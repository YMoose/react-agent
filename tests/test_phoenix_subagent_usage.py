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


class NegativeNoiseTests(unittest.TestCase):
    """Report-larger-than-raw boundaries keep their negative ΔT_noise.

    A report bigger than the raw material it filtered is extra content the
    parent re-carries every remaining episode turn: Saved_k must go negative
    and deepen NetSaved_k instead of being clamped to zero.
    """

    REPORT = "Status: Completed\nFiles Touched: none\nVerification: nothing to verify. " + "x" * 560
    PROMPT = "Do the thing carefully and report results."

    def llm_span(self, sid, start, end, input_tokens):
        return {
            "name": "chat m",
            "spanKind": "LLM",
            "startTime": start,
            "endTime": end,
            "attributes": json.dumps({
                "session": {"id": sid},
                "gen_ai": {"usage": {"input_tokens": input_tokens, "output_tokens": 200}},
            }),
        }

    def subagent_span(self, sid, start, end, child, report):
        result = json.dumps([f'<subagent sessionID="{child}" state="completed">\n{report}\n</subagent>'])
        return {
            "name": "execute_tool subagent",
            "spanKind": "TOOL",
            "startTime": start,
            "endTime": end,
            "attributes": json.dumps({
                "session": {"id": sid},
                "tool": {"name": "subagent", "parameters": {"prompt": self.PROMPT, "agent": "actor"}},
                "gen_ai": {"tool": {"call": {"result": result}}},
            }),
        }

    def child_tool_span(self, sid, start, end, result):
        return {
            "name": "execute_tool read",
            "spanKind": "TOOL",
            "startTime": start,
            "endTime": end,
            "attributes": json.dumps({
                "session": {"id": sid},
                "tool": {"name": "read"},
                "gen_ai": {"tool": {"call": {"result": json.dumps([result])}}},
            }),
        }

    def parent_spans(self, sid, dispatch):
        return [
            self.llm_span(sid, "2026-11-06T02:00:00Z", "2026-11-06T02:00:10Z", 10000),
            dispatch,
            self.llm_span(sid, "2026-11-06T02:00:40Z", "2026-11-06T02:00:50Z", 10455),
            self.llm_span(sid, "2026-11-06T02:01:00Z", "2026-11-06T02:01:10Z", 11000),
        ]

    def build(self, spans):
        return build_economics(spans, None, 2.35, 0)["dispatches"][0]

    def test_report_exceeding_raw_keeps_negative_noise_and_deepens_net(self):
        rec = self.build(self.parent_spans(
            "P1",
            self.subagent_span("P1", "2026-11-06T02:00:20Z", "2026-11-06T02:00:30Z", "C1", self.REPORT),
        ) + [self.child_tool_span("C1", "2026-11-06T02:00:22Z", "2026-11-06T02:00:24Z", "y" * 100)])
        # raw 104 chars ≈ 44 tok < report est 292 tok ⇒ ΔT_noise stays negative
        self.assertEqual(rec.t_report_effective, 292)
        self.assertEqual(rec.dt_noise_tokens, 44 - 292)
        self.assertEqual(rec.n_rem_ep, 2)
        self.assertEqual(rec.compounded_saved, 2 * (44 - 292))
        # NetSaved deepens below the boundary cost alone: −496 − β(17+292)
        self.assertEqual(rec.net_saved_tokens, (2 * (44 - 292)) - round(4.0 * (17 + 292)))
        self.assertIsNone(rec.n_rem_star)  # unamortizable — no threshold exists

    def test_zero_tool_dispatch_scores_minus_report_tokens(self):
        # no child session spans at all ⇒ raw = 0, ΔT_noise = −T_report
        rec = self.build(self.parent_spans(
            "P2",
            self.subagent_span("P2", "2026-11-06T02:00:20Z", "2026-11-06T02:00:30Z", "C2", self.REPORT),
        ))
        self.assertEqual(rec.actor_raw_result_chars, 0)
        self.assertEqual(rec.dt_noise_tokens, -rec.t_report_effective)
        self.assertEqual(rec.compounded_saved, rec.n_rem_ep * -rec.t_report_effective)
        self.assertLess(rec.net_saved_tokens, -rec.t_report_effective)
        self.assertIsNone(rec.n_rem_star)

    def test_positive_noise_still_amortizes_with_threshold(self):
        rec = self.build(self.parent_spans(
            "P3",
            self.subagent_span("P3", "2026-11-06T02:00:20Z", "2026-11-06T02:00:30Z", "C3", self.REPORT),
        ) + [self.child_tool_span("C3", "2026-11-06T02:00:22Z", "2026-11-06T02:00:24Z", "y" * 2000)])
        self.assertGreater(rec.dt_noise_tokens, 0)  # 2004 chars ≈ 852 tok − 292
        self.assertEqual(rec.dt_noise_tokens, 852 - 292)
        self.assertEqual(rec.compounded_saved, 2 * (852 - 292))
        self.assertEqual(rec.n_rem_star, round(4.0 * (17 + 292) / (852 - 292), 2))
        self.assertIsNotNone(rec.n_rem_star)


if __name__ == "__main__":
    unittest.main()
