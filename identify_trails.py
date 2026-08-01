#!/usr/bin/env python3
"""Identify likely hiking routes and visited landmarks from GPX tracks.

The program follows one explicit pipeline for every input file:

1. Read the GPX track and calculate geometry, elevation, and time statistics.
2. Request nearby hiking routes and named features from OpenStreetMap/Overpass.
3. Compare the GPX geometry with those routes and landmarks.
4. Rank the candidates and write CSV, JSON, GeoJSON, and HTML reports.

The program deliberately has no local trail catalog, response cache, mocked
runtime data, or offline fallback. A failed live Overpass request is an error.
"""

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
DEFAULT_OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
USER_AGENT = (
    f"gpx-splitter-trail-identifier/{VERSION} "
    "(+https://github.com/dcf1007/gpx_splitter)"
)


# -----------------------------------------------------------------------------
# Data model
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrackPoint:
    latitude: float
    longitude: float
    elevation_m: float | None = None
    timestamp: datetime | None = None

    @property
    def coordinate(self) -> tuple[float, float]:
        return self.latitude, self.longitude


@dataclass(slots=True)
class TrackAnalysis:
    input_file: str
    track_name: str
    points: list[TrackPoint]
    distance_km: float
    start_end_distance_m: float
    closed_track: bool
    malformed_timestamp_count: int
    forward_timestamp_transitions: int
    backward_timestamp_transitions: int
    equal_timestamp_transitions: int
    duration_minutes: float | None
    elevation_min_m: float | None
    elevation_max_m: float | None
    elevation_gain_m: float | None
    elevation_loss_m: float | None
    bounds: tuple[float, float, float, float]

    @property
    def point_count(self) -> int:
        return len(self.points)

    @property
    def timed_point_count(self) -> int:
        return sum(point.timestamp is not None for point in self.points)

    @property
    def timestamp_order(self) -> str:
        if self.backward_timestamp_transitions and not self.forward_timestamp_transitions:
            return "reverse"
        if self.backward_timestamp_transitions:
            return "mixed"
        return "forward" if self.timed_point_count else "missing"


@dataclass(slots=True)
class NearbyLandmark:
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
class TrailCandidate:
    identifier: str
    name: str
    source: str
    score: float
    confidence: str
    coverage_percent: float | None = None
    median_distance_m: float | None = None
    osm_relation_id: int | None = None
    tags: dict[str, str] = field(default_factory=dict)
    landmarks: list[NearbyLandmark] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TrailIdentification:
    analysis: TrackAnalysis
    best_match: TrailCandidate | None
    candidates: list[TrailCandidate]
    nearby_landmarks: list[NearbyLandmark]
    warnings: list[str]
    overpass_endpoint: str
    osm_base_timestamp: str | None


# -----------------------------------------------------------------------------
# GPX parsing and track statistics
# -----------------------------------------------------------------------------


def xml_local_name(tag: str) -> str:
    """Return the unqualified name of an XML tag."""

    return tag.rsplit("}", 1)[-1]


