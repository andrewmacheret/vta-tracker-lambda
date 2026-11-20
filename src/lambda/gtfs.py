import csv
import io
import os
import sqlite3
import zipfile

SCHEMA = """
create table feed_info (version text, start_date text, end_date text, source_last_modified text);
create table routes (
  route_id text primary key, label text, name text, color text, text_color text, route_type integer
);
-- days is 7 characters of 0 or 1, Monday first
create table calendar (service_id text, start_date text, end_date text, days text);
create table calendar_dates (service_id text, date text, exception_type integer);
create table trips (
  trip_id text primary key, route_id text, service_id text, headsign text, start_sec integer, end_sec integer
);
create index trips_by_service on trips (service_id, start_sec);
create table stop_times (
  trip_id text, seq integer, arrive_sec integer, depart_sec integer, lat real, lon real,
  primary key (trip_id, seq)
) without rowid;
"""

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_time(value):
  # GTFS times go past 24:00:00 for trips that run after midnight
  hours, minutes, seconds = value.strip().split(":")
  return int(hours) * 3600 + int(minutes) * 60 + int(seconds)


def route_label(route):
  # Short map labels, like "Blue Line" -> "Blue" and "Rapid 500" -> "500"
  label = route["route_short_name"].removesuffix(" Line")
  for prefix in ("Rapid ", "Express ", "School "):
    label = label.removeprefix(prefix)
  return label or route["route_id"]


def read_csv(zip_file, name):
  if name not in zip_file.namelist():
    return
  with zip_file.open(name) as f:
    yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))


def build_db(zip_path, db_path, source_last_modified=None):
  if os.path.exists(db_path):
    os.remove(db_path)
  db = sqlite3.connect(db_path)
  db.executescript(SCHEMA)

  with zipfile.ZipFile(zip_path) as z:
    for row in read_csv(z, "feed_info.txt"):
      db.execute(
        "insert into feed_info values (?, ?, ?, ?)",
        (row.get("feed_version"), row.get("feed_start_date"), row.get("feed_end_date"), source_last_modified)
      )

    db.executemany("insert into routes values (?, ?, ?, ?, ?, ?)", (
      (
        r["route_id"], route_label(r), r.get("route_long_name", ""),
        "#" + (r.get("route_color") or "29588c"), "#" + (r.get("route_text_color") or "FFFFFF"),
        int(r.get("route_type") or 3)
      )
      for r in read_csv(z, "routes.txt")
    ))

    db.executemany("insert into calendar values (?, ?, ?, ?)", (
      (r["service_id"], r["start_date"], r["end_date"], "".join(r[day] for day in DAYS))
      for r in read_csv(z, "calendar.txt")
    ))

    db.executemany("insert into calendar_dates values (?, ?, ?)", (
      (r["service_id"], r["date"], int(r["exception_type"]))
      for r in read_csv(z, "calendar_dates.txt")
    ))

    stops = {
      s["stop_id"]: (float(s["stop_lat"]), float(s["stop_lon"]))
      for s in read_csv(z, "stops.txt")
      if s.get("stop_lat") and s.get("stop_lon")
    }

    # Skip stops with no time. Buses just go in a straight line past them.
    spans = {}
    rows = []
    for st in read_csv(z, "stop_times.txt"):
      if not st["arrival_time"] or not st["departure_time"] or st["stop_id"] not in stops:
        continue
      arrive = parse_time(st["arrival_time"])
      depart = parse_time(st["departure_time"])
      lat, lon = stops[st["stop_id"]]
      rows.append((st["trip_id"], int(st["stop_sequence"]), arrive, depart, lat, lon))
      start, end = spans.get(st["trip_id"], (depart, arrive))
      spans[st["trip_id"]] = (min(start, depart), max(end, arrive))
    db.executemany("insert into stop_times values (?, ?, ?, ?, ?, ?)", rows)

    db.executemany("insert into trips values (?, ?, ?, ?, ?, ?)", (
      (t["trip_id"], t["route_id"], t["service_id"], t.get("trip_headsign", ""), *spans[t["trip_id"]])
      for t in read_csv(z, "trips.txt")
      if t["trip_id"] in spans
    ))

  db.commit()
  db.execute("analyze")
  db.execute("vacuum")
  db.close()
