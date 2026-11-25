import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta
from unittest import mock
from urllib.request import urlretrieve

import gtfs
import main

# A tiny feed. Stops A -> B -> C. WK runs Monday to Friday except Thanksgiving (Nov 26).
# HOL only runs on Thanksgiving. T2 runs past midnight.
FEED = {
  "feed_info.txt": "feed_version,feed_start_date,feed_end_date\ntest,20261101,20261231\n",
  "routes.txt": (
    "route_id,route_short_name,route_long_name,route_type,route_color,route_text_color\n"
    'R1,Rapid 1,"Here - There, Somewhere",3,e4002b,FFFFFF\n'
    "Blue,Blue Line,Light rail,0,007ACC,\n"
  ),
  "calendar.txt": (
    "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
    "WK,1,1,1,1,1,0,0,20261101,20261231\n"
  ),
  "calendar_dates.txt": "service_id,date,exception_type\nWK,20261126,2\nHOL,20261126,1\n",
  "stops.txt": "stop_id,stop_lat,stop_lon\nA,37.0,-122.0\nB,37.0,-121.9\nC,37.1,-121.9\nNOPOS,,\n",
  "trips.txt": "route_id,service_id,trip_id,trip_headsign\nR1,WK,T1,THERE\nR1,WK,T2,LATE\nBlue,HOL,T3,HOLIDAY\n",
  "stop_times.txt": (
    "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
    "T1,08:00:00,08:00:00,A,1\n"
    "T1,08:10:00,08:12:00,B,2\n"
    "T1,08:20:00,08:20:00,C,3\n"
    "T2,23:50:00,23:50:00,A,1\n"
    "T2,24:10:00,24:10:00,B,2\n"
    "T2,24:20:00,24:20:00,C,3\n"
    "T3,08:00:00,08:00:00,A,1\n"
    "T3,,,NOPOS,2\n"
    "T3,08:20:00,08:20:00,C,3\n"
  ),
}


def at(value):
  return datetime.fromisoformat(value).replace(tzinfo=main.PACIFIC)


