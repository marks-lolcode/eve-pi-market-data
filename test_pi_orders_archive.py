import datetime
import gzip
import json
import os
import tempfile
import unittest

import pi_orders_archive as pa

STATION = 60003760


def order(oid, tid, buy=False, loc=STATION, **kw):
    o = {"order_id": oid, "type_id": tid, "is_buy_order": buy, "price": 10.5,
         "volume_remain": 7, "volume_total": 9, "issued": "2026-10-20T01:02:03Z",
         "duration": 90, "location_id": loc, "min_volume": 1, "range": "station",
         "system_id": 30000142}
    o.update(kw)
    return o


SCHEMATICS = [
    {"schematicID": 1, "types": [{"typeID": 2389, "isInput": True, "quantity": 3000},
                                 {"typeID": 2392, "isInput": False, "quantity": 20}]},
    {"schematicID": 2, "types": [{"typeID": 2392, "isInput": True, "quantity": 40},
                                 {"typeID": 3689, "isInput": False, "quantity": 5}]},
]


class PiFilter(unittest.TestCase):
    def test_type_set_is_all_schematic_types(self):
        self.assertEqual(pa.pi_type_ids(SCHEMATICS), {2389, 2392, 3689})

    def test_filters_type_and_station(self):
        ids = pa.pi_type_ids(SCHEMATICS)
        orders = [order(1, 2392), order(2, 34), order(3, 2389, loc=1), order(4, 3689, buy=True)]
        rows = pa.filter_pi_rows(orders, ids, STATION)
        self.assertEqual([r[0] for r in rows], [1, 4])

    def test_row_shape(self):
        row = pa.order_row(order(5, 2392, buy=True, min_volume=100))
        self.assertEqual(row, [5, 2392, True, 10.5, 7, 9, "2026-10-20T01:02:03Z", 90, 100])
        self.assertEqual(len(row), len(pa.ARCHIVE_COLUMNS))

    def test_min_volume_defaults_to_one(self):
        o = order(6, 2392)
        del o["min_volume"]
        self.assertEqual(pa.order_row(o)[8], 1)


class PiFile(unittest.TestCase):
    def test_payload_sorted_and_roundtrips_via_gzip(self):
        rows = [pa.order_row(order(9, 3689)), pa.order_row(order(2, 2392)), pa.order_row(order(1, 2392))]
        payload = pa.build_payload(rows, "2026-10-22T05:07:00Z")
        self.assertEqual([r[0] for r in payload["rows"]], [1, 2, 9])
        when = datetime.datetime(2026, 10, 22, 5, 7, tzinfo=datetime.timezone.utc)
        with tempfile.TemporaryDirectory() as d:
            path = pa.write_archive(d, payload, when)
            self.assertTrue(path.replace("\\", "/").endswith("orders/2026/10/22/05.json.gz"))
            with gzip.open(path, "rb") as f:
                back = json.loads(f.read())
            self.assertEqual(back["columns"], pa.ARCHIVE_COLUMNS)
            self.assertEqual(back["generatedAt"], "2026-10-22T05:07:00Z")
            self.assertEqual(back["rows"], payload["rows"])
            a = open(path, "rb").read()
            pa.write_archive(d, payload, when)
            self.assertEqual(a, open(path, "rb").read())  # deterministic bytes


class MalformedOrderIsolated(unittest.TestCase):
    def test_malformed_order_does_not_change_sweep(self):
        import contextlib
        import io
        import sweep_orders

        calls = []

        def fake_original(acc, orders, now_ts):
            calls.append(list(orders))
            acc.append(len(orders))
            return "orig-result"

        def fake_main():
            acc = []
            good = order(1, 2392)
            bad = {"type_id": 2392, "location_id": STATION}  # missing order_id etc.
            sweep_orders.accumulate(acc, [good, bad], 0)
            sweep_orders.accumulate(acc, [good], 0)
            return (0, acc)

        saved = (sweep_orders.accumulate, sweep_orders.main, pa.load_pi_types, pa.ARCHIVE_DIR)
        sweep_orders.accumulate = fake_original
        sweep_orders.main = fake_main
        pa.load_pi_types = lambda: {2392}
        err = io.StringIO()
        try:
            with tempfile.TemporaryDirectory() as d:
                pa.ARCHIVE_DIR = d
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    rc = pa.main()  # must not raise
                self.assertFalse(os.path.exists(os.path.join(d, "orders")))  # no partial archive
        finally:
            sweep_orders.accumulate, sweep_orders.main, pa.load_pi_types, pa.ARCHIVE_DIR = saved
        self.assertEqual(rc, (0, [2, 1]))  # identical to an unwrapped sweep
        self.assertEqual(len(calls), 2)  # original accumulate saw every page
        self.assertIn("collection FAILED", err.getvalue())


if __name__ == "__main__":
    unittest.main()
