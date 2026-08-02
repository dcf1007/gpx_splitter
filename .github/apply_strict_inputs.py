from pathlib import Path
import re
from textwrap import dedent


STRICT_DISCOVERY = dedent('''
def discover_gpx_files(inputs: Sequence[Path]) -> list[Path]:
    """Accept exactly one directory or one or more explicit GPX files."""

    if not inputs:
        raise ValueError("Provide one directory or one or more GPX files")

    paths = [path.expanduser() for path in inputs]
    missing = [path for path in paths if not path.exists()]
    if missing:
        names = ", ".join(str(path) for path in missing)
        raise ValueError(f"Input path does not exist: {names}")

    directories = [path for path in paths if path.is_dir()]
    if directories:
        if len(paths) != 1:
            raise ValueError(
                "Input must be either one directory or one or more GPX files; "
                "do not mix files and directories or provide multiple directories"
            )
        directory = directories[0]
        files = sorted(
            item.resolve()
            for item in directory.iterdir()
            if item.is_file() and item.suffix.lower() == ".gpx"
        )
        if not files:
            raise ValueError(f"No GPX files found in directory: {directory}")
        return files

    invalid_files = [
        path
        for path in paths
        if not path.is_file() or path.suffix.lower() != ".gpx"
    ]
    if invalid_files:
        names = ", ".join(str(path) for path in invalid_files)
        raise ValueError(f"Explicit inputs must be GPX files: {names}")

    return sorted({path.resolve() for path in paths})
''').lstrip()


def replace_discovery(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        r"def discover_gpx_files\([\s\S]*?\n    return sorted\(files\)\n"
    )
    updated, count = pattern.subn(STRICT_DISCOVERY.rstrip() + "\n", text, count=1)
    if count != 1:
        raise RuntimeError(f"Could not replace discover_gpx_files in {path}")
    path.write_text(updated, encoding="utf-8")


# Trail identifier ------------------------------------------------------------

identify_path = Path("identify_trails.py")
replace_discovery(identify_path)
identify = identify_path.read_text(encoding="utf-8")
identify = identify.replace(
    '    parser.add_argument("--recursive", action="store_true")\n',
    "",
)
identify = identify.replace(
    "    files = discover_gpx_files(arguments.inputs, arguments.recursive)\n",
    "    files = discover_gpx_files(arguments.inputs)\n",
)
if "arguments.recursive" in identify or '"--recursive"' in identify:
    raise RuntimeError("Recursive trail-identifier input handling remains")
identify_path.write_text(identify, encoding="utf-8")


# GPX merger ------------------------------------------------------------------

merge_path = Path("merge_gpx.py")
replace_discovery(merge_path)
merge = merge_path.read_text(encoding="utf-8")
merge = merge.replace("    recursive: bool = False,\n", "")
merge = merge.replace(
    "    files = discover_gpx_files(input_paths, recursive)\n",
    "    files = discover_gpx_files(input_paths)\n",
)
merge = merge.replace(
    '    parser.add_argument("--recursive", action="store_true")\n',
    "",
)
merge = merge.replace("            recursive=arguments.recursive,\n", "")
if "arguments.recursive" in merge or '"--recursive"' in merge:
    raise RuntimeError("Recursive merger input handling remains")
merge_path.write_text(merge, encoding="utf-8")


# GPX splitter ----------------------------------------------------------------

splitter_path = Path("gpx_splitter.py")
splitter = splitter_path.read_text(encoding="utf-8")
splitter = splitter.replace(
    "from typing import BinaryIO\n",
    "from typing import BinaryIO, Sequence\n",
    1,
)
if "from typing import BinaryIO, Sequence" not in splitter:
    raise RuntimeError("Could not update splitter typing import")