def parse_gpx_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp and normalize it to UTC."""

    if not value or not value.strip():
        return None

    normalized_value = value.strip()
    if normalized_value.endswith(("Z", "z")):
        normalized_value = normalized_value[:-1] + "+00:00"

    try:
        timestamp = datetime.fromisoformat(normalized_value)
    except ValueError:
        return None

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def calculate_distance_m(
    first_coordinate: tuple[float, float],
    second_coordinate: tuple[float, float],
) -> float:
    """Calculate the great-circle distance between two coordinates."""

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
    return 2 * EARTH_RADIUS_M * math.asin(
        min(1.0, math.sqrt(haversine_value))
    )


def find_child_text(element: etree._Element, child_name: str) -> str | None:
    """Return the text of a direct child, ignoring XML namespaces."""

    for child in element:
        if (
            isinstance(child.tag, str)
            and xml_local_name(child.tag) == child_name
        ):
            return child.text
    return None


def read_gpx_track(
    path: Path,
) -> tuple[str, list[TrackPoint], int]:
    """Stream the first track name and all track points from a GPX file."""

    track_name: str | None = None
    track_points: list[TrackPoint] = []
    malformed_timestamp_count = 0

    try:
        xml_events = etree.iterparse(
            str(path),
            events=("end",),
            huge_tree=True,
            remove_blank_text=False,
        )
        for _, element in xml_events:
            if not isinstance(element.tag, str):
                continue

            element_name = xml_local_name(element.tag)
            parent = element.getparent()

            if element_name == "name" and track_name is None:
                if (
                    parent is not None
                    and isinstance(parent.tag, str)
                    and xml_local_name(parent.tag) == "trk"
                ):
                    track_name = "".join(element.itertext()).strip() or None
                continue

            if element_name != "trkpt":
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

            elevation_text = find_child_text(element, "ele")
            timestamp_text = find_child_text(element, "time")

            try:
                elevation_m = (
                    float(elevation_text) if elevation_text else None
                )
            except ValueError:
                elevation_m = None

            timestamp = parse_gpx_timestamp(timestamp_text)
            if timestamp_text and timestamp is None:
                malformed_timestamp_count += 1

            track_points.append(
                TrackPoint(
                    latitude=latitude,
                    longitude=longitude,
                    elevation_m=elevation_m,
                    timestamp=timestamp,
                )
            )

            # Release parsed XML nodes while keeping the extracted point data.
            element.clear(keep_tail=True)
            if parent is not None:
                while element.getprevious() is not None:
                    del parent[0]
    except etree.XMLSyntaxError as error:
        raise ValueError(f"Invalid GPX/XML in {path}: {error}") from error

    if not track_points:
        raise ValueError(f"No GPX track points found in {path}")

    return track_name or path.stem, track_points, malformed_timestamp_count


def calculate_timestamp_statistics(
    points: Sequence[TrackPoint],
) -> tuple[int, int, int, float | None]:
    """Count timestamp direction changes and calculate total duration."""

    timestamps = [
        point.timestamp for point in points if point.timestamp is not None
    ]
    forward_count = 0
    backward_count = 0
    equal_count = 0

    for previous_timestamp, current_timestamp in zip(
        timestamps, timestamps[1:]
    ):
        if current_timestamp > previous_timestamp:
            forward_count += 1
        elif current_timestamp < previous_timestamp:
            backward_count += 1
        else:
            equal_count += 1

    duration_minutes = None
    if timestamps:
        duration_minutes = (
            max(timestamps) - min(timestamps)
        ).total_seconds() / 60

    return (
        forward_count,
        backward_count,
        equal_count,
        duration_minutes,
    )


def calculate_elevation_statistics(
    points: Sequence[TrackPoint],
) -> tuple[float | None, float | None, float | None, float | None]:
    """Calculate minimum, maximum, accumulated gain, and accumulated loss."""

    elevations = [
        point.elevation_m
        for point in points
        if point.elevation_m is not None
    ]
    if not elevations:
        return None, None, None, None

    elevation_gain_m = 0.0
    elevation_loss_m = 0.0
    previous_elevation_m: float | None = None

    for point in points:
        if point.elevation_m is None:
            continue
        if previous_elevation_m is not None:
            elevation_change_m = point.elevation_m - previous_elevation_m
            elevation_gain_m += max(elevation_change_m, 0.0)
            elevation_loss_m += max(-elevation_change_m, 0.0)
        previous_elevation_m = point.elevation_m

    return (
        min(elevations),
        max(elevations),
        elevation_gain_m,
        elevation_loss_m,
    )


def analyze_gpx_track(path: Path) -> TrackAnalysis:
    """Read one GPX file and calculate its track statistics."""

    track_name, points, malformed_timestamp_count = read_gpx_track(path)

    total_distance_m = sum(
        calculate_distance_m(previous.coordinate, current.coordinate)
        for previous, current in zip(points, points[1:])
    )
    start_end_distance_m = calculate_distance_m(
        points[0].coordinate, points[-1].coordinate
    )
    closed_track_threshold_m = max(
        50.0,
        min(250.0, total_distance_m * 0.03),
    )

    (
        forward_timestamp_transitions,
        backward_timestamp_transitions,
        equal_timestamp_transitions,
        duration_minutes,
    ) = calculate_timestamp_statistics(points)

    (
        elevation_min_m,
        elevation_max_m,
        elevation_gain_m,
        elevation_loss_m,
    ) = calculate_elevation_statistics(points)

    latitudes = [point.latitude for point in points]
    longitudes = [point.longitude for point in points]

    return TrackAnalysis(
        input_file=path.name,
        track_name=track_name,
        points=points,
        distance_km=total_distance_m / 1000,
        start_end_distance_m=start_end_distance_m,
        closed_track=start_end_distance_m <= closed_track_threshold_m,
        malformed_timestamp_count=malformed_timestamp_count,
        forward_timestamp_transitions=forward_timestamp_transitions,
        backward_timestamp_transitions=backward_timestamp_transitions,
        equal_timestamp_transitions=equal_timestamp_transitions,
        duration_minutes=duration_minutes,
        elevation_min_m=elevation_min_m,
        elevation_max_m=elevation_max_m,
        elevation_gain_m=elevation_gain_m,
        elevation_loss_m=elevation_loss_m,
        bounds=(
            min(latitudes),
            min(longitudes),
            max(latitudes),
            max(longitudes),
        ),
    )


# -----------------------------------------------------------------------------
# Geometry helpers used for route matching
# -----------------------------------------------------------------------------


def confidence_label(score: float) -> str:
    for threshold, label in (
        (0.85, "high"),
        (0.68, "medium-high"),
        (0.50, "medium"),
        (0.30, "low"),
    ):
        if score >= threshold:
            return label
    return "very-low"


def project_coordinate_to_metres(
    latitude: float,
    longitude: float,
    reference_latitude: float,
) -> tuple[float, float]:
    return (
        math.radians(longitude)
        * EARTH_RADIUS_M
        * math.cos(math.radians(reference_latitude)),
        math.radians(latitude) * EARTH_RADIUS_M,
    )


def distance_from_point_to_segment_m(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    px, py = point
    sx, sy = start
    ex, ey = end
    dx, dy = ex - sx, ey - sy
    if dx == dy == 0:
        return math.hypot(px - sx, py - sy)
    fraction = ((px - sx) * dx + (py - sy) * dy) / (dx * dx + dy * dy)
    fraction = min(1.0, max(0.0, fraction))
    return math.hypot(px - (sx + fraction * dx), py - (sy + fraction * dy))


def evenly_sample_track_points(
    points: Sequence[TrackPoint], maximum: int = 240
) -> list[TrackPoint]:
    if len(points) <= maximum:
        return list(points)
    step = (len(points) - 1) / (maximum - 1)
    return [points[index] for index in sorted({round(i * step) for i in range(maximum)})]


def format_padded_bounding_box(
    bounds: tuple[float, float, float, float], padding_m: float
) -> str:
    south, west, north, east = bounds
    middle_latitude = (south + north) / 2
    latitude_padding = padding_m / 111_320
    longitude_padding = padding_m / max(
        1.0, 111_320 * math.cos(math.radians(middle_latitude))
    )
    values = (
        south - latitude_padding,
        west - longitude_padding,
        north + latitude_padding,
        east + longitude_padding,
    )
    return ",".join(f"{value:.7f}" for value in values)


# -----------------------------------------------------------------------------
# Live OpenStreetMap / Overpass access
# -----------------------------------------------------------------------------


def build_overpass_query(
    bounds: tuple[float, float, float, float], padding_m: float
) -> str:
    bbox = format_padded_bounding_box(bounds, padding_m)
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


def request_overpass_data(
    analysis: TrackAnalysis,
    urls: Sequence[str],
    padding_m: float,
    timeout_seconds: float,
) -> tuple[dict[str, Any], str]:
    """Request live OSM data, trying each endpoint in order."""

    if not urls:
        raise ValueError("At least one Overpass URL is required")
    body = urllib.parse.urlencode(
        {"data": build_overpass_query(analysis.bounds, padding_m)}
    ).encode()
    failures: list[str] = []
    for index, url in enumerate(urls):
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                data = json.loads(response.read())
            if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
                raise ValueError("response has no Overpass elements array")
            return data, url
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            failures.append(f"{url}: {error}")
            if index + 1 < len(urls):
                time.sleep(1)
    raise RuntimeError("All Overpass requests failed: " + " | ".join(failures))


def extract_osm_feature_coordinate(
    element: dict[str, Any],
) -> tuple[float, float] | None:
    latitude, longitude = element.get("lat"), element.get("lon")
    if latitude is None or longitude is None:
        center = element.get("center") or {}
        latitude, longitude = center.get("lat"), center.get("lon")
    if latitude is None or longitude is None:
        geometry = [
            point
            for point in element.get("geometry") or []
            if "lat" in point and "lon" in point
        ]
        if geometry:
            latitude = sum(float(point["lat"]) for point in geometry) / len(geometry)
            longitude = sum(float(point["lon"]) for point in geometry) / len(geometry)
    if latitude is None or longitude is None:
        return None
    return float(latitude), float(longitude)


def describe_osm_feature_category(tags: dict[str, str]) -> str:
    for key in ("natural", "waterway", "tourism", "leisure", "amenity", "place"):
        if key in tags:
            return f"{key}={tags[key]}"
    return "named_feature"


def distance_from_track_m(
    points: Iterable[TrackPoint], coordinate: tuple[float, float]
) -> float:
    return min(calculate_distance_m(point.coordinate, coordinate) for point in points)


# -----------------------------------------------------------------------------
# Candidate extraction and scoring
# -----------------------------------------------------------------------------


def extract_route_segments(
    relation: dict[str, Any],
    reference_latitude: float,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Convert an OSM route relation's member geometries into segments."""

    route_segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for member in relation.get("members") or []:
        projected_geometry = [
            project_coordinate_to_metres(
                float(point["lat"]),
                float(point["lon"]),
                reference_latitude,
            )
            for point in member.get("geometry") or []
            if "lat" in point and "lon" in point
        ]
        route_segments.extend(zip(projected_geometry, projected_geometry[1:]))
    return route_segments


