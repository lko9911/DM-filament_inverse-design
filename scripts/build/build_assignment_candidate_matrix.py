from __future__ import annotations

from math import prod
from pathlib import Path
import json
import os

from scripts.utils.property_program_utils import (
    get_assignment_property_type,
    get_assignments_in_spatial_order,
    get_effective_gradient_steps,
    get_fixed_requested_color_case_rows,
    get_property_type,
    normalize_ratio_value,
    resolve_property_program_path,
    resolve_assignment_material_pair,
    resolve_gradient_endpoint_compositions,
)


MATERIAL_DICTIONARY_PATH = Path(os.environ.get("B_FDM_MATERIAL_DICTIONARY_PATH", "input/config/material_dictionary.json"))
PROPERTY_PROGRAM_PATH = resolve_property_program_path()
OUTPUT_JSON_PATH = Path("test_sample/derived/matrices/assignment_candidate_matrix.json")
OUTPUT_TXT_PATH = Path("test_sample/derived/matrices/assignment_candidate_matrix.txt")
ETA_MIN_ENV_KEY = "B_FDM_ETA_MIN"
ETA_MAX_ENV_KEY = "B_FDM_ETA_MAX"
BRIGHTER_MODE_ENV_KEY = "B_FDM_BRIGHTER_MODE"

TOTAL_WEIGHT = 48
RATIO_TOLERANCE = 1 / TOTAL_WEIGHT
ROW_WEIGHTS = [2, 3, 3, 4, 4, 4, 4, 4, 4, 4, 4, 3, 3, 2]
WHITE = "White"
BRIGHTER_WHITE_ROW_INDICES = {0, 1, 2, 3, 10, 11, 12, 13}
BRIGHTER_VARIABLE_WEIGHT = sum(
    weight for index, weight in enumerate(ROW_WEIGHTS) if index not in BRIGHTER_WHITE_ROW_INDICES
)


def parse_optional_float_env(env_key: str) -> float | None:
    raw_value = os.environ.get(env_key)
    if raw_value is None or not raw_value.strip():
        return None
    normalized = raw_value.strip().lower()
    if normalized in {"none", "null", "off", "disabled"}:
        return None
    return float(raw_value)


ETA_MIN = parse_optional_float_env(ETA_MIN_ENV_KEY)
ETA_MAX = parse_optional_float_env(ETA_MAX_ENV_KEY)
if ETA_MIN is not None and ETA_MAX is not None and ETA_MIN > ETA_MAX:
    raise ValueError(f"{ETA_MIN_ENV_KEY} must be <= {ETA_MAX_ENV_KEY}")
BRIGHTER_MODE = os.environ.get(BRIGHTER_MODE_ENV_KEY, "").strip().lower() in {"1", "true", "yes", "on", "brighter"}


def load_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def get_total_step_count(property_program: dict) -> int:
    return sum(get_effective_gradient_steps(property_program, assignment) for assignment in property_program.get("assignments", []))


def counts_from_start_ratio(start_ratio: float) -> tuple[int, int, float, float]:
    start_ratio = max(0.0, min(1.0, float(start_ratio)))
    end_ratio = 1.0 - start_ratio
    start_count = int(round(TOTAL_WEIGHT * start_ratio))
    start_count = max(0, min(TOTAL_WEIGHT, start_count))
    end_count = TOTAL_WEIGHT - start_count
    return start_count, end_count, start_count / TOTAL_WEIGHT, end_count / TOTAL_WEIGHT


def is_single_material_assignment(assignment: dict[str, object]) -> bool:
    material_count = int(assignment.get("material_count", 2))
    material_end = str(assignment.get("material_end", "")).strip()
    return material_count <= 1 or not material_end


def get_step_target_counts(step_index: int, step_count: int) -> tuple[int, int, float, float]:
    if step_count <= 0:
        raise ValueError("step_count must be greater than 0")

    # Use interior linear points only.
    # Example:
    # - 13 steps -> 13/14, 12/14, ..., 1/14
    # This removes pure 100/0 and 0/100 endpoint steps.
    start_ratio = (step_count - step_index) / (step_count + 1)
    end_ratio = 1.0 - start_ratio

    return counts_from_start_ratio(start_ratio)


