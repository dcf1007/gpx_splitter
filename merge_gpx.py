#!/usr/bin/env python3
"""Sort and merge chronologically adjacent GPX tracks without changing segments."""

from __future__ import annotations

import argparse
import copy
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from lxml import etree

VERSION = 1
EARTH_RADIUS_KM = 6_371.0088
MERGE_NAMESPACE = "https://github.com/dcf1007/gpx_splitter/merge-gpx/1"
DEFAULT_MAX_TIME_GAP_HOURS = 1.0
DEFAULT_MAX_DISTANCE_GAP_KM = 10.0


@dataclass(frozen=True, slots=True)
class TrackRecord:
    """One source GPX track with validated chronological boundaries."""

    source_path: Path
    source_track_index: int
    element: etree._Element
    start_time: datetime
    end_time: datetime
    start_coordinate: tuple[float, float]
    end_coordinate: tuple[float, float]
    point_count: int

    @property
    def name(self) -> str:
        for child in self.element:
            if isinstance(child.tag, str) and local_name(child) == "name":
                value = "".join(child.itertext()).strip()
                if value:
                    return value
        return f"track {self.source_track_index}"

    @property
    def label(self) -> str:
        return f"{self.source_path.name} / {self.name} (track {self.source_track_index})"


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """A parsed source GPX document and its root element."""

    path: Path
    tree: etree._ElementTree

    @property
    def root(self) -> etree._Element:
        return self.tree.getroot()


def local_name(element: etree._Element) -> str:
    """Return an element's local XML name without its namespace."""

    return etree.QName(element).localname


