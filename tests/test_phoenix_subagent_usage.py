import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from phoenix_subagent_usage import tso


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


if __name__ == "__main__":
    unittest.main()
