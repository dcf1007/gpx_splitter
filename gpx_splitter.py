#!/usr/bin/env python3
"""Split GPX tracks into separate files without discarding track data.

The splitter intentionally ignores root-level waypoints and routes. Every output
contains one subtrack and retains the source GPX root metadata, the complete
track metadata, segment metadata/extensions, and every track-point child,
attribute, and extension.
"""

from __future__ import annotations

import argparse
import math
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from lxml import etree


GPX_ROOT_CHILDREN_TO_INCLUDE = {"trk"}
GPX_ROOT_NON_TRACK_GEOMETRY_TO_IGNORE = {"wpt", "rte"}
WINDOWS_RESERVED_FILENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


@dataclass
class SegmentPiece:
    """One source track-segment portion belonging to one output subtrack."""

    tag: str
    attributes: dict[str, str]
    namespace_map: dict[str | None, str]
    point_file_path: Path
    elements_before_points: tuple[bytes, ...] = ()
    elements_after_points: tuple[bytes, ...] = ()


@dataclass
class Subtrack:
    """A complete output subtrack assembled from one or more segment pieces."""

    first_timestamp: datetime | None = None
    segment_pieces: list[SegmentPiece] = field(default_factory=list)


@dataclass(frozen=True)
class RootContext:
    """Root-level GPX information copied into every generated file."""

    tag: str
    attributes: dict[str, str]
    namespace_map: dict[str | None, str]
    elements_before_tracks: tuple[bytes, ...]
    elements_after_tracks: tuple[bytes, ...]


def local_name(element: etree._Element) -> str:
    """Return an XML element's local name without its namespace."""

    return etree.QName(element).localname


def parse_gpx_timestamp(timestamp_text: str | None) -> datetime | None:
    """Parse a GPX timestamp and normalize it to UTC.

    GPX timestamps are defined as XML Schema date-times and are normally UTC.
    Naive timestamps are therefore treated as UTC. Invalid values are retained
    in the output but returned as ``None`` for split decisions.
    """

    if not timestamp_text or not timestamp_text.strip():
        return None

    normalized_text = timestamp_text.strip()
    if normalized_text.endswith(("Z", "z")):
        normalized_text = f"{normalized_text[:-1]}+00:00"

    try:
        parsed_timestamp = datetime.fromisoformat(normalized_text)
    except ValueError:
        return None

    if parsed_timestamp.tzinfo is None:
        parsed_timestamp = parsed_timestamp.replace(tzinfo=timezone.utc)

    return parsed_timestamp.astimezone(timezone.utc)


def calculate_distance_km(
    first_coordinate: tuple[float, float] | None,
    second_coordinate: tuple[float, float] | None,
) -> float | None:
    """Calculate the great-circle distance between two latitude/longitude pairs."""

    if first_coordinate is None or second_coordinate is None:
        return None

    first_latitude, first_longitude = map(math.radians, first_coordinate)
    second_latitude, second_longitude = map(math.radians, second_coordinate)

    latitude_difference = second_latitude - first_latitude
    longitude_difference = second_longitude - first_longitude
    haversine_value = (
        math.sin(latitude_difference / 2) ** 2
        + math.cos(first_latitude)
        * math.cos(second_latitude)
        * math.sin(longitude_difference / 2) ** 2
    )

    # Clamp for floating-point rounding near antipodal coordinates.
    central_angle = 2 * math.asin(min(1.0, math.sqrt(haversine_value)))
    return 6371.0088 * central_angle


def sanitize_filename_component(value: str) -> str:
    """Create a portable filename component while preserving readable Unicode."""

    invalid_characters = '<>:"/\\|?*\0'
    sanitized_value = "".join(
        "_" if character in invalid_characters or ord(character) < 32 else character
        for character in value.strip()
    )
    sanitized_value = " ".join(sanitized_value.split()).rstrip(" .")

    if not sanitized_value:
        sanitized_value = "unnamed_track"
    if sanitized_value.upper() in WINDOWS_RESERVED_FILENAMES:
        sanitized_value = f"{sanitized_value}_"

    # Leave room for the date, sequence number, extension, and filesystem limits.
    return sanitized_value[:180].rstrip(" .") or "unnamed_track"


