"""Streaming GPX parsing and deterministic track statistics."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from lxml import etree

from .models import TrackAnalysis, TrackPoint

EARTH_RADIUS_M = 6_371_008.8


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_timestamp(value: str | None) -> datetime | None:
    if not value or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith(("Z", "z")):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def haversine_m(first: tuple[float, float], second: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, first)
    lat2, lon2 = map(math.radians, second)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    value = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(value)))


def _child_text(element: etree._Element, name: str) -> str | None:
    for child in element:
        if isinstance(child.tag, str) and local_name(child.tag) == name:
            return child.text
    return None


def analyze_gpx(path: Path) -> TrackAnalysis:
    points: list[TrackPoint] = []
    track_name: str | None = None
    malformed_timestamps = 0

    try:
        iterator = etree.iterparse(
            str(path), events=("end",), huge_tree=True, remove_blank_text=False
        )
        for _, element in iterator:
            if not isinstance(element.tag, str):
                continue
            name = local_name(element.tag)
            parent = element.getparent()
            if name == "name" and track_name is None and parent is not None:
                if isinstance(parent.tag, str) and local_name(parent.tag) == "trk":
                    track_name = "".join(element.itertext()).strip() or None
            elif name == "trkpt":
                try:
                    latitude = float(element.attrib["lat"])
                    longitude = float(element.attrib["lon"])
                except (KeyError, ValueError) as error:
                    raise ValueError(f"Invalid track-point coordinates in {path}") from error
                if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                    raise ValueError(f"Out-of-range track-point coordinates in {path}")

                elevation_text = _child_text(element, "ele")
                timestamp_text = _child_text(element, "time")
                try:
                    elevation = float(elevation_text) if elevation_text else None
                except ValueError:
                    elevation = None
                timestamp = parse_timestamp(timestamp_text)
                if timestamp_text and timestamp is None:
                    malformed_timestamps += 1
                points.append(TrackPoint(latitude, longitude, elevation, timestamp))
                element.clear(keep_tail=True)
                if parent is not None:
                    while element.getprevious() is not None:
                        del parent[0]
    except etree.XMLSyntaxError as error:
        raise ValueError(f"Invalid GPX/XML in {path}: {error}") from error

    if not points:
        raise ValueError(f"No GPX track points found in {path}")

    distance_m = sum(
        haversine_m(previous.coordinate, current.coordinate)
        for previous, current in zip(points, points[1:])
    )
    start_end_m = haversine_m(points[0].coordinate, points[-1].coordinate)
    closed_threshold_m = max(50.0, min(250.0, distance_m * 0.03))

    timestamps = [point.timestamp for point in points if point.timestamp is not None]
    forward = backward = equal = 0
    for previous, current in zip(timestamps, timestamps[1:]):
        if current > previous:
            forward += 1
        elif current < previous:
            backward += 1
        else:
            equal += 1

    elevations = [point.elevation_m for point in points if point.elevation_m is not None]
    gain = loss = None
    if elevations:
        gain = loss = 0.0
        previous_elevation: float | None = None
        for point in points:
            if point.elevation_m is None:
                continue
            if previous_elevation is not None:
                change = point.elevation_m - previous_elevation
                gain += max(change, 0.0)
                loss += max(-change, 0.0)
            previous_elevation = point.elevation_m

    latitudes = [point.latitude for point in points]
    longitudes = [point.longitude for point in points]
    duration = (
        (max(timestamps) - min(timestamps)).total_seconds() / 60
        if timestamps
        else None
    )
    return TrackAnalysis(
        input_file=path.name,
        track_name=track_name or path.stem,
        points=points,
        distance_km=distance_m / 1000,
        start_end_distance_m=start_end_m,
        closed_track=start_end_m <= closed_threshold_m,
        malformed_timestamp_count=malformed_timestamps,
        forward_timestamp_transitions=forward,
        backward_timestamp_transitions=backward,
        equal_timestamp_transitions=equal,
        duration_minutes=duration,
        elevation_min_m=min(elevations) if elevations else None,
        elevation_max_m=max(elevations) if elevations else None,
        elevation_gain_m=gain,
        elevation_loss_m=loss,
        bounds=(min(latitudes), min(longitudes), max(latitudes), max(longitudes)),
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
