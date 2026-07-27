from __future__ import annotations

import json
import os
from pathlib import Path
from pprint import pformat
import runpy
import shutil
import time
import matplotlib.pyplot as plt
import numpy as np
from tqdm import tqdm

from scripts.simulation.simulate_matrix_deposition import (
    build_payload,
    format_payload,
    save_final_stack_image,
    save_stacking_animation,
)
from scripts.utils.property_program_utils import (
    get_assignments_in_spatial_order,
    get_effective_gradient_steps,
    get_property_type,
    PROPERTY_PATH_ENV_KEY,
    resolve_assignment_material_pair,
)
from scripts.property_guided.resolve_property_guided_program import (
    DEFAULT_RESOLVED_OUTPUT_PATH,
    DEFAULT_SUMMARY_PATH as PROPERTY_GUIDED_SUMMARY_PATH,
    resolve_property_guided_program_to_path,
)
from scripts.property_guided.expand_layer_region_program import (
    expand_layer_region_program_to_path,
)


# This file is a workflow-oriented entry point for the current project.
# The goal is not only to execute the pipeline, but also to explain
# what each stage consumes and what it produces.
#
# Important design choice:
# - We do NOT rebuild the material dictionary.
# - We assume the existing material dictionary is already valid and reuse it.
#
# Run this file from the project root:
#   python main.py


PROJECT_ROOT = Path(__file__).resolve().parent
LAYER_REGION_EXPANDED_OUTPUT_PATH = (
    PROJECT_ROOT
    / "test_sample"
    / "derived"
    / "layer_region"
    / "expanded_property_program.json"
)
KNOWN_MATERIALS = {
    "PLA",
    "CPLA",
    "TPU",
    "PETG",
    "SMP",
    "CYAN",
    "MAGENTA",
    "YELLOW",
    "WHITE",
    "BLACK",
    "UNKNOWN",
}
MATERIAL_COLORS = {
    "PLA": "#2563eb",
    "CPLA": "#f97316",
    "TPU": "#10b981",
    "PETG": "#8b5cf6",
    "SMP": "#ef4444",
    "CYAN": "#06b6d4",
    "MAGENTA": "#d946ef",
    "YELLOW": "#eab308",
    "WHITE": "#e5e7eb",
    "BLACK": "#111827",
    "UNKNOWN": "#9ca3af",
    "Other": "#9ca3af",
}
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
ROW_WEIGHTS = [2, 3, 3, 4, 4, 4, 4, 4, 4, 4, 4, 3, 3, 2]


def ratio_plot_color(material_name: str) -> str:
    if str(material_name).upper() == "WHITE":
        return "#94a3b8"
    return MATERIAL_COLORS.get(material_name, MATERIAL_COLORS["Other"])


def resolve_project_path(path_text: str | Path) -> Path:
    path = Path(path_text)
    return path if path.is_absolute() else PROJECT_ROOT / path


# -----------------------------
# Input files used by the flow
# -----------------------------
# These are the main upstream files that feed the current pipeline.
SAMPLE_INFO_PATH_ENV_KEY = "B_FDM_SAMPLE_INFO_PATH"
SELECTED_PROPERTY_FILE = "Property_vase.json"
PROPERTY_PATH = resolve_project_path(
    os.environ.get(
        PROPERTY_PATH_ENV_KEY,
        PROJECT_ROOT / "input" / "config" / SELECTED_PROPERTY_FILE,
    )
)
EFFECTIVE_PROPERTY_PATH = PROPERTY_PATH
SAMPLE_INFO_PATH = resolve_project_path(
    os.environ.get(
        SAMPLE_INFO_PATH_ENV_KEY,
        PROJECT_ROOT / "input" / "config" / "sample_info.json",
    )
)
DEFAULT_MATERIAL_DICTIONARY_PATH = PROJECT_ROOT / "input" / "config" / "material_dictionary.json"
MATERIAL_DICTIONARY_ENV_KEY = "B_FDM_MATERIAL_DICTIONARY_PATH"
ADJACENCY_SEARCH_ALGORITHM_ENV_KEY = "B_FDM_ADJACENCY_SEARCH_ALGORITHM"
BEAM_BEST_PER_STEP_ENV_KEY = "B_FDM_BEAM_BEST_PER_STEP"
RESULT_COUNT_ENV_KEY = "B_FDM_RESULT_COUNT"
ETA_MIN_ENV_KEY = "B_FDM_ETA_MIN"
ETA_MAX_ENV_KEY = "B_FDM_ETA_MAX"
RUN_SOURCE_DM_FILAMENT_ENV_KEY = "B_FDM_RUN_SOURCE_DM_FILAMENT"
SOURCE_DM_MATLAB_COMMAND_ENV_KEY = "B_FDM_MATLAB_COMMAND"
REGION_RECOGNITION_MODE_ENV_KEY = "B_FDM_REGION_RECOGNITION_MODE"

# 0 keeps every best-score tie at each beam step.
# Set this to N > 0 to keep at most N best-score states per step.
DEFAULT_BEAM_BEST_PER_STEP_LIMIT = 50
DEFAULT_ADJACENCY_SEARCH_ALGORITHM = "ga"
ADJACENCY_SEARCH_ALGORITHM_CHOICES = {"astar", "beam", "bfs", "dfs", "dijkstra", "ga"}

# Number of final candidate result folders to generate under out/simulation/candidate_simulations.
DEFAULT_RESULT_COUNT = 100

# Global eta filter for candidate generation.
# None means no global bound. Assignment-level eta still works as the default upper bound.
DEFAULT_ETA_MIN: float | None = None
DEFAULT_ETA_MAX: float | None = None

# MATLAB/G-code generation is optional because it requires a local MATLAB install.
DEFAULT_RUN_SOURCE_DM_FILAMENT = False
DEFAULT_SOURCE_DM_MATLAB_COMMAND = "matlab"


def resolve_non_negative_int(env_key: str, default: int) -> int:
    raw_value = os.environ.get(env_key)
    if raw_value is None or not raw_value.strip():
        return default
    value = int(raw_value)
    if value < 0:
        raise ValueError(f"{env_key} must be >= 0, got {value}")
    return value


def resolve_optional_float(env_key: str, default: float | None) -> float | None:
    raw_value = os.environ.get(env_key)
    if raw_value is None or not raw_value.strip():
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"none", "null", "off", "disabled"}:
        return None
    return float(raw_value)


def resolve_bool(env_key: str, default: bool) -> bool:
    raw_value = os.environ.get(env_key)
    if raw_value is None or not raw_value.strip():
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "y", "on"}


def resolve_adjacency_search_algorithm() -> str:
    raw_value = os.environ.get(ADJACENCY_SEARCH_ALGORITHM_ENV_KEY, DEFAULT_ADJACENCY_SEARCH_ALGORITHM)
    algorithm = raw_value.strip().lower()
    aliases = {
        "genetic": "ga",
        "genetic_algorithm": "ga",
        "beam_search": "beam",
        "a*": "astar",
        "a_star": "astar",
        "diax": "dijkstra",
        "dij": "dijkstra",
        "dijkstra_search": "dijkstra",
        "breadth_first": "bfs",
        "depth_first": "dfs",
    }
    algorithm = aliases.get(algorithm, algorithm)
    if algorithm not in ADJACENCY_SEARCH_ALGORITHM_CHOICES:
        choices = ", ".join(sorted(ADJACENCY_SEARCH_ALGORITHM_CHOICES))
        raise ValueError(f"{ADJACENCY_SEARCH_ALGORITHM_ENV_KEY} must be one of: {choices}")
    return algorithm


MATERIAL_DICTIONARY_PATH = resolve_project_path(
    os.environ.get(MATERIAL_DICTIONARY_ENV_KEY, DEFAULT_MATERIAL_DICTIONARY_PATH)
)
ADJACENCY_SEARCH_ALGORITHM = resolve_adjacency_search_algorithm()
BEAM_BEST_PER_STEP_LIMIT = resolve_non_negative_int(
    BEAM_BEST_PER_STEP_ENV_KEY,
    DEFAULT_BEAM_BEST_PER_STEP_LIMIT,
)
RESULT_COUNT = resolve_non_negative_int(RESULT_COUNT_ENV_KEY, DEFAULT_RESULT_COUNT)
ETA_MIN = resolve_optional_float(ETA_MIN_ENV_KEY, DEFAULT_ETA_MIN)
ETA_MAX = resolve_optional_float(ETA_MAX_ENV_KEY, DEFAULT_ETA_MAX)
if ETA_MIN is not None and ETA_MAX is not None and ETA_MIN > ETA_MAX:
    raise ValueError(f"{ETA_MIN_ENV_KEY} must be <= {ETA_MAX_ENV_KEY}")
RUN_SOURCE_DM_FILAMENT = resolve_bool(
    RUN_SOURCE_DM_FILAMENT_ENV_KEY,
    DEFAULT_RUN_SOURCE_DM_FILAMENT,
)
SOURCE_DM_MATLAB_COMMAND = os.environ.get(
    SOURCE_DM_MATLAB_COMMAND_ENV_KEY,
    DEFAULT_SOURCE_DM_MATLAB_COMMAND,
)


# ---------------------------------
# Internal intermediate targets
# ---------------------------------
# The existing scripts still write to test_sample/derived internally.
# main.py will gather the important results and export them into out/.
INTERNAL_LENGTH_MATRIX_JSON = PROJECT_ROOT / "test_sample" / "derived" / "matrices" / "length_matrix.json"
INTERNAL_ASSIGNMENT_MATRIX_TXT = PROJECT_ROOT / "test_sample" / "derived" / "matrices" / "assignment_candidate_matrix.txt"
INTERNAL_BEAM_ADJACENCY_TXT = PROJECT_ROOT / "test_sample" / "derived" / "adjacency" / "beam_step_adjacency.txt"
INTERNAL_BEST_CLUSTER_TXT = PROJECT_ROOT / "test_sample" / "derived" / "adjacency" / "beam_step_adjacency_clusters_best.txt"
INTERNAL_MATERIAL_SWITCH_REPORT_TXT = (
    PROJECT_ROOT / "test_sample" / "derived" / "simulation" / "beam_step_adjacency_clusters_best_material_switches.txt"
)
INTERNAL_SCORE_FINAL_JSON = (
    PROJECT_ROOT / "test_sample" / "derived" / "simulation" / "beam_step_adjacency_clusters_best_switch_eta_ranked.json"
)
INTERNAL_SCORE_FINAL_TXT = (
    PROJECT_ROOT / "test_sample" / "derived" / "simulation" / "beam_step_adjacency_clusters_best_switch_eta_ranked.txt"
)
INTERNAL_CANDIDATE_SIMULATIONS_DIR = (
    PROJECT_ROOT / "test_sample" / "derived" / "simulation" / "candidate_simulations"
)


