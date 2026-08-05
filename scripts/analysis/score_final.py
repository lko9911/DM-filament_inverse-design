from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections import Counter
from itertools import product
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
from tqdm import tqdm

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.simulation.simulate_matrix_deposition import (
    build_payload,
    format_payload,
    save_final_stack_image,
    save_stacking_animation,
)
from scripts.utils.property_program_utils import (
    get_assignment_property_type,
    get_assignments_in_spatial_order,
    get_effective_gradient_steps,
    resolve_property_program_path,
    resolve_assignment_material_pair,
)


ROW_WEIGHTS = [2, 3, 3, 4, 4, 4, 4, 4, 4, 4, 4, 3, 3, 2]

SOURCE_CANDIDATES_PATH = Path("test_sample/derived/adjacency/beam_step_adjacency_clusters_best.txt")
ADJACENCY_JSON_PATH = Path("test_sample/derived/adjacency/beam_step_adjacency.json")
ADJACENCY_TXT_PATH = Path("test_sample/derived/adjacency/beam_step_adjacency.txt")
SOURCE_MATRIX_TEXT_PATH = Path("test_sample/derived/matrices/assignment_candidate_matrix.txt")
MATERIAL_DICTIONARY_PATH = Path(
    os.environ.get("B_FDM_MATERIAL_DICTIONARY_PATH", "input/config/material_dictionary.json")
)
PROPERTY_PROGRAM_PATH = resolve_property_program_path()
LENGTH_MATRIX_PATH = Path("test_sample/derived/matrices/length_matrix.json")

OUTPUT_JSON_PATH = Path("test_sample/derived/simulation/beam_step_adjacency_clusters_best_switch_eta_ranked.json")
OUTPUT_TXT_PATH = Path("test_sample/derived/simulation/beam_step_adjacency_clusters_best_switch_eta_ranked.txt")

SIMULATION_OUTPUT_DIR = Path("test_sample/derived/simulation/candidate_simulations")
RESULT_COUNT_ENV_KEY = "B_FDM_RESULT_COUNT"
DEFAULT_RESULT_COUNT = 1
SAVE_SIMULATION_GIF_ENV_KEY = "B_FDM_SAVE_SIMULATION_GIF"
AUTO_GIF_MAX_STEP_COUNT = 120
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
    "Other": "#9ca3af",
}


def ratio_plot_color(material_name: str) -> str:
    if str(material_name).upper() == "WHITE":
        return "#94a3b8"
    return MATERIAL_COLORS.get(material_name, MATERIAL_COLORS["Other"])


def parse_non_negative_int_env(env_key: str, default: int) -> int:
    raw_value = os.environ.get(env_key)
    if raw_value is None or not raw_value.strip():
        return default
    value = int(raw_value)
    if value < 0:
        raise ValueError(f"{env_key} must be >= 0, got {value}")
    return value


SAVE_TOP_N_SIMULATIONS = parse_non_negative_int_env(RESULT_COUNT_ENV_KEY, DEFAULT_RESULT_COUNT)


def should_save_simulation_gif(step_count: int) -> bool:
    mode = os.environ.get(SAVE_SIMULATION_GIF_ENV_KEY, "auto").strip().lower()
    if mode in {"1", "true", "yes", "on"}:
        return True
    if mode in {"0", "false", "no", "off"}:
        return False
    return int(step_count) <= AUTO_GIF_MAX_STEP_COUNT


def build_compact_repeated_pattern_view(
    matrix: list[list[int]],
    material_name_matrix: list[list[str]] | None,
    property_program: dict[str, Any],
    repeated_layer_summary: dict[str, Any] | None,
) -> tuple[list[list[int]], list[list[str]], list[str], list[str]] | None:
    if material_name_matrix is None or not isinstance(repeated_layer_summary, dict):
        return None
    run_layer_counts = repeated_layer_summary.get("run_layer_counts")
    run_steps_per_layer = repeated_layer_summary.get("run_steps_per_layer")
    if not isinstance(run_layer_counts, list) or not isinstance(run_steps_per_layer, list):
        return None
    if len(run_layer_counts) != len(run_steps_per_layer) or not run_layer_counts:
        return None

    assignments = get_assignments_in_spatial_order(property_program)
    if len(assignments) != len(matrix[0]):
        return None

    compact_matrix = [[] for _ in matrix]
    compact_materials = [[] for _ in material_name_matrix]
    labels: list[str] = []
    summary_lines = [
        f"compact repeated-pattern view of {len(matrix[0])} full steps",
        f"pattern runs: {len(run_layer_counts)}",
    ]
    source_offset = 0
    for run_index, (raw_layer_count, raw_steps_per_layer) in enumerate(
        zip(run_layer_counts, run_steps_per_layer),
        start=1,
    ):
        layer_count = int(raw_layer_count)
        steps_per_layer = int(raw_steps_per_layer)
        if layer_count <= 0 or steps_per_layer <= 0:
            return None
        if source_offset + steps_per_layer > len(matrix[0]):
            return None

        region_names: list[str] = []
        for local_index in range(steps_per_layer):
            source_col = source_offset + local_index
            assignment = assignments[source_col]
            event = assignment.get("layer_region_event")
            region_name = (
                str(event.get("region_name", f"Step {local_index + 1}"))
                if isinstance(event, dict)
                else f"Step {local_index + 1}"
            )
            region_names.append(region_name)
            labels.append(f"Run {run_index}\n{region_name}")
            for row_index in range(len(matrix)):
                compact_matrix[row_index].append(matrix[row_index][source_col])
                compact_materials[row_index].append(
                    material_name_matrix[row_index][source_col]
                )

        labels.append(f"... x{layer_count}")
        for row_index in range(len(matrix)):
            compact_matrix[row_index].append(0)
            compact_materials[row_index].append("REPEAT")
        summary_lines.append(
            f"Run {run_index}: {' -> '.join(region_names)} x {layer_count} layers"
        )
        source_offset += layer_count * steps_per_layer

    if source_offset != len(matrix[0]):
        return None
    return compact_matrix, compact_materials, labels, summary_lines


