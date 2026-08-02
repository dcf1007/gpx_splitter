from pathlib import Path


merge_path = Path("merge_gpx.py")
merge = merge_path.read_text(encoding="utf-8")

merge = merge.replace("import math\n", "import math\nimport sys\n", 1)

helper_anchor = '''def point_coordinate(point: etree._Element) -> tuple[float, float]:
'''
helper = '''def track_has_valid_timestamp(track: etree._Element) -> bool:
    """Return whether any track point contains a valid GPX timestamp."""

    for point in iter_track_points(track):
        time_element = direct_child(point, "time")
        value = time_element.text if time_element is not None else None
        if parse_timestamp(value) is not None:
            return True
    return False


'''
if helper_anchor not in merge:
    raise RuntimeError("Could not locate timestamp-helper insertion point")
merge = merge.replace(helper_anchor, helper + helper_anchor, 1)

old_track_loop = '''        for index, track in enumerate(source_tracks, start=1):
            tracks.append(validate_track(path, track, index))

    if not tracks:
        raise ValueError("No root-level GPX <trk> elements were found")
'''
new_track_loop = '''        for index, track in enumerate(source_tracks, start=1):
            if not track_has_valid_timestamp(track):
                name_element = direct_child(track, "name")
                track_name = (
                    "".join(name_element.itertext()).strip()
                    if name_element is not None
                    else ""
                )
                label = track_name or f"track {index}"
                print(
                    f"Warning: skipped {path.name} / {label} (track {index}): "
                    "no valid date/time information.",
                    file=sys.stderr,
                )
                continue
            tracks.append(validate_track(path, track, index))

    if not tracks:
        raise ValueError("No tracks with valid date/time information were found")
'''
if old_track_loop not in merge:
    raise RuntimeError("Could not locate track-validation loop")
merge = merge.replace(old_track_loop, new_track_loop, 1)
merge_path.write_text(merge, encoding="utf-8")


test_path = Path("tests/test_merge_gpx.py")
tests = test_path.read_text(encoding="utf-8")
tests = tests.replace(
    "import tempfile\nimport unittest\n",
    "import io\nimport tempfile\nimport unittest\nfrom contextlib import redirect_stderr\n",
    1,
)

insert_anchor = '''    def test_backward_or_missing_timestamps_abort(self) -> None:
'''
new_tests = '''    def test_undated_track_is_skipped_while_dated_track_is_processed(self) -> None:
        source = f"""<gpx xmlns="{GPX_NAMESPACE}" version="1.1" creator="test">
  <trk><name>Undated</name><trkseg>
    <trkpt lat="0" lon="0"/>
    <trkpt lat="0" lon="0.01"><time>not-a-time</time></trkpt>
  </trkseg></trk>
  <trk><name>Dated</name><trkseg>
    <trkpt lat="1" lon="1"><time>2026-07-01T10:00:00Z</time></trkpt>
  </trkseg></trk>
</gpx>"""
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            directory = Path(temporary_directory_name)
            source_path = self.write_file(directory, "input.gpx", source)
            output_path = directory / "merged.gpx"
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                _, groups = merge_gpx.merge_gpx_files([source_path], output_path)

            self.assertEqual([1], [len(group) for group in groups])
            tree = etree.parse(str(output_path))
            self.assertEqual(
                ["Dated"],
                tree.xpath("/gpx:gpx/gpx:trk/gpx:name/text()", namespaces=NS),
            )
            self.assertIn("skipped input.gpx / Undated (track 1)", stderr.getvalue())
            self.assertIn("no valid date/time information", stderr.getvalue())

    def test_partially_timed_track_still_aborts(self) -> None:
        source = f"""<gpx xmlns="{GPX_NAMESPACE}" version="1.1" creator="test">
  <trk><name>Partial</name><trkseg>
    <trkpt lat="0" lon="0"><time>2026-07-01T10:00:00Z</time></trkpt>
    <trkpt lat="0" lon="0.01"/>
  </trkseg></trk>
</gpx>"""
        with tempfile.TemporaryDirectory() as temporary_directory_name:
            directory = Path(temporary_directory_name)
            source_path = self.write_file(directory, "partial.gpx", source)

            with self.assertRaisesRegex(ValueError, "no valid <time>"):
                merge_gpx.merge_gpx_files([source_path], directory / "merged.gpx")

'''
if insert_anchor not in tests:
    raise RuntimeError("Could not locate merge timestamp regression test")