def create_route_candidate(
    relation: dict[str, Any],
    tags: dict[str, str],
    projected_track: Sequence[tuple[float, float]],
    reference_latitude: float,
    route_match_radius_m: float,
) -> TrailCandidate | None:
    """Score one OSM hiking relation against the GPX geometry."""

    route_segments = extract_route_segments(relation, reference_latitude)
    if not route_segments:
        return None

    distances_m = [
        min(
            distance_from_point_to_segment_m(track_point, segment_start, segment_end)
            for segment_start, segment_end in route_segments
        )
        for track_point in projected_track
    ]
    coverage = sum(distance <= route_match_radius_m for distance in distances_m) / len(distances_m)
    median_distance_m = statistics.median(distances_m)
    proximity = max(0.0, 1.0 - median_distance_m / max(route_match_radius_m * 3.0, 1.0))
    score = 0.80 * coverage + 0.20 * proximity

    relation_id = relation.get("id")
    route_name = tags.get("name") or tags.get("ref") or f"OSM route {relation_id}"
    return TrailCandidate(
        identifier=f"osm_relation_{relation_id}",
        name=route_name,
        source="openstreetmap_route",
        score=score,
        confidence=confidence_label(score),
        coverage_percent=coverage * 100,
        median_distance_m=median_distance_m,
        osm_relation_id=int(relation_id) if relation_id is not None else None,
        tags=tags,
        notes=[
            f"{coverage * 100:.1f}% of sampled GPX points are within "
            f"{route_match_radius_m:.0f} m of the OSM route"
        ],
    )


