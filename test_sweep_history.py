import datetime
import io
import json
import unittest
from unittest import mock

import sweep_history as sh

TODAY = datetime.date(2026, 11, 18)


class CalendarAvgVolume(unittest.TestCase):
    def test_one_sale_in_30_days_over_a_long_record_list(self):
        # ESI omits no-trade days: old busy records, then one unit on 11-06.
        data = [{"date": "2025-03-01", "volume": 6}, {"date": "2025-03-02", "volume": 4},
                {"date": "2026-11-06", "volume": 1}]
        self.assertEqual(sh.calendar_avg_volume(data, 30, TODAY), 0.033)

    def test_window_is_exactly_30_dates_before_today(self):
        # 10-19 .. 11-17 is 30 dates; 10-18 is out, today is out.
        self.assertEqual(sh.calendar_avg_volume([{"date": "2026-10-19", "volume": 30}], 30, TODAY), 1.0)
        self.assertEqual(sh.calendar_avg_volume([{"date": "2026-10-18", "volume": 30}], 30, TODAY), 0.0)
        self.assertEqual(sh.calendar_avg_volume([{"date": "2026-11-17", "volume": 30}], 30, TODAY), 1.0)
        self.assertEqual(sh.calendar_avg_volume([{"date": "2026-11-18", "volume": 30}], 30, TODAY), 0.0)

    def test_empty_bad_dates_and_missing_volume(self):
        before = sh._bad_date_rows
        self.assertEqual(sh.calendar_avg_volume([], 30, TODAY), 0.0)
        self.assertEqual(sh.calendar_avg_volume([{"date": "junk", "volume": 9}, {"volume": 9}, {"date": None, "volume": 9}], 30, TODAY), 0.0)
        self.assertEqual(sh._bad_date_rows - before, 3, "skipped rows are counted for the run summary")
        self.assertEqual(sh.calendar_avg_volume([{"date": "2026-11-10"}], 30, TODAY), 0.0, "a missing volume counts as 0, no KeyError")


class FetchHistory(unittest.TestCase):
    def test_row_shape_and_median_still_from_last_30_records(self):
        today = datetime.datetime.now(datetime.timezone.utc).date()
        yesterday = (today - datetime.timedelta(days=1)).isoformat()
        long_ago = "2020-01-01"
        payload = [{"date": long_ago, "volume": 100, "average": 5.0}, {"date": yesterday, "volume": 3, "average": 9.0}]

        class Resp(io.BytesIO):
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch("urllib.request.urlopen", return_value=Resp(json.dumps(payload).encode())):
            row = sh.fetch_history(34, retries=1)
        self.assertEqual(row, [34, 0.1, 7.0])  # 3 units / 30 days; median of both records (5, 9)

    def test_history_volume_basis_is_published(self):
        with open(sh.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertIn('"historyVolumeBasis": "calendar"', src)


if __name__ == "__main__":
    unittest.main()
