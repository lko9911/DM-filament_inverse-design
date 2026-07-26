from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from pprint import pformat
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.simulation.simulate_matrix_deposition import (
    build_payload,
    format_payload,
    save_final_stack_image,
    save_stacking_animation,
)

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
}

# ============================================================
# User settings
# ============================================================
PROPERTY_NAME = "Property_vase"
INPUT_ROOT = PROJECT_ROOT / "out" / PROPERTY_NAME
RANKED_JSON_PATH = INPUT_ROOT / "simulation" / "intermediate" / "beam_step_adjacency_clusters_best_switch_eta_ranked.json"
LENGTH_MATRIX_PATH = INPUT_ROOT / "matrices" / "length_matrix.json"
OUTPUT_ROOT = INPUT_ROOT / "max_eta_ignoring_switch"


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


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


def material_name_to_result_code(material_name: object) -> int:
    normalized = str(material_name).strip().upper()
    if normalized not in MATERIAL_CODES:
        raise ValueError(f"No result material code is defined for material: {material_name}")
    return MATERIAL_CODES[normalized]


def convert_material_names_to_codes(material_name_matrix: list[list[str]]) -> list[list[int]]:
    matrix: list[list[int]] = []
    for row in material_name_matrix:
        matrix.append([material_name_to_result_code(material_name) for material_name in row])
    return matrix


def row_index_to_layer_number(row_index: int, row_count: int) -> int:
    return row_count - row_index


def build_po_from_simulation_payload(candidate_payload: dict[str, Any]) -> list[list[int]]:
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


def save_result_files(
    candidate_payload: dict[str, Any],
    length_values: list[float],
    step_spatial_metadata: list[dict[str, Any]],
    output_dir: Path,
) -> None:
    material_name_matrix = candidate_payload.get("simulation_material_name_matrix")
    if not isinstance(material_name_matrix, list) or not material_name_matrix:
        raise ValueError("simulation_material_name_matrix was not found in candidate simulation payload.")

    matrix = convert_material_names_to_codes(material_name_matrix)
    po = build_po_from_simulation_payload(candidate_payload)
    if len(length_values) != len(matrix[0]):
        raise ValueError(
            f"Length/matrix column mismatch: length has {len(length_values)} items, matrix has {len(matrix[0])} columns."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "length.txt").write_text(f"length = {pformat(length_values, width=120)}\n", encoding="utf-8")
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
                "length": length_values,
                "step_spatial_metadata": step_spatial_metadata,
                "matrix": matrix,
                "po": po,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def select_best_max_eta_candidate(results: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]], float]:
    if not results:
        raise ValueError("No candidate results found.")

    max_eta_sum = max(float(item.get("eta_sum", 0.0)) for item in results)
    max_eta_candidates = [
        item for item in results if abs(float(item.get("eta_sum", 0.0)) - max_eta_sum) <= 1e-12
    ]
    max_eta_candidates.sort(
        key=lambda item: (
            int(item.get("material_switch_count", 10**9)),
            -float(item.get("score", 0)),
            int(item.get("rank", 10**9)),
        )
    )
    return max_eta_candidates[0], max_eta_candidates, max_eta_sum