def get_gradient_composition_target_counts(
    property_program: dict[str, object],
    assignment: dict[str, object],
    step_index: int,
    step_count: int,
) -> tuple[int, int, float, float] | None:
    endpoint_compositions = resolve_gradient_endpoint_compositions(property_program, assignment)
    if endpoint_compositions is None:
        return None

    start_composition, end_composition = endpoint_compositions
    start_material, end_material = resolve_assignment_material_pair(property_program, assignment)
    if start_material == end_material:
        return counts_from_start_ratio(1.0)

    start_endpoint_ratio = float(start_composition.get(start_material, 0.0))
    end_endpoint_ratio = float(end_composition.get(start_material, 0.0))
    alpha = (step_index + 1) / (step_count + 1)
    target_start_ratio = start_endpoint_ratio + (end_endpoint_ratio - start_endpoint_ratio) * alpha
    return counts_from_start_ratio(target_start_ratio)


def get_assignment_step_target_counts(
    property_program: dict[str, object],
    assignment: dict[str, object],
    step_index: int,
    step_count: int,
) -> tuple[int, int, float, float]:
    resolved_step_targets = assignment.get("resolved_step_targets")
    if isinstance(resolved_step_targets, list) and step_index < len(resolved_step_targets):
        resolved_step_target = resolved_step_targets[step_index]
        start_ratio = normalize_ratio_value(
            resolved_step_target.get(
                "ratio_start",
                resolved_step_target.get("material_start_ratio", 100.0),
            )
        )
        return counts_from_start_ratio(start_ratio)

    assignment_type = get_assignment_property_type(property_program, assignment)
    if assignment_type == "Gradient":
        composition_target = get_gradient_composition_target_counts(
            property_program,
            assignment,
            step_index,
            step_count,
        )
        if composition_target is not None:
            return composition_target
        return get_step_target_counts(step_index, step_count)

    if is_single_material_assignment(assignment):
        return counts_from_start_ratio(1.0)
    if step_count == 1 and "material_start_ratio" in assignment:
        return counts_from_start_ratio(normalize_ratio_value(assignment["material_start_ratio"]))
    return get_step_target_counts(step_index, step_count)


def get_assignment_eta_bounds(
    property_program: dict[str, object],
    assignment: dict[str, object],
) -> tuple[float | None, float | None, float, bool]:
    assignment_eta_limit = float(assignment["eta"])
    start_material, end_material = resolve_assignment_material_pair(property_program, assignment)
    if start_material == end_material:
        return None, 0.0, assignment_eta_limit, True
    assignment_type = get_assignment_property_type(property_program, assignment)
    if assignment_type == "Property":
        eta_mode = str(
            assignment.get(
                "eta_mode",
                "auto" if assignment.get("requested_color") else "manual",
            )
        ).strip().lower()
        if eta_mode == "auto":
            assignment_eta_limit = 2.0
        eta_limit = (
            min(assignment_eta_limit, ETA_MAX)
            if ETA_MAX is not None
            else assignment_eta_limit
        )
        return ETA_MIN, eta_limit, assignment_eta_limit, False
    eta_limit = min(assignment_eta_limit, ETA_MAX) if ETA_MAX is not None else assignment_eta_limit
    return ETA_MIN, eta_limit, assignment_eta_limit, False


def is_brighter_case_rows(case_rows: list[str]) -> bool:
    if len(case_rows) != 14:
        return False
    return all((row == WHITE) == (index in BRIGHTER_WHITE_ROW_INDICES) for index, row in enumerate(case_rows))


def brighter_variable_counts(case_rows: list[str]) -> tuple[int, int]:
    start_count = 0
    end_count = 0
    for index, (row, weight) in enumerate(zip(case_rows, ROW_WEIGHTS)):
        if index in BRIGHTER_WHITE_ROW_INDICES:
            continue
        if row == "Material_start":
            start_count += weight
        elif row == "Material_end":
            end_count += weight
    return start_count, end_count


def material_counts_from_case_rows(case_rows: list[str], *, brighter_mode: bool = False) -> tuple[int, int]:
    if brighter_mode:
        return brighter_variable_counts(case_rows)

    start_count = 0
    end_count = 0
    for row, weight in zip(case_rows, ROW_WEIGHTS):
        if row == "Material_start":
            start_count += weight
        elif row == "Material_end":
            end_count += weight
    return start_count, end_count