tests = tests.replace(insert_anchor, new_tests + insert_anchor, 1)
tests = tests.replace(
    "    def test_backward_or_missing_timestamps_abort(self) -> None:\n",
    "    def test_backward_timestamps_abort_and_all_undated_tracks_are_rejected(self) -> None:\n",
    1,
)
tests = tests.replace(
    '            with self.assertRaisesRegex(ValueError, "no valid <time>"):\n                merge_gpx.merge_gpx_files([missing_path], directory / "b.gpx")\n',
    '            stderr = io.StringIO()\n            with redirect_stderr(stderr):\n                with self.assertRaisesRegex(\n                    ValueError,\n                    "No tracks with valid date/time information",\n                ):\n                    merge_gpx.merge_gpx_files([missing_path], directory / "b.gpx")\n            self.assertIn("skipped missing.gpx / Missing (track 1)", stderr.getvalue())\n',
    1,
)
test_path.write_text(tests, encoding="utf-8")


doc_path = Path("MERGE_GPX.md")
doc = doc_path.read_text(encoding="utf-8")
doc = doc.replace(
    "The script writes one GPX file. Tracks that match are combined into one `<trk>`. ",
    "Tracks with no valid date/time information are skipped with a warning. "
    "The script writes one GPX file. Tracks that match are combined into one `<trk>`. ",
    1,
)
doc = doc.replace(
    "3. Requires every track to contain at least one `<trkseg>` and at least one `<trkpt>`.\n"
    "4. Requires every track point to have valid latitude, longitude, and timestamp data.\n"
    "5. Requires timestamps inside each track to be nondecreasing in document order.\n"
    "6. Calculates each track's first timestamp, last timestamp, first coordinate, and last coordinate.\n"
    "7. Sorts all tracks by start timestamp, then end timestamp, then source filename and source track position.\n"
    "8. Rejects any time overlap between sorted tracks.\n"
    "9. Groups adjacent tracks when both the time-gap and distance-gap rules match.\n"
    "10. Writes the sorted groups to one GPX document.\n",
    "3. Skips a track, with a warning, when none of its points has a valid timestamp.\n"
    "4. Requires every retained track to contain at least one `<trkseg>` and at least one `<trkpt>`.\n"
    "5. Requires every point in a retained track to have valid latitude, longitude, and timestamp data.\n"
    "6. Requires timestamps inside each retained track to be nondecreasing in document order.\n"
    "7. Calculates each retained track's first timestamp, last timestamp, first coordinate, and last coordinate.\n"
    "8. Sorts retained tracks by start timestamp, then end timestamp, then source filename and source track position.\n"
    "9. Rejects any time overlap between sorted tracks.\n"
    "10. Groups adjacent tracks when both the time-gap and distance-gap rules match.\n"
    "11. Writes the sorted groups to one GPX document.\n",
    1,
)
doc = doc.replace(
    "Because global sorting and overlap detection depend on reliable time ranges, every `<trkpt>` must contain a valid `<time>` value.\n\n"
    "The script aborts when:\n\n"
    "- a track point has no timestamp;\n"
    "- a timestamp is malformed;\n",
    "A track with no valid timestamps anywhere is skipped and reported on standard error. "
    "If every input track is skipped, the script exits with an error because there is nothing to merge.\n\n"
    "Once a track contains at least one valid timestamp, global sorting and overlap detection require complete chronology. The script aborts when that retained track contains:\n\n"
    "- a track point with no timestamp;\n"
    "- a malformed timestamp;\n",
    1,
)
doc_path.write_text(doc, encoding="utf-8")
