from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lxml import etree

import merge_gpx

GPX_NAMESPACE = "http://www.topografix.com/GPX/1/1"
EXTENSION_NAMESPACE = "https://example.com/extensions"
MERGE_NAMESPACE = merge_gpx.MERGE_NAMESPACE
NS = {"gpx": GPX_NAMESPACE, "ext": EXTENSION_NAMESPACE, "merge": MERGE_NAMESPACE}


class MergeGpxTests(unittest.TestCase):
    def write_file(self, directory: Path, name: str, content: str) -> Path:
        path = directory / name
        path.write_text(content, encoding="utf-8")
        return path

    def canonical_segments(self, path: Path) -> list[bytes]:
        tree = etree.parse(str(path))
        return [
            etree.tostring(segment, method="c14n", exclusive=True)
            for segment in tree.xpath("/gpx:gpx/gpx:trk/gpx:trkseg", namespaces=NS)
        ]

    def test_sorts_and_merges_tracks_while_preserving_all_segments(self) -> None:
        earlier = f"""<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="{GPX_NAMESPACE}" xmlns:ext="{EXTENSION_NAMESPACE}" version="1.1" creator="first">
  <metadata><name>First metadata</name></metadata>
  <wpt lat="0" lon="0"><name>First waypoint</name></wpt>
  <trk source="device-a">
    <name>Morning Part</name>
    <desc>First description</desc>
    <extensions><ext:first-track value="kept"/></extensions>
    <trkseg id="a">
      <trkpt lat="0" lon="0"><time>2026-07-01T10:00:00Z</time><extensions><ext:p>one</ext:p></extensions></trkpt>
    </trkseg>
    <trkseg id="b">
      <extensions><ext:segment>before</ext:segment></extensions>
      <trkpt lat="0" lon="0.001"><time>2026-07-01T10:10:00Z</time></trkpt>
    </trkseg>
  </trk>
  <extensions><ext:first-root>kept</ext:first-root></extensions>
</gpx>
"""
        later = f"""<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="{GPX_NAMESPACE}" xmlns:ext="{EXTENSION_NAMESPACE}" version="1.1" creator="second">
  <metadata><name>Second metadata</name></metadata>
  <wpt lat="0" lon="0.002"><name>Second waypoint</name></wpt>
  <trk source="device-b">
    <name>Morning Continuation</name>
    <desc>Second description</desc>
    <extensions><ext:second-track value="kept"/></extensions>
    <trkseg id="c">
      <trkpt lat="0" lon="0.002"><time>2026-07-01T10:20:00Z</time><ele>25</ele></trkpt>
    </trkseg>
  </trk>
  <extensions><ext:second-root>kept</ext:second-root></extensions>
</gpx>
"""
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            directory = Path(temporary_directory_name)
            earlier_path = self.write_file(directory, "later-listed.gpx", earlier)
            later_path = self.write_file(directory, "earlier-listed.gpx", later)
            original_segments = self.canonical_segments(earlier_path) + self.canonical_segments(later_path)
            output_path = directory / "merged.gpx"

            written_path, groups = merge_gpx.merge_gpx_files(
                [later_path, earlier_path],
                output_path,
                maximum_time_gap_hours=1,
                maximum_distance_gap_km=1,
            )

            self.assertEqual(output_path.resolve(), written_path)
            self.assertEqual([2], [len(group) for group in groups])
            tree = etree.parse(str(output_path))
            self.assertEqual(1.0, tree.xpath("count(/gpx:gpx/gpx:trk)", namespaces=NS))
            self.assertEqual(3.0, tree.xpath("count(/gpx:gpx/gpx:trk/gpx:trkseg)", namespaces=NS))
            self.assertEqual(
                ["a", "b", "c"],
                tree.xpath("/gpx:gpx/gpx:trk/gpx:trkseg/@id", namespaces=NS),
            )
            output_segments = [
                etree.tostring(segment, method="c14n", exclusive=True)
                for segment in tree.xpath("/gpx:gpx/gpx:trk/gpx:trkseg", namespaces=NS)
            ]
            self.assertEqual(original_segments, output_segments)
            self.assertCountEqual(
                ["First waypoint", "Second waypoint"],
                tree.xpath("/gpx:gpx/gpx:wpt/gpx:name/text()", namespaces=NS),
            )
            self.assertEqual(
                "kept",
                tree.xpath("string(/gpx:gpx/gpx:extensions/ext:first-root)", namespaces=NS),
            )
            self.assertEqual(
                "kept",
                tree.xpath("string(/gpx:gpx/gpx:extensions/ext:second-root)", namespaces=NS),
            )
            self.assertEqual(
                ["Morning Part", "Morning Continuation"],
                tree.xpath(
                    "/gpx:gpx/gpx:trk/gpx:extensions/merge:sourceTracks/"
                    "merge:sourceTrack/merge:trackMetadata/gpx:name/text()",
                    namespaces=NS,
                ),
            )
            self.assertEqual(
                "Second metadata",
                tree.xpath(
                    "string(/gpx:gpx/gpx:extensions/merge:sourceDocuments/"
                    "merge:sourceDocument[@file='earlier-listed.gpx']/gpx:metadata/gpx:name)",
                    namespaces=NS,
                ),
            )

    def test_keeps_nonmatching_tracks_separate_and_sorted(self) -> None:
        source = f"""<gpx xmlns="{GPX_NAMESPACE}" version="1.1" creator="test">
  <trk><name>Late</name><trkseg><trkpt lat="10" lon="10"><time>2026-07-01T15:00:00Z</time></trkpt></trkseg></trk>
  <trk><name>Early</name><trkseg><trkpt lat="0" lon="0"><time>2026-07-01T09:00:00Z</time></trkpt></trkseg></trk>
</gpx>"""
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            directory = Path(temporary_directory_name)
            source_path = self.write_file(directory, "input.gpx", source)
            output_path = directory / "merged.gpx"

            _, groups = merge_gpx.merge_gpx_files(
                [source_path],
                output_path,
                maximum_time_gap_hours=1,
                maximum_distance_gap_km=1,
            )

            self.assertEqual([1, 1], [len(group) for group in groups])
            tree = etree.parse(str(output_path))
            self.assertEqual(
                ["Early", "Late"],
                tree.xpath("/gpx:gpx/gpx:trk/gpx:name/text()", namespaces=NS),
            )

    def test_exactly_touching_intervals_can_merge(self) -> None:
        source = f"""<gpx xmlns="{GPX_NAMESPACE}" version="1.1" creator="test">
  <trk><name>First</name><trkseg><trkpt lat="0" lon="0"><time>2026-07-01T10:00:00Z</time></trkpt></trkseg></trk>
  <trk><name>Second</name><trkseg><trkpt lat="0" lon="0"><time>2026-07-01T10:00:00Z</time></trkpt></trkseg></trk>
</gpx>"""
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            directory = Path(temporary_directory_name)
            source_path = self.write_file(directory, "input.gpx", source)
            output_path = directory / "merged.gpx"

            _, groups = merge_gpx.merge_gpx_files(
                [source_path],
                output_path,
                maximum_time_gap_hours=0,
                maximum_distance_gap_km=0,
            )

            self.assertEqual([2], [len(group) for group in groups])

    def test_overlapping_tracks_abort_without_writing_output(self) -> None:
        source = f"""<gpx xmlns="{GPX_NAMESPACE}" version="1.1" creator="test">
  <trk><name>First</name><trkseg>
    <trkpt lat="0" lon="0"><time>2026-07-01T10:00:00Z</time></trkpt>
    <trkpt lat="0" lon="0.01"><time>2026-07-01T11:00:00Z</time></trkpt>
  </trkseg></trk>
  <trk><name>Second</name><trkseg>
    <trkpt lat="0" lon="0.01"><time>2026-07-01T10:30:00Z</time></trkpt>
    <trkpt lat="0" lon="0.02"><time>2026-07-01T12:00:00Z</time></trkpt>
  </trkseg></trk>
</gpx>"""
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            directory = Path(temporary_directory_name)
            source_path = self.write_file(directory, "input.gpx", source)
            output_path = directory / "merged.gpx"

            with self.assertRaisesRegex(
                ValueError,
                r"Tracks overlap in time; merge aborted.*First.*Second.*1800 second",
            ):
                merge_gpx.merge_gpx_files([source_path], output_path)
            self.assertFalse(output_path.exists())

    def test_backward_or_missing_timestamps_abort(self) -> None:
        backward = f"""<gpx xmlns="{GPX_NAMESPACE}" version="1.1" creator="test">
  <trk><name>Backward</name><trkseg>
    <trkpt lat="0" lon="0"><time>2026-07-01T11:00:00Z</time></trkpt>
    <trkpt lat="0" lon="0.01"><time>2026-07-01T10:00:00Z</time></trkpt>
  </trkseg></trk>
</gpx>"""
        missing = f"""<gpx xmlns="{GPX_NAMESPACE}" version="1.1" creator="test">
  <trk><name>Missing</name><trkseg><trkpt lat="0" lon="0"/></trkseg></trk>
</gpx>"""
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            directory = Path(temporary_directory_name)
            backward_path = self.write_file(directory, "backward.gpx", backward)
            missing_path = self.write_file(directory, "missing.gpx", missing)

            with self.assertRaisesRegex(ValueError, "moves backward in time"):
                merge_gpx.merge_gpx_files([backward_path], directory / "a.gpx")
            with self.assertRaisesRegex(ValueError, "no valid <time>"):
                merge_gpx.merge_gpx_files([missing_path], directory / "b.gpx")


if __name__ == "__main__":
    unittest.main()
