from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lxml import etree

from gpx_splitter import split_gpx_tracks


GPX_NAMESPACE = "http://www.topografix.com/GPX/1/1"
EXTENSION_NAMESPACE = "https://example.com/extensions"
NAMESPACES = {"gpx": GPX_NAMESPACE, "ext": EXTENSION_NAMESPACE}


class GpxSplitterTests(unittest.TestCase):
    def write_input(self, directory: Path, body: str) -> Path:
        input_path = directory / "input.gpx"
        input_path.write_text(body, encoding="utf-8")
        return input_path

    def parse_output(self, output_path: Path) -> etree._ElementTree:
        return etree.parse(str(output_path))

    def test_splits_on_date_time_gap_and_untimed_distance(self) -> None:
        source = f"""<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="{GPX_NAMESPACE}" xmlns:ext="{EXTENSION_NAMESPACE}" version="1.1" creator="test">
  <metadata><name>Archive</name></metadata>
  <wpt lat="1" lon="1"><name>Excluded waypoint</name></wpt>
  <rte><name>Excluded route</name></rte>
  <trk>
    <name>Morning/Ride</name>
    <desc>Preserved description</desc>
    <extensions><ext:track-data value="kept"/></extensions>
    <trkseg>
      <trkpt lat="0" lon="0"><ele>10</ele><time>2026-07-01T23:59:00Z</time></trkpt>
      <trkpt lat="0" lon="0.01"><time>2026-07-02T00:01:00Z</time></trkpt>
      <trkpt lat="0" lon="0.02"><time>2026-07-02T08:30:00Z</time></trkpt>
      <trkpt lat="2" lon="2"><extensions><ext:point-data>kept</ext:point-data></extensions></trkpt>
      <extensions><ext:segment-data>kept</ext:segment-data></extensions>
    </trkseg>
  </trk>
  <extensions><ext:root-data>kept</ext:root-data></extensions>
</gpx>
"""

        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            input_path = self.write_input(temporary_directory, source)
            output_directory = temporary_directory / "output"

            outputs = split_gpx_tracks(input_path, output_directory)

            self.assertEqual(
                [path.name for path in outputs],
                [
                    "2026-07-01_Morning_Ride_1.gpx",
                    "2026-07-02_Morning_Ride_2.gpx",
                    "2026-07-02_Morning_Ride_3.gpx",
                    "Morning_Ride_4.gpx",
                ],
            )

            for output_path in outputs:
                document = self.parse_output(output_path)
                self.assertEqual(
                    document.xpath("string(/gpx:gpx/gpx:trk/gpx:name)", namespaces=NAMESPACES),
                    "Morning/Ride",
                )
                self.assertEqual(
                    document.xpath("count(/gpx:gpx/gpx:wpt)", namespaces=NAMESPACES),
                    0.0,
                )
                self.assertEqual(
                    document.xpath("count(/gpx:gpx/gpx:rte)", namespaces=NAMESPACES),
                    0.0,
                )
                self.assertEqual(
                    document.xpath(
                        "string(/gpx:gpx/gpx:trk/gpx:extensions/ext:track-data/@value)",
                        namespaces=NAMESPACES,
                    ),
                    "kept",
                )
                self.assertEqual(
                    document.xpath(
                        "string(/gpx:gpx/gpx:trk/gpx:trkseg/gpx:extensions/ext:segment-data)",
                        namespaces=NAMESPACES,
                    ),
                    "kept",
                )
                self.assertEqual(
                    document.xpath(
                        "string(/gpx:gpx/gpx:extensions/ext:root-data)",
                        namespaces=NAMESPACES,
                    ),
                    "kept",
                )

            fourth_document = self.parse_output(outputs[3])
            self.assertEqual(
                fourth_document.xpath(
                    "string(/gpx:gpx/gpx:trk/gpx:trkseg/gpx:trkpt/gpx:extensions/ext:point-data)",
                    namespaces=NAMESPACES,
                ),
                "kept",
            )

    def test_preserves_multiple_segment_boundaries_without_forcing_a_split(self) -> None:
        source = f"""<gpx xmlns="{GPX_NAMESPACE}" version="1.1" creator="test">
  <trk>
    <name>Two segments</name>
    <trkseg><trkpt lat="1" lon="1"><time>2026-07-01T10:00:00Z</time></trkpt></trkseg>
    <trkseg><trkpt lat="1.01" lon="1.01"><time>2026-07-01T10:05:00Z</time></trkpt></trkseg>
  </trk>
</gpx>"""

        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            input_path = self.write_input(temporary_directory, source)
            outputs = split_gpx_tracks(input_path, temporary_directory / "output")

            self.assertEqual(len(outputs), 1)
            document = self.parse_output(outputs[0])
            self.assertEqual(
                document.xpath(
                    "count(/gpx:gpx/gpx:trk/gpx:trkseg)",
                    namespaces=NAMESPACES,
                ),
                2.0,
            )

    def test_preserves_empty_tracks_and_empty_segments(self) -> None:
        source = f"""<gpx xmlns="{GPX_NAMESPACE}" version="1.1" creator="test">
  <trk><name>Empty segment</name><trkseg><extensions><flag>yes</flag></extensions></trkseg></trk>
  <trk><name>No segments</name><desc>metadata only</desc></trk>
</gpx>"""

        with tempfile.TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            input_path = self.write_input(temporary_directory, source)
            outputs = split_gpx_tracks(input_path, temporary_directory / "output")

            self.assertEqual(
                [path.name for path in outputs],
                ["Empty segment_1.gpx", "No segments_1.gpx"],
            )
            first_document = self.parse_output(outputs[0])
            second_document = self.parse_output(outputs[1])
            self.assertEqual(
                first_document.xpath(
                    "count(/gpx:gpx/gpx:trk/gpx:trkseg)", namespaces=NAMESPACES
                ),
                1.0,
            )
            self.assertEqual(
                second_document.xpath(
                    "string(/gpx:gpx/gpx:trk/gpx:desc)", namespaces=NAMESPACES
                ),
                "metadata only",
            )


if __name__ == "__main__":
    unittest.main()