# --------------------
# Final exported paths
# --------------------
# These are the paths the user should treat as the official workflow outputs.
# Each property program gets its own output tree so runs do not overwrite each other.
OUT_ROOT = PROJECT_ROOT / "out" / PROPERTY_PATH.stem
OUT_MATRICES_DIR = OUT_ROOT / "matrices"
OUT_ADJACENCY_DIR = OUT_ROOT / "adjacency"
OUT_SIMULATION_DIR = OUT_ROOT / "simulation"
OUT_SIMULATION_INTERMEDIATE_DIR = OUT_SIMULATION_DIR / "intermediate"
OUT_RESULT_DIR = OUT_ROOT / "result"

OUT_LENGTH_MATRIX_JSON = OUT_MATRICES_DIR / "length_matrix.json"
OUT_LENGTH_MATRIX_NPY = OUT_MATRICES_DIR / "length_matrix.npy"
OUT_ASSIGNMENT_MATRIX_JSON = OUT_MATRICES_DIR / "assignment_candidate_matrix.json"
OUT_ASSIGNMENT_MATRIX_TXT = OUT_MATRICES_DIR / "assignment_candidate_matrix.txt"
OUT_OPTIMAL_MATRIX_TXT = OUT_MATRICES_DIR / "optimal_beam_candidate_matrix.txt"
OUT_OPTIMAL_MATRIX_ONLY_TXT = OUT_MATRICES_DIR / "optimal_beam_candidate_matrix_only.txt"
OUT_BEAM_ADJACENCY_JSON = OUT_ADJACENCY_DIR / "beam_step_adjacency.json"
OUT_BEAM_ADJACENCY_TXT = OUT_ADJACENCY_DIR / "beam_step_adjacency.txt"
OUT_BEST_CLUSTER_JSON = OUT_ADJACENCY_DIR / "beam_step_adjacency_clusters_best.json"
OUT_BEST_CLUSTER_TXT = OUT_ADJACENCY_DIR / "beam_step_adjacency_clusters_best.txt"
OUT_BEST_CLUSTER_PNG = OUT_ADJACENCY_DIR / "beam_step_adjacency_clusters_best.png"
OUT_CANDIDATE_SIMULATIONS_DIR = OUT_SIMULATION_DIR / "candidate_simulations"
OUT_MATERIAL_SWITCH_JSON = OUT_SIMULATION_INTERMEDIATE_DIR / "beam_step_adjacency_clusters_best_material_switches.json"
OUT_MATERIAL_SWITCH_TXT = OUT_SIMULATION_INTERMEDIATE_DIR / "beam_step_adjacency_clusters_best_material_switches.txt"
OUT_SCORE_FINAL_JSON = OUT_SIMULATION_INTERMEDIATE_DIR / "beam_step_adjacency_clusters_best_switch_eta_ranked.json"
OUT_SCORE_FINAL_TXT = OUT_SIMULATION_INTERMEDIATE_DIR / "beam_step_adjacency_clusters_best_switch_eta_ranked.txt"
OUT_LOCAL_GLOBAL_ANALYSIS_JSON = OUT_SIMULATION_INTERMEDIATE_DIR / "local_global_analysis.json"
OUT_LOCAL_GLOBAL_ANALYSIS_TXT = OUT_SIMULATION_INTERMEDIATE_DIR / "local_global_analysis.txt"
OUT_OPTIMAL_CANDIDATE_JSON = OUT_SIMULATION_INTERMEDIATE_DIR / "optimal_beam_candidate.json"
OUT_OPTIMAL_CANDIDATE_TXT = OUT_SIMULATION_INTERMEDIATE_DIR / "optimal_beam_candidate.txt"
OUT_OPTIMAL_MATERIAL_NAME_MATRIX_JSON = OUT_SIMULATION_INTERMEDIATE_DIR / "optimal_beam_candidate_material_names.json"
OUT_OPTIMAL_SIMULATION_JSON = OUT_SIMULATION_INTERMEDIATE_DIR / "optimal_beam_candidate_simulation.json"
OUT_OPTIMAL_SIMULATION_TXT = OUT_SIMULATION_INTERMEDIATE_DIR / "optimal_beam_candidate_simulation.txt"
OUT_OPTIMAL_SIMULATION_PNG = OUT_SIMULATION_INTERMEDIATE_DIR / "optimal_beam_candidate_simulation.png"
OUT_OPTIMAL_SIMULATION_GIF = OUT_SIMULATION_INTERMEDIATE_DIR / "optimal_beam_candidate_simulation.gif"
OUT_OPTIMAL_RATIO_ETA_PNG = OUT_SIMULATION_INTERMEDIATE_DIR / "optimal_beam_candidate_ratio_eta_plot.png"
OUT_OPTIMAL_RATIO_ETA_JSON = OUT_SIMULATION_INTERMEDIATE_DIR / "optimal_beam_candidate_ratio_eta_plot.json"
OUT_RESULT_LENGTH_TXT = OUT_RESULT_DIR / "length.txt"
OUT_RESULT_MATRIX_TXT = OUT_RESULT_DIR / "matrix.txt"
OUT_RESULT_PO_TXT = OUT_RESULT_DIR / "po.txt"
OUT_RESULT_JSON = OUT_RESULT_DIR / "result.json"
SOURCE_DM_FILAMENT_DIR = PROJECT_ROOT / "Source_DM_filament"
SOURCE_DM_PYTHON_RUNNER = PROJECT_ROOT / "scripts" / "source_dm_filament" / "run_from_result.py"


def build_adjacency_search_stage() -> tuple[str, str, list[Path]]:
    expected_outputs = [
        PROJECT_ROOT / "test_sample" / "derived" / "adjacency" / "beam_step_adjacency.json",
        INTERNAL_BEAM_ADJACENCY_TXT,
    ]
    if ADJACENCY_SEARCH_ALGORITHM == "beam":
        return (
            "scripts/build/beam_search_step_adjacency_from_text.py",
            "Search Step Adjacency With Beam Search",
            expected_outputs,
        )
    if ADJACENCY_SEARCH_ALGORITHM == "ga":
        return (
            "scripts/build/genetic_algorithm_step_adjacency_from_text.py",
            "Search Step Adjacency With Genetic Algorithm",
            expected_outputs,
        )
    if ADJACENCY_SEARCH_ALGORITHM in {"astar", "dijkstra", "bfs", "dfs"}:
        display_name = {
            "astar": "A*",
            "dijkstra": "Dijkstra",
            "bfs": "BFS",
            "dfs": "DFS",
        }[ADJACENCY_SEARCH_ALGORITHM]
        return (
            "scripts/build/path_search_step_adjacency_from_text.py",
            f"Search Step Adjacency With {display_name} Path Search",
            expected_outputs,
        )
    raise ValueError(f"Unsupported adjacency search algorithm: {ADJACENCY_SEARCH_ALGORITHM}")


def ensure_required_inputs() -> None:
    # We stop early if the essential upstream files are missing.
    # This makes the failure mode much clearer than letting a downstream
    # script fail somewhere deeper in the pipeline.
    required_paths = [
        PROPERTY_PATH,
        SAMPLE_INFO_PATH,
        MATERIAL_DICTIONARY_PATH,
    ]

    missing = [path for path in required_paths if not path.exists()]
    if missing:
        missing_text = "\n".join(f"- {path}" for path in missing)
        raise FileNotFoundError(f"Required input files are missing:\n{missing_text}")


def run_script(script_relative_path: str, stage_name: str, expected_outputs: list[Path]) -> None:
    # Each stage is executed as an independent Python script.
    # We use runpy so the original scripts can stay as they are,
    # with their own __main__ entry points and local file logic.
    script_path = PROJECT_ROOT / script_relative_path

    if not script_path.exists():
        raise FileNotFoundError(f"Workflow stage not found: {script_path}")

    print("")
    print(f"[START] {stage_name}")
    print(f"  script: {script_path}")

    previous_material_dictionary_path = os.environ.get(MATERIAL_DICTIONARY_ENV_KEY)
    previous_property_path = os.environ.get(PROPERTY_PATH_ENV_KEY)
    previous_sample_info_path = os.environ.get(SAMPLE_INFO_PATH_ENV_KEY)
    previous_beam_best_per_step_limit = os.environ.get(BEAM_BEST_PER_STEP_ENV_KEY)
    previous_result_count = os.environ.get(RESULT_COUNT_ENV_KEY)
    previous_eta_min = os.environ.get(ETA_MIN_ENV_KEY)
    previous_eta_max = os.environ.get(ETA_MAX_ENV_KEY)
    os.environ[MATERIAL_DICTIONARY_ENV_KEY] = str(MATERIAL_DICTIONARY_PATH)
    os.environ[PROPERTY_PATH_ENV_KEY] = str(EFFECTIVE_PROPERTY_PATH)
    os.environ[SAMPLE_INFO_PATH_ENV_KEY] = str(SAMPLE_INFO_PATH)
    os.environ[BEAM_BEST_PER_STEP_ENV_KEY] = str(BEAM_BEST_PER_STEP_LIMIT)
    os.environ[RESULT_COUNT_ENV_KEY] = str(RESULT_COUNT)
    if ETA_MIN is None:
        os.environ.pop(ETA_MIN_ENV_KEY, None)
    else:
        os.environ[ETA_MIN_ENV_KEY] = str(ETA_MIN)
    if ETA_MAX is None:
        os.environ.pop(ETA_MAX_ENV_KEY, None)
    else:
        os.environ[ETA_MAX_ENV_KEY] = str(ETA_MAX)
    try:
        start = time.perf_counter()
        runpy.run_path(str(script_path), run_name="__main__")
        elapsed = time.perf_counter() - start
    finally:
        if previous_material_dictionary_path is None:
            os.environ.pop(MATERIAL_DICTIONARY_ENV_KEY, None)
        else:
            os.environ[MATERIAL_DICTIONARY_ENV_KEY] = previous_material_dictionary_path
        if previous_property_path is None:
            os.environ.pop(PROPERTY_PATH_ENV_KEY, None)
        else:
            os.environ[PROPERTY_PATH_ENV_KEY] = previous_property_path
        if previous_sample_info_path is None:
            os.environ.pop(SAMPLE_INFO_PATH_ENV_KEY, None)
        else:
            os.environ[SAMPLE_INFO_PATH_ENV_KEY] = previous_sample_info_path
        if previous_beam_best_per_step_limit is None:
            os.environ.pop(BEAM_BEST_PER_STEP_ENV_KEY, None)
        else:
            os.environ[BEAM_BEST_PER_STEP_ENV_KEY] = previous_beam_best_per_step_limit
        if previous_result_count is None:
            os.environ.pop(RESULT_COUNT_ENV_KEY, None)
        else:
            os.environ[RESULT_COUNT_ENV_KEY] = previous_result_count
        if previous_eta_min is None:
            os.environ.pop(ETA_MIN_ENV_KEY, None)
        else:
            os.environ[ETA_MIN_ENV_KEY] = previous_eta_min
        if previous_eta_max is None:
            os.environ.pop(ETA_MAX_ENV_KEY, None)
        else:
            os.environ[ETA_MAX_ENV_KEY] = previous_eta_max

    print(f"[DONE]  {stage_name} ({elapsed:.2f}s)")

    # After each stage, verify that the expected artifacts were actually created.
    # This helps the workflow serve as both documentation and a sanity check.
    for output_path in expected_outputs:
        if output_path.exists():
            print(f"  output: {output_path}")
        else:
            raise FileNotFoundError(
                f"Expected output was not created by stage '{stage_name}': {output_path}"
            )


