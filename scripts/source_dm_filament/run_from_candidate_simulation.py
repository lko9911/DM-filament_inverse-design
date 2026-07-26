from __future__ import annotations

import argparse
import json
from pathlib import Path
from pprint import pformat
import runpy
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CANDIDATE_SIMULATIONS_DIR = PROJECT_ROOT / "out" / "simulation" / "candidate_simulations"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "out" / "dm_filament_results"
DEFAULT_SOURCE_DIR = PROJECT_ROOT / "Source_DM_filament"
DEFAULT_LENGTH_MATRIX_PATHS = [
    PROJECT_ROOT / "out" / "matrices" / "length_matrix.json",
    PROJECT_ROOT / "test_sample" / "derived" / "matrices" / "length_matrix.json",
]
DEFAULT_MATLAB_COMMAND = "matlab"
SOURCE_DM_RUNNER = PROJECT_ROOT / "scripts" / "source_dm_filament" / "run_from_result.py"
MATERIAL_CODES = {
    "PLA": 1,
    "CPLA": 2,
    "TPU": 3,
    "PETG": 4,
    "SMP": 5,
    "CYAN": 100,
    "MAGENTA": 200,
    "YELLOW": 300,
    "WHITE": 400,
    "BLACK": 500,
    "UNKNOWN": 999,
}

# ============================================================
# User settings
# ============================================================
# Edit these values, then run this file.
#
# Examples:
#   CANDIDATE_DIR = "candidate_rank_0001"
#   CANDIDATE_DIR = PROJECT_ROOT / "out" / "simulation" / "candidate_simulations" / "candidate_rank_0001"
#
# If left as None, the script requires the command-line candidate_dir argument.
CANDIDATE_DIR: str | Path | None = "out\\Property_SNU\\result\\candidate_rank_0001"

# Output root for this standalone conversion.
OUTPUT_ROOT: Path = DEFAULT_OUTPUT_ROOT

# None means use the default search order in DEFAULT_LENGTH_MATRIX_PATHS.
LENGTH_MATRIX_PATH: Path | None = None

# Source_DM_filament folder.
SOURCE_DIR: Path = DEFAULT_SOURCE_DIR

# Use "matlab" when MATLAB is available from PATH.
# If needed, set a full path like:
#   Path(r"C:\Program Files\MATLAB\R2025b\bin\matlab.exe")
MATLAB_COMMAND: str = DEFAULT_MATLAB_COMMAND

# Optional ASCII-only staging root for MATLAB execution.
# Example:
#   Path(r"C:\b_fdm_matlab_stage")
MATLAB_STAGE_ROOT: Path | None = Path(tempfile.gettempdir()) / "b_fdm_matlab_stage"

# True: only prepare result_input and generated MATLAB driver.
# False: also run Source_DM_filament/main.m.
PREPARE_ONLY = False

# Extra feed lengths in mm for the first and last filament sections.
FEED_LENGTH_START = 10 # This is end feed length, which is added to the first section. It should be long enough to ensure good filament flow at the start of the print, but not too long to cause excessive oozing or stringing.
FEED_LENGTH_END = 10 # This is first feed length, which is added to the last section. It should be long enough to ensure good filament flow at the end of the print, but not too long to cause excessive oozing or stringing after the print finishes.
# default: FEED_LENGTH_START = 130, FEED_LENGTH_END = 10 are chosen based on typical filament flow requirements and practical considerations for minimizing oozing and stringing. Adjust these values as needed based on the specific materials and printer behavior observed in your tests.

def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object in: {path}")
    return data


def resolve_candidate_dir(candidate: str | Path) -> Path:
    candidate_path = Path(candidate)
    if candidate_path.exists():
        return candidate_path.resolve()

    fallback_path = DEFAULT_CANDIDATE_SIMULATIONS_DIR / str(candidate)
    if fallback_path.exists():
        return fallback_path.resolve()

    raise FileNotFoundError(
        "Candidate simulation folder was not found. "
        f"Tried: {candidate_path} and {fallback_path}"
    )


def find_candidate_simulation_json(candidate_dir: Path) -> Path:
    preferred = candidate_dir / f"{candidate_dir.name}_simulation.json"
    if preferred.exists():
        return preferred

    matches = sorted(candidate_dir.glob("*_simulation.json"))
    if matches:
        return matches[0]

    raise FileNotFoundError(f"No *_simulation.json file was found under: {candidate_dir}")


def is_result_dir(candidate_dir: Path) -> bool:
    required_files = ["length.txt", "matrix.txt", "po.txt"]
    return all((candidate_dir / filename).exists() for filename in required_files)


