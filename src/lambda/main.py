import json
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import gtfs

GTFS_URL = "https://gtfs.vta.org/gtfs_vta.zip"
PACIFIC = ZoneInfo("America/Los_Angeles")
USER_AGENT = "vta-tracker-lambda (https://github.com/andrewmacheret/vta-tracker-lambda)"

DB_BUCKET = os.environ.get("DB_BUCKET", "")
# Bump the version when the schema in gtfs.py changes
DB_KEY = "gtfs-v1.sqlite"
LOCAL_DB = "/tmp/gtfs.sqlite"
# How often a warm Lambda checks S3 for a newer database
CHECK_SECONDS = 600
# How the last daily refresh went, for /healthcheck
STATUS_KEY = "status.json"
# The refresh runs once a day, so anything older means it missed a day
STATUS_MAX_HOURS = 26

HEADERS = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}

_db = None
_db_etag = None
_db_checked_at = 0


def lambda_handler(event, context):
  # The daily schedule sends {"refresh": true}. API Gateway events never have that key.
  if event.get("refresh"):
    return refresh_and_save_status(force=bool(event.get("force")))

  path = event.get("rawPath", "/")
  if path == "/healthcheck":
    return healthcheck()
  if path != "/vehicles":
    return response(404, {"errors": [f"Not found: {path}. Try /vehicles"]})

  try:
    now = parse_time_param(event)
  except ValueError as ex:
    return response(400, {"errors": [str(ex)]})

  try:
    return response(200, find_vehicles(get_db(), now))
  except Exception as ex:
    return response(500, {"errors": [str(ex)]})


def response(status, body):
  return {"statusCode": status, "headers": HEADERS, "body": json.dumps(body)}


def parse_time_param(event):
  value = (event.get("queryStringParameters") or {}).get("time")
  if not value:
    return datetime.now(timezone.utc)
  parsed = datetime.fromisoformat(value)
  if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=PACIFIC)
  return parsed


def active_services(db, service_date):
  ymd = service_date.strftime("%Y%m%d")
  services = {
    row[0] for row in db.execute(
      "select service_id from calendar where start_date <= ? and end_date >= ? and substr(days, ?, 1) = '1'",
      (ymd, ymd, service_date.weekday() + 1)
    )
  }
  # calendar_dates adds (1) or removes (2) service on one date, like holidays
  for service_id, exception_type in db.execute(
    "select service_id, exception_type from calendar_dates where date = ?", (ymd,)
  ):
    if exception_type == 1:
      services.add(service_id)
    else:
      services.discard(service_id)
  return services


TRIPS_SQL = """
select t.trip_id, t.headsign, r.route_id, r.label, r.name, r.color, r.text_color, r.route_type,
  a.depart_sec, a.lat, a.lon, b.arrive_sec, b.lat, b.lon
from trips t
join routes r on r.route_id = t.route_id
join stop_times a on a.trip_id = t.trip_id
  and a.seq = (select max(seq) from stop_times where trip_id = t.trip_id and depart_sec <= ?)
join stop_times b on b.trip_id = t.trip_id
  and b.seq = (select min(seq) from stop_times where trip_id = t.trip_id and seq > a.seq)
where t.service_id in ({services}) and t.start_sec <= ? and t.end_sec > ?
"""


def vehicles_at(db, services, seconds):
  if not services:
    return []
  sql = TRIPS_SQL.format(services=",".join("?" * len(services)))
  vehicles = []
  for row in db.execute(sql, (seconds, *services, seconds, seconds)):
    (trip_id, headsign, route_id, label, name, color, text_color, route_type,
     depart, lat1, lon1, arrive, lat2, lon2) = row
    # A bus waiting at a stop has seconds past its arrival, so cap progress at 1
    progress = 1 if arrive <= depart else min(1, max(0, (seconds - depart) / (arrive - depart)))
    vehicles.append({
      "trip_id": trip_id,
      "route_id": route_id,
      "label": label,
      "name": name,
      "headsign": headsign,
      "color": color,
      "text_color": text_color,
      "rail": route_type in (0, 1, 2),
      "lat": round(lat1 + (lat2 - lat1) * progress, 6),
      "lon": round(lon1 + (lon2 - lon1) * progress, 6),
      "bearing": bearing(lat1, lon1, lat2, lon2),
    })
  return vehicles


