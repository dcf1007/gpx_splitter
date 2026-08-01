# GPX Tools

This repository contains two Python command-line tools for working with GPX tracks.

## Scripts

### GPX Track Splitter

`gpx_splitter.py` extracts GPX tracks and splits them when there is a date change, a long forward time gap, or a large untimed geographic gap. It preserves track, segment, point, namespace, metadata, and extension information.

[Read the GPX splitter documentation](GPX_SPLITTER.md)

### GPX Trail Identifier

`identify_trails.py` analyzes GPX geometry and uses live OpenStreetMap data from Overpass to rank nearby hiking routes and named landmarks. It writes CSV, JSON, GeoJSON, and interactive HTML reports.

[Read the trail identifier documentation](TRAIL_IDENTIFIER.md)

## Installation

Python 3.10 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
```
