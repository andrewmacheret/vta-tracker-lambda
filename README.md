# vta-tracker-lambda

[![Test and deploy](https://github.com/andrewmacheret/vta-tracker-lambda/actions/workflows/deploy.yml/badge.svg)](https://github.com/andrewmacheret/vta-tracker-lambda/actions/workflows/deploy.yml) [![Canary](https://github.com/andrewmacheret/vta-tracker-lambda/actions/workflows/canary.yml/badge.svg)](https://github.com/andrewmacheret/vta-tracker-lambda/actions/workflows/canary.yml) [![License](https://img.shields.io/badge/license-MIT-lightgray.svg)](https://github.com/andrewmacheret/vta-tracker-lambda/blob/master/LICENSE.md)

Shows where [VTA](https://www.vta.org/) buses and light rail trains should be right now. It reads VTA's published timetable ([GTFS](https://gtfs.vta.org/)) and estimates each vehicle's position between its last stop and its next stop. These are timetable estimates, not live GPS.

See it running at [andrewmacheret.com/projects/vta-tracker](https://andrewmacheret.com/projects/vta-tracker/).

## How it works

- `www/` is the web page. It uses [Leaflet](https://leafletjs.com/) with [OpenStreetMap](https://www.openstreetmap.org/) tiles and asks the API for vehicles every 15 seconds.
- `src/lambda/` is a Python AWS Lambda called `VTATracker`, behind API Gateway at `vta-tracker.andrewmacheret.com`.
  - `GET /vehicles` returns the estimated vehicles. Add `?time=2026-11-20T08:00:00` to see another time (Pacific time unless the value has an offset).
  - When called with `{"refresh": true}`, it downloads VTA's GTFS zip, turns it into a SQLite file with `gtfs.py`, and saves it to the private S3 bucket `andrewmacheret-vta-tracker`. It skips the rebuild if VTA's file hasn't changed, unless `"force": true` is passed. An EventBridge rule sends this once a day.
  - API requests download that SQLite file to `/tmp` and check S3 for a newer copy every 10 minutes.

## Development

Run tests with `cd src/lambda && python3 -m unittest` (needs Python 3.14). One test downloads the real VTA feed.

To try the page locally, serve `www/` (for example `python3 -m http.server -d www`) and open it with `?api=<your API URL>`.

## Health check

Once a day, the Canary workflow (`.github/workflows/canary.yml`) checks the page and its scripts and styles (including Leaflet), OpenStreetMap's tiles, and the API's own health check, which fails if the daily timetable refresh failed or didn't run, or the timetable has expired. It's started by [andrewmacheret/canary](https://github.com/andrewmacheret/canary).

## Deploy

Pushes to `master` run the tests, then GitHub Actions deploys the Lambda, rebuilds the database, and uploads `www/` to `s3://andrewmacheret.com/projects/vta-tracker` (see `.github/workflows/deploy.yml`). To redeploy without a code change, use "Run workflow" on the Actions tab.

The workflow signs in to AWS with GitHub OIDC using the IAM role in the `AWS_ROLE_ARN` repo variable.
