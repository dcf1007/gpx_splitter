#!/usr/bin/env python3
"""Identify likely trails and visited highlights from GPX tracks."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from lxml import etree

VERSION = 1
EARTH_RADIUS_M = 6_371_008.8
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
USER_AGENT = (
    f"gpx-splitter-trail-identifier/{VERSION} "
    "(+https://github.com/dcf1007/gpx_splitter)"
)
HIGHLIGHT_CATEGORIES = {
    "natural=cave_entrance",
    "natural=spring",
    "natural=water",
    "natural=peak",
    "natural=saddle",
    "natural=rock",
    "natural=cliff",
    "waterway=waterfall",
    "tourism=viewpoint",
    "tourism=attraction",
    "tourism=information",
    "tourism=picnic_site",
    "tourism=wilderness_hut",
    "tourism=alpine_hut",
    "leisure=nature_reserve",
}


@dataclass(frozen=True, slots=True)
class TrackPoint:
    latitude: float
    longitude: float
    elevation_m: float | None
    timestamp: datetime | None

    @property
    def coordinate(self) -> tuple[float, float]:
        return self.latitude, self.longitude


@dataclass(slots=True)
class TrackAnalysis:
    file_name: str
    track_name: str
    points: list[TrackPoint]
    distance_km: float
    start_end_distance_m: float
    closed: bool
    duration_minutes: float | None
    elevation_min_m: float | None
    elevation_max_m: float | None
    elevation_gain_m: float | None
    elevation_loss_m: float | None
    malformed_timestamps: int
    forward_timestamps: int
    backward_timestamps: int
    equal_timestamps: int
    bounds: tuple[float, float, float, float]

    @property
    def point_count(self) -> int:
        return len(self.points)

    @property
    def timed_point_count(self) -> int:
        return sum(point.timestamp is not None for point in self.points)

    @property
    def timestamp_order(self) -> str:
        if self.backward_timestamps and not self.forward_timestamps:
            return "reverse"
        if self.backward_timestamps:
            return "mixed"
        return "forward" if self.timed_point_count else "missing"


@dataclass(slots=True)
class Landmark:
    identifier: str
    name: str
    category: str
    latitude: float
    longitude: float
    distance_m: float
    visited: bool
    visit_radius_m: float
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Candidate:
    identifier: str
    name: str
    source: str
    score: float
    confidence: str
    coverage_percent: float | None = None
    median_distance_m: float | None = None
    osm_relation_id: int | None = None
    tags: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Identification:
    analysis: TrackAnalysis
    best_match: Candidate | None
    candidates: list[Candidate]
    landmarks: list[Landmark]
    warnings: list[str]
    overpass_endpoint: str
    osm_timestamp: str | None


# GPX parsing -----------------------------------------------------------------


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_timestamp(value: str | None) -> datetime | None:
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


def direct_child_text(element: etree._Element, name: str) -> str | None:
    for child in element:
        if isinstance(child.tag, str) and local_name(child.tag) == name:
            return child.text
    return None


def haversine_m(
    first: tuple[float, float], second: tuple[float, float]
) -> float:
    lat1, lon1 = map(math.radians, first)
    lat2, lon2 = map(math.radians, second)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    value = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(value)))


def read_single_track(path: Path) -> tuple[str, list[TrackPoint], int]:
    """Read exactly one GPX track. Multiple segments are allowed."""

    track_count = 0
    track_name: str | None = None
    points: list[TrackPoint] = []
    malformed_timestamps = 0

    try:
        with path.open("rb") as source:
            events = etree.iterparse(
                source,
                events=("start", "end"),
                huge_tree=True,
                remove_blank_text=False,
            )
            for event, element in events:
                if not isinstance(element.tag, str):
                    continue
                name = local_name(element.tag)

                if event == "start":
                    if name == "trk":
                        track_count += 1
                        if track_count > 1:
                            raise ValueError(
                                f"Multiple GPX tracks found in {path}; "
                                "trail identification requires exactly one "
                                "<trk> element"
                            )
                    continue

                parent = element.getparent()
                if name == "name" and track_name is None:
                    if parent is not None and local_name(parent.tag) == "trk":
                        track_name = "".join(element.itertext()).strip() or None
                    continue
                if name != "trkpt":
                    continue

                try:
                    latitude = float(element.attrib["lat"])
                    longitude = float(element.attrib["lon"])
                except (KeyError, ValueError) as error:
                    raise ValueError(
                        f"Invalid track-point coordinates in {path}"
                    ) from error
                if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                    raise ValueError(
                        f"Out-of-range track-point coordinates in {path}"
                    )

                elevation_text = direct_child_text(element, "ele")
                timestamp_text = direct_child_text(element, "time")
                try:
                    elevation = float(elevation_text) if elevation_text else None
                except ValueError:
                    elevation = None
                timestamp = parse_timestamp(timestamp_text)
                if timestamp_text and timestamp is None:
                    malformed_timestamps += 1
                points.append(
                    TrackPoint(latitude, longitude, elevation, timestamp)
                )

                element.clear(keep_tail=True)
                if parent is not None:
                    while element.getprevious() is not None:
                        del parent[0]
    except etree.XMLSyntaxError as error:
        raise ValueError(f"Invalid GPX/XML in {path}: {error}") from error

    if track_count == 0:
        raise ValueError(f"No GPX <trk> element found in {path}")
    if not points:
        raise ValueError(f"No GPX track points found in {path}")
    return track_name or path.stem, points, malformed_timestamps


def analyze_track(path: Path) -> TrackAnalysis:
    track_name, points, malformed = read_single_track(path)
    distance_m = sum(
        haversine_m(previous.coordinate, current.coordinate)
        for previous, current in zip(points, points[1:])
    )
    start_end_m = haversine_m(points[0].coordinate, points[-1].coordinate)
    closed_threshold_m = max(50.0, min(250.0, distance_m * 0.03))

    timestamps = [point.timestamp for point in points if point.timestamp]
    forward = backward = equal = 0
    for previous, current in zip(timestamps, timestamps[1:]):
        if current > previous:
            forward += 1
        elif current < previous:
            backward += 1
        else:
            equal += 1
    duration = (
        (max(timestamps) - min(timestamps)).total_seconds() / 60
        if timestamps
        else None
    )

    elevations = [
        point.elevation_m
        for point in points
        if point.elevation_m is not None
    ]
    gain = loss = None
    if elevations:
        gain = loss = 0.0
        previous_elevation: float | None = None
        for point in points:
            if point.elevation_m is None:
                continue
            if previous_elevation is not None:
                difference = point.elevation_m - previous_elevation
                gain += max(difference, 0.0)
                loss += max(-difference, 0.0)
            previous_elevation = point.elevation_m

    latitudes = [point.latitude for point in points]
    longitudes = [point.longitude for point in points]
    return TrackAnalysis(
        file_name=path.name,
        track_name=track_name,
        points=points,
        distance_km=distance_m / 1000,
        start_end_distance_m=start_end_m,
        closed=start_end_m <= closed_threshold_m,
        duration_minutes=duration,
        elevation_min_m=min(elevations) if elevations else None,
        elevation_max_m=max(elevations) if elevations else None,
        elevation_gain_m=gain,
        elevation_loss_m=loss,
        malformed_timestamps=malformed,
        forward_timestamps=forward,
        backward_timestamps=backward,
        equal_timestamps=equal,
        bounds=(
            min(latitudes),
            min(longitudes),
            max(latitudes),
            max(longitudes),
        ),
    )


def discover_gpx_files(inputs: Sequence[Path], recursive: bool) -> list[Path]:
    files: set[Path] = set()
    for path in inputs:
        if path.is_file() and path.suffix.lower() == ".gpx":
            files.add(path.resolve())
        elif path.is_dir():
            pattern = "**/*.gpx" if recursive else "*.gpx"
            files.update(item.resolve() for item in path.glob(pattern))
    return sorted(files)


# OpenStreetMap matching -------------------------------------------------------


def confidence(score: float) -> str:
    for threshold, label in (
        (0.85, "high"),
        (0.68, "medium-high"),
        (0.50, "medium"),
        (0.30, "low"),
    ):
        if score >= threshold:
            return label
    return "very-low"


def padded_bbox(
    bounds: tuple[float, float, float, float], padding_m: float
) -> str:
    south, west, north, east = bounds
    latitude = (south + north) / 2
    lat_padding = padding_m / 111_320
    lon_padding = padding_m / max(
        1.0, 111_320 * math.cos(math.radians(latitude))
    )
    values = (
        south - lat_padding,
        west - lon_padding,
        north + lat_padding,
        east + lon_padding,
    )
    return ",".join(f"{value:.7f}" for value in values)


def overpass_query(
    bounds: tuple[float, float, float, float], padding_m: float
) -> str:
    bbox = padded_bbox(bounds, padding_m)
    return f'''[out:json][timeout:90];
(
  relation["type"="route"]["route"~"^(hiking|foot|walking)$"]({bbox});
  nwr["name"]["natural"~"^(spring|cave_entrance|water|peak|saddle|rock|cliff)$"]({bbox});
  nwr["name"]["waterway"="waterfall"]({bbox});
  nwr["name"]["tourism"~"^(viewpoint|attraction|information|picnic_site|wilderness_hut|alpine_hut)$"]({bbox});
  nwr["name"]["leisure"="nature_reserve"]({bbox});
  nwr["name"]["amenity"="parking"]({bbox});
  nwr["name"]["place"~"^(village|hamlet|locality)$"]({bbox});
);
out geom tags center;'''


def request_osm(
    analysis: TrackAnalysis,
    urls: Sequence[str],
    padding_m: float,
    timeout_seconds: float,
) -> tuple[dict[str, Any], str]:
    if not urls:
        raise ValueError("At least one Overpass URL is required")
    body = urllib.parse.urlencode(
        {"data": overpass_query(analysis.bounds, padding_m)}
    ).encode("utf-8")
    errors: list[str] = []
    for index, url in enumerate(urls):
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": (
                    "application/x-www-form-urlencoded; charset=UTF-8"
                ),
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=timeout_seconds
            ) as response:
                data = json.loads(response.read())
            if not isinstance(data, dict) or not isinstance(
                data.get("elements"), list
            ):
                raise ValueError("response has no Overpass elements array")
            return data, url
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            errors.append(f"{url}: {error}")
            if index + 1 < len(urls):
                time.sleep(1)
    raise RuntimeError("All Overpass requests failed: " + " | ".join(errors))


def project(
    latitude: float, longitude: float, reference_latitude: float
) -> tuple[float, float]:
    return (
        math.radians(longitude)
        * EARTH_RADIUS_M
        * math.cos(math.radians(reference_latitude)),
        math.radians(latitude) * EARTH_RADIUS_M,
    )


def point_segment_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    px, py = point
    sx, sy = start
    ex, ey = end
    dx = ex - sx
    dy = ey - sy
    if dx == dy == 0:
        return math.hypot(px - sx, py - sy)
    fraction = ((px - sx) * dx + (py - sy) * dy) / (dx * dx + dy * dy)
    fraction = min(1.0, max(0.0, fraction))
    return math.hypot(px - (sx + fraction * dx), py - (sy + fraction * dy))


def sampled_points(
    points: Sequence[TrackPoint], maximum: int = 240
) -> list[TrackPoint]:
    if len(points) <= maximum:
        return list(points)
    step = (len(points) - 1) / (maximum - 1)
    indexes = sorted({round(index * step) for index in range(maximum)})
    return [points[index] for index in indexes]


def feature_coordinate(element: dict[str, Any]) -> tuple[float, float] | None:
    latitude = element.get("lat")
    longitude = element.get("lon")
    if latitude is None or longitude is None:
        center = element.get("center") or {}
        latitude = center.get("lat")
        longitude = center.get("lon")
    if latitude is None or longitude is None:
        geometry = element.get("geometry") or []
        if geometry:
            latitude = sum(
                float(point["lat"]) for point in geometry
            ) / len(geometry)
            longitude = sum(
                float(point["lon"]) for point in geometry
            ) / len(geometry)
    if latitude is None or longitude is None:
        return None
    return float(latitude), float(longitude)


def feature_category(tags: dict[str, str]) -> str:
    for key in (
        "natural",
        "waterway",
        "tourism",
        "leisure",
        "amenity",
        "place",
    ):
        if key in tags:
            return f"{key}={tags[key]}"
    return "named_feature"


def nearest_track_distance(
    points: Iterable[TrackPoint], coordinate: tuple[float, float]
) -> float:
    return min(haversine_m(point.coordinate, coordinate) for point in points)


def route_segments(
    element: dict[str, Any], reference_latitude: float
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    segments = []
    for member in element.get("members") or []:
        geometry = [
            project(
                float(point["lat"]),
                float(point["lon"]),
                reference_latitude,
            )
            for point in member.get("geometry") or []
            if "lat" in point and "lon" in point
        ]
        segments.extend(zip(geometry, geometry[1:]))
    return segments


def route_candidate(
    element: dict[str, Any],
    tags: dict[str, str],
    track: Sequence[tuple[float, float]],
    reference_latitude: float,
    match_radius_m: float,
) -> Candidate | None:
    segments = route_segments(element, reference_latitude)
    if not segments:
        return None
    distances = [
        min(
            point_segment_distance(point, start, end)
            for start, end in segments
        )
        for point in track
    ]
    coverage = sum(
        distance <= match_radius_m for distance in distances
    ) / len(distances)
    median = statistics.median(distances)
    proximity = max(
        0.0,
        1.0 - median / max(match_radius_m * 3, 1.0),
    )
    score = 0.8 * coverage + 0.2 * proximity
    relation_id = element.get("id")
    return Candidate(
        identifier=f"osm_relation_{relation_id}",
        name=(
            tags.get("name")
            or tags.get("ref")
            or f"OSM route {relation_id}"
        ),
        source="openstreetmap_route",
        score=score,
        confidence=confidence(score),
        coverage_percent=coverage * 100,
        median_distance_m=median,
        osm_relation_id=(
            int(relation_id) if relation_id is not None else None
        ),
        tags=tags,
        notes=[
            f"{coverage * 100:.1f}% of sampled points are within "
            f"{match_radius_m:.0f} m of the route"
        ],
    )


def landmark_from_element(
    element: dict[str, Any],
    tags: dict[str, str],
    analysis: TrackAnalysis,
    default_radius_m: float,
) -> Landmark | None:
    coordinate = feature_coordinate(element)
    if coordinate is None:
        return None
    category = feature_category(tags)
    radius = default_radius_m
    if category in {"natural=peak", "place=village", "place=hamlet"}:
        radius = max(radius, 300.0)
    distance = nearest_track_distance(analysis.points, coordinate)
    return Landmark(
        identifier=f"osm_{element.get('type')}_{element.get('id')}",
        name=tags["name"],
        category=category,
        latitude=coordinate[0],
        longitude=coordinate[1],
        distance_m=distance,
        visited=distance <= radius,
        visit_radius_m=radius,
        tags=tags,
    )


def landmark_score(landmark: Landmark) -> float:
    proximity = max(
        0.0,
        1.0 - landmark.distance_m / max(landmark.visit_radius_m, 1.0),
    )
    bonus = 0.08 if landmark.category in HIGHLIGHT_CATEGORIES else 0.0
    return min(0.95, 0.55 + 0.37 * proximity + bonus)


def landmark_candidate(landmark: Landmark) -> Candidate:
    score = landmark_score(landmark)
    return Candidate(
        identifier=f"landmark_{landmark.identifier}",
        name=landmark.name,
        source="openstreetmap_landmark",
        score=score,
        confidence=confidence(score),
        median_distance_m=landmark.distance_m,
        tags=landmark.tags,
        notes=[
            f"The GPX passes {landmark.distance_m:.0f} m from "
            f"the named OSM feature ({landmark.category})"
        ],
    )


def evaluate_osm(
    analysis: TrackAnalysis,
    data: dict[str, Any],
    route_radius_m: float,
    landmark_radius_m: float,
) -> tuple[list[Candidate], list[Landmark]]:
    reference_latitude = (analysis.bounds[0] + analysis.bounds[2]) / 2
    track = [
        project(point.latitude, point.longitude, reference_latitude)
        for point in sampled_points(analysis.points)
    ]
    candidates: list[Candidate] = []
    landmarks: list[Landmark] = []

    for element in data.get("elements", []):
        tags = {
            str(key): str(value)
            for key, value in (element.get("tags") or {}).items()
        }
        if (
            element.get("type") == "relation"
            and tags.get("route") in {"hiking", "foot", "walking"}
        ):
            candidate = route_candidate(
                element,
                tags,
                track,
                reference_latitude,
                route_radius_m,
            )
            if candidate:
                candidates.append(candidate)
            continue
        if "name" in tags:
            landmark = landmark_from_element(
                element,
                tags,
                analysis,
                landmark_radius_m,
            )
            if landmark:
                landmarks.append(landmark)

    landmarks.sort(key=lambda item: item.distance_m)
    candidates.extend(
        landmark_candidate(landmark)
        for landmark in landmarks
        if landmark.visited
    )
    candidates.sort(
        key=lambda item: (
            item.score,
            item.source == "openstreetmap_route",
        ),
        reverse=True,
    )
    return candidates[:8], landmarks[:30]


def track_warnings(analysis: TrackAnalysis) -> list[str]:
    warnings = []
    if analysis.malformed_timestamps:
        warnings.append(
            f"{analysis.malformed_timestamps} malformed timestamp(s) "
            "were ignored"
        )
    if analysis.backward_timestamps:
        warnings.append(
            f"{analysis.backward_timestamps} backward timestamp "
            "transition(s); geometry remains usable but chronological "
            "direction is unreliable"
        )
    missing = analysis.point_count - analysis.timed_point_count
    if missing:
        warnings.append(f"{missing} point(s) have no valid timestamp")
    if not analysis.closed:
        warnings.append(
            f"track is not closed; start and end are "
            f"{analysis.start_end_distance_m:.0f} m apart"
        )
    return warnings


def identify(
    analysis: TrackAnalysis,
    urls: Sequence[str],
    route_radius_m: float,
    landmark_radius_m: float,
    padding_m: float,
    timeout_seconds: float,
) -> Identification:
    data, endpoint = request_osm(
        analysis,
        urls,
        padding_m,
        timeout_seconds,
    )
    candidates, landmarks = evaluate_osm(
        analysis,
        data,
        route_radius_m,
        landmark_radius_m,
    )
    best = (
        candidates[0]
        if candidates and candidates[0].score >= 0.30
        else None
    )
    osm_timestamp = str(
        (data.get("osm3s") or {}).get("timestamp_osm_base") or ""
    ) or None
    return Identification(
        analysis,
        best,
        candidates,
        landmarks,
        track_warnings(analysis),
        endpoint,
        osm_timestamp,
    )


# Highlight GPX copies ---------------------------------------------------------


def ranked_highlights(result: Identification) -> list[Landmark]:
    unique: dict[str, Landmark] = {}
    for landmark in result.landmarks:
        if (
            not landmark.visited
            or landmark.category not in HIGHLIGHT_CATEGORIES
        ):
            continue
        key = landmark.name.strip().casefold()
        previous = unique.get(key)
        if (
            previous is None
            or landmark_score(landmark) > landmark_score(previous)
        ):
            unique[key] = landmark
    return sorted(
        unique.values(),
        key=lambda item: (
            landmark_score(item),
            -item.distance_m,
            item.name.casefold(),
        ),
        reverse=True,
    )


def first_gpx_date(path: Path) -> str | None:
    try:
        with path.open("rb") as source:
            for _, element in etree.iterparse(
                source,
                events=("end",),
                huge_tree=True,
            ):
                if (
                    not isinstance(element.tag, str)
                    or local_name(element.tag) != "time"
                ):
                    continue
                parent = element.getparent()
                if (
                    parent is None
                    or local_name(parent.tag) not in {"metadata", "trkpt"}
                ):
                    continue
                timestamp = parse_timestamp(element.text)
                if timestamp:
                    return timestamp.date().isoformat()
    except etree.XMLSyntaxError as error:
        raise ValueError(f"Invalid GPX/XML in {path}: {error}") from error
    return None


def safe_highlight_name(name: str) -> str:
    invalid = '<>:"/\\|?*'
    value = "".join(
        "-" if character in invalid else character
        for character in name
    )
    value = "-".join(value.split())
    while "--" in value:
        value = value.replace("--", "-")
    return value.strip(" .-_") or "highlight"


def highlight_description(highlights: Sequence[Landmark]) -> str:
    description = f"Main highlight: {highlights[0].name}."
    other_names = [highlight.name for highlight in highlights[1:8]]
    if other_names:
        description += (
            " Other visited highlights: "
            + "; ".join(other_names)
            + "."
        )
    if len(highlights) > 8:
        description += (
            f" Plus {len(highlights) - 8} additional highlight(s)."
        )
    return description


def set_track_description(
    track: etree._Element,
    description: str,
) -> None:
    existing = next(
        (
            child
            for child in track
            if isinstance(child.tag, str)
            and local_name(child.tag) == "desc"
        ),
        None,
    )
    if existing is not None:
        current = "".join(existing.itertext()).strip()
        existing.text = (
            f"{current}\n\n{description}" if current else description
        )
        return

    namespace = etree.QName(track).namespace
    tag = f"{{{namespace}}}desc" if namespace else "desc"
    new_description = etree.Element(tag)
    new_description.text = description
    index = 0
    for child_index, child in enumerate(track):
        if (
            isinstance(child.tag, str)
            and local_name(child.tag) in {"name", "cmt"}
        ):
            index = child_index + 1
        else:
            break
    track.insert(index, new_description)


def write_highlight_copy(
    result: Identification,
    source_path: Path,
    output_path: Path,
) -> None:
    highlights = ranked_highlights(result)
    if len(highlights) < 2:
        raise ValueError("At least two visited highlights are required")
    try:
        tree = etree.parse(
            str(source_path),
            etree.XMLParser(remove_blank_text=False, huge_tree=True),
        )
    except etree.XMLSyntaxError as error:
        raise ValueError(
            f"Invalid GPX/XML in {source_path}: {error}"
        ) from error
    tracks = [
        child
        for child in tree.getroot()
        if isinstance(child.tag, str) and local_name(child.tag) == "trk"
    ]
    if len(tracks) != 1:
        raise ValueError(
            f"Expected exactly one GPX track in {source_path}, "
            f"found {len(tracks)}"
        )
    set_track_description(
        tracks[0],
        highlight_description(highlights),
    )
    tree.write(
        str(output_path),
        encoding=tree.docinfo.encoding or "UTF-8",
        xml_declaration=True,
        pretty_print=False,
    )


def write_highlight_copies(
    results: Sequence[Identification],
    source_paths: Sequence[Path],
    output_directory: Path,
) -> list[Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    written = []
    used_names: set[str] = set()
    for result, source_path in zip(results, source_paths):
        highlights = ranked_highlights(result)
        if len(highlights) < 2:
            continue
        date = first_gpx_date(source_path)
        name = safe_highlight_name(highlights[0].name)
        base_name = (
            f"{date}_{name}.gpx" if date else f"{name}.gpx"
        )
        output_name = base_name
        suffix = 2
        while output_name.casefold() in used_names:
            output_name = f"{Path(base_name).stem}_{suffix}.gpx"
            suffix += 1
        used_names.add(output_name.casefold())
        output_path = output_directory / output_name
        write_highlight_copy(result, source_path, output_path)
        written.append(output_path)
    return written


# Reports ---------------------------------------------------------------------


def rounded(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def analysis_dict(analysis: TrackAnalysis) -> dict[str, Any]:
    return {
        "input_file": analysis.file_name,
        "track_name": analysis.track_name,
        "point_count": analysis.point_count,
        "distance_km": rounded(analysis.distance_km),
        "start_end_distance_m": rounded(
            analysis.start_end_distance_m, 1
        ),
        "closed_track": analysis.closed,
        "timed_point_count": analysis.timed_point_count,
        "malformed_timestamp_count": analysis.malformed_timestamps,
        "forward_timestamp_transitions": analysis.forward_timestamps,
        "backward_timestamp_transitions": analysis.backward_timestamps,
        "equal_timestamp_transitions": analysis.equal_timestamps,
        "timestamp_order": analysis.timestamp_order,
        "duration_minutes": rounded(analysis.duration_minutes, 1),
        "elevation_min_m": rounded(analysis.elevation_min_m, 1),
        "elevation_max_m": rounded(analysis.elevation_max_m, 1),
        "elevation_gain_m": rounded(analysis.elevation_gain_m, 1),
        "elevation_loss_m": rounded(analysis.elevation_loss_m, 1),
        "bounds": list(analysis.bounds),
    }


def landmark_dict(landmark: Landmark) -> dict[str, Any]:
    data = asdict(landmark)
    data["distance_m"] = round(landmark.distance_m, 1)
    data["visit_radius_m"] = round(landmark.visit_radius_m, 1)
    return data


def candidate_dict(candidate: Candidate) -> dict[str, Any]:
    data = asdict(candidate)
    data["score"] = round(candidate.score, 4)
    data["coverage_percent"] = rounded(
        candidate.coverage_percent, 1
    )
    data["median_distance_m"] = rounded(
        candidate.median_distance_m, 1
    )
    return data


def result_dict(result: Identification) -> dict[str, Any]:
    return {
        "analysis": analysis_dict(result.analysis),
        "best_match": (
            candidate_dict(result.best_match)
            if result.best_match
            else None
        ),
        "candidates": [
            candidate_dict(candidate)
            for candidate in result.candidates
        ],
        "nearby_landmarks": [
            landmark_dict(landmark)
            for landmark in result.landmarks
        ],
        "warnings": result.warnings,
        "overpass_endpoint": result.overpass_endpoint,
        "osm_base_timestamp": result.osm_timestamp,
    }


def write_reports(
    results: Sequence[Identification],
    output_directory: Path,
) -> dict[str, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "csv": output_directory / "trail_identification.csv",
        "json": output_directory / "trail_identification.json",
        "geojson": output_directory / "trail_identification.geojson",
        "html": output_directory / "trail_identification.html",
    }

    paths["json"].write_text(
        json.dumps(
            [result_dict(result) for result in results],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    columns = [
        "file",
        "track_name",
        "likely_trail",
        "source",
        "confidence",
        "score",
        "coverage_percent",
        "distance_km",
        "point_count",
        "duration_minutes",
        "elevation_min_m",
        "elevation_max_m",
        "timestamp_order",
        "closed_track",
        "overpass_endpoint",
        "osm_base_timestamp",
        "warnings",
    ]
    with paths["csv"].open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=columns)
        writer.writeheader()
        for result in results:
            analysis = result.analysis
            best = result.best_match
            writer.writerow(
                {
                    "file": analysis.file_name,
                    "track_name": analysis.track_name,
                    "likely_trail": (
                        best.name if best else "Unmatched"
                    ),
                    "source": best.source if best else "",
                    "confidence": (
                        best.confidence if best else "unmatched"
                    ),
                    "score": rounded(best.score, 3) if best else "",
                    "coverage_percent": (
                        rounded(best.coverage_percent, 1)
                        if best
                        else ""
                    ),
                    "distance_km": rounded(analysis.distance_km),
                    "point_count": analysis.point_count,
                    "duration_minutes": rounded(
                        analysis.duration_minutes, 1
                    ),
                    "elevation_min_m": rounded(
                        analysis.elevation_min_m, 1
                    ),
                    "elevation_max_m": rounded(
                        analysis.elevation_max_m, 1
                    ),
                    "timestamp_order": analysis.timestamp_order,
                    "closed_track": analysis.closed,
                    "overpass_endpoint": result.overpass_endpoint,
                    "osm_base_timestamp": result.osm_timestamp or "",
                    "warnings": " | ".join(result.warnings),
                }
            )

    features = []
    map_tracks = []
    colors = [
        "#1565c0",
        "#2e7d32",
        "#c62828",
        "#6a1b9a",
        "#ef6c00",
    ]
    for index, result in enumerate(results):
        analysis = result.analysis
        best = result.best_match
        line_coordinates = [
            [point.longitude, point.latitude]
            for point in analysis.points
        ]
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "file": analysis.file_name,
                    "name": best.name if best else "Unmatched",
                    "confidence": (
                        best.confidence if best else "unmatched"
                    ),
                    "distance_km": rounded(analysis.distance_km),
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": line_coordinates,
                },
            }
        )
        for landmark in result.landmarks:
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "file": analysis.file_name,
                        "name": landmark.name,
                        "category": landmark.category,
                        "visited": landmark.visited,
                        "distance_m": rounded(
                            landmark.distance_m, 1
                        ),
                    },
                    "geometry": {
                        "type": "Point",
                        "coordinates": [
                            landmark.longitude,
                            landmark.latitude,
                        ],
                    },
                }
            )
        map_tracks.append(
            {
                "file": analysis.file_name,
                "trail": best.name if best else "Unmatched",
                "confidence": (
                    best.confidence if best else "unmatched"
                ),
                "color": colors[index % len(colors)],
                "coordinates": [
                    [point.latitude, point.longitude]
                    for point in analysis.points
                ],
                "distance_km": rounded(analysis.distance_km),
                "landmarks": [
                    landmark_dict(item)
                    for item in result.landmarks[:20]
                ],
            }
        )

    paths["geojson"].write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": features,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    payload = json.dumps(
        map_tracks,
        ensure_ascii=False,
    ).replace("</", "<\\/")
    paths["html"].write_text(
        f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>GPX trail identification</title><link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"><style>html,body,#map{{height:100%;margin:0}}#panel{{position:absolute;z-index:1000;top:12px;right:12px;max-width:420px;background:#fffffff0;padding:12px;font:13px system-ui}}</style></head><body><div id="map"></div><div id="panel"><b>GPX trail identification</b><div id="items"></div></div><script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>const tracks={payload},map=L.map('map'),bounds=[],layers={{}},items=document.getElementById('items');L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}}).addTo(map);for(const t of tracks){{const g=L.layerGroup().addTo(map),line=L.polyline(t.coordinates,{{color:t.color,weight:5}}).addTo(g);bounds.push(...t.coordinates);for(const m of t.landmarks)L.circleMarker([m.latitude,m.longitude],{{radius:5,color:m.visited?'#167c3b':'#9a5500'}}).addTo(g).bindPopup(`<b>${{m.name}}</b><br>${{m.category}}<br>${{m.distance_m}} m`);layers[`${{t.file}} — ${{t.trail}}`]=g;items.insertAdjacentHTML('beforeend',`<p><b style="color:${{t.color}}">${{t.file}}</b><br>${{t.trail}} · ${{t.confidence}} · ${{t.distance_km}} km</p>`)}}L.control.layers(null,layers,{{collapsed:false}}).addTo(map);if(bounds.length)map.fitBounds(bounds,{{padding:[25,25]}});</script></body></html>''',
        encoding="utf-8",
    )
    return paths


# Command line ----------------------------------------------------------------


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Identify likely trails using live OpenStreetMap data."
        )
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("trail_analysis"),
    )
    parser.add_argument(
        "--overpass-url",
        action="append",
        dest="overpass_urls",
    )
    parser.add_argument(
        "--route-match-radius-m",
        type=float,
        default=70.0,
    )
    parser.add_argument(
        "--landmark-visit-radius-m",
        type=float,
        default=180.0,
    )
    parser.add_argument(
        "--query-padding-m",
        type=float,
        default=1500.0,
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=120.0,
    )
    return parser


def print_result(result: Identification) -> None:
    best = result.best_match
    match = (
        f"{best.name} ({best.confidence}, score {best.score:.3f})"
        if best
        else "unmatched"
    )
    analysis = result.analysis
    print(
        f"{analysis.file_name}: {match}; "
        f"{analysis.distance_km:.2f} km; "
        f"{analysis.point_count} points"
    )
    print(f"  live data: {result.overpass_endpoint}")
    print(
        f"  OSM results: {len(result.candidates)} candidate(s), "
        f"{len(result.landmarks)} landmark(s), "
        f"OSM base: {result.osm_timestamp or 'unknown'}"
    )
    for warning in result.warnings:
        print(f"  warning: {warning}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argument_parser()
    arguments = parser.parse_args(argv)
    for name in (
        "route_match_radius_m",
        "landmark_visit_radius_m",
        "query_padding_m",
        "timeout_seconds",
    ):
        if getattr(arguments, name) <= 0:
            parser.error(
                f"--{name.replace('_', '-')} must be greater than zero"
            )

    files = discover_gpx_files(
        arguments.inputs,
        arguments.recursive,
    )
    if not files:
        parser.error("No GPX files found")

    urls = tuple(arguments.overpass_urls or OVERPASS_URLS)
    try:
        # Validate every file before making any live request.
        analyses = [analyze_track(path) for path in files]
        results = [
            identify(
                analysis,
                urls,
                arguments.route_match_radius_m,
                arguments.landmark_visit_radius_m,
                arguments.query_padding_m,
                arguments.timeout_seconds,
            )
            for analysis in analyses
        ]
        report_paths = write_reports(
            results,
            arguments.output_dir,
        )
        highlight_paths = write_highlight_copies(
            results,
            files,
            arguments.output_dir,
        )
    except (
        OSError,
        ValueError,
        RuntimeError,
        json.JSONDecodeError,
    ) as error:
        parser.exit(1, f"Error: {error}\n")

    for result in results:
        print_result(result)
    print("Outputs:")
    for report_type, path in report_paths.items():
        print(f"  {report_type}: {path.resolve()}")
    for path in highlight_paths:
        print(f"  highlight_gpx: {path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