def load_length_values(length_matrix_path: Path | None) -> list[float]:
    candidate_paths = [length_matrix_path] if length_matrix_path is not None else DEFAULT_LENGTH_MATRIX_PATHS
    for path in candidate_paths:
        if path is None or not path.exists():
            continue
        payload = load_json(path)
        length_values = payload.get("length_matrix")
        if isinstance(length_values, list) and all(isinstance(item, (int, float)) for item in length_values):
            return [float(item) for item in length_values]
        raise ValueError(f"length_matrix was not found or invalid in: {path}")

    tried = ", ".join(str(path) for path in candidate_paths if path is not None)
    raise FileNotFoundError(f"No usable length_matrix.json file was found. Tried: {tried}")


def convert_material_names_to_codes(material_name_matrix: list[list[object]]) -> list[list[int]]:
    matrix: list[list[int]] = []
    for row in material_name_matrix:
        code_row: list[int] = []
        for material_name in row:
            normalized = str(material_name).strip().upper()
            if normalized not in MATERIAL_CODES:
                raise ValueError(f"No result material code is defined for material: {material_name}")
            code_row.append(MATERIAL_CODES[normalized])
        matrix.append(code_row)
    return matrix


def flip_matrix_by_step(matrix: list[list[int]]) -> list[list[int]]:
    return [list(reversed(row)) for row in matrix]


def flip_matrix_by_row(matrix: list[list[int]]) -> list[list[int]]:
    return [list(row) for row in reversed(matrix)]


def row_index_to_layer_number(row_index: int, row_count: int) -> int:
    return row_count - row_index


def build_po_from_simulation_payload(candidate_payload: dict) -> list[list[int]]:
    material_name_matrix = candidate_payload.get("simulation_material_name_matrix")
    if not isinstance(material_name_matrix, list) or not material_name_matrix:
        raise ValueError("simulation_material_name_matrix was not found in candidate simulation payload.")

    row_count = int(candidate_payload.get("row_count", len(material_name_matrix)))
    po_segments: list[list[int]] = []
    current_material_code: int | None = None
    current_layers: list[int] = []

    for event in candidate_payload.get("simulation_events", []):
        if str(event.get("event_type")) != "deposit":
            continue

        row_index = int(event["row_index"])
        col_index = int(event["col_index"])
        material_name = material_name_matrix[row_index][col_index]
        material_code = MATERIAL_CODES[str(material_name).strip().upper()]
        layer_number = row_index_to_layer_number(row_index, row_count)

        if current_material_code is None:
            current_material_code = material_code
            current_layers = [layer_number]
            continue

        if material_code != current_material_code:
            po_segments.append([current_material_code, min(current_layers), max(current_layers)])
            current_material_code = material_code
            current_layers = [layer_number]
            continue

        current_layers.append(layer_number)

    if current_material_code is not None and current_layers:
        po_segments.append([current_material_code, min(current_layers), max(current_layers)])

    segment_indexes_by_material: dict[int, list[int]] = {}
    for segment_index, (material_code, _start_layer, _end_layer) in enumerate(po_segments):
        segment_indexes_by_material.setdefault(material_code, []).append(segment_index)

    for segment_indexes in segment_indexes_by_material.values():
        for current_index, next_index in zip(segment_indexes, segment_indexes[1:]):
            next_start_layer = po_segments[next_index][1]
            po_segments[current_index][2] = min(
                po_segments[current_index][2],
                max(po_segments[current_index][1], next_start_layer - 1),
            )

    return po_segments