def qualified_name(parent: etree._Element, name: str) -> str:
    """Create a tag in the same namespace as *parent*."""

    namespace = etree.QName(parent).namespace
    return f"{{{namespace}}}{name}" if namespace else name


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO-8601 GPX timestamp and normalize it to UTC."""

    if not value or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith(("Z", "z")):
        normalized = normalized[:-1] + "+00:00"
    try:
        timestamp = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def direct_child(element: etree._Element, name: str) -> etree._Element | None:
    """Return the first direct child with the requested local name."""

    return next(
        (
            child
            for child in element
            if isinstance(child.tag, str) and local_name(child) == name
        ),
        None,
    )


def point_timestamp(point: etree._Element) -> datetime:
    """Return a valid timestamp or raise a descriptive validation error."""

    time_element = direct_child(point, "time")
    timestamp = parse_timestamp(time_element.text if time_element is not None else None)
    if timestamp is None:
        raise ValueError("track point has no valid <time>")
    return timestamp


def point_coordinate(point: etree._Element) -> tuple[float, float]:
    """Return a valid latitude/longitude pair or raise a validation error."""

    try:
        latitude = float(point.attrib["lat"])
        longitude = float(point.attrib["lon"])
    except (KeyError, ValueError) as error:
        raise ValueError("track point has invalid latitude/longitude attributes") from error
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("track point coordinates are outside valid ranges")
    return latitude, longitude


def iter_track_points(track: etree._Element) -> Iterable[etree._Element]:
    """Yield points in source segment and document order."""

    for segment in track:
        if not isinstance(segment.tag, str) or local_name(segment) != "trkseg":
            continue
        for child in segment:
            if isinstance(child.tag, str) and local_name(child) == "trkpt":
                yield child


def validate_track(
    source_path: Path,
    track: etree._Element,
    track_index: int,
) -> TrackRecord:
    """Validate one track and calculate its chronological and spatial boundaries."""

    segments = [
        child
        for child in track
        if isinstance(child.tag, str) and local_name(child) == "trkseg"
    ]
    if not segments:
        raise ValueError(
            f"{source_path}: track {track_index} contains no <trkseg> elements; "
            "its chronology and merge boundaries cannot be determined"
        )

    points = list(iter_track_points(track))
    if not points:
        raise ValueError(
            f"{source_path}: track {track_index} contains no <trkpt> elements; "
            "its chronology and merge boundaries cannot be determined"
        )

    timestamps: list[datetime] = []
    for point_index, point in enumerate(points, start=1):
        try:
            timestamp = point_timestamp(point)
            point_coordinate(point)
        except ValueError as error:
            raise ValueError(
                f"{source_path}: track {track_index}, point {point_index}: {error}"
            ) from error
        if timestamps and timestamp < timestamps[-1]:
            raise ValueError(
                f"{source_path}: track {track_index} moves backward in time at "
                f"point {point_index}: {timestamp.isoformat()} is earlier than "
                f"{timestamps[-1].isoformat()}"
            )
        timestamps.append(timestamp)

    return TrackRecord(
        source_path=source_path,
        source_track_index=track_index,
        element=track,
        start_time=timestamps[0],
        end_time=timestamps[-1],
        start_coordinate=point_coordinate(points[0]),
        end_coordinate=point_coordinate(points[-1]),
        point_count=len(points),
    )


def parse_source(path: Path) -> SourceDocument:
    """Parse one GPX document without removing whitespace or extension content."""

    try:
        tree = etree.parse(
            str(path),
            etree.XMLParser(remove_blank_text=False, huge_tree=True),
        )
    except etree.XMLSyntaxError as error:
        raise ValueError(f"Invalid GPX/XML in {path}: {error}") from error
    root = tree.getroot()
    if not isinstance(root.tag, str) or local_name(root) != "gpx":
        raise ValueError(f"{path}: root element is not <gpx>")
    return SourceDocument(path=path, tree=tree)


def discover_gpx_files(inputs: Sequence[Path], recursive: bool) -> list[Path]:
    """Resolve GPX files from explicit files and directories."""

    files: set[Path] = set()
    for input_path in inputs:
        path = input_path.expanduser()
        if path.is_file() and path.suffix.lower() == ".gpx":
            files.add(path.resolve())
        elif path.is_dir():
            pattern = "**/*.gpx" if recursive else "*.gpx"
            files.update(file.resolve() for file in path.glob(pattern))
    return sorted(files)


def read_all_tracks(paths: Sequence[Path]) -> tuple[list[SourceDocument], list[TrackRecord]]:
    """Parse compatible documents and validate every root-level GPX track."""

    documents: list[SourceDocument] = []
    tracks: list[TrackRecord] = []
    expected_namespace: str | None = None
    expected_version: str | None = None
    for path in paths:
        document = parse_source(path)
        namespace = etree.QName(document.root).namespace
        version = document.root.attrib.get("version")
        if not documents:
            expected_namespace = namespace
            expected_version = version
        elif namespace != expected_namespace or version != expected_version:
            raise ValueError(
                f"{path}: incompatible GPX root. Expected namespace "
                f"{expected_namespace!r} and version {expected_version!r}, found "
                f"namespace {namespace!r} and version {version!r}"
            )
        documents.append(document)
        source_tracks = [
            child
            for child in document.root
            if isinstance(child.tag, str) and local_name(child) == "trk"
        ]
        for index, track in enumerate(source_tracks, start=1):
            tracks.append(validate_track(path, track, index))

    if not tracks:
        raise ValueError("No root-level GPX <trk> elements were found")
    return documents, tracks


def track_sort_key(track: TrackRecord) -> tuple[datetime, datetime, str, int]:
    """Sort tracks by start, end, source file, and source order."""

    return (
        track.start_time,
        track.end_time,
        str(track.source_path).casefold(),
        track.source_track_index,
    )


def format_interval(track: TrackRecord) -> str:
    return f"[{track.start_time.isoformat()} — {track.end_time.isoformat()}]"


def reject_overlapping_tracks(tracks: Sequence[TrackRecord]) -> None:
    """Abort when any two globally sorted track intervals overlap."""

    if not tracks:
        return
    latest = tracks[0]
    for current in tracks[1:]:
        if current.start_time < latest.end_time:
            overlap_seconds = (latest.end_time - current.start_time).total_seconds()
            raise ValueError(
                "Tracks overlap in time; merge aborted. "
                f"{latest.label} {format_interval(latest)} overlaps "
                f"{current.label} {format_interval(current)} by "
                f"{overlap_seconds:.0f} second(s)."
            )
        if current.end_time > latest.end_time:
            latest = current


def distance_km(
    first: tuple[float, float],
    second: tuple[float, float],
) -> float:
    """Calculate the great-circle distance between two coordinates."""

    first_latitude, first_longitude = map(math.radians, first)
    second_latitude, second_longitude = map(math.radians, second)
    latitude_difference = second_latitude - first_latitude
    longitude_difference = second_longitude - first_longitude
    haversine_value = (
        math.sin(latitude_difference / 2) ** 2
        + math.cos(first_latitude)
        * math.cos(second_latitude)
        * math.sin(longitude_difference / 2) ** 2
    )
    central_angle = 2 * math.asin(min(1.0, math.sqrt(haversine_value)))
    return EARTH_RADIUS_KM * central_angle


def tracks_match(
    previous: TrackRecord,
    current: TrackRecord,
    maximum_time_gap_seconds: float,
    maximum_distance_gap_km: float,
) -> bool:
    """Return whether two adjacent non-overlapping tracks should be merged."""

    time_gap_seconds = (current.start_time - previous.end_time).total_seconds()
    spatial_gap_km = distance_km(previous.end_coordinate, current.start_coordinate)
    return (
        0 <= time_gap_seconds <= maximum_time_gap_seconds
        and spatial_gap_km <= maximum_distance_gap_km
    )


def group_tracks(
    tracks: Sequence[TrackRecord],
    maximum_time_gap_hours: float,
    maximum_distance_gap_km: float,
) -> list[list[TrackRecord]]:
    """Group sorted tracks when both time and space are continuous."""

    maximum_time_gap_seconds = maximum_time_gap_hours * 3600
    groups: list[list[TrackRecord]] = []
    for track in tracks:
        if not groups:
            groups.append([track])
            continue
        previous = groups[-1][-1]
        if tracks_match(
            previous,
            track,
            maximum_time_gap_seconds,
            maximum_distance_gap_km,
        ):
            groups[-1].append(track)
        else:
            groups.append([track])
    return groups


def merge_namespace_map(root: etree._Element) -> dict[str | None, str]:
    """Add a stable namespace prefix for lossless merge provenance."""

    namespace_map = dict(root.nsmap)
    prefix = "merge"
    counter = 2
    while prefix in namespace_map and namespace_map[prefix] != MERGE_NAMESPACE:
        prefix = f"merge{counter}"
        counter += 1
    namespace_map[prefix] = MERGE_NAMESPACE
    return namespace_map


def merge_tag(name: str) -> str:
    return f"{{{MERGE_NAMESPACE}}}{name}"


def ensure_extensions(parent: etree._Element) -> etree._Element:
    """Return or create a GPX <extensions> child before any segment children."""

    existing = direct_child(parent, "extensions")
    if existing is not None:
        return existing
    extensions = etree.Element(qualified_name(parent, "extensions"))
    first_segment_index = next(
        (
            index
            for index, child in enumerate(parent)
            if isinstance(child.tag, str) and local_name(child) == "trkseg"
        ),
        len(parent),
    )
    parent.insert(first_segment_index, extensions)
    return extensions


def append_attribute_snapshot(container: etree._Element, element: etree._Element) -> None:
    """Preserve source element attributes without remapping namespaces."""

    for name, value in element.attrib.items():
        attribute = etree.SubElement(container, merge_tag("attribute"))
        attribute.set("name", name)
        attribute.set("value", value)


def append_track_provenance(
    merged_track: etree._Element,
    source_tracks: Sequence[TrackRecord],
) -> None:
    """Store every source track's non-segment data inside GPX extensions."""

    extensions = ensure_extensions(merged_track)
    sources = etree.SubElement(extensions, merge_tag("sourceTracks"))
    for record in source_tracks:
        source = etree.SubElement(sources, merge_tag("sourceTrack"))
        source.set("file", record.source_path.name)
        source.set("index", str(record.source_track_index))
        source.set("start", record.start_time.isoformat())
        source.set("end", record.end_time.isoformat())
        append_attribute_snapshot(source, record.element)
        metadata = etree.SubElement(source, merge_tag("trackMetadata"))
        for child in record.element:
            if isinstance(child.tag, str) and local_name(child) == "trkseg":
                continue
            metadata.append(copy.deepcopy(child))


