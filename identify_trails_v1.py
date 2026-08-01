#!/usr/bin/env python3
"""Command-line entry point for GPX Trail Identifier version 1."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from trail_identifier_v1 import (
    VERSION,
    analyze_file,
    discover_gpx_files,
    write_outputs,
)
from trail_identifier_v1.osm import DEFAULT_OVERPASS_URLS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Identify likely trails using live OpenStreetMap/Overpass data."
        )
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="GPX files or directories",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Search input directories recursively",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("trail_analysis"),
    )
    parser.add_argument(
        "--overpass-url",
        action="append",
        dest="overpass_urls",
        help="Overpass interpreter URL; repeat to set failover order",
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    files = discover_gpx_files(arguments.inputs, arguments.recursive)
    if not files:
        parser.error("No GPX files found")

    numeric_options = (
        "route_match_radius_m",
        "landmark_visit_radius_m",
        "query_padding_m",
        "timeout_seconds",
    )
    for option_name in numeric_options:
        if getattr(arguments, option_name) <= 0:
            parser.error(
                f"--{option_name.replace('_', '-')} must be greater than zero"
            )

    overpass_urls = tuple(
        arguments.overpass_urls or DEFAULT_OVERPASS_URLS
    )
    try:
        results = [
            analyze_file(
                path,
                overpass_urls=overpass_urls,
                route_match_radius_m=arguments.route_match_radius_m,
                landmark_visit_radius_m=(
                    arguments.landmark_visit_radius_m
                ),
                query_padding_m=arguments.query_padding_m,
                timeout_seconds=arguments.timeout_seconds,
            )
            for path in files
        ]
        outputs = write_outputs(results, arguments.output_dir)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        parser.exit(1, f"Error: {error}\n")

    for result in results:
        best = result.best_match
        match_text = (
            f"{best.name} ({best.confidence}, score {best.score:.3f})"
            if best
            else "unmatched"
        )
        print(
            f"{result.analysis.input_file}: {match_text}; "
            f"{result.analysis.distance_km:.2f} km; "
            f"{result.analysis.point_count} points"
        )
        print(f"  live data: {result.overpass_endpoint}")
        print(
            f"  OSM results: {len(result.candidates)} candidate(s), "
            f"{len(result.nearby_landmarks)} landmark(s), "
            f"OSM base: {result.osm_base_timestamp or 'unknown'}"
        )
        for warning in result.warnings:
            print(f"  warning: {warning}")

    print("Outputs:")
    for name, output_path in outputs.items():
        print(f"  {name}: {output_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
