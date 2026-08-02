# GPX Track Merger

`merge_gpx.py` reads every root-level GPX track from all supplied GPX files, validates their chronology, sorts them globally, and merges adjacent tracks when both their time gap and endpoint distance are within configured limits.

The script writes one GPX file. Tracks that match are combined into one `<trk>`. Every original `<trkseg>` remains a separate `<trkseg>` in its original order.

## Basic usage

Merge tracks from several files:

```bash
python merge_gpx.py track1.gpx track2.gpx track3.gpx
```

The default output is:

```text
merged.gpx
```

Choose another output path:

```bash
python merge_gpx.py track1.gpx track2.gpx --output results/combined.gpx
```

Read every GPX file in one directory:

```bash
python merge_gpx.py recordings --output combined.gpx
```

The input must be either one directory or one or more explicit GPX files. Mixed inputs, multiple directories, and recursive traversal are rejected.

## Processing rules

The script performs these operations in order:

1. Discovers all requested GPX files.
2. Parses all root-level `<trk>` elements from every input file.
3. Requires every track to contain at least one `<trkseg>` and at least one `<trkpt>`.
4. Requires every track point to have valid latitude, longitude, and timestamp data.
5. Requires timestamps inside each track to be nondecreasing in document order.
6. Calculates each track's first timestamp, last timestamp, first coordinate, and last coordinate.
7. Sorts all tracks by start timestamp, then end timestamp, then source filename and source track position.
8. Rejects any time overlap between sorted tracks.
9. Groups adjacent tracks when both the time-gap and distance-gap rules match.
10. Writes the sorted groups to one GPX document.

All input files must use the same GPX root namespace and GPX version. Mixing incompatible GPX versions or namespaces aborts the operation.

## Merge conditions

Two adjacent tracks merge only when both conditions are true:

- the second track starts no more than `--max-time-gap-hours` after the first track ends;
- the distance from the first track's final point to the second track's first point is no more than `--max-distance-gap-km`.

Defaults:

```text
--max-time-gap-hours 1
--max-distance-gap-km 10
```

Example:

```bash
python merge_gpx.py recordings --max-time-gap-hours 0.5 --max-distance-gap-km 2
```

A zero time or distance limit is allowed. Tracks whose intervals touch at exactly the same timestamp are not considered overlapping and may merge when the spatial gap also matches.

Tracks that do not match remain separate `<trk>` elements in the output, but they are still written in chronological order.

## Segment preservation

Original segment boundaries are never flattened.

If the first source track contains two segments and the second contains one:

```xml
<trk>
  <trkseg id="first-a">...</trkseg>
  <trkseg id="first-b">...</trkseg>
</trk>
<trk>
  <trkseg id="second-a">...</trkseg>
</trk>
```

and the tracks match, the output contains:

```xml
<trk>
  <trkseg id="first-a">...</trkseg>
  <trkseg id="first-b">...</trkseg>
  <trkseg id="second-a">...</trkseg>
</trk>
```

Each source `<trkseg>` is deep-copied with its:

- tag and namespace;
- attributes;
- child ordering;
- track points;
- elevations and timestamps;
- segment extensions;
- point extensions;
- other custom children.

A track that is not merged with another track is copied as a complete `<trk>`.

## Track and document metadata preservation

A GPX document and a GPX track can each have only one normal metadata structure. When several tracks or files are combined, their metadata cannot all remain simultaneously in the same standard GPX positions.

The merger therefore uses this policy:

- the first chronologically sorted track supplies the normal track-level metadata for a merged track;
- all original source-track attributes and non-segment children are additionally preserved under a custom merge namespace inside the merged track's `<extensions>`;
- the first input document supplies the output root element and primary `<metadata>`;
- all source waypoints and routes are copied into the output as normal GPX `<wpt>` and `<rte>` elements;
- all root-extension children are copied into the output root `<extensions>`;
- all source-document root attributes, metadata, and extension structures are additionally preserved under the custom merge namespace.

The custom namespace is:

```text
https://github.com/dcf1007/gpx_splitter/merge-gpx/1
```

This preserves the XML data semantically. XML formatting, indentation, namespace-prefix placement, and attribute serialization are not guaranteed to remain byte-for-byte identical.

## Time-overlap errors

Any overlap aborts the complete operation before an output file is written.

For example, these intervals overlap:

```text
Track A: 2026-07-01T10:00:00Z — 2026-07-01T11:00:00Z
Track B: 2026-07-01T10:30:00Z — 2026-07-01T12:00:00Z
```

The error identifies both source files, track names, track indexes, intervals, and overlap duration:

```text
Error: Tracks overlap in time; merge aborted. first.gpx / Track A (track 1)
[2026-07-01T10:00:00+00:00 — 2026-07-01T11:00:00+00:00]
overlaps second.gpx / Track B (track 1)
[2026-07-01T10:30:00+00:00 — 2026-07-01T12:00:00+00:00]
by 1800 second(s).
```

An interval that starts exactly when the previous interval ends is allowed.

## Strict timestamp validation

Because global sorting and overlap detection depend on reliable time ranges, every `<trkpt>` must contain a valid `<time>` value.

The script aborts when:

- a track point has no timestamp;
- a timestamp is malformed;
- timestamps move backward inside one track;
- a track has no segments;
- a track has no points;
- coordinates are missing or invalid.

The input files are never modified.

## Atomic output

The result is first written to a temporary `.partial` file and parsed again. The temporary file replaces the requested output only after successful validation.

The script refuses to use an input GPX file as its output path, even with `--overwrite`.

## Command-line options

```text
--output PATH
--max-time-gap-hours HOURS
--max-distance-gap-km KILOMETRES
--overwrite
--version
```

Show the internal version:

```bash
python merge_gpx.py --version
```

Expected output:

```text
merge_gpx.py 1
```
