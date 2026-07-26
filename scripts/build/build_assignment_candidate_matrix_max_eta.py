from __future__ import annotations

from math import prod
from pathlib import Path
import json
import os

from scripts.utils.property_program_utils import (
    get_assignment_property_type,
    get_assignments_in_spatial_order,
    get_effective_gradient_steps,
    get_property_type,
    normalize_ratio_value,
    resolve_property_program_path,
    resolve_assignment_material_pair,
)


MATERIAL_DICTIONARY_PATH = Path(os.environ.get("B_FDM_MATERIAL_DICTIONARY_PATH", "input/config/material_dictionary.json"))
PROPERTY_PROGRAM_PATH = resolve_property_program_path()
OUTPUT_JSON_PATH = Path("test_sample/derived/matrices/assignment_candidate_matrix_max_eta.json")
OUTPUT_TXT_PATH = Path("test_sample/derived/matrices/assignment_candidate_matrix_max_eta.txt")
ETA_MIN_ENV_KEY = "B_FDM_ETA_MIN"
ETA_MAX_ENV_KEY = "B_FDM_ETA_MAX"

TOTAL_WEIGHT = 48
RATIO_TOLERANCE = 1 / TOTAL_WEIGHT


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


def load_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def counts_from_start_ratio(start_ratio: float) -> tuple[int, int, float, float]:
    start_ratio = max(0.0, min(1.0, float(start_ratio)))
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


def get_assignment_step_target_counts(
    property_program: dict[str, object],
    assignment: dict[str, object],
    step_index: int,
    step_count: int,
) -> tuple[int, int, float, float]:
    assignment_type = get_assignment_property_type(property_program, assignment)
    if assignment_type == "Gradient":
        start_material, end_material = resolve_assignment_material_pair(property_program, assignment)
        if start_material == end_material:
            return counts_from_start_ratio(1.0)
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
        eta_limit = (
            min(assignment_eta_limit, ETA_MAX)
            if ETA_MAX is not None
            else assignment_eta_limit
        )
        return ETA_MIN, eta_limit, assignment_eta_limit, False
    eta_limit = min(assignment_eta_limit, ETA_MAX) if ETA_MAX is not None else assignment_eta_limit
    return ETA_MIN, eta_limit, assignment_eta_limit, False