def create_nearby_landmark(
    element: dict[str, Any],
    tags: dict[str, str],
    analysis: TrackAnalysis,
    default_visit_radius_m: float,
) -> NearbyLandmark | None:
    """Convert a named OSM element into a distance-checked landmark."""

    coordinate = extract_osm_feature_coordinate(element)
    if coordinate is None:
        return None
    category = describe_osm_feature_category(tags)
    visit_radius_m = default_visit_radius_m
    if category in {"natural=peak", "place=village", "place=hamlet"}:
        visit_radius_m = max(visit_radius_m, 300.0)
    distance_m = distance_from_track_m(analysis.points, coordinate)
    element_type = str(element.get("type", ""))
    element_id = element.get("id")
    return NearbyLandmark(
        identifier=f"osm_{element_type}_{element_id}",
        name=tags["name"],
        category=category,
        latitude=coordinate[0],
        longitude=coordinate[1],
        distance_m=distance_m,
        visited=distance_m <= visit_radius_m,
        visit_radius_m=visit_radius_m,
        tags=tags,
    )


def create_landmark_candidate(landmark: NearbyLandmark) -> TrailCandidate:
    """Create a candidate from a landmark the GPX appears to visit."""

    significant_categories = {
        "natural=cave_entrance", "natural=spring", "natural=water",
        "waterway=waterfall", "tourism=attraction", "tourism=viewpoint",
        "leisure=nature_reserve",
    }
    proximity = max(0.0, 1.0 - landmark.distance_m / landmark.visit_radius_m)
    significance_bonus = 0.08 if landmark.category in significant_categories else 0.0
    score = min(0.95, 0.55 + 0.37 * proximity + significance_bonus)
    return TrailCandidate(
        identifier=f"landmark_{landmark.identifier}",
        name=landmark.name,
        source="openstreetmap_landmark",
        score=score,
        confidence=confidence_label(score),
        median_distance_m=landmark.distance_m,
        tags=landmark.tags,
        landmarks=[landmark],
        notes=[
            f"The GPX passes {landmark.distance_m:.0f} m from the named "
            f"OSM feature ({landmark.category})"
        ],
    )