def build_candidate_index(
    material_dictionary: dict[str, dict[str, object]],
    brighter_mode: bool = BRIGHTER_MODE,
) -> dict[int, list[dict[str, object]]]:
    index: dict[int, list[dict[str, object]]] = {}
    for case_key, case_info in material_dictionary.items():
        case_rows = [str(item) for item in case_info.get("case_rows", [])]
        if brighter_mode:
            if not is_brighter_case_rows(case_rows):
                continue
        elif any(row == WHITE for row in case_rows):
            continue
        start_count = int(case_info["material_start_count"])
        end_count = int(case_info["material_end_count"])
        actual_start_count = start_count
        actual_end_count = end_count
        if brighter_mode:
            start_count, end_count = brighter_variable_counts(case_rows)
        index.setdefault(start_count, []).append(
            {
                "case_key": case_key,
                "material_start_count": start_count,
                "material_end_count": end_count,
                "actual_material_start_count": actual_start_count,
                "actual_material_end_count": actual_end_count,
                "material_start_ratio": start_count / TOTAL_WEIGHT,
                "material_end_ratio": end_count / TOTAL_WEIGHT,
                "actual_material_start_ratio": float(case_info["material_start_ratio"]),
                "actual_material_end_ratio": float(case_info["material_end_ratio"]),
                "eta": float(case_info["eta"]),
                "case_rows": case_rows,
            }
        )
    return index


def find_ratio_tolerant_candidates(
    candidate_index: dict[int, list[dict[str, object]]],
    target_start_ratio: float,
    eta_min: float | None,
    eta_limit: float | None,
    ratio_tolerance: float = RATIO_TOLERANCE,
) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    for bucket in candidate_index.values():
        for candidate in bucket:
            ratio_error = abs(float(candidate["material_start_ratio"]) - target_start_ratio)
            candidate_eta = float(candidate["eta"])
            if eta_min is not None and candidate_eta < eta_min:
                continue
            if ratio_error <= ratio_tolerance + 1e-12 and (eta_limit is None or candidate_eta <= eta_limit):
                enriched_candidate = dict(candidate)
                enriched_candidate["material_start_ratio_error"] = ratio_error
                enriched_candidate["material_end_ratio_error"] = ratio_error
                candidates.append(enriched_candidate)

    candidates.sort(key=lambda item: (float(item["material_start_ratio_error"]), item["case_key"]))
    return candidates


def filter_max_eta_candidates(candidates: list[dict[str, object]]) -> list[dict[str, object]]:
    if not candidates:
        return []
    max_eta = max(float(candidate["eta"]) for candidate in candidates)
    filtered = [candidate for candidate in candidates if float(candidate["eta"]) == max_eta]
    filtered.sort(key=lambda item: (float(item["material_start_ratio_error"]), item["case_key"]))
    return filtered


def filter_target_eta_candidates(
    candidates: list[dict[str, object]],
    target_eta: float | None,
) -> list[dict[str, object]]:
    if not candidates or target_eta is None:
        return candidates

    min_eta_error = min(abs(float(candidate["eta"]) - target_eta) for candidate in candidates)
    filtered: list[dict[str, object]] = []
    for candidate in candidates:
        eta_error = abs(float(candidate["eta"]) - target_eta)
        if abs(eta_error - min_eta_error) <= 1e-12:
            enriched_candidate = dict(candidate)
            enriched_candidate["eta_target_error"] = eta_error
            filtered.append(enriched_candidate)

    if filtered:
        min_ratio_error = min(
            float(candidate["material_start_ratio_error"])
            for candidate in filtered
        )
        filtered = [
            candidate
            for candidate in filtered
            if abs(
                float(candidate["material_start_ratio_error"])
                - min_ratio_error
            )
            <= 1e-12
        ]

    filtered.sort(
        key=lambda item: (
            float(item.get("eta_target_error", 0.0)),
            float(item["material_start_ratio_error"]),
            item["case_key"],
        )
    )
    return filtered


def material_compactness_key(
    case_rows: list[object],
    material_name: str,
) -> tuple[int, int]:
    material_indices = [
        index
        for index, row in enumerate(case_rows)
        if str(row) == material_name
    ]
    if not material_indices:
        return (10**9, 10**9)

    run_count = 1 + sum(
        current_index != previous_index + 1
        for previous_index, current_index in zip(
            material_indices,
            material_indices[1:],
        )
    )
    first_index = material_indices[0]
    last_index = material_indices[-1]
    internal_gap_weight = sum(
        ROW_WEIGHTS[index]
        for index in range(first_index, last_index + 1)
        if str(case_rows[index]) != material_name
    )
    return (run_count, internal_gap_weight)