def build_output_track(group: Sequence[TrackRecord]) -> etree._Element:
    """Build one output track while preserving every source segment intact."""

    if len(group) == 1:
        return copy.deepcopy(group[0].element)

    merged_track = copy.deepcopy(group[0].element)
    for child in list(merged_track):
        if isinstance(child.tag, str) and local_name(child) == "trkseg":
            merged_track.remove(child)

    append_track_provenance(merged_track, group)
    for record in group:
        for child in record.element:
            if isinstance(child.tag, str) and local_name(child) == "trkseg":
                merged_track.append(copy.deepcopy(child))
    return merged_track


def append_document_provenance(
    root_extensions: etree._Element,
    documents: Sequence[SourceDocument],
) -> None:
    """Preserve source-level metadata and extensions from every input document."""

    sources = etree.SubElement(root_extensions, merge_tag("sourceDocuments"))
    for document in documents:
        source = etree.SubElement(sources, merge_tag("sourceDocument"))
        source.set("file", document.path.name)
        append_attribute_snapshot(source, document.root)
        for child in document.root:
            if not isinstance(child.tag, str):
                source.append(copy.deepcopy(child))
                continue
            if local_name(child) in {"wpt", "rte", "trk"}:
                continue
            source.append(copy.deepcopy(child))


def create_output_tree(
    documents: Sequence[SourceDocument],
    groups: Sequence[Sequence[TrackRecord]],
) -> etree._ElementTree:
    """Create one GPX document containing sorted and optionally merged tracks."""

    first_root = documents[0].root
    output_root = etree.Element(
        first_root.tag,
        dict(first_root.attrib),
        nsmap=merge_namespace_map(first_root),
    )

    primary_metadata_added = False
    for document in documents:
        for child in document.root:
            if not isinstance(child.tag, str):
                if document is documents[0]:
                    output_root.append(copy.deepcopy(child))
                continue
            name = local_name(child)
            if name == "metadata":
                if not primary_metadata_added:
                    output_root.append(copy.deepcopy(child))
                    primary_metadata_added = True
            elif name in {"wpt", "rte"}:
                output_root.append(copy.deepcopy(child))

    for group in groups:
        output_root.append(build_output_track(group))

    root_extensions = etree.SubElement(
        output_root,
        qualified_name(output_root, "extensions"),
    )
    for document in documents:
        source_extensions = direct_child(document.root, "extensions")
        if source_extensions is not None:
            for child in source_extensions:
                root_extensions.append(copy.deepcopy(child))
    append_document_provenance(root_extensions, documents)
    return etree.ElementTree(output_root)


