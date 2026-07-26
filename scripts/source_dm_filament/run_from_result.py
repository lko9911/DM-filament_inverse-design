from __future__ import annotations

import argparse
import ast
import ctypes
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_DIR = PROJECT_ROOT / "Source_DM_filament"
DEFAULT_MATLAB_COMMAND = "matlab"
DEFAULT_LAYER_LINES = [2, 3, 3, 4, 4, 4, 4, 4, 4, 4, 4, 3, 3, 2]
FEED_LENGTH_START = 200 # End Purge
FEED_LENGTH_END = 60 # Start Purge
MATERIAL_CODE_TO_NAME = {
    1: "PLA",
    2: "CPLA",
    3: "TPU",
    4: "PETG",
    5: "SMP",
    100: "CYAN",
    200: "MAGENTA",
    300: "YELLOW",
    400: "WHITE",
    500: "BLACK",
}
MATERIAL_NAME_TO_CODE = {name: code for code, name in MATERIAL_CODE_TO_NAME.items()}
AUTO_MATERIAL = "AUTO"


# ============================================================
# User settings
# ============================================================
# Edit these values, then run this file.
#
# Example:
#   RESULT_DIR = PROJECT_ROOT / "out" / "result" / "candidate_rank_0078"
#
# The RESULT_DIR folder must contain:
#   length.txt
#   matrix.txt
#   po.txt
RESULT_DIR = PROJECT_ROOT / "out" / "Property_Shark_Purge_Test" / "result" / "candidate_rank_0003"

# None means: <RESULT_DIR>/source_dm_filament
OUTPUT_DIR: Path | None = None

# Use "matlab" when MATLAB is available from PATH.
# If needed, use a full path like:
#   r"C:\Program Files\MATLAB\R2025b\bin\matlab.exe"
MATLAB_COMMAND = DEFAULT_MATLAB_COMMAND

# Optional ASCII-only staging root for MATLAB execution.
# Set this when the project path contains non-ASCII characters and MATLAB
# fails even with Windows short paths.
# Example:
#   Path(r"C:\b_fdm_matlab_stage")
MATLAB_STAGE_ROOT: Path | None = Path(tempfile.gettempdir()) / "b_fdm_matlab_stage"

# True: only creates Matinfo.mat and the generated MATLAB driver.
# False: creates Matinfo.mat, then runs Source_DM_filament/main.m.
PREPARE_ONLY = False


def parse_assignment_file(path: Path, expected_name: str) -> object:
    if not path.exists():
        raise FileNotFoundError(f"Missing {expected_name}.txt: {path}")

    text = path.read_text(encoding="utf-8").strip()
    if "=" not in text:
        raise ValueError(f"{path} must look like '{expected_name} = [...]'.")

    name, value_text = text.split("=", 1)
    if name.strip() != expected_name:
        raise ValueError(f"{path} must define '{expected_name}', got '{name.strip()}'.")

    return ast.literal_eval(value_text.strip())


def load_result_txt_files(result_dir: Path) -> tuple[list[float], list[list[int]], list[list[int]]]:
    length = parse_assignment_file(result_dir / "length.txt", "length")
    matrix = parse_assignment_file(result_dir / "matrix.txt", "matrix")
    po = parse_assignment_file(result_dir / "po.txt", "po")

    if not isinstance(length, list) or not all(isinstance(item, (int, float)) for item in length):
        raise ValueError("length.txt must contain a numeric list.")
    if not isinstance(matrix, list) or not matrix or not all(isinstance(row, list) for row in matrix):
        raise ValueError("matrix.txt must contain a 2D numeric list.")
    if not isinstance(po, list) or not all(isinstance(row, list) and len(row) == 3 for row in po):
        raise ValueError("po.txt must contain an Nx3 numeric list.")

    length_values = [float(item) for item in length]
    material_matrix = [[int(item) for item in row] for row in matrix]
    po_rows = [[int(item) for item in row] for row in po]

    col_count = len(material_matrix[0])
    if any(len(row) != col_count for row in material_matrix):
        raise ValueError("matrix rows must all have the same number of columns.")
    if len(length_values) != col_count:
        raise ValueError(
            f"Length/matrix mismatch: length has {len(length_values)} items, matrix has {col_count} columns."
        )
    if len(material_matrix) != len(DEFAULT_LAYER_LINES):
        raise ValueError(
            f"matrix row count must be {len(DEFAULT_LAYER_LINES)}, got {len(material_matrix)}."
        )

    return length_values, material_matrix, po_rows