def filter_compact_material_candidates(
    candidates: list[dict[str, object]],
    material_name: str,
) -> list[dict[str, object]]:
    if not candidates:
        return []
    best_key = min(
        material_compactness_key(
            list(candidate.get("case_rows", [])),
            material_name,
        )
        for candidate in candidates
    )
    return [
        candidate
        for candidate in candidates
        if material_compactness_key(
            list(candidate.get("case_rows", [])),
            material_name,
        )
        == best_key
    ]


def get_fixed_case_rows_for_step(
    assignment: dict[str, object],
    resolved_step_target: dict[str, object] | None,
) -> list[str] | None:
    eta_mode = str(
        assignment.get("eta_mode", "auto" if assignment.get("requested_color") else "")
    ).strip().lower()
    assignment_mode = str(assignment.get("assignment_mode", "manual")).strip().lower()
    if assignment_mode == "manual" and eta_mode in {"auto", "manual"}:
        # A color recipe defines only the material pair and ratio in the UI.
        # Its historical fixed row layout must not override an independently
        # automatic or manually selected eta.
        return None
    if resolved_step_target is not None:
        rows = resolved_step_target.get("fixed_case_rows")
        if isinstance(rows, list) and rows:
            return [str(row) for row in rows]
        color_recipe = resolved_step_target.get("color_recipe")
        if isinstance(color_recipe, dict):
            rows = color_recipe.get("fixed_case_rows")
            if isinstance(rows, list) and rows:
                return [str(row) for row in rows]

    rows = assignment.get("fixed_case_rows")
    if isinstance(rows, list) and rows:
        return [str(row) for row in rows]
    color_recipe = assignment.get("color_recipe")
    if isinstance(color_recipe, dict):
        rows = color_recipe.get("fixed_case_rows")
        if isinstance(rows, list) and rows:
            return [str(row) for row in rows]
    return get_fixed_requested_color_case_rows(assignment)


def find_fixed_case_candidates(
    candidate_index: dict[int, list[dict[str, object]]],
    fixed_case_rows: list[str],
    eta_min: float | None,
    eta_limit: float | None,
) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    for bucket in candidate_index.values():
        for candidate in bucket:
            if [str(row) for row in candidate.get("case_rows", [])] != fixed_case_rows:
                continue
            candidate_eta = float(candidate["eta"])
            if eta_min is not None and candidate_eta < eta_min:
                continue
            if eta_limit is not None and candidate_eta > eta_limit:
                continue
            enriched_candidate = dict(candidate)
            enriched_candidate["material_start_ratio_error"] = 0.0
            enriched_candidate["material_end_ratio_error"] = 0.0
            enriched_candidate["eta_target_error"] = 0.0
            candidates.append(enriched_candidate)
    candidates.sort(key=lambda item: item["case_key"])
    return candidates