def read_root_context(input_path: Path) -> RootContext:
    """Read root metadata while deferring included tracks to the split pass."""

    root_element: etree._Element | None = None
    root_tag = ""
    root_attributes: dict[str, str] = {}
    root_namespace_map: dict[str | None, str] = {}
    elements_before_tracks: list[bytes] = []
    elements_after_tracks: list[bytes] = []
    current_top_level_name: str | None = None
    has_seen_track = False

    for event, element in etree.iterparse(
        str(input_path),
        events=("start", "end"),
        huge_tree=True,
        remove_blank_text=False,
    ):
        if event == "start":
            if root_element is None:
                root_element = element
                root_tag = element.tag
                root_attributes = dict(element.attrib)
                root_namespace_map = dict(element.nsmap)
            elif element.getparent() is root_element:
                current_top_level_name = local_name(element)
                if current_top_level_name in GPX_ROOT_CHILDREN_TO_INCLUDE:
                    has_seen_track = True
            continue

        parent = element.getparent()
        if parent is root_element:
            element_name = local_name(element)
            is_included_track = element_name in GPX_ROOT_CHILDREN_TO_INCLUDE
            is_ignored_non_track_geometry = (
                element_name in GPX_ROOT_NON_TRACK_GEOMETRY_TO_IGNORE
            )

            # Track elements are processed in the second streaming pass. Waypoints
            # and routes are intentionally ignored. Any other root child is metadata
            # or extension information and is copied into every generated GPX.
            if not is_included_track and not is_ignored_non_track_geometry:
                serialized_element = etree.tostring(
                    element,
                    encoding="utf-8",
                    with_tail=False,
                )
                if has_seen_track:
                    elements_after_tracks.append(serialized_element)
                else:
                    elements_before_tracks.append(serialized_element)

            element.clear(keep_tail=True)
            while element.getprevious() is not None:
                del parent[0]
            current_top_level_name = None
        elif (
            current_top_level_name in GPX_ROOT_CHILDREN_TO_INCLUDE
            or current_top_level_name in GPX_ROOT_NON_TRACK_GEOMETRY_TO_IGNORE
        ):
            # The first pass does not need waypoint, route, or track internals.
            # Clear them as they close so one enormous track cannot fill memory.
            element.clear(keep_tail=True)
            if parent is not None:
                while element.getprevious() is not None:
                    del parent[0]

    if root_element is None:
        raise ValueError("The input file does not contain an XML root element.")

    return RootContext(
        tag=root_tag,
        attributes=root_attributes,
        namespace_map=root_namespace_map,
        elements_before_tracks=tuple(elements_before_tracks),
        elements_after_tracks=tuple(elements_after_tracks),
    )


def create_segment_piece(
    temporary_directory: Path,
    segment_tag: str,
    segment_attributes: dict[str, str],
    segment_namespace_map: dict[str | None, str],
) -> tuple[SegmentPiece, BinaryIO]:
    """Create a disk-backed point stream for one segment piece."""

    point_file = tempfile.NamedTemporaryFile(
        mode="w+b",
        prefix="segment_points_",
        suffix=".xml",
        dir=temporary_directory,
        delete=False,
    )
    point_file.write(b"<points>")

    return (
        SegmentPiece(
            tag=segment_tag,
            attributes=dict(segment_attributes),
            namespace_map=dict(segment_namespace_map),
            point_file_path=Path(point_file.name),
        ),
        point_file,
    )


def close_point_file(point_file: BinaryIO | None) -> None:
    """Finish a temporary point document if it is currently open."""

    if point_file is not None and not point_file.closed:
        point_file.write(b"</points>")
        point_file.close()