def save_result_inputs(candidate_payload: dict, length_values: list[float], result_dir: Path) -> None:
    material_name_matrix = candidate_payload.get("simulation_material_name_matrix")
    if not isinstance(material_name_matrix, list) or not material_name_matrix:
        raise ValueError("simulation_material_name_matrix was not found in candidate simulation payload.")

    matrix = convert_material_names_to_codes(material_name_matrix)
    matrix = flip_matrix_by_step(matrix)
    matrix = flip_matrix_by_row(matrix)
    po = build_po_from_simulation_payload(candidate_payload)
    if len(length_values) != len(matrix[0]):
        raise ValueError(
            f"Length/matrix column mismatch: length has {len(length_values)} items, matrix has {len(matrix[0])} columns."
        )

    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "length.txt").write_text(
        f"length = {pformat(length_values, width=120)}\n",
        encoding="utf-8",
    )
    (result_dir / "matrix.txt").write_text(
        f"matrix = {pformat(matrix, width=120)}\n",
        encoding="utf-8",
    )
    (result_dir / "po.txt").write_text(
        f"po = {pformat(po, width=120)}\n",
        encoding="utf-8",
    )
    (result_dir / "result.json").write_text(
        json.dumps(
            {
                "candidate_rank": candidate_payload.get("candidate_rank"),
                "original_candidate_rank": candidate_payload.get("original_candidate_rank"),
                "candidate_score": candidate_payload.get("candidate_score"),
                "candidate_eta_sum": candidate_payload.get("candidate_eta_sum"),
                "material_switch_count": candidate_payload.get("material_switch_count"),
                "step_reversed_for_simulation": candidate_payload.get("step_reversed_for_simulation"),
                "material_codes": MATERIAL_CODES,
                "length": length_values,
                "matrix": matrix,
                "po": po,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def run_from_candidate_simulation(
    candidate_dir: Path | str,
    *,
    output_root: Path | str = DEFAULT_OUTPUT_ROOT,
    length_matrix_path: Path | None = None,
    source_dir: Path | str = DEFAULT_SOURCE_DIR,
    matlab_command: str = DEFAULT_MATLAB_COMMAND,
    matlab_stage_root: Path | None = MATLAB_STAGE_ROOT,
    prepare_only: bool = False,
    feed_length_start: float = FEED_LENGTH_START,
    feed_length_end: float = FEED_LENGTH_END,
) -> Path:
    candidate_dir = resolve_candidate_dir(candidate_dir)
    output_root = Path(output_root).resolve()
    source_dir = Path(source_dir).resolve()

    output_base = output_root / candidate_dir.name
    source_dm_output_dir = output_base / "source_dm_filament"

    runner_globals = runpy.run_path(str(SOURCE_DM_RUNNER))
    run_from_result_folder = runner_globals["run_from_result_folder"]
    if is_result_dir(candidate_dir):
        result_dir = candidate_dir
    else:
        candidate_json_path = find_candidate_simulation_json(candidate_dir)
        candidate_payload = load_json(candidate_json_path)
        length_values = load_length_values(length_matrix_path.resolve() if length_matrix_path is not None else None)
        result_dir = output_base / "result_input"
        save_result_inputs(candidate_payload, length_values, result_dir)

    run_from_result_folder(
        result_dir,
        source_dir=source_dir,
        output_dir=source_dm_output_dir,
        matlab_command=matlab_command,
        matlab_stage_root=matlab_stage_root,
        feed_length_start=feed_length_start,
        feed_length_end=feed_length_end,
        run_main=not prepare_only,
    )
    return output_base


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert one candidate simulation folder or result folder "
            "into DM_filament inputs and generate G-code in a separate out folder."
        )
    )
    parser.add_argument(
        "candidate_dir",
        nargs="?",
        default=CANDIDATE_DIR,
        help=(
            "Candidate simulation folder path, result folder path, "
            "or folder name like candidate_rank_0001. "
            "If omitted, the script uses CANDIDATE_DIR from the user settings block."
        ),
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=OUTPUT_ROOT,
        help="Root output folder. Default: OUTPUT_ROOT in this file.",
    )
    parser.add_argument(
        "--length-matrix-path",
        type=Path,
        default=LENGTH_MATRIX_PATH,
        help="Optional length_matrix.json path. Default: LENGTH_MATRIX_PATH in this file.",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=SOURCE_DIR,
        help="Source_DM_filament folder. Default: SOURCE_DIR in this file.",
    )
    parser.add_argument(
        "--matlab-command",
        default=MATLAB_COMMAND,
        help="MATLAB command or full matlab.exe path. Default: MATLAB_COMMAND in this file.",
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
        default=PREPARE_ONLY,
        help="Only prepare result_input and Matinfo-related files; do not run Source_DM_filament/main.m.",
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
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.candidate_dir is None:
        raise ValueError("Set CANDIDATE_DIR in this file or pass candidate_dir on the command line.")
    output_base = run_from_candidate_simulation(
        args.candidate_dir,
        output_root=args.output_root,
        length_matrix_path=args.length_matrix_path,
        source_dir=args.source_dir,
        matlab_command=args.matlab_command,
        matlab_stage_root=args.matlab_stage_root,
        prepare_only=args.prepare_only,
        feed_length_start=args.feed_length_start,
        feed_length_end=args.feed_length_end,
    )
    print("")
    print("[DONE] Candidate simulation -> DM_filament")
    print(f"  input : {args.candidate_dir}")
    print(f"  output: {output_base}")


if __name__ == "__main__":
    main()