def is_middle_gradient_step(local_step_index: int, step_count: int) -> bool:
    if step_count <= 0:
        return False
    return local_step_index == (step_count // 2)


def get_resolved_step_target(
    assignment: dict[str, object],
    local_step_index: int,
) -> dict[str, object] | None:
    resolved_step_targets = assignment.get("resolved_step_targets")
    if not isinstance(resolved_step_targets, list):
        return None
    if local_step_index < 0 or local_step_index >= len(resolved_step_targets):
        return None
    target = resolved_step_targets[local_step_index]
    return target if isinstance(target, dict) else None


def build_repeated_layer_template_summary(
    property_program: dict[str, object],
    candidate_matrix: list[dict[str, object]],
) -> dict[str, object] | None:
    assignments_by_index = {
        int(assignment["assignment_index"]): assignment
        for assignment in property_program.get("assignments", [])
        if isinstance(assignment, dict) and "assignment_index" in assignment
    }
    cells_by_layer: dict[int, list[dict[str, object]]] = {}
    for cell in candidate_matrix:
        assignment = assignments_by_index.get(int(cell["assignment_index"]))
        event = assignment.get("layer_region_event") if isinstance(assignment, dict) else None
        if not isinstance(event, dict) or "layer_index" not in event:
            return None
        cells_by_layer.setdefault(int(event["layer_index"]), []).append(cell)

    ordered_layers = sorted(cells_by_layer)
    if len(ordered_layers) <= 1:
        return None

    def cell_signature(cell: dict[str, object]) -> tuple[object, ...]:
        return (
            cell.get("assignment_property_type"),
            cell.get("assignment_material_start"),
            cell.get("assignment_material_end"),
            cell.get("target_material_start_count"),
            cell.get("target_material_end_count"),
            tuple(
                str(candidate.get("case_key"))
                for candidate in cell.get("candidates", [])
                if isinstance(candidate, dict)
            ),
        )

    template_cells = cells_by_layer[ordered_layers[0]]
    template_signature = tuple(cell_signature(cell) for cell in template_cells)
    if not template_signature:
        return None
    if any(
        tuple(cell_signature(cell) for cell in cells_by_layer[layer_index])
        != template_signature
        for layer_index in ordered_layers[1:]
    ):
        return None

    template_pattern_count = prod(
        int(cell.get("candidate_count", 0))
        for cell in template_cells
    )
    return {
        "layer_count": len(ordered_layers),
        "steps_per_layer": len(template_cells),
        "template_pattern_count": template_pattern_count,
        "expanded_step_count": len(candidate_matrix),
        "layer_indices": ordered_layers,
    }



def build_assignment_candidate_matrix(
    property_program: dict,
    material_dictionary: dict[str, dict[str, object]],
) -> dict[str, object]:
    global_brighter_mode = BRIGHTER_MODE or bool(property_program.get("brighter_mode", False))
    regular_candidate_index = build_candidate_index(material_dictionary, brighter_mode=False)
    brighter_candidate_index = build_candidate_index(material_dictionary, brighter_mode=True)
    assignments = get_assignments_in_spatial_order(property_program)
    has_assignment_brighter_flags = any("brighter_mode" in assignment for assignment in assignments)

    candidate_matrix: list[dict[str, object]] = []
    assignments_summary: list[dict[str, object]] = []
    global_step_index = 1
    total_pattern_count = 1

    for assignment in assignments:
        assignment_index = int(assignment["assignment_index"])
        raw_gradient_steps = int(assignment.get("gradient_steps", 0))
        gradient_steps = get_effective_gradient_steps(property_program, assignment)
        gradient_direction = str(assignment.get("gradient_direction", "printing"))
        assignment_type = get_assignment_property_type(property_program, assignment)
        assignment_brighter_mode = (
            bool(assignment.get("brighter_mode", False))
            if has_assignment_brighter_flags
            else global_brighter_mode
        )
        if str(assignment.get("requested_color", "")).strip().upper() == "WHITE":
            assignment_brighter_mode = False
        candidate_index = brighter_candidate_index if assignment_brighter_mode else regular_candidate_index
        assignment_material_start, assignment_material_end = resolve_assignment_material_pair(property_program, assignment)
        assignment_eta_input = float(assignment["eta"])
        assignment_eta_mode = str(
            assignment.get(
                "eta_mode",
                "auto" if assignment_type == "Property" and assignment.get("requested_color") else "manual",
            )
        ).strip().lower()
        eta_min, eta_limit, assignment_eta_limit, eta_fixed_single_material = get_assignment_eta_bounds(
            property_program,
            assignment,
        )
        if assignment_brighter_mode and assignment_eta_mode == "auto":
            brighter_eta_target = 2.0 if eta_fixed_single_material else 4.0
            eta_min = ETA_MIN
            eta_limit = min(brighter_eta_target, ETA_MAX) if ETA_MAX is not None else brighter_eta_target
            assignment_eta_limit = brighter_eta_target
            eta_fixed_single_material = False
        elif assignment_brighter_mode and eta_fixed_single_material:
            eta_min = ETA_MIN
            eta_limit = min(assignment_eta_limit, ETA_MAX) if ETA_MAX is not None else assignment_eta_limit
            eta_fixed_single_material = False

        step_cells: list[dict[str, object]] = []
        assignment_candidate_counts: list[int] = []

        for local_step_index in range(gradient_steps):
            resolved_step_target = get_resolved_step_target(assignment, local_step_index)
            target_start_count, target_end_count, target_start_ratio, target_end_ratio = get_assignment_step_target_counts(
                property_program,
                assignment,
                local_step_index,
                gradient_steps,
            )
            candidate_target_start_count = target_start_count
            candidate_target_start_ratio = target_start_ratio
            if assignment_brighter_mode:
                candidate_target_start_count = round(
                    target_start_ratio * BRIGHTER_VARIABLE_WEIGHT
                )
                candidate_target_start_ratio = candidate_target_start_count / TOTAL_WEIGHT

            fixed_case_rows = get_fixed_case_rows_for_step(assignment, resolved_step_target)
            if fixed_case_rows is not None:
                target_start_count, target_end_count = material_counts_from_case_rows(
                    fixed_case_rows,
                    brighter_mode=assignment_brighter_mode,
                )
                target_start_ratio = target_start_count / TOTAL_WEIGHT
                target_end_ratio = target_end_count / TOTAL_WEIGHT
                candidate_target_start_count = target_start_count
                candidate_target_start_ratio = target_start_ratio

            # Ratio-first filter with tolerance.
            # - Gradient: keep every candidate within the eta upper bound.
            # - Property: keep the independently selected automatic/manual eta target.
            if fixed_case_rows is not None:
                candidates = find_fixed_case_candidates(candidate_index, fixed_case_rows, eta_min, eta_limit)
            elif resolved_step_target is not None:
                candidates = find_ratio_tolerant_candidates(candidate_index, candidate_target_start_ratio, eta_min, None)
            else:
                candidates = find_ratio_tolerant_candidates(candidate_index, candidate_target_start_ratio, eta_min, eta_limit)
            eta_auto_corrected_from = (
                assignment_eta_input
                if assignment_eta_mode == "auto"
                and not eta_fixed_single_material
                and abs(assignment_eta_input - float(eta_limit)) > 1e-12
                else None
            )
            eta_auto_corrected_to = eta_limit if eta_auto_corrected_from is not None else None
            ratio_candidate_count_before_eta = None

            resolved_eta_target = None
            if resolved_step_target is not None and resolved_step_target.get("eta") is not None:
                resolved_eta_target = float(resolved_step_target["eta"])

            if fixed_case_rows is not None:
                pass
            elif resolved_eta_target is not None:
                candidates = filter_target_eta_candidates(candidates, resolved_eta_target)
            elif assignment.get("property_guided_resolution") and assignment_type == "Property" and not eta_fixed_single_material:
                candidates = filter_target_eta_candidates(candidates, float(assignment["eta"]))
            elif assignment_type == "Property" and not eta_fixed_single_material:
                candidates = filter_target_eta_candidates(candidates, eta_limit)
            elif assignment_type == "Gradient" and not eta_fixed_single_material and is_middle_gradient_step(local_step_index, gradient_steps):
                candidates = filter_max_eta_candidates(candidates)
            elif assignment_type == "Gradient" and gradient_steps == 1 and not eta_fixed_single_material:
                candidates = filter_target_eta_candidates(candidates, eta_limit)

            compact_material_preference = None
            if (
                assignment_type == "Property"
                and str(assignment.get("requested_color", "")).strip().upper()
                == "PURPLE"
            ):
                candidates = filter_compact_material_candidates(
                    candidates,
                    "Material_end",
                )
                compact_material_preference = "Material_end"

            step_cell = {
                "global_step_index": global_step_index,
                "assignment_index": assignment_index,
                "assignment_property_type": assignment_type,
                "assignment_material_start": assignment_material_start,
                "assignment_material_end": assignment_material_end,
                "assignment_brighter_mode": assignment_brighter_mode,
                "local_step_index": local_step_index + 1,
                "gradient_direction": gradient_direction,
                "eta_min": eta_min,
                "eta_limit": eta_limit,
                "assignment_eta_limit": assignment_eta_limit,
                "assignment_eta_mode": assignment_eta_mode,
                "eta_auto_corrected_from": eta_auto_corrected_from,
                "eta_auto_corrected_to": eta_auto_corrected_to,
                "ratio_candidate_count_before_eta": ratio_candidate_count_before_eta,
                "global_eta_max": ETA_MAX,
                "eta_fixed_single_material": eta_fixed_single_material,
                "eta_target": resolved_eta_target if resolved_eta_target is not None else eta_limit,
                "eta_optimization_mode": (
                    "fixed_case_rows"
                    if fixed_case_rows is not None
                    else "guided_exact"
                    if resolved_eta_target is not None
                    else "exact" if assignment_type == "Property" and not eta_fixed_single_material
                    else "upper_bound"
                ),
                "target_material_start_count": target_start_count,
                "target_material_end_count": target_end_count,
                "target_material_start_ratio": target_start_ratio,
                "target_material_end_ratio": target_end_ratio,
                "candidate_target_material_start_count": candidate_target_start_count,
                "candidate_target_material_start_ratio": candidate_target_start_ratio,
                "resolved_step_target": resolved_step_target,
                "fixed_case_rows": fixed_case_rows,
                "compact_material_preference": compact_material_preference,
                "ratio_tolerance": RATIO_TOLERANCE,
                "candidate_count": len(candidates),
                "candidates": candidates,
            }
            step_cells.append(step_cell)
            candidate_matrix.append(step_cell)
            assignment_candidate_counts.append(len(candidates))
            global_step_index += 1

        assignment_pattern_count = prod(assignment_candidate_counts) if assignment_candidate_counts else 0
        total_pattern_count *= assignment_pattern_count if assignment_pattern_count > 0 else 0

        assignments_summary.append(
            {
                "assignment_index": assignment_index,
                "assignment_property_type": assignment_type,
                "assignment_material_start": assignment_material_start,
                "assignment_material_end": assignment_material_end,
                "assignment_brighter_mode": assignment_brighter_mode,
                "raw_gradient_steps": raw_gradient_steps,
                "gradient_steps": gradient_steps,
                "gradient_direction": gradient_direction,
                "eta_min": eta_min,
                "eta_limit": eta_limit,
                "assignment_eta_limit": assignment_eta_limit,
                "assignment_eta_mode": assignment_eta_mode,
                "eta_auto_corrected_from": step_cells[0].get("eta_auto_corrected_from") if step_cells else None,
                "eta_auto_corrected_to": step_cells[0].get("eta_auto_corrected_to") if step_cells else None,
                "global_eta_max": ETA_MAX,
                "eta_fixed_single_material": eta_fixed_single_material,
                "eta_target": eta_limit,
                "eta_optimization_mode": "exact" if assignment_type == "Property" and not eta_fixed_single_material else "upper_bound",
                "assignment_candidate_count": assignment_pattern_count,
                "step_cells": step_cells,
            }
        )

    repeated_layer_template = build_repeated_layer_template_summary(
        property_program,
        candidate_matrix,
    )
    unconstrained_pattern_count = total_pattern_count
    if repeated_layer_template is not None:
        total_pattern_count = int(repeated_layer_template["template_pattern_count"])

    return {
        "property_type": get_property_type(property_program),
        "matrix_rows": 1,
        "matrix_cols": len(candidate_matrix),
        "total_step_count": len(candidate_matrix),
        "total_pattern_count": total_pattern_count,
        "unconstrained_cartesian_pattern_count": unconstrained_pattern_count,
        "repeated_layer_template": repeated_layer_template,
        "eta_min": ETA_MIN,
        "eta_max": ETA_MAX,
        "brighter_mode": global_brighter_mode,
        "has_assignment_brighter_mode": has_assignment_brighter_flags,
        "assignments": assignments_summary,
        "candidate_matrix": candidate_matrix,
    }


def save_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def format_candidate_matrix_text(payload: dict) -> str:
    lines: list[str] = []
    lines.append(f"matrix_rows: {payload['matrix_rows']}")
    lines.append(f"matrix_cols: {payload['matrix_cols']}")
    lines.append(f"total_pattern_count: {payload['total_pattern_count']}")
    lines.append(
        "unconstrained_cartesian_pattern_count: "
        f"{payload.get('unconstrained_cartesian_pattern_count', payload['total_pattern_count'])}"
    )
    lines.append(
        "repeated_layer_template: "
        f"{payload.get('repeated_layer_template')}"
    )
    lines.append(f"eta_min: {payload.get('eta_min')}")
    lines.append(f"eta_max: {payload.get('eta_max')}")
    lines.append(f"brighter_mode: {payload.get('brighter_mode', False)}")
    lines.append(f"has_assignment_brighter_mode: {payload.get('has_assignment_brighter_mode', False)}")
    lines.append("")

    for assignment in payload.get("assignments", []):
        lines.append(
            "assignment_{assignment:02d} pattern_count: {count}".format(
                assignment=int(assignment["assignment_index"]),
                count=int(assignment["assignment_candidate_count"]),
            )
        )

    lines.append("")

    step_cells = payload.get("steps") or payload.get("candidate_matrix", [])
    for cell in step_cells:
        candidates = cell.get("candidates", [])
        candidate_keys = ", ".join(candidate["case_key"] for candidate in candidates)
        eta_limit = cell.get("eta_limit")
        eta_limit_text = "None" if eta_limit is None else f"{float(eta_limit):g}"
        eta_target = cell.get("eta_target")
        eta_target_text = "None" if eta_target is None else f"{float(eta_target):g}"
        eta_correction_text = ""
        if cell.get("eta_auto_corrected_to") is not None:
            eta_correction_text = (
                " | eta_auto_corrected {before}->{after}".format(
                    before=cell.get("eta_auto_corrected_from"),
                    after=cell.get("eta_auto_corrected_to"),
                )
            )
        candidate_target_text = ""
        if cell.get("assignment_brighter_mode", False):
            candidate_target_text = (
                " | bright_candidate_target {count} ({ratio:.6f})".format(
                    count=int(cell.get("candidate_target_material_start_count", cell["target_material_start_count"])),
                    ratio=float(cell.get("candidate_target_material_start_ratio", cell["target_material_start_ratio"])),
                )
            )
        lines.append(
            "step_{global_step:03d} | assignment {assignment} | local_step {local_step} | "
            "materials {material_start}->{material_end} | "
            "target {start_count}/{end_count} ({start_ratio:.6f}/{end_ratio:.6f}) | "
            "ratio_tol<= {ratio_tolerance:.6f} | eta>= {eta_min} | eta<= {eta} | eta_target {eta_target}"
            "{candidate_target}{eta_correction} | candidate_count {count}".format(
                global_step=int(cell["global_step_index"]),
                assignment=int(cell["assignment_index"]),
                local_step=int(cell["local_step_index"]),
                material_start=cell.get("assignment_material_start", ""),
                material_end=cell.get("assignment_material_end", ""),
                start_count=int(cell["target_material_start_count"]),
                end_count=int(cell["target_material_end_count"]),
                start_ratio=float(cell["target_material_start_ratio"]),
                end_ratio=float(cell["target_material_end_ratio"]),
                ratio_tolerance=float(cell["ratio_tolerance"]),
                eta_min=cell["eta_min"],
                eta=eta_limit_text,
                eta_target=eta_target_text,
                count=int(cell["candidate_count"]),
                candidate_target=candidate_target_text,
                eta_correction=eta_correction_text,
            )
        )
        lines.append(f"  candidates: {candidate_keys}")

    return "\n".join(lines)


def format_theoretical_pattern_count(value: object) -> str:
    count_text = str(int(value))
    if len(count_text) <= 18:
        return count_text
    significant_digits = count_text[:4]
    return (
        f"{significant_digits[0]}.{significant_digits[1:]}e+{len(count_text) - 1} "
        f"({len(count_text)} digits; exact value saved in JSON)"
    )


def main() -> None:
    property_program = load_json(PROPERTY_PROGRAM_PATH)
    material_dictionary = load_json(MATERIAL_DICTIONARY_PATH)

    payload = build_assignment_candidate_matrix(property_program, material_dictionary)
    save_json(OUTPUT_JSON_PATH, payload)
    OUTPUT_TXT_PATH.write_text(format_candidate_matrix_text(payload), encoding="utf-8")

    print(f"Total step count: {payload['total_step_count']}")
    repeated_layer_template = payload.get("repeated_layer_template")
    if isinstance(repeated_layer_template, dict):
        print(
            "Repeated layer template pattern count: "
            f"{payload['total_pattern_count']} "
            f"({repeated_layer_template['steps_per_layer']} regions x "
            f"{repeated_layer_template['layer_count']} layers; "
            "occurrence lengths remain exact)"
        )
        print(
            "Unconstrained Cartesian count (not used): "
            f"{format_theoretical_pattern_count(payload['unconstrained_cartesian_pattern_count'])}"
        )
    else:
        print(
            "Theoretical Cartesian pattern count (not enumerated): "
            f"{format_theoretical_pattern_count(payload['total_pattern_count'])}"
        )
    print(f"Saved JSON to: {OUTPUT_JSON_PATH}")
    print(f"Saved TXT to: {OUTPUT_TXT_PATH}")


if __name__ == "__main__":
    main()