def write_subtrack(
    output_path: Path,
    root_context: RootContext,
    track_tag: str,
    track_attributes: dict[str, str],
    track_namespace_map: dict[str | None, str],
    track_elements_before_segments: tuple[bytes, ...],
    track_elements_after_segments: tuple[bytes, ...],
    subtrack: Subtrack,
    overwrite: bool,
) -> None:
    """Assemble one complete GPX output from its disk-backed segment pieces."""

    if output_path.exists() and not overwrite:
        raise FileExistsError(
            f"Output file already exists: {output_path}. Use --overwrite to replace it."
        )

    temporary_output_path = output_path.with_suffix(f"{output_path.suffix}.partial")

    try:
        with etree.xmlfile(str(temporary_output_path), encoding="UTF-8") as xml_writer:
            xml_writer.write_declaration()
            with xml_writer.element(
                root_context.tag,
                root_context.attributes,
                nsmap=root_context.namespace_map,
            ):
                for serialized_element in root_context.elements_before_tracks:
                    xml_writer.write(etree.fromstring(serialized_element))

                with xml_writer.element(
                    track_tag,
                    track_attributes,
                    nsmap=track_namespace_map,
                ):
                    for serialized_element in track_elements_before_segments:
                        xml_writer.write(etree.fromstring(serialized_element))

                    for segment_piece in subtrack.segment_pieces:
                        with xml_writer.element(
                            segment_piece.tag,
                            segment_piece.attributes,
                            nsmap=segment_piece.namespace_map,
                        ):
                            for serialized_element in segment_piece.elements_before_points:
                                xml_writer.write(etree.fromstring(serialized_element))

                            # Points were spooled to disk while streaming the large
                            # source file. Reparse one point at a time to keep memory
                            # bounded when producing the final GPX.
                            for _, point_element in etree.iterparse(
                                str(segment_piece.point_file_path),
                                events=("end",),
                                tag="{*}trkpt",
                                huge_tree=True,
                                remove_blank_text=False,
                            ):
                                xml_writer.write(point_element)
                                point_element.clear(keep_tail=True)
                                point_parent = point_element.getparent()
                                if point_parent is not None:
                                    while point_element.getprevious() is not None:
                                        del point_parent[0]

                            for serialized_element in segment_piece.elements_after_points:
                                xml_writer.write(etree.fromstring(serialized_element))

                    for serialized_element in track_elements_after_segments:
                        xml_writer.write(etree.fromstring(serialized_element))

                for serialized_element in root_context.elements_after_tracks:
                    xml_writer.write(etree.fromstring(serialized_element))

        temporary_output_path.replace(output_path)
    except Exception:
        temporary_output_path.unlink(missing_ok=True)
        raise


