from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import gpx_splitter
import identify_trails
import merge_gpx

GPX = """<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1" creator="test">
  <trk><name>{name}</name><trkseg>
    <trkpt lat="0" lon="0"><time>2026-07-01T10:00:00Z</time></trkpt>
  </trkseg></trk>
</gpx>"""


class StrictInputModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.directory = self.root / "inputs"
        self.directory.mkdir()
        self.first = self.directory / "first.gpx"
        self.second = self.directory / "second.GPX"
        self.nested_directory = self.directory / "nested"
        self.nested_directory.mkdir()
        self.nested = self.nested_directory / "nested.gpx"
        self.first.write_text(GPX.format(name="First"), encoding="utf-8")
        self.second.write_text(GPX.format(name="Second"), encoding="utf-8")
        self.nested.write_text(GPX.format(name="Nested"), encoding="utf-8")
        (self.directory / "ignored.txt").write_text("ignored", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def discovery_functions():
        return (
            gpx_splitter.discover_gpx_files,
            identify_trails.discover_gpx_files,
            merge_gpx.discover_gpx_files,
        )

    def test_single_directory_is_non_recursive(self) -> None:
        expected = [self.first.resolve(), self.second.resolve()]
        for discover in self.discovery_functions():
            with self.subTest(module=discover.__module__):
                self.assertEqual(expected, discover([self.directory]))

    def test_one_or_more_explicit_files_are_accepted(self) -> None:
        expected = [self.first.resolve(), self.second.resolve()]
        for discover in self.discovery_functions():
            with self.subTest(module=discover.__module__):
                self.assertEqual(expected, discover([self.second, self.first]))
                self.assertEqual([self.first.resolve()], discover([self.first]))

    def test_mixed_inputs_and_multiple_directories_are_rejected(self) -> None:
        other = self.root / "other"
        other.mkdir()
        for discover in self.discovery_functions():
            with self.subTest(module=discover.__module__, mode="mixed"):
                with self.assertRaisesRegex(ValueError, "either one directory"):
                    discover([self.directory, self.first])
            with self.subTest(module=discover.__module__, mode="directories"):
                with self.assertRaisesRegex(ValueError, "multiple directories"):
                    discover([self.directory, other])

    def test_non_gpx_explicit_file_is_rejected(self) -> None:
        text_file = self.root / "not-gpx.txt"
        text_file.write_text("x", encoding="utf-8")
        for discover in self.discovery_functions():
            with self.subTest(module=discover.__module__):
                with self.assertRaisesRegex(ValueError, "must be GPX files"):
                    discover([text_file])

    def test_splitter_processes_directory_inputs(self) -> None:
        output_root = self.root / "outputs"
        results = gpx_splitter.split_gpx_inputs(
            [self.directory],
            output_root=output_root,
        )
        self.assertEqual({self.first.resolve(), self.second.resolve()}, set(results))
        self.assertEqual(
            {"first_split_tracks", "second_split_tracks"},
            {paths[0].parent.name for paths in results.values()},
        )


if __name__ == "__main__":
    unittest.main()