def ensure_out_directories() -> None:
    # The exported results are separated from test_sample so the pipeline output
    # is easier to inspect without mixing it with input and legacy derived files.
    OUT_MATRICES_DIR.mkdir(parents=True, exist_ok=True)
    OUT_ADJACENCY_DIR.mkdir(parents=True, exist_ok=True)
    OUT_SIMULATION_DIR.mkdir(parents=True, exist_ok=True)
    OUT_SIMULATION_INTERMEDIATE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_RESULT_DIR.mkdir(parents=True, exist_ok=True)
    for internal_dir in (
        PROJECT_ROOT / "test_sample" / "derived" / "matrices",
        PROJECT_ROOT / "test_sample" / "derived" / "adjacency",
        PROJECT_ROOT / "test_sample" / "derived" / "simulation",
    ):
        internal_dir.mkdir(parents=True, exist_ok=True)


def copy_file(src: Path, dst: Path) -> None:
    if not src.exists():
        raise FileNotFoundError(f"Cannot export missing file: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def copy_directory(src: Path, dst: Path) -> None:
    if not src.exists():
        raise FileNotFoundError(f"Cannot export missing directory: {src}")
    if dst.exists():
        remove_directory_if_exists(dst)
    shutil.copytree(src, dst)


def make_backup_path(path: Path) -> Path:
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    candidate = path.with_name(f"{path.name}_old_{timestamp}")
    suffix = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}_old_{timestamp}_{suffix}")
        suffix += 1
    return candidate


def remove_directory_if_exists(path: Path) -> None:
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except OSError as exc:
        backup_path = make_backup_path(path)
        try:
            path.rename(backup_path)
        except OSError as rename_exc:
            raise OSError(
                f"Could not remove or rename existing directory: {path}. "
                "Close files opened under this folder and retry."
            ) from rename_exc
        print(
            f"[WARN] Could not remove existing directory, renamed it instead:\n"
            f"  from: {path}\n"
            f"  to  : {backup_path}\n"
            f"  reason: {exc}"
        )


def remove_file_if_exists(path: Path) -> None:
    if path.exists():
        path.unlink()


def cleanup_legacy_simulation_outputs() -> None:
    # These files used to live directly under out/simulation.
    # Keep final candidate folders at the top level and move reports into intermediate/.
    for filename in [
        "beam_step_adjacency_clusters_best_material_switches.json",
        "beam_step_adjacency_clusters_best_material_switches.txt",
        "beam_step_adjacency_clusters_best_switch_eta_ranked.json",
        "beam_step_adjacency_clusters_best_switch_eta_ranked.txt",
        "optimal_beam_candidate.json",
        "optimal_beam_candidate.txt",
        "optimal_beam_candidate_material_names.json",
        "optimal_beam_candidate_simulation.json",
        "optimal_beam_candidate_simulation.txt",
        "optimal_beam_candidate_simulation.png",
        "optimal_beam_candidate_simulation.gif",
        "optimal_beam_candidate_ratio_eta_plot.png",
        "optimal_beam_candidate_ratio_eta_plot.json",
    ]:
        remove_file_if_exists(OUT_SIMULATION_DIR / filename)


def export_outputs_to_out() -> None:
    # This export step is the bridge between the existing internal scripts
    # and the cleaner top-level output directory requested by the user.
    print("")
    print("[START] Export Workflow Outputs To out/")
    cleanup_legacy_simulation_outputs()
    export_bar = tqdm(total=14, desc="Export Outputs To out/", unit="file")
    try:
        copy_file(
            PROJECT_ROOT / "test_sample" / "derived" / "matrices" / "length_matrix.json",
            OUT_LENGTH_MATRIX_JSON,
        )
        export_bar.update(1)
        copy_file(
            PROJECT_ROOT / "test_sample" / "derived" / "matrices" / "length_matrix.npy",
            OUT_LENGTH_MATRIX_NPY,
        )
        export_bar.update(1)
        copy_file(
            PROJECT_ROOT / "test_sample" / "derived" / "matrices" / "assignment_candidate_matrix.json",
            OUT_ASSIGNMENT_MATRIX_JSON,
        )
        export_bar.update(1)
        copy_file(
            PROJECT_ROOT / "test_sample" / "derived" / "matrices" / "assignment_candidate_matrix.txt",
            OUT_ASSIGNMENT_MATRIX_TXT,
        )
        export_bar.update(1)
        copy_file(
            PROJECT_ROOT / "test_sample" / "derived" / "adjacency" / "beam_step_adjacency.json",
            OUT_BEAM_ADJACENCY_JSON,
        )
        export_bar.update(1)
        copy_file(
            PROJECT_ROOT / "test_sample" / "derived" / "adjacency" / "beam_step_adjacency.txt",
            OUT_BEAM_ADJACENCY_TXT,
        )
        export_bar.update(1)
        copy_file(
            PROJECT_ROOT / "test_sample" / "derived" / "adjacency" / "beam_step_adjacency_clusters_best.json",
            OUT_BEST_CLUSTER_JSON,
        )
        export_bar.update(1)
        copy_file(
            PROJECT_ROOT / "test_sample" / "derived" / "adjacency" / "beam_step_adjacency_clusters_best.txt",
            OUT_BEST_CLUSTER_TXT,
        )
        export_bar.update(1)
        best_cluster_png = PROJECT_ROOT / "test_sample" / "derived" / "adjacency" / "beam_step_adjacency_clusters_best.png"
        if best_cluster_png.exists():
            copy_file(best_cluster_png, OUT_BEST_CLUSTER_PNG)
        export_bar.update(1)
        remove_directory_if_exists(OUT_ADJACENCY_DIR / "beam_step_adjacency_cluster_images_top100")
        copy_file(
            PROJECT_ROOT / "test_sample" / "derived" / "simulation" / "beam_step_adjacency_clusters_best_material_switches.json",
            OUT_MATERIAL_SWITCH_JSON,
        )
        export_bar.update(1)
        copy_file(
            PROJECT_ROOT / "test_sample" / "derived" / "simulation" / "beam_step_adjacency_clusters_best_material_switches.txt",
            OUT_MATERIAL_SWITCH_TXT,
        )
        export_bar.update(1)
        copy_file(INTERNAL_SCORE_FINAL_JSON, OUT_SCORE_FINAL_JSON)
        export_bar.update(1)
        copy_file(INTERNAL_SCORE_FINAL_TXT, OUT_SCORE_FINAL_TXT)
        export_bar.update(1)
        copy_directory(INTERNAL_CANDIDATE_SIMULATIONS_DIR, OUT_CANDIDATE_SIMULATIONS_DIR)
        export_bar.update(1)
    finally:
        export_bar.close()

    print("[DONE]  Export Workflow Outputs To out/")
    print(f"  output root: {OUT_ROOT}")


def load_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_region_recognition_mode(property_path: Path) -> str:
    configured_mode = os.environ.get(REGION_RECOGNITION_MODE_ENV_KEY)
    if configured_mode is None:
        configured_mode = load_json(property_path).get(
            "region_recognition_mode",
            "layer-region",
        )
    normalized = str(configured_mode).strip().lower().replace("_", "-")
    return "z-axis" if normalized in {"z", "z-axis", "zaxis"} else "layer-region"


def normalize_material_name(name: str) -> str:
    normalized = str(name).strip().upper()
    return normalized if normalized in KNOWN_MATERIALS else "Other"


def hex_to_rgb01(color_hex: str) -> tuple[float, float, float]:
    color_hex = color_hex.lstrip("#")
    return tuple(int(color_hex[index:index + 2], 16) / 255.0 for index in (0, 2, 4))


def build_optimal_candidate_summary() -> dict[str, object]:
    # Score Final already applies the final ranking rule.
    payload = load_json(OUT_SCORE_FINAL_JSON)
    results = payload.get("results", [])
    if not results:
        raise ValueError("No candidate results were found in the Score Final report.")

    optimal = results[0]

    return {
        "selection_rule": (
            "Choose the beam_step_adjacency_clusters_best candidate by Score Final priority: "
            "gradient_eta_target_error_sum ascending, material_switch_count ascending, "
            "score descending, rank ascending."
        ),
        "source_candidates_path": payload.get("source_candidates_path"),
        "min_material_switch_count": int(optimal["material_switch_count"]),
        "eta_sum": float(optimal.get("eta_sum", 0.0)),
        "eta_avg": float(optimal.get("eta_avg", 0.0)),
        "eta_min": float(optimal.get("eta_min", 0.0)),
        "eta_max": float(optimal.get("eta_max", 0.0)),
        "rank": int(optimal["rank"]),
        "score": int(optimal["score"]),
        "step_scores": optimal.get("step_scores", []),
        "selected_case_keys": optimal.get("selected_case_keys", []),
        "material_switch_events": optimal.get("material_switch_events", []),
    }


