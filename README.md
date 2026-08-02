# GPX Tools

This repository contains three Python command-line tools for working with GPX tracks.

## Scripts

### GPX Track Splitter

`gpx_splitter.py` extracts GPX tracks and splits them when there is a date change, a long forward time gap, or a large untimed geographic gap. It preserves track, segment, point, namespace, metadata, and extension information.

[Read the GPX splitter documentation](GPX_SPLITTER.md)

### GPX Track Merger

`merge_gpx.py` reads every track from all supplied GPX files, sorts them chronologically, rejects overlapping time ranges, and merges adjacent tracks when both their time and spatial gaps match. Original tracks remain separate GPX segments inside each merged track.

[Read the GPX merger documentation](MERGE_GPX.md)

### GPX Trail Identifier

`identify_trails.py` analyzes a single track per GPX file, uses live OpenStreetMap data from Overpass to rank nearby hiking routes and named landmarks, and writes CSV, JSON, GeoJSON, and interactive HTML reports. Every successfully analyzed track receives a renamed enriched GPX copy. Visited highlights and parking areas are added as waypoints, and highlight descriptions are added when highlights are present.

[Read the trail identifier documentation](TRAIL_IDENTIFIER.md)

## Installation

Python 3.10 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
```
