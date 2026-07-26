from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from tqdm import tqdm

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.simulation.simulate_matrix_deposition import build_payload
from scripts.utils.property_program_utils import (
    get_assignments_in_spatial_order,
    get_effective_gradient_steps,
    resolve_property_program_path,
    resolve_assignment_material_pair,
)


SOURCE_CANDIDATES_PATH = Path("test_sample/derived/adjacency/beam_step_adjacency_clusters_best.txt")
MATERIAL_DICTIONARY_PATH = Path(os.environ.get("B_FDM_MATERIAL_DICTIONARY_PATH", "input/config/material_dictionary.json"))
PROPERTY_PROGRAM_PATH = resolve_property_program_path()
OUTPUT_JSON_PATH = Path("test_sample/derived/simulation/beam_step_adjacency_clusters_best_material_switches.json")
OUTPUT_TXT_PATH = Path("test_sample/derived/simulation/beam_step_adjacency_clusters_best_material_switches.txt")

BEST_RE = re.compile(
    r"^(?P<rank>\d+)\.\s+score\s+(?P<score>\d+)"
    r"(?:\s+\|\s+eta_sum\s+(?P<eta_sum>[0-9.]+))?"
    r"(?:\s+\|\s+material_switch_count\s+(?P<material_switch_count>\d+))?"
    r"\s+\|\s+step_scores\s+\[(?P<step_scores>[^\]]*)\]\s+\|\s+"
    r"selected_case_keys\s+(?P<keys>.*)\s*$"
)