def build_final_ranking_terminal_summary() -> dict[str, object]:
    # This compact summary makes the ranking/filtering scale visible in the terminal.
    payload = load_json(OUT_SCORE_FINAL_JSON)
    results = payload.get("results", [])
    summary = payload.get("summary", {})
    saved_candidate_count = len(
        list(OUT_CANDIDATE_SIMULATIONS_DIR.glob("candidate_rank_*/candidate_rank_*_simulation.json"))
    )

    return {
        "requested_result_count": RESULT_COUNT,
        "saved_result_count": saved_candidate_count,
        "ranked_candidate_count": int(summary.get("candidate_count", len(results))),
        "best_gradient_eta_target_error_sum": float(
            summary.get("best_gradient_eta_target_error_sum", 0.0)
        ),
        "best_gradient_eta_target_candidate_count": int(
            summary.get("best_gradient_eta_target_candidate_count", 0)
        ),
        "min_material_switch_count_at_best_gradient_eta_target": int(
            summary.get("min_material_switch_count_at_best_gradient_eta_target", 0)
        ),
        "best_gradient_eta_target_min_switch_candidate_count": int(
            summary.get("best_gradient_eta_target_min_switch_candidate_count", 0)
        ),
        "best_rank_after_sort": summary.get("best_rank_after_sort"),
        "switch_count_histogram": summary.get("switch_count_histogram", {}),
        "skipped_candidate_count": len(payload.get("skipped_candidates", [])),
    }


def print_final_ranking_terminal_summary(summary: dict[str, object]) -> None:
    print("")
    print("Final ranking count summary:")
    print(f"  requested result count              : {summary['requested_result_count']}")
    print(f"  saved result count                  : {summary['saved_result_count']}")
    print(f"  ranked candidate count              : {summary['ranked_candidate_count']}")
    print(f"  skipped candidate count             : {summary['skipped_candidate_count']}")
    print(
        "  best gradient eta target error sum : "
        f"{summary['best_gradient_eta_target_error_sum']:.6f}"
    )
    print(
        "  candidates at best eta target      : "
        f"{summary['best_gradient_eta_target_candidate_count']}"
    )
    print(
        "  min switch at best eta target      : "
        f"{summary['min_material_switch_count_at_best_gradient_eta_target']}"
    )
    print(
        "  candidates tied at eta+switch      : "
        f"{summary['best_gradient_eta_target_min_switch_candidate_count']}"
    )
    print(f"  best original rank after sort       : {summary['best_rank_after_sort']}")
    print(f"  switch count histogram              : {summary['switch_count_histogram']}")


def build_local_global_analysis() -> dict[str, object]:
    adjacency_payload = load_json(OUT_BEAM_ADJACENCY_JSON)
    ranking_payload = load_json(OUT_SCORE_FINAL_JSON)

    local_items = adjacency_payload.get("local_gradient_preselection", [])
    ranking_summary = ranking_payload.get("summary", {})
    ranking_results = ranking_payload.get("results", [])
    top_result = ranking_results[0] if ranking_results else None

    local_summary = {
        "gradient_assignment_count": len(local_items),
        "gradient_assignments": local_items,
    }

    global_summary = {
        "ranked_candidate_count": int(ranking_summary.get("candidate_count", len(ranking_results))),
        "saved_candidate_count": len(
            list(OUT_CANDIDATE_SIMULATIONS_DIR.glob("candidate_rank_*/candidate_rank_*_simulation.json"))
        ),
        "min_material_switch_count_global": int(
            ranking_summary.get(
                "min_material_switch_count_global",
                ranking_summary.get("min_material_switch_count_at_best_gradient_eta_target", 0),
            )
        ),
        "min_switch_candidate_count": int(
            ranking_summary.get(
                "min_switch_candidate_count",
                ranking_summary.get("best_gradient_eta_target_min_switch_candidate_count", 0),
            )
        ),
        "switch_count_histogram": ranking_summary.get("switch_count_histogram", {}),
        "top_candidate": (
            {
                "rank": int(top_result["rank"]),
                "score": int(top_result["score"]),
                "material_switch_count": int(top_result["material_switch_count"]),
                "eta_sum": float(top_result.get("eta_sum", 0.0)),
                "selected_case_keys": list(top_result.get("selected_case_keys", [])),
            }
            if top_result is not None
            else None
        ),
    }

    return {
        "source_adjacency_json": str(OUT_BEAM_ADJACENCY_JSON),
        "source_score_final_json": str(OUT_SCORE_FINAL_JSON),
        "local_summary": local_summary,
        "global_summary": global_summary,
    }


def save_local_global_analysis(payload: dict[str, object]) -> None:
    OUT_LOCAL_GLOBAL_ANALYSIS_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines: list[str] = []
    lines.append("local_gradient_analysis:")
    lines.append(
        f"  gradient_assignment_count: {payload['local_summary']['gradient_assignment_count']}"
    )
    for item in payload["local_summary"]["gradient_assignments"]:
        lines.append(
            "  assignment_{assignment:02d} | local_steps {steps} | combinations {combos} | "
            "best_local_score {score} | best_score_tie_count {tie_count} | "
            "best_local_switch {switches} | best_pattern_count_after_switch {pattern_count} | "
            "best_local_eta_sum_min {eta_sum_min:.6f} | best_local_eta_sum_max {eta_sum_max:.6f} | "
            "left_property {left_prop} | right_property {right_prop}".format(
                assignment=int(item["assignment_index"]),
                steps=int(item["local_step_count"]),
                combos=int(item["combination_count"]),
                score=int(item["best_local_score"]),
                tie_count=int(item["best_local_best_score_tie_count"]),
                switches=int(item["best_local_material_switch_count"]),
                pattern_count=int(item["best_local_pattern_count_after_switch"]),
                eta_sum_min=float(item["best_local_eta_sum_min"]),
                eta_sum_max=float(item["best_local_eta_sum_max"]),
                left_prop=bool(item.get("uses_left_boundary_property", False)),
                right_prop=bool(item.get("uses_right_boundary_property", False)),
            )
        )
        for index, case_key_set in enumerate(item["selected_case_key_sets"], start=1):
            lines.append(f"    pattern_{index:02d}: {', '.join(case_key_set)}")

    lines.append("")
    lines.append("global_pattern_analysis:")
    lines.append(
        f"  ranked_candidate_count: {payload['global_summary']['ranked_candidate_count']}"
    )
    lines.append(
        f"  saved_candidate_count: {payload['global_summary']['saved_candidate_count']}"
    )
    lines.append(
        "  min_material_switch_count_global: "
        f"{payload['global_summary']['min_material_switch_count_global']}"
    )
    lines.append(
        "  min_switch_candidate_count: "
        f"{payload['global_summary']['min_switch_candidate_count']}"
    )
    lines.append(
        f"  switch_count_histogram: {payload['global_summary']['switch_count_histogram']}"
    )
    top_candidate = payload["global_summary"].get("top_candidate")
    if top_candidate is None:
        lines.append("  top_candidate: none")
    else:
        lines.append(
            "  top_candidate: rank={rank}, score={score}, switches={switches}, eta_sum={eta_sum:.6f}".format(
                rank=int(top_candidate["rank"]),
                score=int(top_candidate["score"]),
                switches=int(top_candidate["material_switch_count"]),
                eta_sum=float(top_candidate["eta_sum"]),
            )
        )
        lines.append(
            "  top_candidate_case_keys: " + ", ".join(top_candidate["selected_case_keys"])
        )

    OUT_LOCAL_GLOBAL_ANALYSIS_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def print_local_global_analysis_summary(payload: dict[str, object]) -> None:
    print("")
    print("Local / Global analysis:")
    print(
        f"  local gradient groups            : {payload['local_summary']['gradient_assignment_count']}"
    )
    print(
        f"  global ranked candidates         : {payload['global_summary']['ranked_candidate_count']}"
    )
    print(
        "  global min switch count          : "
        f"{payload['global_summary']['min_material_switch_count_global']}"
    )
    top_candidate = payload["global_summary"].get("top_candidate")
    if top_candidate is None:
        print("  global top candidate             : none")
    else:
        print(
            "  global top candidate             : "
            f"rank {top_candidate['rank']} | score {top_candidate['score']} | "
            f"switches {top_candidate['material_switch_count']} | eta_sum {top_candidate['eta_sum']:.6f}"
        )
    print(f"  analysis txt                     : {OUT_LOCAL_GLOBAL_ANALYSIS_TXT}")


def save_optimal_candidate_summary(summary: dict[str, object]) -> None:
    OUT_OPTIMAL_CANDIDATE_JSON.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = [
        "optimal_candidate_rule:",
        str(summary["selection_rule"]),
        "",
        f"rank: {summary['rank']}",
        f"score: {summary['score']}",
        f"min_material_switch_count: {summary['min_material_switch_count']}",
        f"eta_sum: {summary.get('eta_sum', 0.0):.6f}",
        f"eta_avg: {summary.get('eta_avg', 0.0):.6f}",
        f"eta_min: {summary.get('eta_min', 0.0):.6f}",
        f"eta_max: {summary.get('eta_max', 0.0):.6f}",
        f"selected_case_keys: {', '.join(summary['selected_case_keys'])}",
        f"step_scores: {summary['step_scores']}",
        "",
        "material_switch_events:",
    ]

    events = summary.get("material_switch_events", [])
    if events:
        for event in events:
            lines.append(
                "  - switch_{switch_index}: row={row_index}, col={trigger_col_index}, {from_value}->{to_value}".format(
                    switch_index=event["switch_index"],
                    row_index=event["row_index"],
                    trigger_col_index=event["trigger_col_index"],
                    from_value=event["from_value"],
                    to_value=event["to_value"],
                )
            )
    else:
        lines.append("  - none")

    OUT_OPTIMAL_CANDIDATE_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_case_lookup(material_dictionary: dict[str, dict[str, object]]) -> dict[str, list[str]]:
    case_lookup: dict[str, list[str]] = {}
    for case_key, case_info in material_dictionary.items():
        rows = [str(item) for item in case_info["case_rows"]]
        case_lookup[case_key] = rows
    return case_lookup