splitter_helpers = STRICT_DISCOVERY + dedent('''


def plan_output_directories(
    input_files: Sequence[Path],
    output_root: Path | None,
) -> dict[Path, Path]:
    """Choose one isolated output directory per source GPX file."""

    if output_root is None:
        return {
            input_path: input_path.with_name(f"{input_path.stem}_split_tracks")
            for input_path in input_files
        }

    root = output_root.expanduser().resolve()
    if len(input_files) == 1:
        return {input_files[0]: root}

    planned: dict[Path, Path] = {}
    used_names: set[str] = set()
    for input_path in input_files:
        base_name = f"{sanitize_filename_component(input_path.stem)}_split_tracks"
        directory_name = base_name
        suffix = 2
        while directory_name.casefold() in used_names:
            directory_name = f"{base_name}_{suffix}"
            suffix += 1
        used_names.add(directory_name.casefold())
        planned[input_path] = root / directory_name
    return planned


def split_gpx_inputs(
    inputs: Sequence[Path],
    output_root: Path | None = None,
    time_gap_hours: float = 1.0,
    distance_gap_km: float = 10.0,
    overwrite: bool = False,
) -> dict[Path, list[Path]]:
    """Split all files selected by the strict file-or-directory input mode."""

    input_files = discover_gpx_files(inputs)
    output_directories = plan_output_directories(input_files, output_root)
    return {
        input_path: split_gpx_tracks(
            input_path=input_path,
            output_directory=output_directories[input_path],
            time_gap_hours=time_gap_hours,
            distance_gap_km=distance_gap_km,
            overwrite=overwrite,
        )
        for input_path in input_files
    }
''')

if "def discover_gpx_files(" in splitter:
    raise RuntimeError("Splitter already contains discovery helpers")
anchor = "\ndef read_root_context(input_path: Path) -> RootContext:\n"
if anchor not in splitter:
    raise RuntimeError("Could not find splitter helper insertion point")
splitter = splitter.replace(anchor, "\n" + splitter_helpers + anchor, 1)

new_main = dedent('''
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Split GPX tracks when the UTC date changes, timestamps are far apart, "
            "or untimed consecutive points are separated by a massive distance."
        )
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="One directory or one or more GPX files.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "Output directory. One source writes directly there; multiple sources "
            "use separate <name>_split_tracks subdirectories."
        ),
    )
    parser.add_argument(
        "--time-gap-hours",
        type=float,
        default=1.0,
        help="Split timed points when the gap exceeds this many hours (default: 1).",
    )
    parser.add_argument(
        "--distance-gap-km",
        type=float,
        default=10.0,
        help=(
            "When either consecutive point lacks a valid timestamp, split if their "
            "distance exceeds this value in kilometres (default: 10)."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace generated GPX files that already exist.",
    )
    arguments = parser.parse_args(argv)

    try:
        outputs_by_input = split_gpx_inputs(
            inputs=arguments.inputs,
            output_root=arguments.output_dir,
            time_gap_hours=arguments.time_gap_hours,
            distance_gap_km=arguments.distance_gap_km,
            overwrite=arguments.overwrite,
        )
    except (OSError, ValueError, etree.XMLSyntaxError) as error:
        parser.exit(status=1, message=f"Error: {error}\n")

    total_outputs = sum(len(paths) for paths in outputs_by_input.values())
    print(
        f"Processed {len(outputs_by_input)} GPX input file(s); "
        f"created {total_outputs} GPX file(s)."
    )
    destinations = plan_output_directories(
        list(outputs_by_input),
        arguments.output_dir,
    )
    for input_path, written_files in outputs_by_input.items():
        print(
            f"  {input_path}: {len(written_files)} file(s) in "
            f"{destinations[input_path].resolve()}"
        )
    return 0
''').lstrip().rstrip()

updated, count = re.subn(
    r"def main\(\) -> int:[\s\S]*?(?=\n\nif __name__ == \"__main__\":)",
    new_main,
    splitter,
    count=1,
)
if count != 1:
    raise RuntimeError("Could not replace splitter main function")
