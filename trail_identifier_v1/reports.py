"""CSV, JSON, GeoJSON, and interactive HTML report writers."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from .models import Candidate, FileResult, Landmark, TrackAnalysis


def _round(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def _analysis_dict(analysis: TrackAnalysis) -> dict[str, Any]:
    return {
        "input_file": analysis.input_file,
        "track_name": analysis.track_name,
        "point_count": analysis.point_count,
        "distance_km": _round(analysis.distance_km),
        "start_end_distance_m": _round(analysis.start_end_distance_m, 1),
        "closed_track": analysis.closed_track,
        "timed_point_count": analysis.timed_point_count,
        "malformed_timestamp_count": analysis.malformed_timestamp_count,
        "forward_timestamp_transitions": analysis.forward_timestamp_transitions,
        "backward_timestamp_transitions": analysis.backward_timestamp_transitions,
        "equal_timestamp_transitions": analysis.equal_timestamp_transitions,
        "timestamp_order": analysis.timestamp_order,
        "duration_minutes": _round(analysis.duration_minutes, 1),
        "elevation_min_m": _round(analysis.elevation_min_m, 1),
        "elevation_max_m": _round(analysis.elevation_max_m, 1),
        "elevation_gain_m": _round(analysis.elevation_gain_m, 1),
        "elevation_loss_m": _round(analysis.elevation_loss_m, 1),
        "bounds": list(analysis.bounds),
    }


def _landmark_dict(landmark: Landmark) -> dict[str, Any]:
    data = asdict(landmark)
    data["distance_m"] = round(landmark.distance_m, 1)
    data["visit_radius_m"] = round(landmark.visit_radius_m, 1)
    return data


def _candidate_dict(candidate: Candidate) -> dict[str, Any]:
    return {
        "identifier": candidate.identifier,
        "name": candidate.name,
        "source": candidate.source,
        "score": round(candidate.score, 4),
        "confidence": candidate.confidence,
        "coverage_percent": _round(candidate.coverage_percent, 1),
        "median_distance_m": _round(candidate.median_distance_m, 1),
        "osm_relation_id": candidate.osm_relation_id,
        "tags": candidate.tags,
        "landmarks": [
            _landmark_dict(landmark) for landmark in candidate.landmarks
        ],
        "notes": candidate.notes,
    }


def write_json(results: Sequence[FileResult], path: Path) -> None:
    payload = [
        {
            "analysis": _analysis_dict(result.analysis),
            "best_match": (
                _candidate_dict(result.best_match)
                if result.best_match
                else None
            ),
            "candidates": [
                _candidate_dict(candidate) for candidate in result.candidates
            ],
            "nearby_landmarks": [
                _landmark_dict(landmark)
                for landmark in result.nearby_landmarks
            ],
            "warnings": result.warnings,
            "overpass_endpoint": result.overpass_endpoint,
            "osm_base_timestamp": result.osm_base_timestamp,
        }
        for result in results
    ]
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_csv(results: Sequence[FileResult], path: Path) -> None:
    fields = [
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
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for result in results:
            analysis = result.analysis
            best = result.best_match
            writer.writerow(
                {
                    "file": analysis.input_file,
                    "track_name": analysis.track_name,
                    "likely_trail": best.name if best else "Unmatched",
                    "source": best.source if best else "",
                    "confidence": best.confidence if best else "unmatched",
                    "score": _round(best.score, 3) if best else "",
                    "coverage_percent": (
                        _round(best.coverage_percent, 1) if best else ""
                    ),
                    "distance_km": _round(analysis.distance_km),
                    "point_count": analysis.point_count,
                    "duration_minutes": _round(analysis.duration_minutes, 1),
                    "elevation_min_m": _round(analysis.elevation_min_m, 1),
                    "elevation_max_m": _round(analysis.elevation_max_m, 1),
                    "timestamp_order": analysis.timestamp_order,
                    "closed_track": analysis.closed_track,
                    "overpass_endpoint": result.overpass_endpoint,
                    "osm_base_timestamp": result.osm_base_timestamp or "",
                    "warnings": " | ".join(result.warnings),
                }
            )


def write_geojson(results: Sequence[FileResult], path: Path) -> None:
    features: list[dict[str, Any]] = []
    for result in results:
        analysis = result.analysis
        best = result.best_match
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "file": analysis.input_file,
                    "name": best.name if best else "Unmatched",
                    "confidence": (
                        best.confidence if best else "unmatched"
                    ),
                    "distance_km": _round(analysis.distance_km),
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [point.longitude, point.latitude]
                        for point in analysis.points
                    ],
                },
            }
        )
        for landmark in result.nearby_landmarks:
            features.append(
                {
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
                        "coordinates": [
                            landmark.longitude,
                            landmark.latitude,
                        ],
                    },
                }
            )
    path.write_text(
        json.dumps(
            {"type": "FeatureCollection", "features": features},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def write_html(results: Sequence[FileResult], path: Path) -> None:
    colors = [
        "#1565c0",
        "#2e7d32",
        "#c62828",
        "#6a1b9a",
        "#ef6c00",
        "#00838f",
    ]
    tracks = []
    for index, result in enumerate(results):
        analysis = result.analysis
        best = result.best_match
        tracks.append(
            {
                "file": analysis.input_file,
                "trail": best.name if best else "Unmatched",
                "confidence": best.confidence if best else "unmatched",
                "coverage": (
                    _round(best.coverage_percent, 1) if best else None
                ),
                "color": colors[index % len(colors)],
                "coordinates": [
                    [point.latitude, point.longitude]
                    for point in analysis.points
                ],
                "distance_km": _round(analysis.distance_km),
                "point_count": analysis.point_count,
                "warnings": result.warnings,
                "landmarks": [
                    _landmark_dict(landmark)
                    for landmark in result.nearby_landmarks[:20]
                ],
            }
        )

    payload = json.dumps(tracks, ensure_ascii=False).replace("</", "<\\/")
    page = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GPX trail identification</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>html,body,#map{{height:100%;margin:0}}body{{font-family:system-ui,sans-serif}}#panel{{position:absolute;z-index:1000;top:12px;right:12px;width:min(430px,calc(100vw - 44px));max-height:calc(100vh - 48px);overflow:auto;background:#fffffff2;padding:12px 14px;border-radius:8px;box-shadow:0 2px 12px #0004}}.card{{border-top:1px solid #ddd;padding:9px 0;cursor:pointer}}.card h2,.card p{{margin:3px 0}}.card h2{{font-size:14px}}.card p{{font-size:12px}}</style>
</head><body><div id="map"></div><div id="panel"><h3>Live OSM trail identification</h3><div id="cards"></div></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>
const tracks={payload},map=L.map('map'),bounds=[],layers={{}},cards=document.getElementById('cards');
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}}).addTo(map);
tracks.forEach(t=>{{const g=L.layerGroup().addTo(map),line=L.polyline(t.coordinates,{{color:t.color,weight:5}}).addTo(g);bounds.push(...t.coordinates);t.landmarks.forEach(m=>L.circleMarker([m.latitude,m.longitude],{{radius:6,color:m.visited?'#167c3b':'#9a5500'}}).addTo(g).bindPopup(`<b>${{m.name}}</b><br>${{m.category}}<br>${{m.distance_m}} m`));layers[`${{t.file}} — ${{t.trail}}`]=g;const d=document.createElement('div');d.className='card';d.innerHTML=`<h2 style="color:${{t.color}}">${{t.file}}</h2><p><b>${{t.trail}}</b> · ${{t.confidence}}${{t.coverage===null?'':` · ${{t.coverage}}%`}}</p><p>${{t.distance_km}} km · ${{t.point_count}} points</p>${{t.warnings.map(w=>`<p><i>${{w}}</i></p>`).join('')}}`;d.onclick=()=>map.fitBounds(line.getBounds().pad(.15));cards.appendChild(d)}});
L.control.layers(null,layers,{{collapsed:false}}).addTo(map);if(bounds.length)map.fitBounds(bounds,{{padding:[25,25]}});
</script></body></html>'''
    path.write_text(page, encoding="utf-8")


def write_outputs(
    results: Sequence[FileResult], output_directory: Path
) -> dict[str, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "csv": output_directory / "trail_identification.csv",
        "json": output_directory / "trail_identification.json",
        "geojson": output_directory / "trail_identification.geojson",
        "html": output_directory / "trail_identification.html",
    }
    write_csv(results, paths["csv"])
    write_json(results, paths["json"])
    write_geojson(results, paths["geojson"])
    write_html(results, paths["html"])
    return paths