def build_assignment_step_material_pairs(property_program: dict) -> list[tuple[str, str]]:
    step_pairs: list[tuple[str, str]] = []
    for assignment in get_assignments_in_spatial_order(property_program):
        start_material, end_material = resolve_assignment_material_pair(property_program, assignment)
        start_material = normalize_material_name(start_material)
        end_material = normalize_material_name(end_material)
        gradient_steps = get_effective_gradient_steps(property_program, assignment)
        for _ in range(gradient_steps):
            step_pairs.append((start_material, end_material))
    return step_pairs


def build_step_assignment_indices(property_program: dict) -> list[int]:
    step_assignment_indices: list[int] = []
    for assignment in get_assignments_in_spatial_order(property_program):
        assignment_index = int(assignment.get("assignment_index", 0))
        gradient_steps = get_effective_gradient_steps(property_program, assignment)
        for _ in range(gradient_steps):
            step_assignment_indices.append(assignment_index)
    return step_assignment_indices


def build_step_spatial_metadata_from_length_payload(length_payload: dict) -> list[dict[str, object]]:
    step_metadata: list[dict[str, object]] = []
    for assignment in length_payload.get("assignments", []):
        assignment_index = int(assignment.get("assignment_index", 0))
        for step in assignment.get("step_table", []):
            step_metadata.append(
                {
                    "assignment_index": assignment_index,
                    "assignment_step_index": int(step.get("step_index", 0)),
                    "start_voxel_index": step.get("start_voxel_index"),
                    "end_voxel_index": step.get("end_voxel_index"),
                    "start_layer": step.get("layer_start"),
                    "end_layer": step.get("layer_end"),
                    "step_filament_e_mm": step.get("step_filament_e_mm"),
                }
            )
    return step_metadata


def build_matrix_from_selection(selected_case_keys: list[str], case_lookup: dict[str, list[str]]) -> list[list[str]]:
    selected_rows_per_step = [case_lookup[case_key] for case_key in selected_case_keys]
    row_count = len(selected_rows_per_step[0])
    return [
        [selected_rows_per_step[step_index][row_index] for step_index in range(len(selected_rows_per_step))]
        for row_index in range(row_count)
    ]


def materialize_case_rows(case_rows: list[str], start_material: str, end_material: str) -> list[str]:
    rows: list[str] = []
    for label in case_rows:
        if label == "Material_start":
            rows.append(start_material)
        elif label == "Material_end":
            rows.append(end_material)
        elif label == "White":
            rows.append("WHITE")
        else:
            raise ValueError(f"Unknown material label: {label}")
    return rows


