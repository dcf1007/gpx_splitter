"""Live OpenStreetMap trail identification for GPX tracks."""

VERSION = "1.0.0"

from .gpx import analyze_gpx, discover_gpx_files
from .osm import analyze_file
from .reports import write_outputs

__all__ = ["VERSION", "analyze_file", "analyze_gpx", "discover_gpx_files", "write_outputs"]
