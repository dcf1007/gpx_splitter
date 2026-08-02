from pathlib import Path
import re

STRICT_DISCOVERY = '''def discover_gpx_files(
    inputs: Sequence[Path],
    recursive: bool = False,
) -> list[Path]:
    """Accept exactly one directory or one or more GPX files."""

    if not inputs:
        raise ValueError("Provide one GPX directory or one or more GPX files")

    paths = [path.expanduser() for path in inputs]
    missing = [path for path in paths if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Input path does not exist: {missing[0]}")

    directories = [path for path in paths if path.is_dir()]
    if directories:
        if len(paths) != 1:
            raise ValueError(
                "Provide either one directory or one or more GPX files; "
                "do not mix files and directories or provide multiple directories"
            )
        directory = directories[0]
        pattern = "**/*.gpx" if recursive else "*.gpx"
        files = sorted(
            file_path.resolve()
            for file_path in directory.glob(pattern)
            if file_path.is_file()
        )
        if not files:
            raise ValueError(f"No GPX files found in directory: {directory}")
        return files

    files: set[Path] = set()
    for path in paths:
        if not path.is_file():
            raise ValueError(f"Input is not a regular file: {path}")
        if path.suffix.lower() != ".gpx":
            raise ValueError(f"Input file is not a GPX file: {path}")
        files.add(path.resolve())
    return sorted(files)
'''


def replace_discovery(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        r"def discover_gpx_files\([\s\S]*?\n    return sorted\(files\)\n",
        re.MULTILINE,
    )
    updated, count = pattern.subn(STRICT_DISCOVERY.rstrip() + "\n", text, count=1)
    if count != 1:
        raise RuntimeError(f"Could not replace discover_gpx_files in {path}")
    path.write_text(updated, encoding="utf-8")


replace_discovery(Path("identify_trails.py"))
replace_discovery(Path("merge_gpx.py"))

splitter_path = Path("gpx_splitter.py")
splitter = splitter_path.read_text(encoding="utf-8")
splitter = splitter.replace(
    "from typing import BinaryIO\n",
    "from typing import BinaryIO, Sequence\n",
    1,
)
if "from typing import BinaryIO, Sequence" not in splitter:
    raise RuntimeError("Could not update splitter typing import")

splitter_helpers = STRICT_DISCOVERY + '''


def plan_output_directories(
    input_files: Sequence[Path],
    output_root: Path | None,
) -> dict[Path, Path]:
    """Choose one output directory per source GPX file."""

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
        stem = sanitize_filename_component(input_path.stem)
        base_name = f"{stem}_split_tracks"
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
    recursive: bool = False,
    time_gap_hours: float = 1.0,
    distance_gap_km: float = 10.0,
    overwrite: bool = False,
) -> dict[Path, list[Path]]:
    """Discover and split all GPX files from one valid input mode."""

    input_files = discover_gpx_files(inputs, recursive)
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
'''

main_pattern = re.compile(
    r"def main\(\) -> int:\n[\s\S]*?(?=\n\nif __name__ == \"__main__\":)",
    re.MULTILINE,
)
new_main = '''def main() -> int:
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
        help="Exactly one directory, or one or more GPX files.",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Search the single input directory recursively.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "Output directory. With several source files, creates one "
            "source-specific subdirectory per GPX file. Without this option, "
            "each source uses <input_name>_split_tracks beside the input file."
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
    arguments = parser.parse_args()

    try:
        outputs_by_input = split_gpx_inputs(
            inputs=arguments.inputs,
            output_root=arguments.output_dir,
            recursive=arguments.recursive,
            time_gap_hours=arguments.time_gap_hours,
            distance_gap_km=arguments.distance_gap_km,
            overwrite=arguments.overwrite,
        )
    except (OSError, ValueError, etree.XMLSyntaxError) as error:
        parser.exit(status=1, message=f"Error: {error}\n")

    total_outputs = 0
    for input_path, written_files in outputs_by_input.items():
        total_outputs += len(written_files)
        output_directory = (
            written_files[0].parent
            if written_files
            else plan_output_directories(
                list(outputs_by_input), arguments.output_dir
            )[input_path]
        )
        print(
            f"{input_path}: created {len(written_files)} GPX file(s) "
            f"in {output_directory.resolve()}"
        )
    print(
        f"Processed {len(outputs_by_input)} input GPX file(s); "
        f"created {total_outputs} output file(s)."
    )
    return 0
'''
if "def discover_gpx_files(" in splitter:
    raise RuntimeError("Splitter already contains discovery helpers")
