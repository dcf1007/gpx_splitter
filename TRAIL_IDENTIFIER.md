# GPX Trail Identifier

`identify_trails.py` analyzes one or more GPX tracks and attempts to identify the most likely known hiking route or named destination near each track.

The script is intentionally self-contained. All Python code is in `identify_trails.py`, and the internal version is `1`.

## Processing flow

For each GPX file, the script performs the following steps:

1. Streams the GPX XML and extracts the track name, coordinates, elevations, and timestamps.
2. Calculates distance, duration, elevation range, elevation gain and loss, start/end separation, track closure, and timestamp-order diagnostics.
3. Builds a padded bounding box around the GPX geometry.
4. Sends a live query to OpenStreetMap through an Overpass API endpoint.
5. Retrieves nearby hiking route relations and named geographic or tourism features.
6. Measures how much of the GPX lies close to each route and how close the GPX passes to each landmark.
7. Ranks the resulting route and landmark candidates.
8. Writes summary and detailed reports.

There is no local trail catalog, response cache, mocked runtime data, or offline fallback. If all configured Overpass endpoints fail, the command exits with an error and does not silently create partial identification results.

## Basic usage

Analyze one GPX file:

```bash
python identify_trails.py track.gpx
```

Analyze several files:

```bash
python identify_trails.py track.gpx track1.gpx "ACTIVE LOG 001.gpx"
```

Analyze all GPX files in a directory:

```bash
python identify_trails.py path/to/gpx_directory
```

Include subdirectories:

```bash
python identify_trails.py path/to/gpx_directory --recursive
```

Show the internal version:

```bash
python identify_trails.py --version
```

The expected output is:

```text
identify_trails.py 1
```

## Output directory

By default, reports are written to `trail_analysis/` in the current working directory.

Use a different directory with:

```bash
python identify_trails.py track.gpx --output-dir results
```

The script creates:

- `trail_identification.csv` — one summary row per GPX file;
- `trail_identification.json` — complete statistics, candidates, landmarks, tags, warnings, and API metadata;
- `trail_identification.geojson` — GPX lines and nearby landmark points;
- `trail_identification.html` — an interactive Leaflet map using OpenStreetMap background tiles.

The HTML report requires an internet connection when opened because it loads Leaflet and map tiles from public services.

## Matching behavior

### Hiking routes

The script requests OSM relations where:

- `type=route`; and
- `route` is `hiking`, `foot`, or `walking`.

It samples the GPX track, measures the distance from each sample to the OSM route geometry, and combines:

- the percentage of samples within the route matching radius; and
- the median distance from the GPX to the route.

The default route matching radius is 70 metres.

### Named landmarks

The query also requests named features such as:

- springs, cave entrances, water features, peaks, saddles, rocks, and cliffs;
- waterfalls;
- viewpoints, attractions, information points, picnic sites, and huts;
- nature reserves;
- parking areas;
- villages, hamlets, and localities.

A landmark becomes a candidate when the GPX passes within its visit radius. The default visit radius is 180 metres. Peaks, villages, and hamlets use at least 300 metres because their mapped point may not coincide with the exact visited location.

A landmark candidate identifies a named place the track appears to visit. It does not necessarily represent the formal name of the complete trail.

## Command-line options

```text
--recursive
--output-dir PATH
--overpass-url URL
--route-match-radius-m METRES
--landmark-visit-radius-m METRES
--query-padding-m METRES
--timeout-seconds SECONDS
```

`--overpass-url` may be repeated to define a custom failover order. Without it, the script uses its built-in public endpoints in order.

All radius, padding, and timeout values must be greater than zero.

## Console diagnostics

For each GPX file, the command prints:

- the best match or `unmatched`;
- confidence and score when a match exists;
- track distance and point count;
- the Overpass endpoint that returned the data;
- the number of candidates and landmarks found;
- the OSM base timestamp;
- warnings for malformed, missing, reverse, or mixed timestamps and for open tracks.

An `unmatched` result after a successful live query means no candidate reached the minimum score. It is different from an API failure, which terminates the command with an error.

## Limitations

Results depend on the quality and completeness of current OpenStreetMap data. A valid track may remain unmatched when:

- the route is not represented by a named OSM hiking relation;
- the relevant path or landmark is unnamed;
- the route geometry is incomplete;
- several named routes share the same path;
- GPS error or dense terrain moves the recording outside the configured radius;
- the GPX contains only a short access section of a longer route.

The score is a geometric ranking, not proof of the route's official identity. Review the candidates, landmarks, and interactive map when the result is ambiguous.