def compress_material_column(material_column: Sequence[int], layer_lines: Sequence[int]) -> list[list[int]]:
    compressed: list[list[int]] = []
    current_material = int(material_column[0])
    current_line_sum = int(layer_lines[0])

    for material, line_count in zip(material_column[1:], layer_lines[1:]):
        material = int(material)
        line_count = int(line_count)
        if material == current_material:
            current_line_sum += line_count
        else:
            compressed.append([current_material, current_line_sum])
            current_material = material
            current_line_sum = line_count

    compressed.append([current_material, current_line_sum])
    return compressed


def flip_matrix_by_step(matrix: list[list[int]]) -> list[list[int]]:
    return [list(reversed(row)) for row in matrix]


def flip_matrix_by_row(matrix: list[list[int]]) -> list[list[int]]:
    return [list(row) for row in reversed(matrix)]


def build_dm_material_matrix(material_matrix: list[list[int]]) -> list[list[int]]:
    # Result matrix files are written in the human-readable orientation.
    # Source_DM_filament expects the legacy mirrored orientation that was
    # previously baked into standalone export helpers, so we mirror both axes
    # only for the MATLAB handoff.
    return flip_matrix_by_row(flip_matrix_by_step(material_matrix))


def build_tdef_blocks(material_matrix: list[list[int]]) -> list[list[list[int]]]:
    column_count = len(material_matrix[0])
    blocks: list[list[list[int]]] = []
    for col_index in range(column_count):
        material_column = [row[col_index] for row in material_matrix]
        blocks.append(compress_material_column(material_column, DEFAULT_LAYER_LINES))
    return blocks


def material_code_to_name(material_code: int) -> str:
    return MATERIAL_CODE_TO_NAME.get(int(material_code), f"UNKNOWN_{int(material_code)}")


def parse_optional_material_code(value: str | int | None) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == AUTO_MATERIAL:
        return None
    upper = text.upper()
    if upper in MATERIAL_NAME_TO_CODE:
        return MATERIAL_NAME_TO_CODE[upper]
    try:
        code = int(float(text))
    except ValueError as exc:
        choices = ", ".join([AUTO_MATERIAL, *MATERIAL_NAME_TO_CODE])
        raise ValueError(f"Unknown material '{value}'. Use one of: {choices}, or a material code.") from exc
    if code not in MATERIAL_CODE_TO_NAME:
        raise ValueError(f"Unknown material code '{code}'.")
    return code


def build_po_material_switch_lines(po_rows: Sequence[Sequence[int]]) -> list[str]:
    lines: list[str] = []
    lines.append("po_material_segments:")
    for index, row in enumerate(po_rows, start=1):
        material_code, start_index, end_index = (int(row[0]), int(row[1]), int(row[2]))
        lines.append(
            f"  segment_{index:02d}: {material_code_to_name(material_code)} ({material_code}) | "
            f"po_range {start_index}->{end_index}"
        )

    lines.append("")
    lines.append("po_material_switches:")
    switch_index = 1
    for index in range(len(po_rows) - 1):
        current_code = int(po_rows[index][0])
        next_code = int(po_rows[index + 1][0])
        if current_code == next_code:
            continue
        current_end = int(po_rows[index][2])
        next_start = int(po_rows[index + 1][1])
        lines.append(
            f"  switch_{switch_index:02d}: "
            f"{material_code_to_name(current_code)} ({current_code}) -> "
            f"{material_code_to_name(next_code)} ({next_code}) | "
            f"boundary {current_end}->{next_start}"
        )
        switch_index += 1

    if switch_index == 1:
        lines.append("  none")

    return lines


def add_feed_margin_po_row(
    po_rows: list[list[int]],
    material_code: int | None,
    *,
    position: str,
) -> list[list[int]]:
    if material_code is None:
        return po_rows
    material_code = int(material_code)
    if any(int(row[0]) == material_code for row in po_rows):
        return po_rows
    margin_row = [material_code, 1, len(DEFAULT_LAYER_LINES)]
    if position == "start":
        return [margin_row, *po_rows]
    if position == "end":
        return [*po_rows, margin_row]
    raise ValueError(f"Unknown feed margin po position: {position}")