splitter = splitter.replace(
    "def main() -> int:\n",
    splitter_helpers + "\n\n\ndef main() -> int:\n",
    1,
)
splitter, count = main_pattern.subn(new_main.rstrip(), splitter, count=1)
if count != 1:
    raise RuntimeError("Could not replace splitter main function")
splitter_path.write_text(splitter, encoding="utf-8")

test_content = r'''from __future__ import annotations

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


class InputModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.first = self.directory / "first.gpx"
        self.second = self.directory / "second.gpx"
        self.first.write_text(GPX.format(name="First"), encoding="utf-8")
        self.second.write_text(GPX.format(name="Second"), encoding="utf-8")
        self.other_directory = self.directory / "other"
        self.other_directory.mkdir()
        (self.other_directory / "third.gpx").write_text(
            GPX.format(name="Third"), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_all_scripts_accept_one_directory(self) -> None:
        expected = [self.first.resolve(), self.second.resolve()]
        for module in (gpx_splitter, identify_trails, merge_gpx):
            with self.subTest(module=module.__name__):
                self.assertEqual(
                    expected,
                    module.discover_gpx_files([self.directory], recursive=False),
                )

    def test_all_scripts_accept_file_arguments(self) -> None:
        expected = [self.first.resolve(), self.second.resolve()]
        for module in (gpx_splitter, identify_trails, merge_gpx):
            with self.subTest(module=module.__name__):
                self.assertEqual(
                    expected,
                    module.discover_gpx_files(
                        [self.second, self.first], recursive=False
                    ),
                )

    def test_all_scripts_reject_mixed_inputs_and_multiple_directories(self) -> None:
        for module in (gpx_splitter, identify_trails, merge_gpx):
            with self.subTest(module=module.__name__, mode="mixed"):
                with self.assertRaisesRegex(ValueError, "either one directory"):
                    module.discover_gpx_files(
                        [self.first, self.other_directory], recursive=False
                    )
            with self.subTest(module=module.__name__, mode="directories"):
                with self.assertRaisesRegex(ValueError, "multiple directories"):
                    module.discover_gpx_files(
                        [self.directory, self.other_directory], recursive=False
                    )

    def test_splitter_processes_a_directory_into_source_specific_folders(self) -> None:
        output_root = self.directory / "outputs"
        result = gpx_splitter.split_gpx_inputs(
            [self.directory], output_root=output_root
        )
        self.assertEqual({self.first.resolve(), self.second.resolve()}, set(result))
        self.assertEqual(
            {"first_split_tracks", "second_split_tracks"},
            {paths[0].parent.name for paths in result.values()},
        )


if __name__ == "__main__":
    unittest.main()
'''
Path("tests/test_input_modes.py").write_text(test_content, encoding="utf-8")

documentation_updates = {
    "GPX_SPLITTER.md": '''

## Input modes

The splitter accepts exactly one of these input forms:

```bash
python gpx_splitter.py recordings_directory
python gpx_splitter.py first.gpx second.gpx third.gpx
```

Do not mix file and directory arguments, and do not provide more than one directory. Directory searches are non-recursive unless `--recursive` is supplied.

With multiple source files and `--output-dir`, the splitter creates a separate `<source_name>_split_tracks` subdirectory for each source GPX file to prevent output-name collisions.
''',
    "TRAIL_IDENTIFIER.md": '''

## Input modes

The identifier accepts either one directory or one or more GPX file paths. File and directory arguments cannot be mixed, and multiple directory arguments are rejected. Use `--recursive` to include GPX files in subdirectories of the single directory input.
''',
    "MERGE_GPX.md": '''

## Input modes

The merger accepts either one directory or one or more GPX file paths. File and directory arguments cannot be mixed, and multiple directory arguments are rejected. Use `--recursive` to include GPX files in subdirectories of the single directory input.
''',
}
for filename, addition in documentation_updates.items():
    path = Path(filename)
    text = path.read_text(encoding="utf-8")
    if "## Input modes" not in text:
        text += addition
    path.write_text(text, encoding="utf-8")

readme_path = Path("README.md")
readme = readme_path.read_text(encoding="utf-8")
note = (
    "\nAll three scripts accept either one directory or one or more GPX "
    "files. Mixed file/directory inputs and multiple directories are rejected.\n"
)
if note.strip() not in readme:
    readme += note
readme_path.write_text(readme, encoding="utf-8")
