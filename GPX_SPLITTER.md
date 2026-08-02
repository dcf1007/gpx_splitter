# GPX Track Splitter

A streaming Python utility that extracts and splits GPX tracks while preserving the XML information associated with each track.

## Split rules

For each pair of consecutive track points, a new subtrack starts when:

1. both points have valid timestamps, time moves forwards or remains equal, and their UTC calendar dates differ;
2. both points have valid timestamps, time moves forwards, and the timestamp gap is greater than the configured threshold (one hour by default); or
3. time information is missing or invalid on either point and the great-circle distance between the points exceeds the configured threshold (10 km by default).

If time moves backwards between two consecutive points, that pair is left in the same subtrack. It does not trigger a split, even if the two timestamps have different UTC dates.

The one-hour and 10 km defaults are intended for hiking tracks. They detect long recording pauses and obvious untimed discontinuities while avoiding splits for normal short rests or GPS drift. Both thresholds are configurable.

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

The input must be either one directory containing GPX files or one or more explicit GPX files. Do not mix files and directories or provide multiple directories. Directory input is non-recursive.

```bash
python gpx_splitter.py recording.gpx
python gpx_splitter.py first.gpx second.gpx third.gpx
python gpx_splitter.py recordings_directory
```

Without `--output-dir`, each source uses a sibling `<input_name>_split_tracks/` directory. With one source and `--output-dir`, outputs are written directly there. With several sources, the selected directory contains one source-specific `<input_name>_split_tracks/` subdirectory per GPX file.

```bash
python gpx_splitter.py recordings_directory   --output-dir split_results   --time-gap-hours 1   --distance-gap-km 10
```

Replace files from a previous run:

```bash
python gpx_splitter.py recordings_directory --overwrite
```

Output names use:

```text
YYYY-MM-DD_original_track_name_subtrack_number.gpx
original_track_name_subtrack_number.gpx
```

The date is the first valid UTC timestamp in that subtrack. The date prefix is omitted when the subtrack has no valid timestamp. Numbering is continuous for tracks that resolve to the same sanitized filename.

## Tests

```bash
python -m unittest discover -s tests -v
```
