import datetime
import unittest

import sweep_history as sh

TODAY = datetime.date(2026, 11, 18)


class CalendarAvgVolume(unittest.TestCase):
    def test_one_sale_in_30_days_over_a_long_record_list(self):
        # ESI omits no-trade days: old busy records, then one unit on 11-06.
        data = [{"date": "2025-03-01", "volume": 6}, {"date": "2025-03-02", "volume": 4},
                {"date": "2026-11-06", "volume": 1}]
        self.assertEqual(sh.calendar_avg_volume(data, 30, TODAY), 0.033)

    def test_window_edges(self):
        self.assertEqual(sh.calendar_avg_volume([{"date": "2026-10-19", "volume": 30}], 30, TODAY), 1.0)
        self.assertEqual(sh.calendar_avg_volume([{"date": "2026-10-18", "volume": 30}], 30, TODAY), 0.0)

    def test_empty_and_bad_dates(self):
        self.assertEqual(sh.calendar_avg_volume([], 30, TODAY), 0.0)
        self.assertEqual(sh.calendar_avg_volume([{"date": "junk", "volume": 9}, {"volume": 9}], 30, TODAY), 0.0)


if __name__ == "__main__":
    unittest.main()