def evaluate_overpass_response(
    analysis: TrackAnalysis,
    data: dict[str, Any],
    route_match_radius_m: float,
    landmark_visit_radius_m: float,
    maximum_candidates: int = 8,
) -> tuple[list[TrailCandidate], list[NearbyLandmark]]:
    """Convert an Overpass response into ranked trails and landmarks."""

    reference_latitude = (analysis.bounds[0] + analysis.bounds[2]) / 2
    projected_track = [
        project_coordinate_to_metres(point.latitude, point.longitude, reference_latitude)
        for point in evenly_sample_track_points(analysis.points)
    ]
    route_candidates: list[TrailCandidate] = []
    nearby_landmarks: list[NearbyLandmark] = []

    for element in data.get("elements", []):
        tags = {str(key): str(value) for key, value in (element.get("tags") or {}).items()}
        element_type = str(element.get("type", ""))
        route_type = tags.get("route")
        if element_type == "relation" and route_type in {"hiking", "foot", "walking"}:
            route_candidate = create_route_candidate(
                element, tags, projected_track, reference_latitude, route_match_radius_m
            )
            if route_candidate is not None:
                route_candidates.append(route_candidate)
            continue
        if "name" not in tags:
            continue
        landmark = create_nearby_landmark(element, tags, analysis, landmark_visit_radius_m)
        if landmark is not None:
            nearby_landmarks.append(landmark)

    nearby_landmarks.sort(key=lambda landmark: landmark.distance_m)
    visited_landmarks = [landmark for landmark in nearby_landmarks if landmark.visited]
    landmark_candidates = [
        create_landmark_candidate(landmark)
        for landmark in visited_landmarks[:maximum_candidates]
    ]
    for candidate in route_candidates:
        candidate.landmarks = visited_landmarks[:10]
    candidates = route_candidates + landmark_candidates
    candidates.sort(
        key=lambda candidate: (candidate.score, candidate.source == "openstreetmap_route"),
        reverse=True,
    )
    return candidates[:maximum_candidates], nearby_landmarks[:30]


def build_track_warnings(analysis: TrackAnalysis) -> list[str]:
    warnings: list[str] = []
    if analysis.malformed_timestamp_count:
        warnings.append(f"{analysis.malformed_timestamp_count} malformed timestamp(s) were ignored")
    if analysis.backward_timestamp_transitions:
        warnings.append(
            f"{analysis.backward_timestamp_transitions} backward timestamp transition(s); "
            "geometry remains usable but chronological direction is unreliable"
        )
    missing = analysis.point_count - analysis.timed_point_count
    if missing:
        warnings.append(f"{missing} point(s) have no valid timestamp")
    if not analysis.closed_track:
        warnings.append(
            f"track is not closed; start and end are {analysis.start_end_distance_m:.0f} m apart"
        )
    return warnings


def identify_trail(
    path: Path,
    overpass_urls: Sequence[str] = DEFAULT_OVERPASS_URLS,
    route_match_radius_m: float = 70.0,
    landmark_visit_radius_m: float = 180.0,
    query_padding_m: float = 1500.0,
    timeout_seconds: float = 120.0,
) -> TrailIdentification:
    """Run the complete identification pipeline for one GPX file."""

    analysis = analyze_gpx_track(path)
    data, endpoint = request_overpass_data(
        analysis, overpass_urls, query_padding_m, timeout_seconds
    )
    candidates, landmarks = evaluate_overpass_response(
        analysis, data, route_match_radius_m, landmark_visit_radius_m
    )
    best_match = candidates[0] if candidates and candidates[0].score >= 0.30 else None
    osm_timestamp = str((data.get("osm3s") or {}).get("timestamp_osm_base") or "") or None
    return TrailIdentification(
        analysis=analysis,
        best_match=best_match,
        candidates=candidates,
        nearby_landmarks=landmarks,
        warnings=build_track_warnings(analysis),
        overpass_endpoint=endpoint,
        osm_base_timestamp=osm_timestamp,
    )


