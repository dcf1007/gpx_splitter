from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lxml import etree

import identify_trails


class TrailIdentifierTests(unittest.TestCase):
    def test_multiple_tracks_are_rejected(self) -> None:
        gpx = """<?xml version="1.0"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>First</name><trkseg><trkpt lat="1" lon="1"/></trkseg></trk>
  <trk><name>Second</name><trkseg><trkpt lat="2" lon="2"/></trkseg></trk>
</gpx>
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "multiple.gpx"
            path.write_text(gpx, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Multiple GPX tracks found"):
                identify_trails.analyze_track(path)

    def test_multiple_highlights_create_a_dated_described_copy(self) -> None:
        gpx = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <metadata><time>2025-05-06T09:00:00Z</time></metadata>
  <trk>
    <name>Walk</name>
    <desc>Original description.</desc>
    <trkseg>
      <trkpt lat="40.0" lon="-3.0"/>
      <trkpt lat="40.001" lon="-3.001"/>
    </trkseg>
  </trk>
</gpx>
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source_path = directory / "source.gpx"
            source_path.write_text(gpx, encoding="utf-8")
            original_contents = source_path.read_bytes()

            analysis = identify_trails.analyze_track(source_path)
            landmarks = [
                identify_trails.Landmark(
                    "cave",
                    "Main Cave",
                    "natural=cave_entrance",
                    40.0,
                    -3.0,
                    5.0,
                    True,
                    180.0,
                ),
                identify_trails.Landmark(
                    "view",
                    "River View",
                    "tourism=viewpoint",
                    40.0,
                    -3.0,
                    20.0,
                    True,
                    180.0,
                ),
                identify_trails.Landmark(
                    "parking",
                    "Parking Area",
                    "amenity=parking",
                    40.0,
                    -3.0,
                    1.0,
                    True,
                    180.0,
                ),
            ]
            identification = identify_trails.Identification(
                analysis=analysis,
                best_match=None,
                candidates=[],
                landmarks=landmarks,
                warnings=[],
                overpass_endpoint="test",
                osm_timestamp=None,
            )

            output_directory = directory / "results"
            output_paths = identify_trails.write_highlight_copies(
                [identification],
                [source_path],
                output_directory,
            )

            self.assertEqual(
                [output_directory / "2025-05-06_Main-Cave.gpx"],
                output_paths,
            )
            self.assertEqual(original_contents, source_path.read_bytes())

            tree = etree.parse(str(output_paths[0]))
            description = tree.xpath(
                'string(/*[local-name()="gpx"]/*[local-name()="trk"]/*[local-name()="desc"])'
            )
            self.assertEqual(
                "Original description.\n\n"
                "Main highlight: Main Cave. "
                "Other visited highlights: River View.",
                description,
            )


if __name__ == "__main__":
    unittest.main()