def save_optimal_candidate_matrix(
    summary: dict[str, object],
    material_dictionary: dict[str, dict[str, object]],
    property_program: dict,
) -> list[list[int]]:
    # Convert the selected optimal candidate into the 14 x steps matrix form
    # so the final simulation is tied to the actual minimum-switch candidate.
    case_lookup = build_case_lookup(material_dictionary)
    label_matrix = build_matrix_from_selection(summary["selected_case_keys"], case_lookup)
    step_material_pairs = build_assignment_step_material_pairs(property_program)
    if label_matrix and len(label_matrix[0]) != len(step_material_pairs):
        raise ValueError(
            "Optimal candidate step count does not match the active property program. "
            f"candidate_steps={len(label_matrix[0])}, property_steps={len(step_material_pairs)}, "
            f"property_path={PROPERTY_PATH}"
        )
    material_name_matrix: list[list[str]] = []
    for matrix_row in label_matrix:
        material_name_row: list[str] = []
        for col_index, label in enumerate(matrix_row):
            start_material, end_material = step_material_pairs[col_index]
            if label == "Material_start":
                material_name_row.append(start_material)
            elif label == "Material_end":
                material_name_row.append(end_material)
            elif label == "White":
                material_name_row.append("WHITE")
            else:
                raise ValueError(f"Unknown material label: {label}")
        material_name_matrix.append(material_name_row)
    matrix = convert_material_names_to_codes(material_name_matrix)

    matrix_text = "matrix = " + repr(matrix)
    OUT_OPTIMAL_MATRIX_ONLY_TXT.write_text(matrix_text + "\n", encoding="utf-8")
    OUT_OPTIMAL_MATERIAL_NAME_MATRIX_JSON.write_text(
        json.dumps({"material_name_matrix": material_name_matrix}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = [
        f"rank: {summary['rank']}",
        f"score: {summary['score']}",
        f"material_switch_count: {summary['min_material_switch_count']}",
        f"eta_sum: {summary.get('eta_sum', 0.0):.6f}",
        f"selected_case_keys: {', '.join(summary['selected_case_keys'])}",
        "",
        matrix_text,
    ]
    OUT_OPTIMAL_MATRIX_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return matrix


def convert_material_names_to_codes(material_name_matrix: list[list[str]]) -> list[list[int]]:
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


def material_name_to_result_code(material_name: object) -> int:
    normalized = str(material_name).strip().upper()
    if normalized not in MATERIAL_CODES:
        raise ValueError(f"No result material code is defined for material: {material_name}")
    return MATERIAL_CODES[normalized]


def row_index_to_layer_number(row_index: int, row_count: int) -> int:
    # Simulation row 0 is the top layer. Result layer numbers count from bottom = 1.
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
        material_code = material_name_to_result_code(material_name)
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

    # If the same material appears again later, the earlier segment should end
    # right before the later segment starts so the process windows do not overlap.
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


def save_optimal_candidate_simulation(
    summary: dict[str, object],
    matrix: list[list[int]],
    material_name_matrix: list[list[str]],
) -> dict[str, object]:
    simulation_payload = build_payload(matrix, material_name_matrix)
    simulation_payload["source_matrix_path"] = "optimal_beam_candidate_matrix"
    simulation_payload["candidate_rank"] = 1
    simulation_payload["original_candidate_rank"] = int(summary["rank"])
    simulation_payload["candidate_score"] = int(summary["score"])
    simulation_payload["candidate_eta_sum"] = float(summary.get("eta_sum", 0.0))
    simulation_payload["candidate_selected_case_keys"] = [str(item) for item in summary["selected_case_keys"]]

    OUT_OPTIMAL_SIMULATION_JSON.write_text(
        json.dumps(simulation_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    OUT_OPTIMAL_SIMULATION_TXT.write_text(format_payload(simulation_payload), encoding="utf-8")

    final_state = np.array(simulation_payload["final_state"], dtype=object)
    simulation_matrix = simulation_payload["simulation_matrix"]
    simulation_material_name_matrix = simulation_payload.get("simulation_material_name_matrix")
    compact_candidate_dir = OUT_CANDIDATE_SIMULATIONS_DIR / "candidate_rank_0001"
    compact_png = compact_candidate_dir / "candidate_rank_0001_simulation.png"
    compact_gif = compact_candidate_dir / "candidate_rank_0001_simulation.gif"
    if compact_png.exists():
        shutil.copy2(compact_png, OUT_OPTIMAL_SIMULATION_PNG)
        print(f"Reused compact repeated-pattern PNG: {compact_png}")
    else:
        save_final_stack_image(
            final_state,
            simulation_matrix,
            simulation_material_name_matrix,
            simulation_payload["prioritized_value"],
            simulation_payload["material_switch_events"],
            OUT_OPTIMAL_SIMULATION_PNG,
        )

    if compact_gif.exists():
        shutil.copy2(compact_gif, OUT_OPTIMAL_SIMULATION_GIF)
    elif len(simulation_matrix[0]) <= 120:
        save_stacking_animation(
            simulation_matrix,
            simulation_material_name_matrix,
            simulation_payload["simulation_events"],
            simulation_payload["prioritized_value"],
            simulation_payload["material_switch_events"],
            OUT_OPTIMAL_SIMULATION_GIF,
        )
    else:
        if OUT_OPTIMAL_SIMULATION_GIF.exists():
            OUT_OPTIMAL_SIMULATION_GIF.unlink()
        simulation_payload["animation_skipped"] = True
        simulation_payload["animation_skip_reason"] = (
            f"{len(simulation_matrix[0])} steps exceeds the automatic GIF limit of 120."
        )
        OUT_OPTIMAL_SIMULATION_JSON.write_text(
            json.dumps(simulation_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            f"Skipped duplicate optimal GIF for {len(simulation_matrix[0])} steps; "
            "the compact PNG is retained."
        )
    return simulation_payload


def format_selected_case_keys_for_terminal(selected_case_keys: list[object]) -> str:
    keys = [str(item) for item in selected_case_keys]
    try:
        adjacency_payload = load_json(OUT_BEAM_ADJACENCY_JSON)
        repeated_summary = adjacency_payload.get("repeated_layer_summary")
        if isinstance(repeated_summary, dict):
            layer_counts = repeated_summary.get("run_layer_counts")
            steps_per_layer = repeated_summary.get("run_steps_per_layer")
            if (
                isinstance(layer_counts, list)
                and isinstance(steps_per_layer, list)
                and len(layer_counts) == len(steps_per_layer)
            ):
                parts: list[str] = []
                offset = 0
                for run_index, (raw_layers, raw_steps) in enumerate(
                    zip(layer_counts, steps_per_layer),
                    start=1,
                ):
                    layer_count = int(raw_layers)
                    step_count = int(raw_steps)
                    template = keys[offset : offset + step_count]
                    expanded = template * layer_count
                    if keys[offset : offset + len(expanded)] != expanded:
                        break
                    parts.append(
                        f"Run {run_index}: [{', '.join(template)}] x {layer_count}"
                    )
                    offset += len(expanded)
                if offset == len(keys) and parts:
                    return " | ".join(parts)
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        pass
    if len(keys) <= 12:
        return ", ".join(keys)
    return ", ".join(keys[:6]) + f", ... ({len(keys)} total) ..., " + ", ".join(keys[-6:])


def find_result_candidate_simulation_paths() -> list[Path]:
    if not OUT_CANDIDATE_SIMULATIONS_DIR.exists():
        if OUT_OPTIMAL_SIMULATION_JSON.exists():
            return [OUT_OPTIMAL_SIMULATION_JSON]
        raise FileNotFoundError(f"Final candidate simulation directory not found: {OUT_CANDIDATE_SIMULATIONS_DIR}")

    simulation_path_entries: list[tuple[int, int, Path]] = []
    for simulation_path in OUT_CANDIDATE_SIMULATIONS_DIR.glob("candidate_rank_*/candidate_rank_*_simulation.json"):
        payload = load_json(simulation_path)
        simulation_path_entries.append(
            (
                int(payload.get("candidate_rank", 10**9)),
                simulation_path,
            )
        )

    if not simulation_path_entries:
        if OUT_OPTIMAL_SIMULATION_JSON.exists():
            return [OUT_OPTIMAL_SIMULATION_JSON]
        raise FileNotFoundError(f"No final candidate simulation JSON files found under: {OUT_CANDIDATE_SIMULATIONS_DIR}")

    simulation_path_entries.sort()
    return [entry[-1] for entry in simulation_path_entries]


def save_single_result_files(
    candidate_payload: dict,
    length: list[float],
    step_spatial_metadata: list[dict[str, object]],
    output_dir: Path,
) -> tuple[list[float], list[list[int]], list[list[int]]]:
    material_name_matrix = candidate_payload.get("simulation_material_name_matrix")
    if not isinstance(material_name_matrix, list) or not material_name_matrix:
        raise ValueError("simulation_material_name_matrix was not found in candidate simulation payload.")

    matrix = convert_material_names_to_codes(material_name_matrix)
    po = build_po_from_simulation_payload(candidate_payload)
    if len(length) != len(matrix[0]):
        raise ValueError(
            f"Length/matrix column mismatch: length has {len(length)} items, matrix has {len(matrix[0])} columns."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "length.txt").write_text(f"length = {pformat(length, width=120)}\n", encoding="utf-8")
    (output_dir / "matrix.txt").write_text(f"matrix = {pformat(matrix, width=120)}\n", encoding="utf-8")
    (output_dir / "po.txt").write_text(f"po = {pformat(po, width=120)}\n", encoding="utf-8")
    (output_dir / "result.json").write_text(
        json.dumps(
            {
                "candidate_rank": candidate_payload.get("candidate_rank"),
                "original_candidate_rank": candidate_payload.get("original_candidate_rank"),
                "candidate_score": candidate_payload.get("candidate_score"),
                "candidate_eta_sum": candidate_payload.get("candidate_eta_sum"),
                "material_switch_count": candidate_payload.get("material_switch_count"),
                "step_reversed_for_simulation": candidate_payload.get("step_reversed_for_simulation"),
                "material_codes": MATERIAL_CODES,
                "length": length,
                "step_spatial_metadata": step_spatial_metadata,
                "matrix": matrix,
                "po": po,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return length, matrix, po


def save_result_files() -> None:
    # Final compact outputs requested by the user.
    # These are based on the actual final candidate simulations under out/simulation/candidate_simulations.
    length_payload = load_json(OUT_LENGTH_MATRIX_JSON)
    length = length_payload.get("length_matrix")
    step_spatial_metadata = build_step_spatial_metadata_from_length_payload(length_payload)
    if not isinstance(length, list):
        raise ValueError(f"length_matrix was not found in {OUT_LENGTH_MATRIX_JSON}")

    simulation_paths = find_result_candidate_simulation_paths()
    remove_directory_if_exists(OUT_RESULT_DIR)
    OUT_RESULT_DIR.mkdir(parents=True, exist_ok=True)

    primary_length: list[float] | None = None
    primary_matrix: list[list[int]] | None = None
    primary_po: list[list[int]] | None = None
    result_entries: list[dict[str, object]] = []
    for simulation_path in simulation_paths:
        candidate_payload = load_json(simulation_path)
        candidate_length = length
        candidate_rank = int(candidate_payload.get("candidate_rank", len(result_entries) + 1))
        candidate_name = simulation_path.parent.name
        if not candidate_name.startswith("candidate_rank_"):
            candidate_name = f"candidate_rank_{candidate_rank:04d}"
        saved_length, saved_matrix, saved_po = save_single_result_files(
            candidate_payload,
            candidate_length,
            step_spatial_metadata,
            OUT_RESULT_DIR / candidate_name,
        )
        result_entries.append(
            {
                "candidate_name": candidate_name,
                "candidate_rank": candidate_payload.get("candidate_rank"),
                "original_candidate_rank": candidate_payload.get("original_candidate_rank"),
                "candidate_score": candidate_payload.get("candidate_score"),
                "candidate_eta_sum": candidate_payload.get("candidate_eta_sum"),
                "material_switch_count": candidate_payload.get("material_switch_count"),
                "result_dir": str(OUT_RESULT_DIR / candidate_name),
            }
        )
        if primary_length is None or primary_matrix is None or primary_po is None:
            primary_length = saved_length
            primary_matrix = saved_matrix
            primary_po = saved_po

    if primary_length is None or primary_matrix is None or primary_po is None:
        raise ValueError("No result candidate could be saved.")

    OUT_RESULT_LENGTH_TXT.write_text(f"length = {pformat(primary_length, width=120)}\n", encoding="utf-8")
    OUT_RESULT_MATRIX_TXT.write_text(f"matrix = {pformat(primary_matrix, width=120)}\n", encoding="utf-8")
    OUT_RESULT_PO_TXT.write_text(f"po = {pformat(primary_po, width=120)}\n", encoding="utf-8")
    OUT_RESULT_JSON.write_text(
        json.dumps(
            {
                "source": "out/simulation/candidate_simulations",
                "primary_candidate": result_entries[0],
                "candidate_results": result_entries,
                "material_codes": MATERIAL_CODES,
                "length": primary_length,
                "step_spatial_metadata": step_spatial_metadata,
                "matrix": primary_matrix,
                "po": primary_po,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def find_result_json_paths() -> list[Path]:
    candidate_result_paths = sorted(OUT_RESULT_DIR.glob("candidate_rank_*/result.json"))
    if candidate_result_paths:
        return candidate_result_paths
    if OUT_RESULT_JSON.exists():
        return [OUT_RESULT_JSON]
    return []


def run_source_dm_filament_from_results() -> None:
    if not RUN_SOURCE_DM_FILAMENT:
        print("")
        print("Source_DM_filament step skipped.")
        print(f"  Set {RUN_SOURCE_DM_FILAMENT_ENV_KEY}=1 to run MATLAB main.m from out/result.")
        return

    if not SOURCE_DM_PYTHON_RUNNER.exists():
        raise FileNotFoundError(f"Source_DM_filament Python runner not found: {SOURCE_DM_PYTHON_RUNNER}")

    result_json_paths = find_result_json_paths()
    if not result_json_paths:
        raise FileNotFoundError(f"No result.json files found under {OUT_RESULT_DIR}")

    print("")
    print("[START] Run Source_DM_filament main.m from result folders")
    print(f"  runner: {SOURCE_DM_PYTHON_RUNNER}")

    runner_globals = runpy.run_path(str(SOURCE_DM_PYTHON_RUNNER))
    run_from_result_folder = runner_globals["run_from_result_folder"]
    for result_json_path in result_json_paths:
        output_dir = result_json_path.parent / "source_dm_filament"
        start = time.perf_counter()
        run_from_result_folder(
            result_json_path.parent,
            source_dir=SOURCE_DM_FILAMENT_DIR,
            output_dir=output_dir,
            matlab_command=SOURCE_DM_MATLAB_COMMAND,
            run_main=True,
        )
        elapsed = time.perf_counter() - start
        print(f"[DONE]  {result_json_path.parent.name} Source_DM_filament ({elapsed:.2f}s)")
        print(f"  output: {output_dir}")


def save_optimal_ratio_eta_plot(
    summary: dict[str, object],
    material_dictionary: dict[str, dict[str, object]],
    property_program: dict,
) -> None:
    # Build a step-wise summary where each step contains:
    # - the two active materials for that assignment step
    # - the material ratios from the selected case
    # - the selected case eta value
    selected_case_keys = [str(item) for item in summary["selected_case_keys"]]
    step_material_pairs = build_assignment_step_material_pairs(property_program)
    length_payload = load_json(OUT_LENGTH_MATRIX_JSON)
    step_spatial_metadata = build_step_spatial_metadata_from_length_payload(length_payload)
    if len(selected_case_keys) != len(step_material_pairs):
        raise ValueError("Selected case count does not match the property step count.")
    if len(selected_case_keys) != len(step_spatial_metadata):
        raise ValueError("Selected case count does not match the step spatial metadata count.")

    step_entries: list[dict[str, object]] = []
    active_materials: list[str] = []
    for step_index, case_key in enumerate(selected_case_keys):
        case_info = material_dictionary[case_key]
        start_material, end_material = step_material_pairs[step_index]
        case_rows = [str(item) for item in case_info.get("case_rows", [])]
        materialized_rows = materialize_case_rows(case_rows, start_material, end_material)
        if len(materialized_rows) != len(ROW_WEIGHTS):
            raise ValueError(f"Expected {len(ROW_WEIGHTS)} material rows for {case_key}, got {len(materialized_rows)}.")
        material_counts: dict[str, float] = {}
        for material_name, weight in zip(materialized_rows, ROW_WEIGHTS):
            material_counts[material_name] = material_counts.get(material_name, 0.0) + float(weight)
        total_weight = sum(material_counts.values()) or 1.0
        eta_value = float(case_info["eta"])
        ratio_map = {
            material_name: material_count / total_weight
            for material_name, material_count in material_counts.items()
        }
        for material_name in ratio_map:
            if material_name not in active_materials:
                active_materials.append(material_name)
        step_entries.append(
            {
                "step_index": step_index + 1,
                "assignment_index": step_spatial_metadata[step_index]["assignment_index"],
                "assignment_step_index": step_spatial_metadata[step_index]["assignment_step_index"],
                "start_voxel_index": step_spatial_metadata[step_index]["start_voxel_index"],
                "end_voxel_index": step_spatial_metadata[step_index]["end_voxel_index"],
                "start_layer": step_spatial_metadata[step_index]["start_layer"],
                "end_layer": step_spatial_metadata[step_index]["end_layer"],
                "case_key": case_key,
                "start_material": start_material,
                "end_material": end_material,
                "material_ratios": ratio_map,
                "eta": eta_value,
            }
        )

    x = np.arange(1, len(step_entries) + 1, dtype=float)
    eta_values = np.array([float(item["eta"]) for item in step_entries], dtype=float)
    ratio_series = {
        material_name: np.array(
            [float(item["material_ratios"].get(material_name, 0.0)) for item in step_entries],
            dtype=float,
        )
        for material_name in active_materials
    }

    # Create a bottom color strip by blending the active step materials
    # according to the selected case ratios.
    blend_rgb = np.zeros((1, len(step_entries), 3), dtype=float)
    for col_index, item in enumerate(step_entries):
        ratios = item["material_ratios"]
        total_ratio = sum(float(value) for value in ratios.values()) or 1.0
        rgb = np.zeros(3, dtype=float)
        for material_name, ratio in ratios.items():
            color_rgb = np.array(hex_to_rgb01(MATERIAL_COLORS.get(material_name, MATERIAL_COLORS["Other"])))
            rgb += color_rgb * (float(ratio) / total_ratio)
        blend_rgb[0, col_index, :] = rgb

    fig = plt.figure(figsize=(11, 4.8))
    grid = fig.add_gridspec(2, 1, height_ratios=[6, 0.8], hspace=0.05)
    ax_ratio = fig.add_subplot(grid[0, 0])
    ax_band = fig.add_subplot(grid[1, 0], sharex=ax_ratio)
    ax_eta = ax_ratio.twinx()

    single_step_marker_offsets = {}
    if len(step_entries) == 1 and len(active_materials) > 1:
        center = (len(active_materials) - 1) / 2
        single_step_marker_offsets = {
            material_name: (index - center) * 0.08
            for index, material_name in enumerate(active_materials)
        }

    for material_name in active_materials:
        color = ratio_plot_color(material_name)
        if len(step_entries) == 1:
            x_center = float(x[0]) + single_step_marker_offsets.get(material_name, 0.0)
            ax_ratio.plot(
                [x_center - 0.06, x_center + 0.06],
                [float(ratio_series[material_name][0]), float(ratio_series[material_name][0])],
                linestyle="-",
                linewidth=1.8,
                color=color,
                label=material_name,
            )
        else:
            ax_ratio.step(
                x,
                ratio_series[material_name],
                where="mid",
                linewidth=1.8,
                color=color,
                label=material_name,
            )

    if len(step_entries) == 1:
        ax_eta.plot(
            [float(x[0]) - 0.06, float(x[0]) + 0.06],
            [float(eta_values[0]), float(eta_values[0])],
            linewidth=1.6,
            color="#ef4444",
            label="eta",
        )
    else:
        ax_eta.step(
            x,
            eta_values,
            where="mid",
            linewidth=1.6,
            color="#ef4444",
            label="eta",
        )

    ax_ratio.set_xlim(0.5, len(step_entries) + 0.5)
    ax_ratio.set_ylim(-0.05, 1.05)
    ax_ratio.set_ylabel("phi")
    ax_eta.set_ylabel("eta", color="#ef4444")
    ax_eta.tick_params(axis="y", colors="#ef4444")
    ax_ratio.grid(axis="y", color="#d1d5db", linewidth=0.8, alpha=0.8)
    ax_ratio.set_title("Optimal Candidate Material Ratios and Eta")

    legend_handles = [
        plt.Line2D([0], [0], color=ratio_plot_color(name), linewidth=1.8)
        for name in active_materials
    ]
    ax_ratio.legend(
        legend_handles,
        active_materials,
        title="Base materials",
        loc="upper left",
        ncol=min(4, max(1, len(active_materials))),
        frameon=True,
    )

    ax_band.imshow(
        blend_rgb,
        aspect="auto",
        extent=(0.5, len(step_entries) + 0.5, 0.0, 1.0),
        origin="lower",
    )
    ax_band.set_yticks([])
    ax_band.set_ylabel("")
    ax_band.set_xlabel("E")
    ax_band.set_xticks(x)
    ax_band.set_xticklabels([str(int(value)) for value in x], fontsize=8)
    for boundary in np.arange(0.5, len(step_entries) + 1.5, 1.0):
        ax_band.axvline(boundary, color="#111827", linewidth=0.8)

    plt.setp(ax_ratio.get_xticklabels(), visible=False)
    fig.tight_layout()
    fig.savefig(OUT_OPTIMAL_RATIO_ETA_PNG, dpi=220, bbox_inches="tight")
    plt.close(fig)

    OUT_OPTIMAL_RATIO_ETA_JSON.write_text(
        json.dumps(
            {
                "active_materials": active_materials,
                "steps": step_entries,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def print_workflow_overview() -> None:
    # This overview is intentionally verbose.
    # The user asked for the intermediate process to be explained,
    # so we print the pipeline meaning before execution starts.
    print("Workflow overview")
    print("-----------------")
    print("1. Reuse the existing material dictionary.")
    print("   - Input:  selected material dictionary path")
    print("   - Reason: the material dictionary is already prepared, so we do not rebuild it.")
    print("")
    print("2. Build the assignment length matrix.")
    print("   - Inputs: sample_info.json + selected property JSON")
    print("   - Output: length_matrix.json / length_matrix.npy")
    print("   - Meaning: converts the geometric or assignment-related source information")
    print("              into a matrix form that later stages can consume.")
    print("")
    print("3. Build the assignment candidate matrix.")
    print("   - Inputs: selected property JSON + selected material dictionary")
    print("   - Internal output: test_sample/derived/matrices/assignment_candidate_matrix.json / .txt")
    print("   - Exported output: out/matrices/assignment_candidate_matrix.json / .txt")
    print("   - Meaning: lists valid material case candidates for every step.")
    print("")
    print(f"4. Search step adjacency candidates with {ADJACENCY_SEARCH_ALGORITHM}.")
    print("   - Input: assignment_candidate_matrix.txt")
    print("   - Internal output: test_sample/derived/adjacency/beam_step_adjacency.json / .txt")
    print("   - Exported output: out/adjacency/beam_step_adjacency.json / .txt")
    if ADJACENCY_SEARCH_ALGORITHM == "beam":
        print("   - Meaning: expands candidates step by step and keeps best-score beam states.")
        print(f"   - Beam best-per-step limit: {BEAM_BEST_PER_STEP_LIMIT} (0 means unlimited)")
    elif ADJACENCY_SEARCH_ALGORITHM in {"astar", "dijkstra", "bfs", "dfs"}:
        print("   - Meaning: treats candidate selection as a layered path-search problem.")
        print("   - Path search limits: B_FDM_PATH_SEARCH_MAX_EXPANSIONS / B_FDM_PATH_SEARCH_MAX_RESULTS")
    else:
        print("   - Meaning: evolves full candidate sequences and keeps the best adjacency-score candidates.")
    print(f"   - Select with: {ADJACENCY_SEARCH_ALGORITHM_ENV_KEY}=beam, ga, astar, dijkstra, bfs, or dfs")
    print("")
    print("5. Evaluate and keep the best adjacency clusters.")
    print("   - Input: beam_step_adjacency.txt")
    print("   - Internal output: test_sample/derived/adjacency/beam_step_adjacency_clusters_best.json / .txt / .png")
    print("   - Exported output: out/adjacency/beam_step_adjacency_clusters_best.json / .txt / .png")
    print("   - Meaning: keeps every best adjacency-score candidate for switch optimization.")
    print("")
    print("6. Compute material switch counts with the lightweight simulation logic.")
    print("   - Inputs: beam_step_adjacency_clusters_best.txt + selected material dictionary")
    print("   - Internal output: test_sample/derived/simulation/beam_step_adjacency_clusters_best_material_switches.json / .txt")
    print("   - Exported output: out/simulation/intermediate/beam_step_adjacency_clusters_best_material_switches.json / .txt")
    print("   - Meaning: estimates the number of material switches for all shortlisted candidates")
    print("              without generating the full visual deposition simulation for each one.")
    print("")
    print("7. Run Score Final ranking.")
    print("   - Inputs: beam_step_adjacency_clusters_best.txt + selected material dictionary")
    print("   - Internal output: test_sample/derived/simulation/beam_step_adjacency_clusters_best_switch_eta_ranked.json / .txt")
    print("   - Exported output: out/simulation/intermediate/beam_step_adjacency_clusters_best_switch_eta_ranked.json / .txt")
    print(f"   - Final result folders: out/simulation/candidate_simulations, top {RESULT_COUNT} candidate(s)")
    print("   - Meaning: treats the shortlisted local candidates as the global candidate pool,")
    print("              then ranks them by material switch count first and saves each selected candidate with")
    print("              simulation + ratio_eta_plot.")
    print("   - Eta target rule: candidate generation first applies")
    print("                 eta_min <= candidate eta <= min(assignment eta, eta_max if set).")
    print("                 Final ranking then checks each gradient assignment using the")
    print("                 maximum eta among its steps as the representative eta.")
    print("")
    print("8. Choose the optimal beam-step adjacency candidate.")
    print("   - Rule: use Score Final priority:")
    print("           gradient eta target error asc, material switch count asc, score desc, rank asc.")
    print("   - Exported output: out/simulation/intermediate/optimal_beam_candidate.json / .txt")
    print("")
    print("9. Save compact final result files.")
    print("   - Output: out/result/length.txt")
    print("   - Output: out/result/matrix.txt")
    print("   - Output: out/result/po.txt")
    print("   - Output: out/result/result.json")
    print("")
    print("11. Optionally run Source_DM_filament/main.m from result files.")
    print(f"   - Enabled by: {RUN_SOURCE_DM_FILAMENT_ENV_KEY}=1")
    print("   - Input: out/result/candidate_rank_*/result.json")
    print("   - Output: out/result/candidate_rank_*/source_dm_filament")
    print("")
    print("Optional note")
    print("-------------")
    print("A detailed visual deposition simulation for one chosen candidate is still separate.")
    print("That path starts from a candidate-specific matrix file and is handled by:")
    print("  scripts/simulation/simulate_matrix_deposition.py")


def main() -> None:
    global EFFECTIVE_PROPERTY_PATH
    # Step 0: check the minimum required inputs before doing any work.
    ensure_required_inputs()
    ensure_out_directories()
    EFFECTIVE_PROPERTY_PATH = PROPERTY_PATH
    resolved_property_path, property_guided_summary = resolve_property_guided_program_to_path(
        PROPERTY_PATH,
        DEFAULT_RESOLVED_OUTPUT_PATH,
        PROPERTY_GUIDED_SUMMARY_PATH,
    )
    if property_guided_summary.get("resolved_assignments"):
        EFFECTIVE_PROPERTY_PATH = resolved_property_path
    region_recognition_mode = resolve_region_recognition_mode(
        EFFECTIVE_PROPERTY_PATH
    )
    if region_recognition_mode == "z-axis":
        layer_region_summary = {
            "expanded_event_count": 0,
            "reason": (
                "z-axis mode uses the component-level property program directly, "
                "matching the b-FDM_main2 workflow."
            ),
        }
    else:
        expanded_property_path, layer_region_summary = (
            expand_layer_region_program_to_path(
                EFFECTIVE_PROPERTY_PATH,
                LAYER_REGION_EXPANDED_OUTPUT_PATH,
            )
        )
        if int(layer_region_summary.get("expanded_event_count", 0)) > 0:
            EFFECTIVE_PROPERTY_PATH = expanded_property_path

    # Show the flow first so this file also works as a readable project map.
    print_workflow_overview()

    print("")
    print("Selected property program:")
    print(f"  {PROPERTY_PATH}")
    if EFFECTIVE_PROPERTY_PATH != PROPERTY_PATH:
        print("Effective property program:")
        print(f"  {EFFECTIVE_PROPERTY_PATH}")
    if property_guided_summary.get("resolved_assignments"):
        print("Property-guided resolved assignments:")
        print(f"  {len(property_guided_summary.get('resolved_assignments', []))}")
    print("Region recognition mode:")
    print(f"  {region_recognition_mode}")
    if region_recognition_mode == "z-axis":
        print("Z-axis component behavior:")
        print("  Using the component-level Property/Gradient assignments directly.")
    if int(layer_region_summary.get("expanded_event_count", 0)) > 0:
        print("Layer-region execution events:")
        print(f"  {layer_region_summary['expanded_event_count']}")
        print("Layer-region mapped deposition E:")
        print(f"  {layer_region_summary['total_region_deposition_e_mm']:.6f} mm")
    print("Selected sample info:")
    print(f"  {SAMPLE_INFO_PATH}")
    print("Reused existing material dictionary:")
    print(f"  {MATERIAL_DICTIONARY_PATH}")
    print("Beam best-score states kept per step:")
    print(f"  {BEAM_BEST_PER_STEP_LIMIT} (0 means unlimited)")
    print("Adjacency search algorithm:")
    print(f"  {ADJACENCY_SEARCH_ALGORITHM} ({ADJACENCY_SEARCH_ALGORITHM_ENV_KEY}=beam, ga, astar, dijkstra, bfs, or dfs)")
    print("Final candidate result folders to save:")
    print(f"  {RESULT_COUNT}")
    print("Global eta filter for candidate generation:")
    print(f"  min: {ETA_MIN if ETA_MIN is not None else '(none)'}")
    print(f"  max: {ETA_MAX if ETA_MAX is not None else '(assignment eta)'}")
    print("Run Source_DM_filament from result files:")
    print(f"  {RUN_SOURCE_DM_FILAMENT} ({RUN_SOURCE_DM_FILAMENT_ENV_KEY}=1 to enable)")

    stages: list[tuple[str, str, list[Path]]] = [
        (
            "scripts/build/assignment_length_matrix.py",
            "Build Assignment Length Matrix",
            [
                INTERNAL_LENGTH_MATRIX_JSON,
                PROJECT_ROOT / "test_sample" / "derived" / "matrices" / "length_matrix.npy",
            ],
        ),
        (
            "scripts/build/build_assignment_candidate_matrix.py",
            "Build Assignment Candidate Matrix",
            [
                PROJECT_ROOT / "test_sample" / "derived" / "matrices" / "assignment_candidate_matrix.json",
                INTERNAL_ASSIGNMENT_MATRIX_TXT,
            ],
        ),
        build_adjacency_search_stage(),
        (
            "scripts/analysis/evaluate_beam_step_adjacency_clusters.py",
            "Evaluate Best Beam Adjacency Clusters",
            [
                PROJECT_ROOT / "test_sample" / "derived" / "adjacency" / "beam_step_adjacency_clusters_best.json",
                INTERNAL_BEST_CLUSTER_TXT,
            ],
        ),
        (
            "scripts/analysis/calculate_material_switches_for_best_candidates.py",
            "Calculate Material Switch Counts For Best Candidates",
            [
                PROJECT_ROOT
                / "test_sample"
                / "derived"
                / "simulation"
                / "beam_step_adjacency_clusters_best_material_switches.json",
                INTERNAL_MATERIAL_SWITCH_REPORT_TXT,
            ],
        ),
        (
            "scripts/analysis/score_final.py",
            "Run Score Final Ranking",
            [
                INTERNAL_SCORE_FINAL_JSON,
                INTERNAL_SCORE_FINAL_TXT,
                INTERNAL_CANDIDATE_SIMULATIONS_DIR,
            ],
        ),
    ]

    for script_path, stage_name, expected_outputs in stages:
        run_script(script_path, stage_name, expected_outputs)

    # Export the important final and intermediate artifacts into out/
    # so the user can inspect one clean result directory.
    export_outputs_to_out()
    final_ranking_summary = build_final_ranking_terminal_summary()
    print_final_ranking_terminal_summary(final_ranking_summary)
    local_global_analysis = build_local_global_analysis()
    save_local_global_analysis(local_global_analysis)
    print_local_global_analysis_summary(local_global_analysis)
    if int(final_ranking_summary["ranked_candidate_count"]) <= 0:
        print("")
        print("Workflow stopped after ranking.")
        print("Reason:")
        print("  No candidates satisfied the final ranking constraints.")
        print("Check:")
        print(f"  score final ranking   : {OUT_SCORE_FINAL_TXT}")
        print(f"  intermediate files    : {OUT_SIMULATION_INTERMEDIATE_DIR}")
        return
    if int(final_ranking_summary["saved_result_count"]) <= 0:
        print("")
        print("Workflow stopped after ranking.")
        print("Reason:")
        print("  Ranked candidates exist, but none produced a valid simulation export.")
        print("Check:")
        print(f"  score final ranking   : {OUT_SCORE_FINAL_TXT}")
        print(f"  intermediate files    : {OUT_SIMULATION_INTERMEDIATE_DIR}")
        return

    # Read the material switch report and extract the single best candidate
    # according to the minimum material switch count rule.
    optimal_summary = build_optimal_candidate_summary()
    save_optimal_candidate_summary(optimal_summary)

    # Convert the selected optimal candidate back into a matrix so the
    # final simulation is based on the chosen minimum-switch solution.
    material_dictionary = load_json(MATERIAL_DICTIONARY_PATH)
    property_program = load_json(EFFECTIVE_PROPERTY_PATH)
    optimal_matrix = save_optimal_candidate_matrix(optimal_summary, material_dictionary, property_program)
    optimal_material_name_payload = load_json(OUT_OPTIMAL_MATERIAL_NAME_MATRIX_JSON)
    optimal_material_name_matrix = optimal_material_name_payload["material_name_matrix"]
    save_optimal_candidate_simulation(optimal_summary, optimal_matrix, optimal_material_name_matrix)
    save_result_files()
    run_source_dm_filament_from_results()
    save_optimal_ratio_eta_plot(optimal_summary, material_dictionary, property_program)

    # Final summary:
    # Print the most important files so the user knows where to look next.
    print("")
    print("Workflow completed successfully.")
    print("Main checkpoints:")
    print(f"  assignment candidates : {OUT_ASSIGNMENT_MATRIX_TXT}")
    print(f"  adjacency candidates  : {OUT_BEAM_ADJACENCY_TXT}")
    print(f"  best candidate groups : {OUT_BEST_CLUSTER_TXT}")
    print(f"  switch count report   : {OUT_MATERIAL_SWITCH_TXT}")
    print(f"  score final ranking   : {OUT_SCORE_FINAL_TXT}")
    print(f"  local/global analysis : {OUT_LOCAL_GLOBAL_ANALYSIS_TXT}")
    print(f"  final candidates      : {OUT_CANDIDATE_SIMULATIONS_DIR}")
    print(f"  intermediate files    : {OUT_SIMULATION_INTERMEDIATE_DIR}")
    print(f"  optimal candidate     : {OUT_OPTIMAL_CANDIDATE_TXT}")
    print(f"  optimal ratio-eta plot: {OUT_OPTIMAL_RATIO_ETA_PNG}")
    print(f"  result length         : {OUT_RESULT_LENGTH_TXT}")
    print(f"  result matrix         : {OUT_RESULT_MATRIX_TXT}")
    print(f"  result po             : {OUT_RESULT_PO_TXT}")
    print(f"  result json           : {OUT_RESULT_JSON}")
    if RUN_SOURCE_DM_FILAMENT:
        print(f"  source dm outputs     : {OUT_RESULT_DIR}\\candidate_rank_*\\source_dm_filament")

    optimal_summary = build_optimal_candidate_summary()
    print("")
    print("Optimal candidate summary:")
    print(f"  rank                  : {optimal_summary['rank']}")
    print(f"  score                 : {optimal_summary['score']}")
    print(f"  material switches     : {optimal_summary['min_material_switch_count']}")
    print(f"  eta_sum               : {optimal_summary.get('eta_sum', 0.0):.6f}")
    print(
        "  selected_case_keys    : "
        f"{format_selected_case_keys_for_terminal(optimal_summary['selected_case_keys'])}"
    )


if __name__ == "__main__":
    main()