def bearing(lat1, lon1, lat2, lon2):
  """Compass degrees from the first point to the second (0 is north, 90 is east)."""
  if (lat1, lon1) == (lat2, lon2):
    return None
  # Longitude degrees get shorter away from the equator
  east = (lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
  return round(math.degrees(math.atan2(east, lat2 - lat1))) % 360


def find_vehicles(db, now):
  local = now.astimezone(PACIFIC)
  seconds = local.hour * 3600 + local.minute * 60 + local.second
  vehicles = []
  # Yesterday's late trips have times past 24:00:00, so check them too
  for days_ago in (0, 1):
    service_date = local.date() - timedelta(days=days_ago)
    vehicles += vehicles_at(db, active_services(db, service_date), seconds + days_ago * 86400)
  vehicles.sort(key=lambda v: (v["route_id"], v["trip_id"]))

  version, start_date, end_date = db.execute("select version, start_date, end_date from feed_info").fetchone()
  return {
    "time": local.isoformat(),
    "feed": {"version": version, "start_date": start_date, "end_date": end_date},
    "vehicles": vehicles,
  }


def get_db():
  global _db, _db_etag, _db_checked_at
  if _db is not None and time.time() - _db_checked_at < CHECK_SECONDS:
    return _db

  import boto3
  s3 = boto3.client("s3")
  try:
    etag = s3.head_object(Bucket=DB_BUCKET, Key=DB_KEY)["ETag"]
  except Exception:
    # Keep using the old copy if S3 has a hiccup
    if _db is not None:
      return _db
    raise

  if etag != _db_etag:
    s3.download_file(DB_BUCKET, DB_KEY, LOCAL_DB + ".new")
    if _db is not None:
      _db.close()
    os.replace(LOCAL_DB + ".new", LOCAL_DB)
    _db = sqlite3.connect(f"file:{LOCAL_DB}?mode=ro", uri=True)
    _db_etag = etag
  _db_checked_at = time.time()
  return _db


def refresh_and_save_status(force=False):
  try:
    result = refresh(force=force)
  except Exception as ex:
    save_status({"ok": False, "error": str(ex)})
    raise
  save_status({"ok": True, **result})
  return result


def save_status(status):
  import boto3
  status = {**status, "checked_at": datetime.now(timezone.utc).isoformat()}
  boto3.client("s3").put_object(Bucket=DB_BUCKET, Key=STATUS_KEY, Body=json.dumps(status),
                                ContentType="application/json")


def load_status():
  import boto3
  return json.loads(boto3.client("s3").get_object(Bucket=DB_BUCKET, Key=STATUS_KEY)["Body"].read())


def healthcheck(now=None):
  """GET /healthcheck, for the daily canary (github.com/andrewmacheret/canary).
  It's public, so it reads how the last refresh went instead of downloading the feed again."""
  now = now or datetime.now(timezone.utc)
  checks = [
    run_check("Daily timetable refresh", lambda: refresh_status(now)),
    run_check("Timetable", lambda: timetable_status(now)),
  ]
  ok = all(c["ok"] for c in checks)
  return {
    "statusCode": 200 if ok else 503,
    "headers": {**HEADERS, "Cache-Control": "no-store"},
    "body": json.dumps({"status": "ok" if ok else "fail", "checks": checks}),
  }


def run_check(name, check):
  try:
    return {"name": name, "ok": True, "detail": check()}
  except Exception as ex:
    return {"name": name, "ok": False, "detail": str(ex)}


def refresh_status(now):
  try:
    status = load_status()
  except Exception as ex:
    raise RuntimeError(f"couldn't read {STATUS_KEY}: {ex}")
  hours = (now - datetime.fromisoformat(status["checked_at"])).total_seconds() / 3600
  if not status["ok"]:
    raise RuntimeError(f"failed {hours:.0f} hours ago: {status['error']}")
  if hours > STATUS_MAX_HOURS:
    raise RuntimeError(f"last ran {hours:.0f} hours ago")
  return f"ran {hours:.0f} hours ago, {'downloaded a new feed' if status.get('updated') else 'feed unchanged'}"


def timetable_status(now):
  version, start_date, end_date = get_db().execute("select version, start_date, end_date from feed_info").fetchone()
  # Past its end date, no buses or trains show up
  if end_date < now.astimezone(PACIFIC).strftime("%Y%m%d"):
    raise RuntimeError(f"feed {version} ended on {end_date}")
  return f"feed {version}, runs through {end_date}"


def refresh(force=False):
  import boto3
  s3 = boto3.client("s3")

  with urlopen(Request(GTFS_URL, method="HEAD", headers={"User-Agent": USER_AGENT}), timeout=10) as conn:
    last_modified = conn.headers["Last-Modified"]

  if not force:
    try:
      current = s3.head_object(Bucket=DB_BUCKET, Key=DB_KEY)["Metadata"].get("source-last-modified")
    except s3.exceptions.ClientError:
      current = None
    if current == last_modified:
      return {"updated": False, "source_last_modified": last_modified}

  zip_path = "/tmp/gtfs.zip"
  build_path = "/tmp/gtfs-build.sqlite"
  with urlopen(Request(GTFS_URL, headers={"User-Agent": USER_AGENT}), timeout=60) as conn, open(zip_path, "wb") as f:
    f.write(conn.read())
  gtfs.build_db(zip_path, build_path, source_last_modified=last_modified)
  s3.upload_file(build_path, DB_BUCKET, DB_KEY, ExtraArgs={"Metadata": {"source-last-modified": last_modified}})
  os.remove(zip_path)
  os.remove(build_path)
  return {"updated": True, "source_last_modified": last_modified}