class FeedTestCase(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.tmp = tempfile.TemporaryDirectory()
    zip_path = os.path.join(cls.tmp.name, "feed.zip")
    with zipfile.ZipFile(zip_path, "w") as z:
      for name, text in FEED.items():
        z.writestr(name, text)
    db_path = os.path.join(cls.tmp.name, "feed.sqlite")
    gtfs.build_db(zip_path, db_path)
    cls.db = sqlite3.connect(db_path)

  @classmethod
  def tearDownClass(cls):
    cls.db.close()
    cls.tmp.cleanup()

  def trips_at(self, value):
    return {v["trip_id"]: v for v in main.find_vehicles(self.db, at(value))["vehicles"]}


class TestBuildDb(FeedTestCase):
  def test_parse_time_past_midnight(self):
    self.assertEqual(gtfs.parse_time("25:01:02"), 25 * 3600 + 62)

  def test_routes(self):
    rows = self.db.execute("select route_id, label, name, color, text_color from routes order by route_id").fetchall()
    self.assertEqual(rows, [
      ("Blue", "Blue", "Light rail", "#007ACC", "#FFFFFF"),
      ("R1", "1", "Here - There, Somewhere", "#e4002b", "#FFFFFF"),
    ])

  def test_trip_spans(self):
    rows = self.db.execute("select trip_id, start_sec, end_sec from trips order by trip_id").fetchall()
    self.assertEqual(rows, [("T1", 8 * 3600, 8 * 3600 + 1200), ("T2", 23 * 3600 + 3000, 24 * 3600 + 1200),
                            ("T3", 8 * 3600, 8 * 3600 + 1200)])

  def test_skips_stops_without_time_or_position(self):
    self.assertEqual(self.db.execute("select seq from stop_times where trip_id = 'T3'").fetchall(), [(1,), (3,)])


class TestFindVehicles(FeedTestCase):
  def test_halfway_between_stops(self):
    # Friday 8:05 is halfway from A (leaves 8:00) to B (arrives 8:10)
    vehicles = self.trips_at("2026-11-20T08:05:00")
    self.assertEqual(list(vehicles), ["T1"])
    self.assertEqual((vehicles["T1"]["lat"], vehicles["T1"]["lon"]), (37.0, -121.95))
    self.assertEqual(vehicles["T1"]["label"], "1")
    self.assertEqual(vehicles["T1"]["headsign"], "THERE")
    self.assertFalse(vehicles["T1"]["rail"])

  def test_waiting_at_stop(self):
    vehicles = self.trips_at("2026-11-20T08:11:00")
    self.assertEqual((vehicles["T1"]["lat"], vehicles["T1"]["lon"]), (37.0, -121.9))

  def test_bearing(self):
    # A -> B heads east, then B -> C heads north
    self.assertEqual(self.trips_at("2026-11-20T08:05:00")["T1"]["bearing"], 90)
    self.assertEqual(self.trips_at("2026-11-20T08:15:00")["T1"]["bearing"], 0)
    self.assertEqual(main.bearing(37.1, -121.9, 37.0, -122.0), 219)
    self.assertIsNone(main.bearing(37.0, -122.0, 37.0, -122.0))

  def test_before_and_after_trip(self):
    self.assertEqual(self.trips_at("2026-11-20T07:59:59"), {})
    self.assertEqual(self.trips_at("2026-11-20T08:20:00"), {})

  def test_not_on_weekend(self):
    self.assertEqual(self.trips_at("2026-11-21T08:05:00"), {})

  def test_after_midnight_uses_yesterdays_service(self):
    # Saturday 0:05 is Friday's 24:05. T2 left A at 23:50 and gets to B at 24:10.
    vehicles = self.trips_at("2026-11-21T00:05:00")
    self.assertEqual(list(vehicles), ["T2"])
    self.assertEqual(vehicles["T2"]["lon"], -121.925)

  def test_holiday_exceptions(self):
    vehicles = self.trips_at("2026-11-26T08:10:00")
    self.assertEqual(list(vehicles), ["T3"])
    self.assertTrue(vehicles["T3"]["rail"])
    self.assertEqual((vehicles["T3"]["lat"], vehicles["T3"]["lon"]), (37.05, -121.95))

  def test_feed_info(self):
    result = main.find_vehicles(self.db, at("2026-11-20T08:05:00"))
    self.assertEqual(result["feed"], {"version": "test", "start_date": "20261101", "end_date": "20261231"})
    self.assertEqual(result["time"], "2026-11-20T08:05:00-08:00")

  def test_time_zone(self):
    vehicles = main.find_vehicles(self.db, datetime.fromisoformat("2026-11-20T16:05:00+00:00"))["vehicles"]
    self.assertEqual([v["trip_id"] for v in vehicles], ["T1"])


class TestHandler(FeedTestCase):
  def setUp(self):
    patcher = mock.patch("main.get_db", return_value=self.db)
    patcher.start()
    self.addCleanup(patcher.stop)

  def call(self, path, time=None):
    event = {"rawPath": path, "queryStringParameters": {"time": time} if time else None}
    result = main.lambda_handler(event, {})
    self.assertEqual(result["headers"], main.HEADERS)
    return result["statusCode"], json.loads(result["body"])

  def test_vehicles(self):
    status, body = self.call("/vehicles", "2026-11-20T08:05:00")
    self.assertEqual(status, 200)
    self.assertEqual([v["trip_id"] for v in body["vehicles"]], ["T1"])

  def test_vehicles_now(self):
    status, body = self.call("/vehicles")
    self.assertEqual(status, 200)
    self.assertIn("vehicles", body)

  def test_bad_time(self):
    status, body = self.call("/vehicles", "yesterday")
    self.assertEqual(status, 400)
    self.assertIn("errors", body)

  def test_not_found(self):
    status, _ = self.call("/nope")
    self.assertEqual(status, 404)

  def test_db_error(self):
    with mock.patch("main.get_db", side_effect=Exception("boom")):
      status, body = self.call("/vehicles")
    self.assertEqual((status, body), (500, {"errors": ["boom"]}))

  @mock.patch("main.save_status")
  @mock.patch("main.refresh", return_value={"updated": True})
  def test_refresh_event(self, refresh, save_status):
    self.assertEqual(main.lambda_handler({"refresh": True, "force": True}, {}), {"updated": True})
    refresh.assert_called_once_with(force=True)
    save_status.assert_called_once_with({"ok": True, "updated": True})

  @mock.patch("main.save_status")
  @mock.patch("main.refresh", side_effect=Exception("VTA is down"))
  def test_failed_refresh_saves_the_error(self, _, save_status):
    with self.assertRaises(Exception):
      main.lambda_handler({"refresh": True}, {})
    save_status.assert_called_once_with({"ok": False, "error": "VTA is down"})


class TestHealthcheck(FeedTestCase):
  NOW = at("2026-11-20T08:00:00")

  def setUp(self):
    patcher = mock.patch("main.get_db", return_value=self.db)
    patcher.start()
    self.addCleanup(patcher.stop)

  def check(self, status, now=NOW):
    with mock.patch("main.load_status", return_value=status):
      result = main.healthcheck(now)
    return result["statusCode"], json.loads(result["body"])

  def ran(self, hours_ago):
    return (self.NOW - timedelta(hours=hours_ago)).isoformat()

  def test_ok(self):
    status, body = self.check({"ok": True, "updated": False, "checked_at": self.ran(3)})
    self.assertEqual(status, 200)
    self.assertEqual(body["status"], "ok")
    self.assertEqual([c["detail"] for c in body["checks"]],
                     ["ran 3 hours ago, feed unchanged", "feed test, runs through 20261231"])

  def test_failed_refresh(self):
    status, body = self.check({"ok": False, "error": "HTTP Error 503", "checked_at": self.ran(3)})
    self.assertEqual(status, 503)
    self.assertEqual(body["checks"][0]["detail"], "failed 3 hours ago: HTTP Error 503")

  def test_missed_a_day(self):
    status, body = self.check({"ok": True, "checked_at": self.ran(30)})
    self.assertEqual(status, 503)
    self.assertEqual(body["checks"][0]["detail"], "last ran 30 hours ago")

  def test_feed_ended(self):
    status, body = self.check({"ok": True, "checked_at": self.ran(3)}, now=at("2027-01-02T08:00:00"))
    self.assertEqual(status, 503)
    self.assertEqual(body["checks"][1]["detail"], "feed test ended on 20261231")

  def test_through_the_handler(self):
    with mock.patch("main.load_status", side_effect=Exception("AccessDenied")):
      result = main.lambda_handler({"rawPath": "/healthcheck"}, {})
    self.assertEqual(result["statusCode"], 503)
    self.assertEqual(result["headers"]["Cache-Control"], "no-store")
    self.assertEqual(json.loads(result["body"])["checks"][0]["detail"], "couldn't read status.json: AccessDenied")


class TestRealFeed(unittest.TestCase):
  # Downloads the real VTA feed (about 5 MB)
  def test_build_real_feed(self):
    with tempfile.TemporaryDirectory() as tmp:
      zip_path = os.path.join(tmp, "gtfs.zip")
      db_path = os.path.join(tmp, "gtfs.sqlite")
      urlretrieve(main.GTFS_URL, zip_path)
      gtfs.build_db(zip_path, db_path)
      db = sqlite3.connect(db_path)
      try:
        self.assertGreater(db.execute("select count(*) from trips").fetchone()[0], 1000)
        self.assertGreater(db.execute("select count(*) from stop_times").fetchone()[0], 100000)
        start_date, end_date = db.execute("select start_date, end_date from feed_info").fetchone()
        # Pick a weekday 8 AM inside the feed's dates, so buses should be running
        day = datetime.strptime(start_date, "%Y%m%d")
        while day.weekday() >= 5:
          day += timedelta(days=1)
        vehicles = main.find_vehicles(db, day.replace(hour=8, tzinfo=main.PACIFIC))["vehicles"]
        self.assertGreater(len(vehicles), 50)
      finally:
        db.close()


if __name__ == "__main__":
  unittest.main()
