"""Live OpenStreetMap/Overpass discovery and geometric trail matching."""

from __future__ import annotations

import json
import math
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable, Sequence

from .gpx import EARTH_RADIUS_M, analyze_gpx, haversine_m
from .models import Candidate, FileResult, Landmark, TrackAnalysis, TrackPoint

DEFAULT_OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
USER_AGENT = (
    "gpx-splitter-trail-identifier/1.0 "
    "(+https://github.com/dcf1007/gpx_splitter)"
)


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


def _project(
    latitude: float, longitude: float, reference_latitude: float
) -> tuple[float, float]:
    return (
        math.radians(longitude)
        * EARTH_RADIUS_M
        * math.cos(math.radians(reference_latitude)),
        math.radians(latitude) * EARTH_RADIUS_M,
    )


def _point_segment_distance(
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
    nearest_x = sx + fraction * dx
    nearest_y = sy + fraction * dy
    return math.hypot(px - nearest_x, py - nearest_y)


def _sample_points(
    points: Sequence[TrackPoint], maximum_samples: int = 240
) -> list[TrackPoint]:
    if len(points) <= maximum_samples:
        return list(points)
    step = (len(points) - 1) / (maximum_samples - 1)
    indexes = sorted({round(index * step) for index in range(maximum_samples)})
    return [points[index] for index in indexes]


def _padded_bbox(
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


def build_overpass_query(
    bounds: tuple[float, float, float, float], padding_m: float
) -> str:
    bbox = _padded_bbox(bounds, padding_m)
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


def fetch_overpass_data(
    analysis: TrackAnalysis,
    overpass_urls: Sequence[str] = DEFAULT_OVERPASS_URLS,
    *,
    padding_m: float = 1500.0,
    timeout_seconds: float = 120.0,
) -> tuple[dict[str, Any], str]:
    if not overpass_urls:
        raise ValueError("At least one Overpass URL is required")

    body = urllib.parse.urlencode(
        {"data": build_overpass_query(analysis.bounds, padding_m)}
    ).encode("utf-8")
    failures: list[str] = []
    for index, endpoint in enumerate(overpass_urls):
        request = urllib.request.Request(
            endpoint,
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
            return data, endpoint
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            failures.append(f"{endpoint}: {error}")
            if index + 1 < len(overpass_urls):
                time.sleep(1.0)

    raise RuntimeError("All Overpass requests failed: " + " | ".join(failures))


def _feature_coordinate(element: dict[str, Any]) -> tuple[float, float] | None:
    latitude = element.get("lat")
    longitude = element.get("lon")
    if latitude is None or longitude is None:
        center = element.get("center") or {}
        latitude = center.get("lat")
        longitude = center.get("lon")
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


def _category(tags: dict[str, str]) -> str:
    for key in ("natural", "waterway", "tourism", "leisure", "amenity", "place"):
        if key in tags:
            return f"{key}={tags[key]}"
    return "named_feature"


def _nearest_track_point_distance(
    points: Iterable[TrackPoint], coordinate: tuple[float, float]
) -> float:
    return min(haversine_m(point.coordinate, coordinate) for point in points)


def parse_overpass_candidates(
    analysis: TrackAnalysis,
    data: dict[str, Any],
    *,
    route_match_radius_m: float = 70.0,
    landmark_visit_radius_m: float = 180.0,
    maximum_candidates: int = 8,
) -> tuple[list[Candidate], list[Landmark]]:
    reference_latitude = (analysis.bounds[0] + analysis.bounds[2]) / 2
    projected_track = [
        _project(point.latitude, point.longitude, reference_latitude)
        for point in _sample_points(analysis.points)
    ]
    routes: list[Candidate] = []
    landmarks: list[Landmark] = []

    for element in data.get("elements", []):
        tags = {
            str(key): str(value)
            for key, value in (element.get("tags") or {}).items()
        }
        element_type = str(element.get("type", ""))
        element_id = element.get("id")

        if element_type == "relation" and tags.get("route") in {
            "hiking",
            "foot",
            "walking",
        }:
            segments: list[
                tuple[tuple[float, float], tuple[float, float]]
            ] = []
            for member in element.get("members") or []:
                geometry = [
                    _project(
                        float(point["lat"]),
                        float(point["lon"]),
                        reference_latitude,
                    )
                    for point in member.get("geometry") or []
                    if "lat" in point and "lon" in point
                ]
                segments.extend(zip(geometry, geometry[1:]))
            if not segments:
                continue

            distances = [
                min(
                    _point_segment_distance(point, start, end)
                    for start, end in segments
                )
                for point in projected_track
            ]
            coverage = sum(
                distance <= route_match_radius_m for distance in distances
            ) / len(distances)
            median_distance = statistics.median(distances)
            proximity = max(
                0.0,
                1.0
                - median_distance / max(route_match_radius_m * 3.0, 1.0),
            )
            score = 0.80 * coverage + 0.20 * proximity
            route_name = (
                tags.get("name")
                or tags.get("ref")
                or f"OSM route {element_id}"
            )
            routes.append(
                Candidate(
                    identifier=f"osm_relation_{element_id}",
                    name=route_name,
                    source="openstreetmap_route",
                    score=score,
                    confidence=confidence_label(score),
                    coverage_percent=coverage * 100,
                    median_distance_m=median_distance,
                    osm_relation_id=(
                        int(element_id) if element_id is not None else None
                    ),
                    tags=tags,
                    notes=[
                        f"{coverage * 100:.1f}% of sampled GPX points are within "
                        f"{route_match_radius_m:.0f} m of the OSM route"
                    ],
                )
            )
            continue

        if not tags.get("name"):
            continue
        coordinate = _feature_coordinate(element)
        if coordinate is None:
            continue
        feature_category = _category(tags)
        radius = landmark_visit_radius_m
        if feature_category in {
            "natural=peak",
            "place=village",
            "place=hamlet",
        }:
            radius = max(radius, 300.0)
        distance_m = _nearest_track_point_distance(analysis.points, coordinate)
        landmarks.append(
            Landmark(
                identifier=f"osm_{element_type}_{element_id}",
                name=tags["name"],
                category=feature_category,
                latitude=coordinate[0],
                longitude=coordinate[1],
                distance_m=distance_m,
                visited=distance_m <= radius,
                visit_radius_m=radius,
                tags=tags,
            )
        )

    landmarks.sort(key=lambda item: item.distance_m)
    visited = [item for item in landmarks if item.visited]
    significant = {
        "natural=cave_entrance",
        "natural=spring",
        "natural=water",
        "waterway=waterfall",
        "tourism=attraction",
        "tourism=viewpoint",
        "leisure=nature_reserve",
    }
    landmark_candidates: list[Candidate] = []
    for landmark in visited[:maximum_candidates]:
        proximity = max(
            0.0,
            1.0 - landmark.distance_m / landmark.visit_radius_m,
        )
        score = min(
            0.95,
            0.55
            + 0.37 * proximity
            + (0.08 if landmark.category in significant else 0.0),
        )
        landmark_candidates.append(
            Candidate(
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
        )

    for route in routes:
        route.landmarks = visited[:10]
    candidates = routes + landmark_candidates
    candidates.sort(
        key=lambda item: (
            item.score,
            item.source == "openstreetmap_route",
        ),
        reverse=True,
    )
    return candidates[:maximum_candidates], landmarks[:30]


def result_warnings(analysis: TrackAnalysis) -> list[str]:
    warnings: list[str] = []
    if analysis.malformed_timestamp_count:
        warnings.append(
            f"{analysis.malformed_timestamp_count} malformed timestamp(s) were ignored"
        )
    if analysis.backward_timestamp_transitions:
        warnings.append(
            f"{analysis.backward_timestamp_transitions} backward timestamp "
            "transition(s); geometry remains usable but chronological direction "
            "is unreliable"
        )
    missing = analysis.point_count - analysis.timed_point_count
    if missing:
        warnings.append(f"{missing} point(s) have no valid timestamp")
    if not analysis.closed_track:
        warnings.append(
            f"track is not closed; start and end are "
            f"{analysis.start_end_distance_m:.0f} m apart"
        )
    return warnings


def analyze_file(
    path,
    *,
    overpass_urls: Sequence[str] = DEFAULT_OVERPASS_URLS,
    route_match_radius_m: float = 70.0,
    landmark_visit_radius_m: float = 180.0,
    query_padding_m: float = 1500.0,
    timeout_seconds: float = 120.0,
) -> FileResult:
    analysis = analyze_gpx(path)
    data, endpoint = fetch_overpass_data(
        analysis,
        overpass_urls,
        padding_m=query_padding_m,
        timeout_seconds=timeout_seconds,
    )
    candidates, landmarks = parse_overpass_candidates(
        analysis,
        data,
        route_match_radius_m=route_match_radius_m,
        landmark_visit_radius_m=landmark_visit_radius_m,
    )
    best_match = (
        candidates[0]
        if candidates and candidates[0].score >= 0.30
        else None
    )
    osm_timestamp = str(
        (data.get("osm3s") or {}).get("timestamp_osm_base") or ""
    ) or None
    return FileResult(
        analysis=analysis,
        best_match=best_match,
        candidates=candidates,
        nearby_landmarks=landmarks,
        warnings=result_warnings(analysis),
        overpass_endpoint=endpoint,
        osm_base_timestamp=osm_timestamp,
    )