def build_po_rows_with_feed_margins(
    po_rows: list[list[int]],
    feed_start_material_code: int | None,
    feed_end_material_code: int | None,
) -> list[list[int]]:
    effective_po_rows = [list(row) for row in po_rows]
    effective_po_rows = add_feed_margin_po_row(
        effective_po_rows,
        feed_start_material_code,
        position="start",
    )
    effective_po_rows = add_feed_margin_po_row(
        effective_po_rows,
        feed_end_material_code,
        position="end",
    )
    return effective_po_rows


def write_po_material_switch_report(output_dir: Path, po_rows: Sequence[Sequence[int]]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "po_material_switches.txt"
    report_path.write_text("\n".join(build_po_material_switch_lines(po_rows)) + "\n", encoding="utf-8")
    return report_path


def matlab_quote(value: Path | str) -> str:
    return str(value).replace("\\", "/").replace("'", "''")


def matlab_row(values: Sequence[int | float]) -> str:
    return " ".join(f"{value:.12g}" if isinstance(value, float) else str(value) for value in values)


def matlab_matrix(rows: Sequence[Sequence[int | float]]) -> str:
    if not rows:
        return "[]"
    return "[" + "; ".join(matlab_row(row) for row in rows) + "]"


def build_ti(length_values: Sequence[float]) -> list[list[float]]:
    column_count = len(length_values)
    to_rows = [[float(index), float(length_values[index])] for index in range(column_count)]
    ti = [[float(column_count - 1), float(FEED_LENGTH_START)]]
    ti.extend(reversed(to_rows))
    ti.append([0.0, float(FEED_LENGTH_END)])
    return ti


def write_matlab_driver(
    result_dir: Path,
    output_dir: Path,
    source_dir: Path,
    length_values: list[float],
    material_matrix: list[list[int]],
    po_rows: list[list[int]],
    run_main: bool,
    feed_length_start: float,
    feed_length_end: float,
    feed_start_material_code: int | None = None,
    feed_end_material_code: int | None = None,
    output_basename: str | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)

    matinfo_path = output_dir / "Matinfo.mat"
    driver_path = output_dir / "run_source_dm_from_result_generated.m"
    dm_material_matrix = build_dm_material_matrix(material_matrix)
    tdef_blocks = build_tdef_blocks(dm_material_matrix)
    tdef_entries: list[tuple[int, list[list[int]]]] = [
        (index, block)
        for index, block in enumerate(tdef_blocks)
    ]
    next_tdef_id = len(tdef_entries)
    feed_start_tdef_id: int | None = None
    feed_end_tdef_id: int | None = None
    if feed_start_material_code is not None:
        feed_start_tdef_id = next_tdef_id
        next_tdef_id += 1
        tdef_entries.append((feed_start_tdef_id, [[int(feed_start_material_code), sum(DEFAULT_LAYER_LINES)]]))
    if feed_end_material_code is not None:
        feed_end_tdef_id = next_tdef_id
        next_tdef_id += 1
        tdef_entries.append((feed_end_tdef_id, [[int(feed_end_material_code), sum(DEFAULT_LAYER_LINES)]]))
    effective_po_rows = build_po_rows_with_feed_margins(
        po_rows,
        feed_start_material_code,
        feed_end_material_code,
    )
    ti_rows = build_ti_with_feed_lengths(
        length_values,
        feed_length_start,
        feed_length_end,
        feed_start_tdef_id=feed_start_tdef_id,
        feed_end_tdef_id=feed_end_tdef_id,
    )

    lines = [
        "clear;",
        f"resultDir = '{matlab_quote(result_dir)}';",
        f"sourceDir = '{matlab_quote(source_dir)}';",
        f"outputDir = '{matlab_quote(output_dir)}';",
        f"fileNameDateOverride = '{matlab_quote(output_basename if output_basename is not None else output_dir.name)}';",
        f"matinfoPath = '{matlab_quote(matinfo_path)}';",
        f"lengthValues = {matlab_matrix([[value] for value in length_values])};",
        f"materialMatrix = {matlab_matrix(dm_material_matrix)};",
        f"po = {matlab_matrix(effective_po_rows)};",
        f"layerLines = {matlab_matrix([DEFAULT_LAYER_LINES])};",
        f"feedLengthStart = {feed_length_start};",
        f"feedLengthEnd = {feed_length_end};",
        f"feedStartMaterial = '{material_code_to_name(feed_start_material_code) if feed_start_material_code is not None else AUTO_MATERIAL}';",
        f"feedEndMaterial = '{material_code_to_name(feed_end_material_code) if feed_end_material_code is not None else AUTO_MATERIAL}';",
        f"Ti = {matlab_matrix(ti_rows)};",
        f"Tdef = cell({len(tdef_entries)}, 2);",
    ]

    for index, (tdef_id, block) in enumerate(tdef_entries, start=1):
        lines.append(f"Tdef{{{index}, 1}} = {tdef_id};")
        lines.append(f"Tdef{{{index}, 2}} = {matlab_matrix(block)};")

    lines.extend(
        [
            "save(matinfoPath, 'Ti', 'Tdef', 'po', 'materialMatrix', 'lengthValues', "
            "'layerLines', 'feedLengthStart', 'feedLengthEnd', 'feedStartMaterial', 'feedEndMaterial', 'resultDir');",
        ]
    )

    if run_main:
        matlab_output_basename = output_basename if output_basename is not None else output_dir.name
        lines.extend(
            [
                "addpath(sourceDir);",
                "setenv('B_FDM_SOURCE_DM_MATINFO_PATH', matinfoPath);",
                "setenv('B_FDM_SOURCE_DM_OUTPUT_DIR', outputDir);",
                f"setenv('B_FDM_SOURCE_DM_OUTPUT_BASENAME', '{matlab_quote(matlab_output_basename)}');",
                "oldDir = pwd;",
                "cleanupObj = onCleanup(@() cd(oldDir));",
                "cd(sourceDir);",
                "run('main.m');",
            ]
        )

    driver_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return driver_path


def build_ti_with_feed_lengths(
    length_values: Sequence[float],
    feed_length_start: float,
    feed_length_end: float,
    *,
    feed_start_tdef_id: int | None = None,
    feed_end_tdef_id: int | None = None,
) -> list[list[float]]:
    column_count = len(length_values)
    to_rows = [[float(index), float(length_values[index])] for index in range(column_count)]
    start_id = float(feed_start_tdef_id if feed_start_tdef_id is not None else column_count - 1)
    end_id = float(feed_end_tdef_id if feed_end_tdef_id is not None else 0)
    ti = [[start_id, float(feed_length_start)]]
    ti.extend(reversed(to_rows))
    ti.append([end_id, float(feed_length_end)])
    return ti


def resolve_matlab_executable(matlab_command: str) -> str:
    command_path = Path(matlab_command)
    if command_path.exists():
        return str(command_path)

    resolved = shutil.which(matlab_command)
    if resolved is None:
        raise FileNotFoundError(
            f"MATLAB command was not found: {matlab_command}. "
            "Pass --matlab-command with the full matlab.exe path."
        )
    return resolved


def path_has_non_ascii(path: Path) -> bool:
    return not str(path).isascii()


def to_matlab_safe_path(path: Path) -> Path:
    if os.name != "nt" or not path_has_non_ascii(path):
        return path

    buffer = ctypes.create_unicode_buffer(4096)
    result = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
    if result == 0 or not buffer.value:
        return path
    return Path(buffer.value)


def to_windows_extended_path(path: Path) -> str:
    resolved = str(Path(path).resolve())
    if os.name != "nt":
        return resolved
    normalized = resolved.replace("/", "\\")
    if normalized.startswith("\\\\?\\"):
        return normalized
    if normalized.startswith("\\\\"):
        return "\\\\?\\UNC\\" + normalized.lstrip("\\")
    return "\\\\?\\" + normalized


def copy_directory_contents(source_dir: Path, destination_dir: Path) -> None:
    os.makedirs(to_windows_extended_path(destination_dir), exist_ok=True)
    for item in source_dir.iterdir():
        destination = destination_dir / item.name
        if item.is_dir():
            os.makedirs(to_windows_extended_path(destination), exist_ok=True)
            shutil.copytree(
                to_windows_extended_path(item),
                to_windows_extended_path(destination),
                dirs_exist_ok=True,
            )
        else:
            os.makedirs(to_windows_extended_path(destination.parent), exist_ok=True)
            shutil.copy2(to_windows_extended_path(item), to_windows_extended_path(destination))


def copy_result_input_files(result_dir: Path, destination_dir: Path) -> None:
    os.makedirs(to_windows_extended_path(destination_dir), exist_ok=True)
    required_names = ["length.txt", "matrix.txt", "po.txt"]
    optional_names = ["result.json"]
    for name in required_names:
        source_path = result_dir / name
        if not source_path.exists():
            raise FileNotFoundError(f"Missing result input file: {source_path}")
        shutil.copy2(
            to_windows_extended_path(source_path),
            to_windows_extended_path(destination_dir / name),
        )
    for name in optional_names:
        source_path = result_dir / name
        if source_path.exists():
            shutil.copy2(
                to_windows_extended_path(source_path),
                to_windows_extended_path(destination_dir / name),
            )


def run_from_result_folder(
    result_dir: Path | str,
    *,
    source_dir: Path | str = DEFAULT_SOURCE_DIR,
    output_dir: Path | str | None = None,
    matlab_command: str = DEFAULT_MATLAB_COMMAND,
    run_main: bool = True,
    matlab_stage_root: Path | str | None = MATLAB_STAGE_ROOT,
    feed_length_start: float = FEED_LENGTH_START,
    feed_length_end: float = FEED_LENGTH_END,
    feed_start_material: str | int | None = None,
    feed_end_material: str | int | None = None,
) -> Path:
    result_dir = Path(result_dir).resolve()
    source_dir = Path(source_dir).resolve()
    output_dir = (Path(output_dir).resolve() if output_dir is not None else result_dir / "source_dm_filament")

    if not result_dir.exists():
        raise FileNotFoundError(f"Result folder not found: {result_dir}")
    if not source_dir.exists():
        raise FileNotFoundError(f"Source_DM_filament folder not found: {source_dir}")
    if not (source_dir / "main.m").exists():
        raise FileNotFoundError(f"main.m not found under: {source_dir}")

    length_values, material_matrix, po_rows = load_result_txt_files(result_dir)
    feed_start_material_code = parse_optional_material_code(feed_start_material)
    feed_end_material_code = parse_optional_material_code(feed_end_material)
    effective_po_rows = build_po_rows_with_feed_margins(
        po_rows,
        feed_start_material_code,
        feed_end_material_code,
    )
    matlab_executable = resolve_matlab_executable(matlab_command)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[START] Source_DM_filament from result: {result_dir}")
    print(f"  matinfo: {output_dir / 'Matinfo.mat'}")
    print(
        "  feed materials: "
        f"start={material_code_to_name(feed_start_material_code) if feed_start_material_code is not None else AUTO_MATERIAL}, "
        f"end={material_code_to_name(feed_end_material_code) if feed_end_material_code is not None else AUTO_MATERIAL}"
    )
    switch_report_path = write_po_material_switch_report(output_dir, effective_po_rows)
    print(f"  po switch report: {switch_report_path}")

    non_ascii_paths = any(path_has_non_ascii(path) for path in (result_dir, source_dir, output_dir))
    if non_ascii_paths and matlab_stage_root is not None:
        stage_root = Path(matlab_stage_root).resolve()
        stage_run_root = stage_root / "run"
        staged_result_dir = stage_run_root / "result_input"
        staged_source_dir = stage_run_root / "Source_DM_filament"
        staged_output_dir = stage_run_root / "source_dm_filament"
        if stage_run_root.exists():
            shutil.rmtree(stage_run_root)
        copy_result_input_files(result_dir, staged_result_dir)
        shutil.copytree(source_dir, staged_source_dir)
        print(f"  path mode: ASCII staging root -> {stage_root}")
        driver_path = write_matlab_driver(
            result_dir=staged_result_dir,
            output_dir=staged_output_dir,
            source_dir=staged_source_dir,
            length_values=length_values,
            material_matrix=material_matrix,
            po_rows=po_rows,
            run_main=run_main,
            feed_length_start=feed_length_start,
            feed_length_end=feed_length_end,
            feed_start_material_code=feed_start_material_code,
            feed_end_material_code=feed_end_material_code,
            output_basename=output_dir.name,
        )
        matlab_driver_dir = driver_path.parent
        matlab_start_dir = staged_source_dir
        start = time.perf_counter()
        if run_main:
            try:
                batch_code = (
                    f"cd('{matlab_quote(matlab_driver_dir)}'); "
                    f"run('{matlab_quote(driver_path.name)}');"
                )
                print(f"  driver : {driver_path}")
                subprocess.run([matlab_executable, "-batch", batch_code], cwd=matlab_start_dir, check=True)
            finally:
                if staged_output_dir.exists():
                    copy_directory_contents(staged_output_dir, output_dir)
        else:
            print(f"  driver : {driver_path}")
            if staged_output_dir.exists():
                copy_directory_contents(staged_output_dir, output_dir)
        elapsed = time.perf_counter() - start
    else:
        matlab_result_dir = to_matlab_safe_path(result_dir)
        matlab_source_dir = to_matlab_safe_path(source_dir)
        matlab_output_dir = to_matlab_safe_path(output_dir)
        if non_ascii_paths:
            print("  path mode: Windows short-path for MATLAB")
        driver_path = write_matlab_driver(
            result_dir=matlab_result_dir,
            output_dir=matlab_output_dir,
            source_dir=matlab_source_dir,
            length_values=length_values,
            material_matrix=material_matrix,
            po_rows=po_rows,
            run_main=run_main,
            feed_length_start=feed_length_start,
            feed_length_end=feed_length_end,
            feed_start_material_code=feed_start_material_code,
            feed_end_material_code=feed_end_material_code,
            output_basename=output_dir.name,
        )
        matlab_driver_dir = to_matlab_safe_path(driver_path.parent)
        matlab_driver_name = driver_path.name
        batch_code = (
            f"cd('{matlab_quote(matlab_driver_dir)}'); "
            f"run('{matlab_quote(matlab_driver_name)}');"
        )
        print(f"  driver : {driver_path}")
        start = time.perf_counter()
        subprocess.run([matlab_executable, "-batch", batch_code], cwd=matlab_source_dir, check=True)
        elapsed = time.perf_counter() - start

    print(f"[DONE]  Source_DM_filament ({elapsed:.2f}s)")
    print(f"  output : {output_dir}")
    return output_dir / "Matinfo.mat"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create Source_DM_filament Matinfo.mat from a result folder and run main.m."
    )
    parser.add_argument(
        "result_dir",
        nargs="?",
        type=Path,
        default=RESULT_DIR,
        help="Folder containing length.txt, matrix.txt, and po.txt. Defaults to RESULT_DIR in this file.",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help="Source_DM_filament folder. Defaults to the repository Source_DM_filament.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIR,
        help="Output folder. Defaults to OUTPUT_DIR in this file or <result_dir>/source_dm_filament.",
    )
    parser.add_argument(
        "--matlab-command",
        default=MATLAB_COMMAND,
        help="MATLAB command or full matlab.exe path.",
    )
    parser.add_argument(
        "--matlab-stage-root",
        type=Path,
        default=MATLAB_STAGE_ROOT,
        help="Optional ASCII-only staging root for MATLAB execution.",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Only generate Matinfo.mat; do not run Source_DM_filament/main.m.",
    )
    parser.add_argument(
        "--feed-length-start",
        type=float,
        default=FEED_LENGTH_START,
        help="Extra start feed length in mm. Default: FEED_LENGTH_START in this file.",
    )
    parser.add_argument(
        "--feed-length-end",
        type=float,
        default=FEED_LENGTH_END,
        help="Extra end feed length in mm. Default: FEED_LENGTH_END in this file.",
    )
    parser.add_argument(
        "--feed-start-material",
        default=AUTO_MATERIAL,
        help="Material for the feed-length-start margin. Use AUTO to reuse the existing edge pattern.",
    )
    parser.add_argument(
        "--feed-end-material",
        default=AUTO_MATERIAL,
        help="Material for the feed-length-end margin. Use AUTO to reuse the existing edge pattern.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_from_result_folder(
        args.result_dir,
        source_dir=args.source_dir,
        output_dir=args.output_dir,
        matlab_command=args.matlab_command,
        matlab_stage_root=args.matlab_stage_root,
        feed_length_start=args.feed_length_start,
        feed_length_end=args.feed_length_end,
        feed_start_material=args.feed_start_material,
        feed_end_material=args.feed_end_material,
        run_main=not (args.prepare_only or PREPARE_ONLY),
    )


if __name__ == "__main__":
    main()