BEST_RE_ORIGINAL = re.compile(
    r"^(?P<rank>\d+)\.\s+score\s+(?P<score>\d+)"
    r"(?:\s+\|\s+eta_sum\s+(?P<eta_sum>[0-9.]+))?"
    r"(?:\s+\|\s+material_switch_count\s+(?P<material_switch_count>\d+))?"
    r"\s+\|\s+step_scores\s+\[(?P<step_scores>[^\]]*)\]\s+\|\s+selected_case_keys\s+(?P<keys>.*)\s*$"
)

BEST_RE_SIMPLIFIED = re.compile(
    r"^(?P<rank>\d+)\.\s+score=(?P<score>\d+)\s+material_switch_count=(?P<material_switch_count>\d+)\s+selected_case_keys=(?P<keys>.*)\s*$"
)
STEP_RE = re.compile(
    r"^step_(?P<step>\d+)\s+\|\s+assignment\s+(?P<assignment>\d+)\s+\|\s+local_step\s+(?P<local_step>\d+)"
    r"(?:\s+\|\s+materials\s+(?P<start>[A-Za-z0-9_]+)->(?P<end>[A-Za-z0-9_]+))?"
    r"\s+\|\s+target\s+(?P<start_count>\d+)/(?P<end_count>\d+)"
    r"\s+\((?P<start_ratio>[0-9.]+)/(?P<end_ratio>[0-9.]+)\)"
    r"(?:\s+\|\s+ratio_tol<=\s+(?P<ratio_tolerance>[0-9.]+))?"
    r"(?:\s+\|\s+eta>=\s+(?P<eta_min>[0-9.]+|None))?"
    r"\s+\|\s+eta<=\s+(?P<eta>[0-9.]+|None)"
    r"\s+\|.*candidate_count\s+(?P<count>\d+)\s*$"
)
CANDIDATES_RE = re.compile(r"^\s*candidates:\s*(?P<candidates>.*)\s*$")


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_text_lines(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Text file not found: {path}")
    return path.read_text(encoding="utf-8-sig").splitlines()


def build_step_spatial_metadata_from_length_payload(length_payload: dict[str, Any]) -> list[dict[str, Any]]:
    step_metadata: list[dict[str, Any]] = []
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


def parse_best_candidates(lines: list[str]) -> list[dict[str, Any]]:
    in_best_candidates = False
    candidates: list[dict[str, Any]] = []

    for line in lines:
        stripped = line.strip()

        if stripped == "best_candidates:":
            in_best_candidates = True
            continue

        if not in_best_candidates or not stripped:
            continue

        match_original = BEST_RE_ORIGINAL.match(stripped)
        if match_original:
            step_scores_raw = match_original.group("step_scores").strip()
            step_scores = (
                [int(item.strip()) for item in step_scores_raw.split(",") if item.strip()]
                if step_scores_raw
                else []
            )
            selected_case_keys = [item.strip() for item in match_original.group("keys").split(",") if item.strip()]
            candidates.append(
                {
                    "rank": int(match_original.group("rank")),
                    "score": int(match_original.group("score")),
                    "step_scores": step_scores,
                    "selected_case_keys": selected_case_keys,
                }
            )
            continue

        match_simplified = BEST_RE_SIMPLIFIED.match(stripped)
        if match_simplified:
            selected_case_keys = [item.strip() for item in match_simplified.group("keys").split(",") if item.strip()]
            candidates.append(
                {
                    "rank": int(match_simplified.group("rank")),
                    "score": int(match_simplified.group("score")),
                    "step_scores": [],
                    "selected_case_keys": selected_case_keys,
                }
            )
            continue

    if not candidates:
        raise ValueError(f"No best candidates found in {SOURCE_CANDIDATES_PATH}")

    return candidates


def parse_steps_from_adjacency_text(lines: list[str]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None

    for line in lines:
        if not line.strip():
            continue

        step_match = STEP_RE.match(line)
        if step_match:
            pending = {
                "step_index": int(step_match.group("step")),
                "assignment_index": int(step_match.group("assignment")),
                "local_step_index": int(step_match.group("local_step")),
                "material_start": str(step_match.group("start") or ""),
                "material_end": str(step_match.group("end") or ""),
                "candidate_count": int(step_match.group("count")),
                "candidate_keys": [],
            }
            steps.append(pending)
            continue

        if pending is None:
            continue

        candidates_match = CANDIDATES_RE.match(line)
        if candidates_match:
            raw = candidates_match.group("candidates").strip()
            pending["candidate_keys"] = [item.strip() for item in raw.split(",") if item.strip()] if raw else []

    if not steps:
        raise ValueError(f"No parsed step blocks found in {ADJACENCY_TXT_PATH}")

    return steps


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


def same_row_score(prev_rows: list[str], curr_rows: list[str]) -> int:
    if len(prev_rows) != len(curr_rows):
        raise ValueError("Row length mismatch between steps.")
    return sum(1 for prev, curr in zip(prev_rows, curr_rows) if prev == curr)


def compute_step_score_with_skip_connection(
    selected_rows_per_step: list[list[str]],
    candidate_rows: list[str],
) -> int:
    prev_rows = selected_rows_per_step[-1]
    adjacent_score = same_row_score(prev_rows, candidate_rows)

    skip_score = 0
    if len(selected_rows_per_step) >= 2:
        two_steps_back_rows = selected_rows_per_step[-2]
        skip_score = same_row_score(two_steps_back_rows, candidate_rows)

    return adjacent_score + skip_score


def expand_local_global_candidate_pool(
    adjacency_payload: dict[str, Any],
    step_source_lines: list[str],
) -> list[dict[str, Any]]:
    steps = parse_steps_from_adjacency_text(step_source_lines)
    local_preselection = adjacency_payload.get("local_gradient_preselection", [])
    if not local_preselection:
        return parse_best_candidates(load_text_lines(SOURCE_CANDIDATES_PATH))

    preselection_by_assignment = {
        int(item["assignment_index"]): item for item in local_preselection
    }
    positions_by_assignment: dict[int, list[int]] = {}
    for step_index, step in enumerate(steps):
        positions_by_assignment.setdefault(int(step["assignment_index"]), []).append(step_index)

    fixed_case_keys: list[str | None] = [None] * len(steps)
    varying_assignments: list[dict[str, Any]] = []

    for assignment_index, item in sorted(preselection_by_assignment.items()):
        positions = positions_by_assignment.get(assignment_index, [])
        local_step_count = int(item.get("local_step_count", 0))
        if len(positions) != local_step_count:
            raise ValueError(
                f"Assignment {assignment_index} local step count mismatch: "
                f"positions={len(positions)} local_step_count={local_step_count}"
            )
        varying_assignments.append(
            {
                "assignment_index": assignment_index,
                "positions": positions,
                "selected_case_key_sets": [list(case_keys) for case_keys in item.get("selected_case_key_sets", [])],
            }
        )

    for step_index, step in enumerate(steps):
        assignment_index = int(step["assignment_index"])
        if assignment_index in preselection_by_assignment:
            continue
        candidate_keys = list(step.get("candidate_keys", []))
        if not candidate_keys:
            raise ValueError(
                f"Expected at least one fixed candidate for step {step['step_index']}, got 0."
            )
        fixed_case_keys[step_index] = candidate_keys[0]

    candidates: list[dict[str, Any]] = []
    pattern_products = product(*(item["selected_case_key_sets"] for item in varying_assignments))
    for rank, selected_patterns in enumerate(pattern_products, start=1):
        selected_case_keys = list(fixed_case_keys)
        source_patterns: list[dict[str, Any]] = []
        for assignment_info, pattern_case_keys in zip(varying_assignments, selected_patterns):
            positions = assignment_info["positions"]
            if len(pattern_case_keys) != len(positions):
                raise ValueError(
                    f"Assignment {assignment_info['assignment_index']} pattern length mismatch: "
                    f"{len(pattern_case_keys)} vs {len(positions)}"
                )
            for offset, step_position in enumerate(positions):
                selected_case_keys[step_position] = pattern_case_keys[offset]
            source_patterns.append(
                {
                    "assignment_index": assignment_info["assignment_index"],
                    "selected_case_keys": list(pattern_case_keys),
                }
            )

        if any(case_key is None for case_key in selected_case_keys):
            raise ValueError("Expanded candidate contains unassigned step case keys.")

        candidates.append(
            {
                "rank": rank,
                "score": 0,
                "step_scores": [],
                "selected_case_keys": [str(case_key) for case_key in selected_case_keys],
                "source_patterns": source_patterns,
            }
        )

    if not candidates:
        raise ValueError("No expanded global candidates were generated from local preselection.")

    return candidates


def build_case_lookup(material_dictionary: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    case_lookup: dict[str, list[str]] = {}
    for case_key, case_info in material_dictionary.items():
        case_rows = case_info.get("case_rows", [])
        case_lookup[case_key] = [str(item) for item in case_rows]
    return case_lookup


def build_assignment_step_material_pairs(property_program: dict[str, Any]) -> list[tuple[str, str]]:
    step_pairs: list[tuple[str, str]] = []
    for assignment in get_assignments_in_spatial_order(property_program):
        start_material, end_material = resolve_assignment_material_pair(property_program, assignment)
        start_material = start_material or "Other"
        end_material = end_material or start_material
        gradient_steps = get_effective_gradient_steps(property_program, assignment)
        for _ in range(gradient_steps):
            step_pairs.append((start_material, end_material))
    return step_pairs


def build_step_assignment_indices(property_program: dict[str, Any]) -> list[int]:
    step_assignment_indices: list[int] = []
    for assignment in get_assignments_in_spatial_order(property_program):
        assignment_index = int(assignment.get("assignment_index", 0))
        gradient_steps = get_effective_gradient_steps(property_program, assignment)
        for _ in range(gradient_steps):
            step_assignment_indices.append(assignment_index)
    return step_assignment_indices


def hex_to_rgb01(color_hex: str) -> tuple[float, float, float]:
    color_hex = color_hex.lstrip("#")
    return tuple(int(color_hex[index:index + 2], 16) / 255.0 for index in (0, 2, 4))


def build_binary_matrix_from_selection(
    selected_case_keys: list[str],
    case_lookup: dict[str, list[str]],
) -> list[list[str]]:
    if not selected_case_keys:
        raise ValueError("selected_case_keys is empty.")

    selected_rows_per_step: list[list[str]] = []
    for case_key in selected_case_keys:
        if case_key not in case_lookup:
            raise KeyError(f"Case key not found in material dictionary: {case_key}")
        selected_rows_per_step.append(case_lookup[case_key])

    row_count = len(selected_rows_per_step[0])
    if any(len(rows) != row_count for rows in selected_rows_per_step):
        raise ValueError("Inconsistent row counts among selected cases.")

    return [
        [selected_rows_per_step[step_index][row_index] for step_index in range(len(selected_rows_per_step))]
        for row_index in range(row_count)
    ]


def build_material_name_matrix(
    binary_matrix: list[list[str]],
    step_material_pairs: list[tuple[str, str]],
) -> list[list[str]]:
    if not binary_matrix:
        raise ValueError("binary_matrix is empty.")

    col_count = len(binary_matrix[0])
    if col_count != len(step_material_pairs):
        raise ValueError(
            f"Column count mismatch: binary_matrix has {col_count} columns, "
            f"but step_material_pairs has {len(step_material_pairs)} items."
        )

    material_name_matrix: list[list[str]] = []
    for row in binary_matrix:
        material_name_row: list[str] = []
        for col_index, label in enumerate(row):
            start_material, end_material = step_material_pairs[col_index]
            material_name_row.append(materialize_case_rows([label], start_material, end_material)[0])
        material_name_matrix.append(material_name_row)

    return material_name_matrix


def evaluate_selected_case_keys(
    selected_case_keys: list[str],
    steps: list[dict[str, Any]],
    material_dictionary: dict[str, dict[str, Any]],
    case_lookup: dict[str, list[int]],
    step_material_pairs: list[tuple[str, str]],
) -> dict[str, Any]:
    if len(selected_case_keys) != len(steps):
        raise ValueError(
            "Selected case count does not match parsed step count "
            f"({len(selected_case_keys)} vs {len(steps)})."
        )

    selected_rows_per_step: list[list[str]] = []
    step_scores: list[int] = []
    total_score = 0

    for step_index, case_key in enumerate(selected_case_keys):
        case_info = material_dictionary[case_key]
        case_rows = [str(item) for item in case_info.get("case_rows", [])]
        step = steps[step_index]
        rows = materialize_case_rows(
            case_rows,
            str(step.get("material_start", "")),
            str(step.get("material_end", "")),
        )
        if selected_rows_per_step:
            step_score = compute_step_score_with_skip_connection(selected_rows_per_step, rows)
            step_scores.append(step_score)
            total_score += step_score
        selected_rows_per_step.append(rows)

    binary_matrix = build_binary_matrix_from_selection(selected_case_keys, case_lookup)
    material_name_matrix = build_material_name_matrix(binary_matrix, step_material_pairs)

    return {
        "score": total_score,
        "step_scores": step_scores,
        "selected_rows_per_step": selected_rows_per_step,
        "binary_matrix": binary_matrix,
        "material_name_matrix": material_name_matrix,
    }


def build_candidate_eta_stats(
    selected_case_keys: list[str],
    material_dictionary: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    eta_values: list[float] = []
    eta_details: list[dict[str, Any]] = []

    for case_key in selected_case_keys:
        if case_key not in material_dictionary:
            raise KeyError(f"Case key not found in material dictionary: {case_key}")
        eta_value = float(material_dictionary[case_key].get("eta", 0.0))
        eta_values.append(eta_value)
        eta_details.append({"case_key": case_key, "eta": eta_value})

    eta_sum = sum(eta_values)
    eta_avg = eta_sum / len(eta_values) if eta_values else 0.0

    return {
        "eta_sum": eta_sum,
        "eta_avg": eta_avg,
        "eta_min": min(eta_values) if eta_values else 0.0,
        "eta_max": max(eta_values) if eta_values else 0.0,
        "eta_values": eta_values,
        "eta_details": eta_details,
    }


def build_gradient_eta_target_stats(
    selected_case_keys: list[str],
    material_dictionary: dict[str, dict[str, Any]],
    property_program: dict[str, Any],
) -> dict[str, Any]:
    step_assignment_indices = build_step_assignment_indices(property_program)
    if len(step_assignment_indices) != len(selected_case_keys):
        raise ValueError(
            "Selected case count does not match the property assignment step count "
            f"({len(selected_case_keys)} vs {len(step_assignment_indices)})."
        )

    eta_by_assignment: dict[int, list[float]] = {}
    for step_index, case_key in enumerate(selected_case_keys):
        assignment_index = step_assignment_indices[step_index]
        case_info = material_dictionary[case_key]
        eta_by_assignment.setdefault(assignment_index, []).append(float(case_info.get("eta", 0.0)))

    details: list[dict[str, Any]] = []
    for assignment in get_assignments_in_spatial_order(property_program):
        if get_assignment_property_type(property_program, assignment) != "Gradient":
            continue
        assignment_index = int(assignment.get("assignment_index", 0))
        target_eta = float(assignment.get("eta", 0.0))
        eta_values = eta_by_assignment.get(assignment_index, [])
        representative_eta = max(eta_values) if eta_values else 0.0
        eta_target_error = abs(representative_eta - target_eta)
        details.append(
            {
                "assignment_index": assignment_index,
                "target_eta": target_eta,
                "representative_eta": representative_eta,
                "eta_target_error": eta_target_error,
                "step_count": int(get_effective_gradient_steps(property_program, assignment)),
            }
        )

    gradient_eta_target_error_sum = sum(float(item["eta_target_error"]) for item in details)
    gradient_eta_target_hit_count = sum(
        1 for item in details if abs(float(item["eta_target_error"])) <= 1e-12
    )
    gradient_assignment_count = len(details)
    all_gradient_eta_targets_hit = (
        gradient_assignment_count == gradient_eta_target_hit_count
        if gradient_assignment_count > 0
        else True
    )

    return {
        "gradient_eta_target_error_sum": gradient_eta_target_error_sum,
        "gradient_eta_target_hit_count": gradient_eta_target_hit_count,
        "gradient_assignment_count": gradient_assignment_count,
        "all_gradient_eta_targets_hit": all_gradient_eta_targets_hit,
        "gradient_eta_target_details": details,
    }


def build_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    switch_counter = Counter(int(item["material_switch_count"]) for item in results)
    eta_sum_values = [float(item["eta_sum"]) for item in results]
    min_switch_count = min((int(item["material_switch_count"]) for item in results), default=0)
    min_switch_results = [
        item for item in results if int(item["material_switch_count"]) == min_switch_count
    ]
    max_eta_sum_at_min_switch = (
        max(float(item["eta_sum"]) for item in min_switch_results)
        if min_switch_results
        else 0.0
    )

    return {
        "candidate_count": len(results),
        "min_material_switch_count_global": min_switch_count,
        "min_switch_candidate_count": len(min_switch_results),
        "max_eta_sum_at_min_switch": max_eta_sum_at_min_switch,
        "max_eta_sum": max(eta_sum_values) if eta_sum_values else 0.0,
        "min_eta_sum": min(eta_sum_values) if eta_sum_values else 0.0,
        "best_rank_after_sort": results[0]["rank"] if results else None,
        "switch_count_histogram": dict(sorted(switch_counter.items())),
        # Compatibility aliases for downstream summary readers.
        "best_gradient_eta_target_error_sum": 0.0,
        "best_gradient_eta_target_candidate_count": len(results),
        "min_material_switch_count_at_best_gradient_eta_target": min_switch_count,
        "best_gradient_eta_target_min_switch_candidate_count": len(min_switch_results),
    }


def build_failed_simulation_payload(reason: str) -> dict[str, Any]:
    return {
        "material_switch_count": 10**9,
        "material_switch_events": [],
        "simulation_event_count": 0,
        "material_switch_support_point_count": 0,
        "final_material_counts": {},
        "final_named_material_counts": {},
        "prioritized_value": 1,
        "simulation_failed": True,
        "simulation_failure_reason": reason,
    }


def format_switch_events(switch_events: list[dict[str, Any]]) -> str:
    if not switch_events:
        return "(none)"
    return "; ".join(
        f"{event['switch_index']}@row{event['row_index']},col{event['trigger_col_index']}:"
        f"{event.get('from_material', event['from_value'])}->{event.get('to_material', event['to_value'])}"
        for event in switch_events
    )


def format_eta_details(eta_details: list[dict[str, Any]]) -> str:
    if not eta_details:
        return "(none)"
    return ", ".join(f"{item['case_key']}:{float(item['eta']):.6f}" for item in eta_details)


def format_report(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"source_candidates_path: {payload['source_candidates_path']}")
    lines.append(f"candidate_count: {payload['summary']['candidate_count']}")
    lines.append(f"skipped_candidate_count: {len(payload.get('skipped_candidates', []))}")
    lines.append(
        "min_material_switch_count_global: "
        f"{payload['summary']['min_material_switch_count_global']}"
    )
    lines.append(
        "min_switch_candidate_count: "
        f"{payload['summary']['min_switch_candidate_count']}"
    )
    lines.append(
        "max_eta_sum_at_min_switch: "
        f"{payload['summary']['max_eta_sum_at_min_switch']:.6f}"
    )
    lines.append(f"max_eta_sum: {payload['summary']['max_eta_sum']:.6f}")
    lines.append(f"min_eta_sum: {payload['summary']['min_eta_sum']:.6f}")
    lines.append(f"best_rank_after_sort: {payload['summary']['best_rank_after_sort']}")
    lines.append("switch_count_histogram:")
    for switch_count, freq in payload["summary"]["switch_count_histogram"].items():
        lines.append(f"- {switch_count}: {freq}")

    lines.append("")
    lines.append("sorting_priority:")
    lines.append("1) material_switch_count ascending")
    lines.append("2) eta_sum descending")
    lines.append("3) score descending")
    lines.append("4) rank ascending")
    lines.append("")
    lines.append("candidate_results:")

    for item in payload["results"]:
        lines.append(
            f"{int(item['rank']):04d}. "
            f"score={item['score']} "
            f"material_switch_count={item['material_switch_count']} "
            f"eta_sum={item['eta_sum']:.6f} "
            f"eta_avg={item['eta_avg']:.6f} "
            f"eta_min={item['eta_min']:.6f} "
            f"eta_max={item['eta_max']:.6f} "
            f"selected_case_keys={', '.join(item['selected_case_keys'])}"
        )
        lines.append(f"      switch_events={format_switch_events(item['material_switch_events'])}")
        if item.get("simulation_failed"):
            lines.append(f"      simulation_failed={item.get('simulation_failure_reason', 'unknown')}")
        lines.append(f"      eta_details={format_eta_details(item['eta_details'])}")
        lines.append(
            "      gradient_eta_target_details="
            + ", ".join(
                "assignment_{assignment_index}:target={target_eta:.6f},representative={representative_eta:.6f},error={eta_target_error:.6f}".format(
                    **detail
                )
                for detail in item["gradient_eta_target_details"]
            )
        )

    skipped_candidates = payload.get("skipped_candidates", [])
    if skipped_candidates:
        lines.append("")
        lines.append("skipped_candidates:")
        for item in skipped_candidates:
            lines.append(
                f"{int(item['rank']):04d}. "
                f"score={item['score']} "
                f"reason={item['reason']} "
                f"selected_case_keys={', '.join(item['selected_case_keys'])}"
            )

    return "\n".join(lines) + "\n"


def save_candidate_simulation_files(
    result_item: dict[str, Any],
    output_rank: int,
    output_dir: Path,
    material_dictionary: dict[str, dict[str, Any]],
    property_program: dict[str, Any],
    repeated_layer_summary: dict[str, Any] | None = None,
) -> None:
    if result_item.get("simulation_failed"):
        return

    original_rank = int(result_item["rank"])
    score = int(result_item["score"])
    switch_count = int(result_item["material_switch_count"])
    eta_sum = float(result_item["eta_sum"])

    candidate_name = f"candidate_rank_{output_rank:04d}"
    candidate_dir = output_dir / candidate_name
    candidate_dir.mkdir(parents=True, exist_ok=True)

    simulation_payload = build_payload(
        result_item["binary_matrix"],
        result_item["material_name_matrix"],
    )

    simulation_payload["source_matrix_path"] = f"score_final_original_candidate_rank_{original_rank:04d}"
    simulation_payload["candidate_rank"] = output_rank
    simulation_payload["original_candidate_rank"] = original_rank
    simulation_payload["candidate_score"] = score
    simulation_payload["candidate_eta_sum"] = eta_sum
    simulation_payload["candidate_selected_case_keys"] = result_item["selected_case_keys"]
    simulation_payload["candidate_binary_matrix"] = result_item["binary_matrix"]
    simulation_payload["candidate_material_name_matrix"] = result_item["material_name_matrix"]

    json_path = candidate_dir / f"{candidate_name}_simulation.json"
    txt_path = candidate_dir / f"{candidate_name}_simulation.txt"
    png_path = candidate_dir / f"{candidate_name}_simulation.png"
    gif_path = candidate_dir / f"{candidate_name}_simulation.gif"
    ratio_eta_png_path = candidate_dir / f"{candidate_name}_ratio_eta_plot.png"
    ratio_eta_json_path = candidate_dir / f"{candidate_name}_ratio_eta_plot.json"

    json_path.write_text(
        json.dumps(simulation_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    txt_path.write_text(format_payload(simulation_payload), encoding="utf-8")

    final_state = np.array(simulation_payload["final_state"], dtype=object)
    simulation_matrix = simulation_payload["simulation_matrix"]
    simulation_material_name_matrix = simulation_payload.get("simulation_material_name_matrix")

    compact_view = build_compact_repeated_pattern_view(
        simulation_matrix,
        simulation_material_name_matrix,
        property_program,
        repeated_layer_summary,
    )
    if compact_view is None:
        image_state = final_state
        image_matrix = simulation_matrix
        image_materials = simulation_material_name_matrix
        image_labels = None
        image_summary = None
    else:
        image_matrix, image_materials, image_labels, image_summary = compact_view
        image_state = np.full(
            (len(image_matrix), len(image_matrix[0])),
            1,
            dtype=object,
        )

    save_final_stack_image(
        image_state,
        image_matrix,
        image_materials,
        simulation_payload["prioritized_value"],
        simulation_payload["material_switch_events"],
        png_path,
        x_tick_labels=image_labels,
        summary_extra_lines=image_summary,
    )
    gif_saved = should_save_simulation_gif(len(simulation_matrix[0]))
    if gif_saved:
        save_stacking_animation(
            simulation_matrix,
            simulation_material_name_matrix,
            simulation_payload["simulation_events"],
            simulation_payload["prioritized_value"],
            simulation_payload["material_switch_events"],
            gif_path,
        )
    else:
        print(
            f"Skipped GIF for {len(simulation_matrix[0])} steps "
            f"(auto limit: {AUTO_GIF_MAX_STEP_COUNT}; "
            f"override with {SAVE_SIMULATION_GIF_ENV_KEY}=1)"
        )
    save_candidate_ratio_eta_plot(
        result_item,
        material_dictionary,
        property_program,
        ratio_eta_png_path,
        ratio_eta_json_path,
    )

    summary_path = candidate_dir / f"{candidate_name}_summary.txt"
    summary_path.write_text(
        "\n".join(
            [
                f"rank: {output_rank}",
                f"original_candidate_rank: {original_rank}",
                f"score: {score}",
                f"material_switch_count: {switch_count}",
                f"eta_sum: {eta_sum:.6f}",
                f"selected_case_keys: {', '.join(result_item['selected_case_keys'])}",
                f"json: {json_path}",
                f"txt: {txt_path}",
                f"png: {png_path}",
                f"gif: {gif_path if gif_saved else '(skipped for large step count)'}",
                f"ratio_eta_png: {ratio_eta_png_path}",
                f"ratio_eta_json: {ratio_eta_json_path}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def save_candidate_ratio_eta_plot(
    result_item: dict[str, Any],
    material_dictionary: dict[str, dict[str, Any]],
    property_program: dict[str, Any],
    png_path: Path,
    json_path: Path,
) -> None:
    selected_case_keys = [str(item) for item in result_item["selected_case_keys"]]
    step_material_pairs = build_assignment_step_material_pairs(property_program)
    length_payload = load_json(LENGTH_MATRIX_PATH)
    step_spatial_metadata = build_step_spatial_metadata_from_length_payload(length_payload)
    if len(selected_case_keys) != len(step_material_pairs):
        raise ValueError("Selected case count does not match the property step count.")
    if len(selected_case_keys) != len(step_spatial_metadata):
        raise ValueError("Selected case count does not match the step spatial metadata count.")

    step_entries: list[dict[str, Any]] = []
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

    blend_rgb = np.zeros((1, len(step_entries), 3), dtype=float)
    for col_index, item in enumerate(step_entries):
        ratios = item["material_ratios"]
        total_ratio = sum(float(value) for value in ratios.values()) or 1.0
        rgb = np.zeros(3, dtype=float)
        for material_name, ratio in ratios.items():
            color_rgb = np.array(hex_to_rgb01(MATERIAL_COLORS.get(material_name, MATERIAL_COLORS["Other"])))
            rgb += color_rgb * (float(ratio) / total_ratio)
        blend_rgb[0, col_index, :] = rgb

    fig = plt.figure(figsize=(11, 4.8), constrained_layout=True)
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
        ax_eta.step(x, eta_values, where="mid", linewidth=1.6, color="#ef4444", label="eta")
    ax_ratio.set_xlim(0.5, len(step_entries) + 0.5)
    ax_ratio.set_ylim(-0.05, 1.05)
    ax_ratio.set_ylabel("phi")
    ax_eta.set_ylabel("eta", color="#ef4444")
    ax_eta.tick_params(axis="y", colors="#ef4444")
    ax_ratio.grid(axis="y", color="#d1d5db", linewidth=0.8, alpha=0.8)
    ax_ratio.set_title(f"Candidate Rank {int(result_item['rank']):04d} Material Ratios and Eta")

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
    ax_band.set_xlabel("E")
    ax_band.set_xticks(x)
    ax_band.set_xticklabels([str(int(value)) for value in x], fontsize=8)
    for boundary in np.arange(0.5, len(step_entries) + 1.5, 1.0):
        ax_band.axvline(boundary, color="#111827", linewidth=0.8)

    plt.setp(ax_ratio.get_xticklabels(), visible=False)
    fig.savefig(png_path, dpi=220, bbox_inches="tight")
    plt.close(fig)

    json_path.write_text(
        json.dumps(
            {
                "rank": int(result_item["rank"]),
                "score": int(result_item["score"]),
                "material_switch_count": int(result_item["material_switch_count"]),
                "eta_sum": float(result_item["eta_sum"]),
                "active_materials": active_materials,
                "steps": step_entries,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> None:
    property_program = load_json(PROPERTY_PROGRAM_PATH)
    material_dictionary = load_json(MATERIAL_DICTIONARY_PATH)
    adjacency_payload = load_json(ADJACENCY_JSON_PATH)
    step_source_lines = load_text_lines(SOURCE_MATRIX_TEXT_PATH)
    parsed_steps = parse_steps_from_adjacency_text(step_source_lines)

    case_lookup = build_case_lookup(material_dictionary)
    step_material_pairs = build_assignment_step_material_pairs(property_program)
    candidates = expand_local_global_candidate_pool(adjacency_payload, step_source_lines)

    results: list[dict[str, Any]] = []
    skipped_candidates: list[dict[str, Any]] = []

    for candidate in tqdm(candidates, desc="Rank candidates", unit="candidate"):
        selected_case_keys = candidate["selected_case_keys"]

        evaluation = evaluate_selected_case_keys(
            selected_case_keys,
            parsed_steps,
            material_dictionary,
            case_lookup,
            step_material_pairs,
        )

        eta_stats = build_candidate_eta_stats(selected_case_keys, material_dictionary)
        gradient_eta_target_stats = build_gradient_eta_target_stats(
            selected_case_keys,
            material_dictionary,
            property_program,
        )
        try:
            simulation_payload = build_payload(
                evaluation["binary_matrix"],
                evaluation["material_name_matrix"],
            )
        except RuntimeError as exc:
            if "safety loop" not in str(exc):
                raise
            simulation_payload = build_failed_simulation_payload(str(exc))

        results.append(
            {
                "rank": candidate["rank"],
                "score": evaluation["score"],
                "step_scores": evaluation["step_scores"],
                "selected_case_keys": selected_case_keys,
                "source_patterns": candidate.get("source_patterns", []),

                "material_switch_count": int(simulation_payload["material_switch_count"]),
                "material_switch_events": simulation_payload["material_switch_events"],
                "simulation_event_count": int(simulation_payload["simulation_event_count"]),
                "material_switch_support_point_count": int(simulation_payload["material_switch_support_point_count"]),
                "final_material_counts": simulation_payload["final_material_counts"],
                "final_named_material_counts": simulation_payload.get("final_named_material_counts", {}),
                "prioritized_value": simulation_payload["prioritized_value"],
                "simulation_failed": bool(simulation_payload.get("simulation_failed", False)),
                "simulation_failure_reason": simulation_payload.get("simulation_failure_reason"),

                "eta_sum": eta_stats["eta_sum"],
                "eta_avg": eta_stats["eta_avg"],
                "eta_min": eta_stats["eta_min"],
                "eta_max": eta_stats["eta_max"],
                "eta_values": eta_stats["eta_values"],
                "eta_details": eta_stats["eta_details"],
                "gradient_eta_target_error_sum": gradient_eta_target_stats["gradient_eta_target_error_sum"],
                "gradient_eta_target_hit_count": gradient_eta_target_stats["gradient_eta_target_hit_count"],
                "gradient_eta_target_details": gradient_eta_target_stats["gradient_eta_target_details"],

                "binary_matrix": evaluation["binary_matrix"],
                "material_name_matrix": evaluation["material_name_matrix"],
            }
        )

    results.sort(
        key=lambda item: (
            int(item["material_switch_count"]),
            -float(item["eta_sum"]),
            -int(item["score"]),
            int(item["rank"]),
        )
    )

    payload = {
        "source_candidates_path": f"{ADJACENCY_JSON_PATH} + {SOURCE_MATRIX_TEXT_PATH}",
        "summary": build_summary(results),
        "results": results,
        "skipped_candidates": skipped_candidates,
    }

    OUTPUT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SIMULATION_OUTPUT_DIR.exists():
        shutil.rmtree(SIMULATION_OUTPUT_DIR)
    SIMULATION_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    OUTPUT_JSON_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    OUTPUT_TXT_PATH.write_text(format_report(payload), encoding="utf-8")

    top_n = min(SAVE_TOP_N_SIMULATIONS, len(results))
    saved_simulation_count = 0
    for item in tqdm(results[:top_n], desc="Save top candidate simulations", unit="candidate"):
        if item.get("simulation_failed"):
            skipped_candidates.append(
                {
                    "rank": item["rank"],
                    "score": item["score"],
                    "selected_case_keys": item["selected_case_keys"],
                    "reason": item.get("simulation_failure_reason", "simulation failed"),
                }
            )
            continue
        saved_simulation_count += 1
        save_candidate_simulation_files(
            item,
            saved_simulation_count,
            SIMULATION_OUTPUT_DIR,
            material_dictionary,
            property_program,
            adjacency_payload.get("repeated_layer_summary"),
        )

    print(f"Processed candidates: {len(candidates)}")
    if skipped_candidates:
        print(f"Candidates with simulation failure retained in ranking but not exported: {len(skipped_candidates)}")
    print(f"Best rank after sort: {payload['summary']['best_rank_after_sort']}")
    print("Final ranking summary:")
    print(f"  requested result count: {SAVE_TOP_N_SIMULATIONS}")
    print(f"  saved result count: {saved_simulation_count}")
    print(f"  ranked candidate count: {payload['summary']['candidate_count']}")
    print(
        "  global min material switch count: "
        f"{payload['summary']['min_material_switch_count_global']}"
    )
    print(
        "  candidates at min switch count: "
        f"{payload['summary']['min_switch_candidate_count']}"
    )
    print(
        "  max eta sum at min switch count: "
        f"{payload['summary']['max_eta_sum_at_min_switch']:.6f}"
    )
    print(f"Saved ranking JSON: {OUTPUT_JSON_PATH}")
    print(f"Saved ranking TXT: {OUTPUT_TXT_PATH}")
    print(f"Saved top {saved_simulation_count} simulations under: {SIMULATION_OUTPUT_DIR}")


if __name__ == "__main__":
    main()