def split_gpx_tracks(
    input_path: Path,
    output_directory: Path,
    time_gap_hours: float = 1.0,
    distance_gap_km: float = 10.0,
    overwrite: bool = False,
) -> list[Path]:
    """Split every GPX track according to date, timestamp gap, or distance gap."""

    if time_gap_hours <= 0:
        raise ValueError("time_gap_hours must be greater than zero.")
    if distance_gap_km <= 0:
        raise ValueError("distance_gap_km must be greater than zero.")

    input_path = input_path.expanduser().resolve()
    output_directory = output_directory.expanduser().resolve()

    if not input_path.is_file():
        raise FileNotFoundError(f"Input GPX file does not exist: {input_path}")

    output_directory.mkdir(parents=True, exist_ok=True)
    root_context = read_root_context(input_path)
    time_gap_seconds = time_gap_hours * 3600
    written_files: list[Path] = []
    next_sequence_by_track_name: dict[str, int] = {}
    malformed_timestamp_count = 0
    invalid_coordinate_count = 0
    track_count = 0

    with tempfile.TemporaryDirectory(prefix="gpx_splitter_") as temporary_directory_name:
        temporary_directory = Path(temporary_directory_name)
        root_element: etree._Element | None = None
        current_track_element: etree._Element | None = None
        current_segment_element: etree._Element | None = None

        track_tag = ""
        track_attributes: dict[str, str] = {}
        track_namespace_map: dict[str | None, str] = {}
        track_elements_before_segments: list[bytes] = []
        track_elements_after_segments: list[bytes] = []
        track_name: str | None = None
        has_seen_segment = False
        subtracks: list[Subtrack] = []
        current_subtrack: Subtrack | None = None

        segment_tag = ""
        segment_attributes: dict[str, str] = {}
        segment_namespace_map: dict[str | None, str] = {}
        segment_elements_before_points: list[bytes] = []
        segment_elements_after_points: list[bytes] = []
        segment_pieces: list[SegmentPiece] = []
        segment_has_seen_point = False
        current_segment_piece: SegmentPiece | None = None
        current_point_file: BinaryIO | None = None

        previous_timestamp: datetime | None = None
        previous_coordinate: tuple[float, float] | None = None

        try:
            for event, element in etree.iterparse(
                str(input_path),
                events=("start", "end"),
                huge_tree=True,
                remove_blank_text=False,
            ):
                if event == "start":
                    if root_element is None:
                        root_element = element
                        continue

                    parent = element.getparent()
                    if (
                        parent is root_element
                        and local_name(element) in GPX_ROOT_CHILDREN_TO_INCLUDE
                    ):
                        track_count += 1
                        current_track_element = element
                        track_tag = element.tag
                        track_attributes = dict(element.attrib)
                        track_namespace_map = dict(element.nsmap)
                        track_elements_before_segments = []
                        track_elements_after_segments = []
                        track_name = None
                        has_seen_segment = False
                        subtracks = []
                        current_subtrack = None
                        previous_timestamp = None
                        previous_coordinate = None
                    elif (
                        current_track_element is not None
                        and parent is current_track_element
                        and local_name(element) == "trkseg"
                    ):
                        current_segment_element = element
                        segment_tag = element.tag
                        segment_attributes = dict(element.attrib)
                        segment_namespace_map = dict(element.nsmap)
                        segment_elements_before_points = []
                        segment_elements_after_points = []
                        segment_pieces = []
                        segment_has_seen_point = False
                        current_segment_piece = None
                        current_point_file = None
                        has_seen_segment = True
                    continue

                # Ignore and clear root-level waypoints/routes. Tracks are handled
                # separately, and global metadata was captured during the first pass.
                if current_track_element is None:
                    if element is not root_element:
                        parent = element.getparent()
                        element.clear(keep_tail=True)
                        if parent is not None:
                            while element.getprevious() is not None:
                                del parent[0]
                    continue

                parent = element.getparent()

                if (
                    current_segment_element is not None
                    and parent is current_segment_element
                    and local_name(element) == "trkpt"
                ):
                    timestamp_element = next(
                        (
                            child
                            for child in element
                            if isinstance(child.tag, str) and local_name(child) == "time"
                        ),
                        None,
                    )
                    raw_timestamp = (
                        timestamp_element.text if timestamp_element is not None else None
                    )
                    current_timestamp = parse_gpx_timestamp(raw_timestamp)
                    if raw_timestamp and current_timestamp is None:
                        malformed_timestamp_count += 1

                    try:
                        latitude = float(element.attrib["lat"])
                        longitude = float(element.attrib["lon"])
                        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                            raise ValueError
                        current_coordinate: tuple[float, float] | None = (
                            latitude,
                            longitude,
                        )
                    except (KeyError, ValueError):
                        current_coordinate = None
                        invalid_coordinate_count += 1

                    should_split = False
                    if previous_timestamp is not None and current_timestamp is not None:
                        timestamp_difference_seconds = (
                            current_timestamp - previous_timestamp
                        ).total_seconds()

                        # Backward-moving timestamps are retained as-is. They do not
                        # trigger either the date-change rule or the time-gap rule.
                        if timestamp_difference_seconds >= 0:
                            should_split = (
                                current_timestamp.date() != previous_timestamp.date()
                                or timestamp_difference_seconds > time_gap_seconds
                            )
                    elif previous_coordinate is not None and current_coordinate is not None:
                        distance_km = calculate_distance_km(
                            previous_coordinate,
                            current_coordinate,
                        )
                        should_split = (
                            distance_km is not None and distance_km > distance_gap_km
                        )

                    if should_split:
                        close_point_file(current_point_file)
                        current_point_file = None
                        current_segment_piece = None
                        current_subtrack = None

                    if current_subtrack is None:
                        current_subtrack = Subtrack()
                        subtracks.append(current_subtrack)

                    if current_segment_piece is None:
                        current_segment_piece, current_point_file = create_segment_piece(
                            temporary_directory,
                            segment_tag,
                            segment_attributes,
                            segment_namespace_map,
                        )
                        current_subtrack.segment_pieces.append(current_segment_piece)
                        segment_pieces.append(current_segment_piece)

                    if current_subtrack.first_timestamp is None and current_timestamp is not None:
                        current_subtrack.first_timestamp = current_timestamp

                    assert current_point_file is not None
                    current_point_file.write(
                        etree.tostring(element, encoding="utf-8", with_tail=False)
                    )

                    segment_has_seen_point = True
                    previous_timestamp = current_timestamp
                    previous_coordinate = current_coordinate

                    element.clear(keep_tail=True)
                    while element.getprevious() is not None:
                        del parent[0]
                    continue

                if current_segment_element is not None and element is current_segment_element:
                    close_point_file(current_point_file)
                    current_point_file = None

                    # Preserve empty source segments by attaching them to the
                    # current subtrack (or creating the track's first subtrack).
                    if not segment_has_seen_point:
                        if current_subtrack is None:
                            current_subtrack = Subtrack()
                            subtracks.append(current_subtrack)
                        current_segment_piece, current_point_file = create_segment_piece(
                            temporary_directory,
                            segment_tag,
                            segment_attributes,
                            segment_namespace_map,
                        )
                        close_point_file(current_point_file)
                        current_point_file = None
                        current_subtrack.segment_pieces.append(current_segment_piece)
                        segment_pieces.append(current_segment_piece)

                    before_points = tuple(segment_elements_before_points)
                    after_points = tuple(segment_elements_after_points)
                    for segment_piece in segment_pieces:
                        segment_piece.elements_before_points = before_points
                        segment_piece.elements_after_points = after_points

                    current_segment_element = None
                    current_segment_piece = None
                    element.clear(keep_tail=True)
                    while element.getprevious() is not None:
                        del parent[0]
                    continue

                if current_segment_element is not None and parent is current_segment_element:
                    serialized_element = etree.tostring(
                        element,
                        encoding="utf-8",
                        with_tail=False,
                    )
                    if segment_has_seen_point:
                        segment_elements_after_points.append(serialized_element)
                    else:
                        segment_elements_before_points.append(serialized_element)

                    element.clear(keep_tail=True)
                    while element.getprevious() is not None:
                        del parent[0]
                    continue

                if element is current_track_element:
                    if not subtracks:
                        # A track with no segments still produces one metadata-only GPX.
                        subtracks.append(Subtrack())

                    original_track_name = track_name or f"unnamed_track_{track_count}"
                    filename_track_name = sanitize_filename_component(original_track_name)
                    next_sequence_number = next_sequence_by_track_name.get(
                        filename_track_name,
                        1,
                    )

                    for subtrack in subtracks:
                        output_sequence_number = next_sequence_number
                        next_sequence_number += 1
                        date_prefix = (
                            f"{subtrack.first_timestamp.date().isoformat()}_"
                            if subtrack.first_timestamp is not None
                            else ""
                        )
                        output_path = output_directory / (
                            f"{date_prefix}{filename_track_name}_"
                            f"{output_sequence_number}.gpx"
                        )

                        write_subtrack(
                            output_path=output_path,
                            root_context=root_context,
                            track_tag=track_tag,
                            track_attributes=track_attributes,
                            track_namespace_map=track_namespace_map,
                            track_elements_before_segments=tuple(
                                track_elements_before_segments
                            ),
                            track_elements_after_segments=tuple(
                                track_elements_after_segments
                            ),
                            subtrack=subtrack,
                            overwrite=overwrite,
                        )
                        written_files.append(output_path)

                    next_sequence_by_track_name[filename_track_name] = next_sequence_number
                    current_track_element = None
                    element.clear(keep_tail=True)
                    if parent is not None:
                        while element.getprevious() is not None:
                            del parent[0]
                    continue

                if parent is current_track_element and local_name(element) != "trkseg":
                    serialized_element = etree.tostring(
                        element,
                        encoding="utf-8",
                        with_tail=False,
                    )
                    if not has_seen_segment:
                        track_elements_before_segments.append(serialized_element)
                    else:
                        track_elements_after_segments.append(serialized_element)

                    if track_name is None and local_name(element) == "name":
                        candidate_name = "".join(element.itertext()).strip()
                        if candidate_name:
                            track_name = candidate_name

                    element.clear(keep_tail=True)
                    while element.getprevious() is not None:
                        del parent[0]
        finally:
            close_point_file(current_point_file)

    if malformed_timestamp_count:
        print(
            f"Warning: retained {malformed_timestamp_count} malformed timestamp(s); "
            "those points were treated as untimed for split decisions.",
            file=sys.stderr,
        )
    if invalid_coordinate_count:
        print(
            f"Warning: retained {invalid_coordinate_count} point(s) with missing or "
            "invalid coordinates; distance splitting could not use those points.",
            file=sys.stderr,
        )

    return written_files


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Split GPX tracks when the UTC date changes, timestamps are far apart, "
            "or untimed consecutive points are separated by a massive distance."
        )
    )
    parser.add_argument("input_gpx", type=Path, help="Path to the source GPX file.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "Directory for generated files. Defaults to "
            "<input_name>_split_tracks beside the input file."
        ),
    )
    parser.add_argument(
        "--time-gap-hours",
        type=float,
        default=1.0,
        help="Split timed points when the gap exceeds this many hours (default: 1).",
    )
    parser.add_argument(
        "--distance-gap-km",
        type=float,
        default=10.0,
        help=(
            "When either consecutive point lacks a valid timestamp, split if their "
            "distance exceeds this value in kilometres (default: 10)."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace generated GPX files that already exist.",
    )
    arguments = parser.parse_args()

    output_directory = arguments.output_dir or arguments.input_gpx.with_name(
        f"{arguments.input_gpx.stem}_split_tracks"
    )

    try:
        written_files = split_gpx_tracks(
            input_path=arguments.input_gpx,
            output_directory=output_directory,
            time_gap_hours=arguments.time_gap_hours,
            distance_gap_km=arguments.distance_gap_km,
            overwrite=arguments.overwrite,
        )
    except (OSError, ValueError, etree.XMLSyntaxError) as error:
        parser.exit(status=1, message=f"Error: {error}\n")

    print(f"Created {len(written_files)} GPX file(s) in {output_directory.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