def load_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_text_lines(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Text file not found: {path}")
    return path.read_text(encoding="utf-8-sig").splitlines()


def parse_best_candidates(lines: list[str]) -> list[dict[str, object]]:
    in_best_candidates = False
    candidates: list[dict[str, object]] = []

    for line in lines:
        if line.strip() == "best_candidates:":
            in_best_candidates = True
            continue
        if not in_best_candidates:
            continue

        match = BEST_RE.match(line.strip())
        if not match:
            continue

        step_scores_raw = match.group("step_scores").strip()
        step_scores = [int(item.strip()) for item in step_scores_raw.split(",") if item.strip()] if step_scores_raw else []
        selected_case_keys = [item.strip() for item in match.group("keys").split(",") if item.strip()]
        candidates.append(
            {
                "rank": int(match.group("rank")),
                "score": int(match.group("score")),
                "step_scores": step_scores,
                "selected_case_keys": selected_case_keys,
            }
        )

    if not candidates:
        raise ValueError(f"No best candidates found in {SOURCE_CANDIDATES_PATH}")

    return candidates


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
        start_material = start_material or "Other"
        end_material = end_material or start_material
        gradient_steps = get_effective_gradient_steps(property_program, assignment)
        for _ in range(gradient_steps):
            step_pairs.append((start_material, end_material))
    return step_pairs


def build_matrix_from_selection(selected_case_keys: list[str], case_lookup: dict[str, list[str]]) -> list[list[str]]:
    selected_rows_per_step = [case_lookup[case_key] for case_key in selected_case_keys]
    row_count = len(selected_rows_per_step[0])
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
            if label == "Material_start":
                material_name_row.append(start_material)
            elif label == "Material_end":
                material_name_row.append(end_material)
            elif label == "White":
                material_name_row.append("WHITE")
            else:
                raise ValueError(f"Unknown material label: {label}")
        material_name_matrix.append(material_name_row)
    return material_name_matrix


def reverse_matrix_steps(matrix: list[list[object]]) -> list[list[object]]:
    return [list(reversed(row)) for row in matrix]


def iter_matching_cols_right_to_left(row: list[object], target_value: object) -> list[int]:
    return [col_index for col_index in range(len(row) - 1, -1, -1) if row[col_index] == target_value]


def find_next_pending_material(
    matrix: list[list[object]],
    state: list[list[object | None]],
    exclude_value: object,
    preferred_row_index: int | None = None,
) -> object | None:
    row_indices: list[int] = []
    if preferred_row_index is not None:
        row_indices.append(preferred_row_index)
    row_indices.extend(
        row_index for row_index in range(len(matrix) - 1, -1, -1) if row_index != preferred_row_index
    )
    for row_index in row_indices:
        for col_index in range(len(matrix[row_index]) - 1, -1, -1):
            if state[row_index][col_index] is not None:
                continue
            candidate_value = matrix[row_index][col_index]
            if candidate_value == exclude_value:
                continue
            return candidate_value
    return None


def simulate_material_switches(material_name_matrix: list[list[str]]) -> tuple[int, list[dict[str, object]]]:
    # Use the same simulation engine as the final candidate PNG/GIF generation.
    # Keeping a separate copy here caused ranking-time switch counts to diverge
    # and, for some ratio-driven candidates, hit the safety-loop guard.
    binary_placeholder = [[0 for _ in row] for row in material_name_matrix]
    try:
        payload = build_payload(binary_placeholder, material_name_matrix)
    except RuntimeError as exc:
        if "safety loop" not in str(exc):
            raise
        return 10**9, []
    return int(payload["material_switch_count"]), payload["material_switch_events"]


def build_summary(results: list[dict[str, object]]) -> dict[str, object]:
    counter = Counter(int(item["material_switch_count"]) for item in results)
    min_switch_count = min(counter) if counter else 0
    best_ranks = [int(item["rank"]) for item in results if int(item["material_switch_count"]) == min_switch_count]
    return {
        "candidate_count": len(results),
        "min_material_switch_count": min_switch_count,
        "min_count_candidate_ranks": best_ranks,
        "switch_count_histogram": dict(sorted(counter.items())),
    }


def format_report(payload: dict[str, object]) -> str:
    lines: list[str] = []
    lines.append(f"source_candidates_path: {payload['source_candidates_path']}")
    lines.append(f"candidate_count: {payload['summary']['candidate_count']}")
    lines.append(f"min_material_switch_count: {payload['summary']['min_material_switch_count']}")
    lines.append(
        "min_count_candidate_ranks: "
        + ", ".join(str(item) for item in payload["summary"]["min_count_candidate_ranks"])
    )
    lines.append("switch_count_histogram:")
    for switch_count, freq in payload["summary"]["switch_count_histogram"].items():
        lines.append(f"- {switch_count}: {freq}")
    lines.append("")
    lines.append("candidate_results:")
    for item in payload["results"]:
        switch_events = item["material_switch_events"]
        if switch_events:
            switch_text = "; ".join(
                f"{event['switch_index']}@row{event['row_index']},col{event['trigger_col_index']}:{event['from_value']}->{event['to_value']}"
                for event in switch_events
            )
        else:
            switch_text = "(none)"
        lines.append(
            f"{item['rank']:04d}. score={item['score']} material_switch_count={item['material_switch_count']} "
            f"selected_case_keys={', '.join(item['selected_case_keys'])}"
        )
        lines.append(f"      switch_events={switch_text}")
    return "\n".join(lines) + "\n"


def main() -> None:
    property_program = load_json(PROPERTY_PROGRAM_PATH)
    material_dictionary = load_json(MATERIAL_DICTIONARY_PATH)
    case_lookup = build_case_lookup(material_dictionary)
    step_material_pairs = build_assignment_step_material_pairs(property_program)
    candidates = parse_best_candidates(load_text_lines(SOURCE_CANDIDATES_PATH))

    results: list[dict[str, object]] = []
    for candidate in tqdm(
        candidates,
        desc="Calculate material switches",
        unit="candidate",
    ):
        binary_matrix = build_matrix_from_selection(candidate["selected_case_keys"], case_lookup)
        material_name_matrix = build_material_name_matrix(binary_matrix, step_material_pairs)
        switch_count, switch_events = simulate_material_switches(material_name_matrix)
        results.append(
            {
                "rank": candidate["rank"],
                "score": candidate["score"],
                "step_scores": candidate["step_scores"],
                "selected_case_keys": candidate["selected_case_keys"],
                "material_switch_count": switch_count,
                "material_switch_events": switch_events,
            }
        )

    results.sort(key=lambda item: (int(item["material_switch_count"]), int(item["rank"])))
    payload = {
        "source_candidates_path": str(SOURCE_CANDIDATES_PATH),
        "summary": build_summary(results),
        "results": results,
    }

    OUTPUT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_TXT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    OUTPUT_TXT_PATH.write_text(format_report(payload), encoding="utf-8")

    print(f"Processed candidates: {len(candidates)}")
    print(f"Minimum material switches: {payload['summary']['min_material_switch_count']}")
    print(f"Saved JSON: {OUTPUT_JSON_PATH}")
    print(f"Saved TXT: {OUTPUT_TXT_PATH}")


if __name__ == "__main__":
    main()