splitter_path.write_text(updated, encoding="utf-8")


# Documentation ---------------------------------------------------------------

readme_path = Path("README.md")
readme = readme_path.read_text(encoding="utf-8")
note = (
    "All three scripts accept exactly one directory or one or more explicit GPX "
    "files. Files and directories cannot be mixed, multiple directories are "
    "rejected, and directory input is non-recursive.\n\n"
)
if note not in readme:
    readme = readme.replace("## Scripts\n", note + "## Scripts\n", 1)
readme_path.write_text(readme, encoding="utf-8")

splitter_doc = Path("GPX_SPLITTER.md")
text = splitter_doc.read_text(encoding="utf-8")
usage_start = text.index("## Usage\n")
tests_start = text.index("## Tests\n")
usage = '''## Usage

The input must be either one directory containing GPX files or one or more explicit GPX files. Do not mix files and directories or provide multiple directories. Directory input is non-recursive.

```bash
python gpx_splitter.py recording.gpx
python gpx_splitter.py first.gpx second.gpx third.gpx
python gpx_splitter.py recordings_directory
```

Without `--output-dir`, each source uses a sibling `<input_name>_split_tracks/` directory. With one source and `--output-dir`, outputs are written directly there. With several sources, the selected directory contains one source-specific `<input_name>_split_tracks/` subdirectory per GPX file.

```bash
python gpx_splitter.py recordings_directory \
  --output-dir split_results \
  --time-gap-hours 1 \
  --distance-gap-km 10
```

Replace files from a previous run:

```bash
python gpx_splitter.py recordings_directory --overwrite
```

Output names use:

```text
YYYY-MM-DD_original_track_name_subtrack_number.gpx
original_track_name_subtrack_number.gpx
```

The date is the first valid UTC timestamp in that subtrack. The date prefix is omitted when the subtrack has no valid timestamp. Numbering is continuous for tracks that resolve to the same sanitized filename.

'''
splitter_doc.write_text(text[:usage_start] + usage + text[tests_start:], encoding="utf-8")

trail_doc = Path("TRAIL_IDENTIFIER.md")
text = trail_doc.read_text(encoding="utf-8")
text = text.replace(
    "Analyze all GPX files in a directory:\n\n```bash\npython identify_trails.py path/to/gpx_directory\n```\n\nInclude subdirectories:\n\n```bash\npython identify_trails.py path/to/gpx_directory --recursive\n```\n",
    "Analyze all GPX files in one directory:\n\n```bash\npython identify_trails.py path/to/gpx_directory\n```\n\nThe input must be either one directory or one or more explicit GPX files. Mixed inputs, multiple directories, and recursive traversal are rejected.\n",
)
text = text.replace("--recursive\n", "")
if "--recursive" in text:
    raise RuntimeError("Recursive trail-identifier documentation remains")
trail_doc.write_text(text, encoding="utf-8")

merge_doc = Path("MERGE_GPX.md")
text = merge_doc.read_text(encoding="utf-8")
text = text.replace(
    "Read every GPX file in a directory:\n\n```bash\npython merge_gpx.py recordings --output combined.gpx\n```\n\nInclude subdirectories:\n\n```bash\npython merge_gpx.py recordings --recursive --output combined.gpx\n```\n",
    "Read every GPX file in one directory:\n\n```bash\npython merge_gpx.py recordings --output combined.gpx\n```\n\nThe input must be either one directory or one or more explicit GPX files. Mixed inputs, multiple directories, and recursive traversal are rejected.\n",
)
text = text.replace("--recursive\n", "")
if "--recursive" in text:
    raise RuntimeError("Recursive merger documentation remains")
merge_doc.write_text(text, encoding="utf-8")


# Regression tests ------------------------------------------------------------

test_content = '''from __future__ import annotations

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
'''
Path("tests/test_input_modes.py").write_text(test_content, encoding="utf-8")