def discover_gpx_files(inputs: Sequence[Path], recursive: bool) -> list[Path]:
    """Resolve GPX files from file and directory command-line inputs."""

    files: set[Path] = set()
    for path in inputs:
        if path.is_file() and path.suffix.lower() == ".gpx":
            files.add(path.resolve())
        elif path.is_dir():
            pattern = "**/*.gpx" if recursive else "*.gpx"
            files.update(item.resolve() for item in path.glob(pattern))
    return sorted(files)


# -----------------------------------------------------------------------------
# Report serialization
# -----------------------------------------------------------------------------


def round_optional(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def track_analysis_to_dict(analysis: TrackAnalysis) -> dict[str, Any]:
    return {
        "input_file": analysis.input_file,
        "track_name": analysis.track_name,
        "point_count": analysis.point_count,
        "distance_km": round_optional(analysis.distance_km),
        "start_end_distance_m": round_optional(analysis.start_end_distance_m, 1),
        "closed_track": analysis.closed_track,
        "timed_point_count": analysis.timed_point_count,
        "malformed_timestamp_count": analysis.malformed_timestamp_count,
        "forward_timestamp_transitions": analysis.forward_timestamp_transitions,
        "backward_timestamp_transitions": analysis.backward_timestamp_transitions,
        "equal_timestamp_transitions": analysis.equal_timestamp_transitions,
        "timestamp_order": analysis.timestamp_order,
        "duration_minutes": round_optional(analysis.duration_minutes, 1),
        "elevation_min_m": round_optional(analysis.elevation_min_m, 1),
        "elevation_max_m": round_optional(analysis.elevation_max_m, 1),
        "elevation_gain_m": round_optional(analysis.elevation_gain_m, 1),
        "elevation_loss_m": round_optional(analysis.elevation_loss_m, 1),
        "bounds": list(analysis.bounds),
    }


def landmark_to_dict(landmark: NearbyLandmark) -> dict[str, Any]:
    data = asdict(landmark)
    data["distance_m"] = round(landmark.distance_m, 1)
    data["visit_radius_m"] = round(landmark.visit_radius_m, 1)
    return data


def candidate_to_dict(candidate: TrailCandidate) -> dict[str, Any]:
    return {
        "identifier": candidate.identifier,
        "name": candidate.name,
        "source": candidate.source,
        "score": round(candidate.score, 4),
        "confidence": candidate.confidence,
        "coverage_percent": round_optional(candidate.coverage_percent, 1),
        "median_distance_m": round_optional(candidate.median_distance_m, 1),
        "osm_relation_id": candidate.osm_relation_id,
        "tags": candidate.tags,
        "landmarks": [landmark_to_dict(landmark) for landmark in candidate.landmarks],
        "notes": candidate.notes,
    }


def identification_to_dict(identification: TrailIdentification) -> dict[str, Any]:
    return {
        "analysis": track_analysis_to_dict(identification.analysis),
        "best_match": candidate_to_dict(identification.best_match) if identification.best_match else None,
        "candidates": [candidate_to_dict(candidate) for candidate in identification.candidates],
        "nearby_landmarks": [landmark_to_dict(landmark) for landmark in identification.nearby_landmarks],
        "warnings": identification.warnings,
        "overpass_endpoint": identification.overpass_endpoint,
        "osm_base_timestamp": identification.osm_base_timestamp,
    }


def write_json_report(identifications: Sequence[TrailIdentification], path: Path) -> None:
    path.write_text(
        json.dumps([identification_to_dict(item) for item in identifications], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_csv_report(identifications: Sequence[TrailIdentification], path: Path) -> None:
    columns = [
        "file", "track_name", "likely_trail", "source", "confidence", "score",
        "coverage_percent", "distance_km", "point_count", "duration_minutes",
        "elevation_min_m", "elevation_max_m", "timestamp_order", "closed_track",
        "overpass_endpoint", "osm_base_timestamp", "warnings",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=columns)
        writer.writeheader()
        for identification in identifications:
            analysis = identification.analysis
            best = identification.best_match
            writer.writerow({
                "file": analysis.input_file,
                "track_name": analysis.track_name,
                "likely_trail": best.name if best else "Unmatched",
                "source": best.source if best else "",
                "confidence": best.confidence if best else "unmatched",
                "score": round_optional(best.score, 3) if best else "",
                "coverage_percent": round_optional(best.coverage_percent, 1) if best else "",
                "distance_km": round_optional(analysis.distance_km),
                "point_count": analysis.point_count,
                "duration_minutes": round_optional(analysis.duration_minutes, 1),
                "elevation_min_m": round_optional(analysis.elevation_min_m, 1),
                "elevation_max_m": round_optional(analysis.elevation_max_m, 1),
                "timestamp_order": analysis.timestamp_order,
                "closed_track": analysis.closed_track,
                "overpass_endpoint": identification.overpass_endpoint,
                "osm_base_timestamp": identification.osm_base_timestamp or "",
                "warnings": " | ".join(identification.warnings),
            })


def write_geojson_report(identifications: Sequence[TrailIdentification], path: Path) -> None:
    features: list[dict[str, Any]] = []
    for identification in identifications:
        analysis = identification.analysis
        best = identification.best_match
        features.append({
            "type": "Feature",
            "properties": {
                "file": analysis.input_file,
                "name": best.name if best else "Unmatched",
                "confidence": best.confidence if best else "unmatched",
                "distance_km": round_optional(analysis.distance_km),
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [[point.longitude, point.latitude] for point in analysis.points],
            },
        })
        for landmark in identification.nearby_landmarks:
            features.append({
                "type": "Feature",
                "properties": {
                    "file": analysis.input_file,
                    "name": landmark.name,
                    "category": landmark.category,
                    "visited": landmark.visited,
                    "distance_m": round(landmark.distance_m, 1),
                },
                "geometry": {
                    "type": "Point",
                    "coordinates": [landmark.longitude, landmark.latitude],
                },
            })
    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_map_track_data(identifications: Sequence[TrailIdentification]) -> list[dict[str, Any]]:
    colors = ["#1565c0", "#2e7d32", "#c62828", "#6a1b9a", "#ef6c00", "#00838f"]
    map_tracks: list[dict[str, Any]] = []
    for index, identification in enumerate(identifications):
        analysis = identification.analysis
        best = identification.best_match
        map_tracks.append({
            "file": analysis.input_file,
            "trail": best.name if best else "Unmatched",
            "confidence": best.confidence if best else "unmatched",
            "coverage": round_optional(best.coverage_percent, 1) if best else None,
            "color": colors[index % len(colors)],
            "coordinates": [[point.latitude, point.longitude] for point in analysis.points],
            "distance_km": round_optional(analysis.distance_km),
            "point_count": analysis.point_count,
            "warnings": identification.warnings,
            "landmarks": [landmark_to_dict(landmark) for landmark in identification.nearby_landmarks[:20]],
        })
    return map_tracks


HTML_REPORT_TEMPLATE = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GPX trail identification</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>html,body,#map{height:100%;margin:0}body{font-family:system-ui,sans-serif}#panel{position:absolute;z-index:1000;top:12px;right:12px;width:min(430px,calc(100vw - 44px));max-height:calc(100vh - 48px);overflow:auto;background:#fffffff2;padding:12px 14px;border-radius:8px;box-shadow:0 2px 12px #0004}.card{border-top:1px solid #ddd;padding:9px 0;cursor:pointer}.card h2,.card p{margin:3px 0}.card h2{font-size:14px}.card p{font-size:12px}</style>
</head><body><div id="map"></div><div id="panel"><h3>Live OSM trail identification</h3><div id="cards"></div></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>
const tracks=__TRACK_DATA__,map=L.map('map'),bounds=[],layers={},cards=document.getElementById('cards');
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}).addTo(map);
tracks.forEach(t=>{const g=L.layerGroup().addTo(map),line=L.polyline(t.coordinates,{color:t.color,weight:5}).addTo(g);bounds.push(...t.coordinates);t.landmarks.forEach(m=>L.circleMarker([m.latitude,m.longitude],{radius:6,color:m.visited?'#167c3b':'#9a5500'}).addTo(g).bindPopup(`<b>${m.name}</b><br>${m.category}<br>${m.distance_m} m`));layers[`${t.file} — ${t.trail}`]=g;const d=document.createElement('div');d.className='card';d.innerHTML=`<h2 style="color:${t.color}">${t.file}</h2><p><b>${t.trail}</b> · ${t.confidence}${t.coverage===null?'':` · ${t.coverage}%`}</p><p>${t.distance_km} km · ${t.point_count} points</p>${t.warnings.map(w=>`<p><i>${w}</i></p>`).join('')}`;d.onclick=()=>map.fitBounds(line.getBounds().pad(.15));cards.appendChild(d)});
L.control.layers(null,layers,{collapsed:false}).addTo(map);if(bounds.length)map.fitBounds(bounds,{padding:[25,25]});
</script></body></html>'''


def write_html_report(identifications: Sequence[TrailIdentification], path: Path) -> None:
    map_data = json.dumps(build_map_track_data(identifications), ensure_ascii=False).replace("</", "<\\/")
    path.write_text(HTML_REPORT_TEMPLATE.replace("__TRACK_DATA__", map_data), encoding="utf-8")


def write_reports(
    identifications: Sequence[TrailIdentification],
    output_directory: Path,
) -> dict[str, Path]:
    """Write all report formats and return their paths."""

    output_directory.mkdir(parents=True, exist_ok=True)
    report_paths = {
        "csv": output_directory / "trail_identification.csv",
        "json": output_directory / "trail_identification.json",
        "geojson": output_directory / "trail_identification.geojson",
        "html": output_directory / "trail_identification.html",
    }
    write_csv_report(identifications, report_paths["csv"])
    write_json_report(identifications, report_paths["json"])
    write_geojson_report(identifications, report_paths["geojson"])
    write_html_report(identifications, report_paths["html"])
    return report_paths


# -----------------------------------------------------------------------------
# Command-line interface
# -----------------------------------------------------------------------------


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Identify likely trails using live OpenStreetMap/Overpass data."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("inputs", nargs="+", type=Path, help="GPX files or directories")
    parser.add_argument("--recursive", action="store_true", help="Search input directories recursively")
    parser.add_argument("--output-dir", type=Path, default=Path("trail_analysis"))
    parser.add_argument(
        "--overpass-url", action="append", dest="overpass_urls",
        help="Overpass interpreter URL; repeat to define failover order",
    )
    parser.add_argument("--route-match-radius-m", type=float, default=70.0)
    parser.add_argument("--landmark-visit-radius-m", type=float, default=180.0)
    parser.add_argument("--query-padding-m", type=float, default=1500.0)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    return parser


def validate_positive_options(
    parser: argparse.ArgumentParser, arguments: argparse.Namespace
) -> None:
    """Reject zero or negative distance and timeout options."""

    option_names = (
        "route_match_radius_m", "landmark_visit_radius_m",
        "query_padding_m", "timeout_seconds",
    )
    for option_name in option_names:
        if getattr(arguments, option_name) <= 0:
            option = option_name.replace("_", "-")
            parser.error(f"--{option} must be greater than zero")


def print_identification_summary(result: TrailIdentification) -> None:
    """Print one concise console summary."""

    best_match = result.best_match
    match_text = (
        f"{best_match.name} ({best_match.confidence}, score {best_match.score:.3f})"
        if best_match else "unmatched"
    )
    analysis = result.analysis
    print(
        f"{analysis.input_file}: {match_text}; "
        f"{analysis.distance_km:.2f} km; {analysis.point_count} points"
    )
    print(f"  live data: {result.overpass_endpoint}")
    print(
        f"  OSM results: {len(result.candidates)} candidate(s), "
        f"{len(result.nearby_landmarks)} landmark(s), "
        f"OSM base: {result.osm_base_timestamp or 'unknown'}"
    )
    for warning in result.warnings:
        print(f"  warning: {warning}")


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, identify each GPX file, and write the reports."""

    parser = build_argument_parser()
    arguments = parser.parse_args(argv)
    validate_positive_options(parser, arguments)

    gpx_files = discover_gpx_files(arguments.inputs, arguments.recursive)
    if not gpx_files:
        parser.error("No GPX files found")

    overpass_urls = tuple(arguments.overpass_urls or DEFAULT_OVERPASS_URLS)
    identifications: list[TrailIdentification] = []

    try:
        for gpx_file in gpx_files:
            identifications.append(
                identify_trail(
                    gpx_file,
                    overpass_urls=overpass_urls,
                    route_match_radius_m=arguments.route_match_radius_m,
                    landmark_visit_radius_m=arguments.landmark_visit_radius_m,
                    query_padding_m=arguments.query_padding_m,
                    timeout_seconds=arguments.timeout_seconds,
                )
            )
        report_paths = write_reports(identifications, arguments.output_dir)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        parser.exit(1, f"Error: {error}\n")

    for identification in identifications:
        print_identification_summary(identification)

    print("Outputs:")
    for report_type, report_path in report_paths.items():
        print(f"  {report_type}: {report_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
