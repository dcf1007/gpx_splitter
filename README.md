# GPX Track Splitter

A streaming Python utility that extracts and splits GPX tracks while preserving the XML information associated with each track.

## Split rules

For each pair of consecutive track points, a new subtrack starts when:

1. both points have valid timestamps and their UTC calendar dates differ;
2. both points have valid timestamps and the timestamp gap is greater than the configured threshold (six hours by default);
3. time information is missing or invalid on either point and the great-circle distance between the points exceeds the configured threshold (100 km by default); or
4. both timestamps are valid but time moves backwards.

The 100 km default is deliberately conservative. It is large enough to avoid splitting most legitimate sparse tracks, while still detecting the kind of discontinuity normally caused by unrelated recording sessions or corrupted untimed data. Both thresholds are configurable.

## Preserved information

Every generated file contains:

- the source GPX root attributes and namespace declarations;
- root-level metadata, extensions, and other non-waypoint/non-route/non-track elements;
- all track attributes and track-level elements, including extensions;
- original track-segment boundaries, attributes, metadata, and extensions;
- every track-point attribute, child element, and extension.

Waypoints and routes are intentionally excluded because this tool operates exclusively on tracks. The original `<trk><name>` value is not modified; only the output filename is sanitized when required by the filesystem.

The parser uses two bounded-memory passes over the source file. Track points are temporarily spooled to disk so that even segment-level extensions appearing after the points can be copied into every split segment. Temporary disk usage can therefore approach the size of the track currently being processed.

## Installation

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
```

Python 3.10 or newer is required.

## Usage

```bash
python gpx_splitter.py massive_file.gpx
```

By default, files are written to `massive_file_split_tracks/` beside the input file.

Custom thresholds and output directory:

```bash
python gpx_splitter.py massive_file.gpx \
  --output-dir split_tracks \
  --time-gap-hours 4 \
  --distance-gap-km 75
```

Replace files from a previous run:

```bash
python gpx_splitter.py massive_file.gpx --overwrite
```

Output names use:

```text
YYYY-MM-DD_original_track_name_subtrack_number.gpx
original_track_name_subtrack_number.gpx
```

The date is the first valid UTC timestamp in that subtrack. The date prefix is omitted when the subtrack has no valid timestamp. Numbering is continuous for tracks that resolve to the same sanitized filename, preventing accidental collisions when a GPX contains duplicate track names.

## Tests

```bash
python -m unittest discover -s tests -v
```
