"""Data models shared by the GPX parser, OSM matcher, and report writers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


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
    landmarks: list[Landmark] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FileResult:
    analysis: TrackAnalysis
    best_match: Candidate | None
    candidates: list[Candidate]
    nearby_landmarks: list[Landmark]
    warnings: list[str]
    overpass_endpoint: str
    osm_base_timestamp: str | None