def save_simulation_bundle(candidate_item: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    candidate_dir = output_dir / "candidate_rank_0001"
    candidate_dir.mkdir(parents=True, exist_ok=True)

    simulation_payload = build_payload(
        candidate_item["binary_matrix"],
        candidate_item["material_name_matrix"],
    )
    simulation_payload["source_matrix_path"] = f"max_eta_original_candidate_rank_{int(candidate_item['rank']):04d}"
    simulation_payload["candidate_rank"] = 1
    simulation_payload["original_candidate_rank"] = int(candidate_item["rank"])
    simulation_payload["candidate_score"] = int(candidate_item["score"])
    simulation_payload["candidate_eta_sum"] = float(candidate_item["eta_sum"])
    simulation_payload["candidate_selected_case_keys"] = candidate_item["selected_case_keys"]

    json_path = candidate_dir / "candidate_rank_0001_simulation.json"
    txt_path = candidate_dir / "candidate_rank_0001_simulation.txt"
    png_path = candidate_dir / "candidate_rank_0001_simulation.png"
    gif_path = candidate_dir / "candidate_rank_0001_simulation.gif"
    material_names_json_path = candidate_dir / "candidate_rank_0001_material_name_matrix.json"

    json_path.write_text(json.dumps(simulation_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    txt_path.write_text(format_payload(simulation_payload), encoding="utf-8")
    material_names_json_path.write_text(
        json.dumps(
            {
                "original_candidate_rank": int(candidate_item["rank"]),
                "selected_case_keys": candidate_item["selected_case_keys"],
                "material_name_matrix": candidate_item["material_name_matrix"],
                "binary_matrix": candidate_item["binary_matrix"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    final_state = np.array(simulation_payload["final_state"], dtype=object)
    simulation_matrix = simulation_payload["simulation_matrix"]
    simulation_material_name_matrix = simulation_payload.get("simulation_material_name_matrix")
    save_final_stack_image(
        final_state,
        simulation_matrix,
        simulation_material_name_matrix,
        simulation_payload["prioritized_value"],
        simulation_payload["material_switch_events"],
        png_path,
    )
    save_stacking_animation(
        simulation_matrix,
        simulation_material_name_matrix,
        simulation_payload["simulation_events"],
        simulation_payload["prioritized_value"],
        simulation_payload["material_switch_events"],
        gif_path,
    )
    return simulation_payload


def save_summary(
    chosen_candidate: dict[str, Any],
    max_eta_candidates: list[dict[str, Any]],
    max_eta_sum: float,
    output_root: Path,
) -> None:
    payload = {
        "property_name": PROPERTY_NAME,
        "selection_rule": {
            "primary": "eta_sum descending",
            "secondary": "material_switch_count ascending within max eta candidates",
            "tie_breakers": [
                "score descending",
                "original rank ascending",
            ],
        },
        "max_eta_sum": max_eta_sum,
        "max_eta_candidate_count": len(max_eta_candidates),
        "chosen_candidate": {
            "original_rank": int(chosen_candidate["rank"]),
            "score": int(chosen_candidate["score"]),
            "eta_sum": float(chosen_candidate["eta_sum"]),
            "material_switch_count": int(chosen_candidate["material_switch_count"]),
            "selected_case_keys": chosen_candidate["selected_case_keys"],
        },
        "all_max_eta_candidates": [
            {
                "original_rank": int(item["rank"]),
                "score": int(item["score"]),
                "eta_sum": float(item["eta_sum"]),
                "material_switch_count": int(item["material_switch_count"]),
            }
            for item in max_eta_candidates
        ],
    }
    (output_root / "max_eta_selection_summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_root / "max_eta_selection_summary.txt").write_text(
        "\n".join(
            [
                f"property_name: {PROPERTY_NAME}",
                "selection_rule: eta_sum descending, then material_switch_count ascending",
                "tie_breakers: score descending, original rank ascending",
                f"max_eta_sum: {max_eta_sum:.6f}",
                f"max_eta_candidate_count: {len(max_eta_candidates)}",
                f"chosen_original_rank: {int(chosen_candidate['rank'])}",
                f"chosen_score: {int(chosen_candidate['score'])}",
                f"chosen_eta_sum: {float(chosen_candidate['eta_sum']):.6f}",
                f"chosen_material_switch_count: {int(chosen_candidate['material_switch_count'])}",
                "chosen_selected_case_keys: " + ", ".join(chosen_candidate["selected_case_keys"]),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    ranked_payload = load_json(RANKED_JSON_PATH)
    results = ranked_payload.get("results", [])
    if not isinstance(results, list):
        raise ValueError(f"'results' list was not found in {RANKED_JSON_PATH}")

    chosen_candidate, max_eta_candidates, max_eta_sum = select_best_max_eta_candidate(results)
    length_payload = load_json(LENGTH_MATRIX_PATH)
    length_values = length_payload.get("length_matrix")
    if not isinstance(length_values, list):
        raise ValueError(f"'length_matrix' list was not found in {LENGTH_MATRIX_PATH}")
    length_values = [float(value) for value in length_values]
    step_spatial_metadata = build_step_spatial_metadata_from_length_payload(length_payload)

    if OUTPUT_ROOT.exists():
        shutil.rmtree(OUTPUT_ROOT)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    save_summary(chosen_candidate, max_eta_candidates, max_eta_sum, OUTPUT_ROOT)
    simulation_payload = save_simulation_bundle(chosen_candidate, OUTPUT_ROOT / "simulation" / "candidate_simulations")
    save_result_files(
        simulation_payload,
        length_values,
        step_spatial_metadata,
        OUTPUT_ROOT / "result" / "candidate_rank_0001",
    )

    (OUTPUT_ROOT / "result" / "length.txt").write_text(
        (OUTPUT_ROOT / "result" / "candidate_rank_0001" / "length.txt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (OUTPUT_ROOT / "result" / "matrix.txt").write_text(
        (OUTPUT_ROOT / "result" / "candidate_rank_0001" / "matrix.txt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (OUTPUT_ROOT / "result" / "po.txt").write_text(
        (OUTPUT_ROOT / "result" / "candidate_rank_0001" / "po.txt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (OUTPUT_ROOT / "result" / "result.json").write_text(
        (OUTPUT_ROOT / "result" / "candidate_rank_0001" / "result.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    print("")
    print("[DONE] Exported max-eta pattern ignoring material switches")
    print(f"  property: {PROPERTY_NAME}")
    print(f"  max_eta_sum: {max_eta_sum:.6f}")
    print(f"  chosen original rank: {int(chosen_candidate['rank'])}")
    print(f"  output: {OUTPUT_ROOT}")


if __name__ == "__main__":
    main()