def build_candidate_index(material_dictionary: dict[str, dict[str, object]]) -> dict[int, list[dict[str, object]]]:
    index: dict[int, list[dict[str, object]]] = {}
    for case_key, case_info in material_dictionary.items():
        start_count = int(case_info["material_start_count"])
        index.setdefault(start_count, []).append(
            {
                "case_key": case_key,
                "material_start_count": start_count,
                "material_end_count": int(case_info["material_end_count"]),
                "material_start_ratio": float(case_info["material_start_ratio"]),
                "material_end_ratio": float(case_info["material_end_ratio"]),
                "eta": float(case_info["eta"]),
                "case_rows": [str(item) for item in case_info.get("case_rows", [])],
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
            if ratio_error <= ratio_tolerance and (eta_limit is None or candidate_eta <= eta_limit):
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
    filtered.sort(key=lambda item: item["case_key"])
    return filtered



def build_candidate_matrix(
    property_program: dict,
    material_dictionary: dict[str, dict[str, object]],
) -> dict[str, object]:
    candidate_index = build_candidate_index(material_dictionary)
    assignments = get_assignments_in_spatial_order(property_program)

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
        material_start, material_end = resolve_assignment_material_pair(property_program, assignment)
        eta_min, eta_limit, assignment_eta_limit, eta_fixed_single_material = get_assignment_eta_bounds(
            property_program,
            assignment,
        )

        step_cells: list[dict[str, object]] = []
        assignment_candidate_counts: list[int] = []

        for local_step_index in range(gradient_steps):
            target_start_count, target_end_count, target_start_ratio, target_end_ratio = get_assignment_step_target_counts(
                property_program,
                assignment,
                local_step_index,
                gradient_steps,
            )

            candidates = find_ratio_tolerant_candidates(candidate_index, target_start_ratio, eta_min, eta_limit)
            candidates = filter_max_eta_candidates(candidates)

            max_candidate_eta = candidates[0]["eta"] if candidates else None
            step_cell = {
                "global_step_index": global_step_index,
                "assignment_index": assignment_index,
                "assignment_property_type": assignment_type,
                "local_step_index": local_step_index + 1,
                "gradient_direction": gradient_direction,
                "eta_min": eta_min,
                "eta_limit": eta_limit,
                "assignment_eta_limit": assignment_eta_limit,
                "global_eta_max": ETA_MAX,
                "eta_fixed_single_material": eta_fixed_single_material,
                "eta_target": eta_limit,
                "assignment_material_start": material_start,
                "assignment_material_end": material_end,
                "max_candidate_eta": max_candidate_eta,
                "target_material_start_count": target_start_count,
                "target_material_end_count": target_end_count,
                "target_material_start_ratio": target_start_ratio,
                "target_material_end_ratio": target_end_ratio,
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
                "raw_gradient_steps": raw_gradient_steps,
                "gradient_steps": gradient_steps,
                "gradient_direction": gradient_direction,
                "eta_min": eta_min,
                "eta_limit": eta_limit,
                "assignment_eta_limit": assignment_eta_limit,
                "global_eta_max": ETA_MAX,
                "eta_fixed_single_material": eta_fixed_single_material,
                "eta_target": eta_limit,
                "assignment_material_start": material_start,
                "assignment_material_end": material_end,
                "max_candidate_eta": step_cells[0]["max_candidate_eta"] if step_cells else None,
                "assignment_candidate_count": assignment_pattern_count,
                "step_cells": step_cells,
            }
        )

    return {
        "property_type": get_property_type(property_program),
        "matrix_rows": 1,
        "matrix_cols": len(candidate_matrix),
        "total_step_count": len(candidate_matrix),
        "total_pattern_count": total_pattern_count,
        "eta_min": ETA_MIN,
        "eta_max": ETA_MAX,
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
    lines.append(f"eta_min: {payload.get('eta_min')}")
    lines.append(f"eta_max: {payload.get('eta_max')}")
    lines.append("")

    for assignment in payload.get("assignments", []):
        lines.append(
            "assignment_{assignment:02d} pattern_count: {count}".format(
                assignment=int(assignment["assignment_index"]),
                count=int(assignment["assignment_candidate_count"]),
            )
        )

    lines.append("")

    for cell in payload.get("candidate_matrix", []):
        candidates = cell.get("candidates", [])
        candidate_keys = ", ".join(candidate["case_key"] for candidate in candidates)
        eta_limit = cell.get("eta_limit")
        eta_limit_text = "None" if eta_limit is None else f"{float(eta_limit):g}"
        eta_target = cell.get("eta_target")
        eta_target_text = "None" if eta_target is None else f"{float(eta_target):g}"
        lines.append(
            "step_{global_step:03d} | assignment {assignment} | local_step {local_step} | "
            "materials {material_start}->{material_end} | "
            "target {start_count}/{end_count} ({start_ratio:.6f}/{end_ratio:.6f}) | "
            "ratio_tol<= {ratio_tolerance:.6f} | eta>= {eta_min} | eta<= {eta} | eta_target {eta_target} | "
            "max_eta {max_eta} | candidate_count {count}".format(
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
                max_eta=cell["max_candidate_eta"],
                count=int(cell["candidate_count"]),
            )
        )
        lines.append(f"  candidates: {candidate_keys}")

    return "\n".join(lines)


def main() -> None:
    property_program = load_json(PROPERTY_PROGRAM_PATH)
    material_dictionary = load_json(MATERIAL_DICTIONARY_PATH)

    payload = build_candidate_matrix(property_program, material_dictionary)
    save_json(OUTPUT_JSON_PATH, payload)
    OUTPUT_TXT_PATH.write_text(format_candidate_matrix_text(payload), encoding="utf-8")

    print(f"Total step count: {payload['total_step_count']}")
    print(f"Total pattern count: {payload['total_pattern_count']}")
    print(f"Saved JSON to: {OUTPUT_JSON_PATH}")
    print(f"Saved TXT to: {OUTPUT_TXT_PATH}")


if __name__ == "__main__":
    main()