def write_output(
    tree: etree._ElementTree,
    output_path: Path,
    source_doctype: str,
    overwrite: bool,
) -> None:
    """Write atomically so a failed operation never leaves a partial GPX."""

    if output_path.exists() and not overwrite:
        raise FileExistsError(
            f"Output file already exists: {output_path}. Use --overwrite to replace it."
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".partial")
    try:
        tree.write(
            str(temporary_path),
            encoding="UTF-8",
            xml_declaration=True,
            pretty_print=False,
            doctype=source_doctype or None,
        )
        etree.parse(str(temporary_path), etree.XMLParser(huge_tree=True))
        temporary_path.replace(output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def merge_gpx_files(
    input_paths: Sequence[Path],
    output_path: Path,
    maximum_time_gap_hours: float = DEFAULT_MAX_TIME_GAP_HOURS,
    maximum_distance_gap_km: float = DEFAULT_MAX_DISTANCE_GAP_KM,
    recursive: bool = False,
    overwrite: bool = False,
) -> tuple[Path, list[list[TrackRecord]]]:
    """Read, validate, sort, group, and write tracks from all input GPX files."""

    if maximum_time_gap_hours < 0:
        raise ValueError("maximum_time_gap_hours must be zero or greater")
    if maximum_distance_gap_km < 0:
        raise ValueError("maximum_distance_gap_km must be zero or greater")

    resolved_output_path = output_path.expanduser().resolve()
    files = discover_gpx_files(input_paths, recursive)
    if not files:
        raise ValueError("No GPX input files were found")
    if resolved_output_path in files:
        raise ValueError(
            f"Output path {resolved_output_path} is also an input GPX file; "
            "refusing to overwrite source data"
        )

    documents, tracks = read_all_tracks(files)
    sorted_tracks = sorted(tracks, key=track_sort_key)
    reject_overlapping_tracks(sorted_tracks)
    groups = group_tracks(
        sorted_tracks,
        maximum_time_gap_hours,
        maximum_distance_gap_km,
    )
    output_tree = create_output_tree(documents, groups)
    write_output(
        output_tree,
        resolved_output_path,
        documents[0].tree.docinfo.doctype,
        overwrite,
    )
    return resolved_output_path, groups


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Sort all tracks from all input GPX files and merge adjacent tracks "
            "when both their time gap and spatial gap are within configured limits."
        )
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="GPX files or directories")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("merged.gpx"),
        help="Output GPX path (default: merged.gpx)",
    )
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument(
        "--max-time-gap-hours",
        type=float,
        default=DEFAULT_MAX_TIME_GAP_HOURS,
        help="Maximum forward gap for merging (default: 1 hour)",
    )
    parser.add_argument(
        "--max-distance-gap-km",
        type=float,
        default=DEFAULT_MAX_DISTANCE_GAP_KM,
        help="Maximum endpoint distance for merging (default: 10 km)",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    arguments = parser.parse_args(argv)
    try:
        output_path, groups = merge_gpx_files(
            input_paths=arguments.inputs,
            output_path=arguments.output,
            maximum_time_gap_hours=arguments.max_time_gap_hours,
            maximum_distance_gap_km=arguments.max_distance_gap_km,
            recursive=arguments.recursive,
            overwrite=arguments.overwrite,
        )
    except (OSError, ValueError, etree.XMLSyntaxError) as error:
        parser.exit(1, f"Error: {error}\n")

    track_count = sum(len(group) for group in groups)
    merged_group_count = sum(len(group) > 1 for group in groups)
    print(
        f"Read {track_count} track(s); wrote {len(groups)} output track(s); "
        f"merged {merged_group_count} group(s)."
    )
    for group_index, group in enumerate(groups, start=1):
        labels = ", ".join(track.label for track in group)
        print(f"  output track {group_index}: {labels}")
    print(f"Output: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
