from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os


SOURCE_MATRIX_PATH = Path("test_sample/derived/matrices/assignment_candidate_matrix_max_eta.json")
MATERIAL_DICTIONARY_PATH = Path(os.environ.get("B_FDM_MATERIAL_DICTIONARY_PATH", "input/config/material_dictionary.json"))
PROPERTY_PROGRAM_PATH = Path("input/config/Property_sample.json")
OUTPUT_JSON_PATH = Path("test_sample/derived/continuity/representative_patterns.json")
OUTPUT_TXT_PATH = Path("test_sample/derived/continuity/representative_patterns.txt")

TARGET_COUNT = 100
BEAM_WIDTH = 256

ROW_WEIGHTS = [2, 3, 3, 4, 4, 4, 4, 4, 4, 4, 4, 3, 3, 2]


def load_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def materialize_case_rows(case_rows: list[str], start_material: str, end_material: str) -> list[str]:
    materialized: list[str] = []
    for label in case_rows:
        if label == "Material_start":
            materialized.append(start_material)
        elif label == "Material_end":
            materialized.append(end_material)
        elif label == "White":
            materialized.append("WHITE")
        else:
            raise ValueError(f"Unknown material label: {label}")
    return materialized


def weighted_similarity(rows_a: list[str], rows_b: list[str]) -> int:
    if len(rows_a) != len(rows_b):
        raise ValueError("Row length mismatch")
    return sum(weight for weight, a, b in zip(ROW_WEIGHTS, rows_a, rows_b) if a == b)


def build_assignment_lookup(property_program: dict) -> dict[int, dict[str, str]]:
    lookup: dict[int, dict[str, str]] = {}
    for assignment in property_program.get("assignments", []):
        assignment_index = int(assignment["assignment_index"])
        lookup[assignment_index] = {
            "material_start": str(assignment["material_start"]),
            "material_end": str(assignment["material_end"]),
        }
    return lookup


def build_case_lookup(material_dictionary: dict[str, dict[str, object]]) -> dict[str, list[str]]:
    case_lookup: dict[str, list[str]] = {}
    for case_key, case_info in material_dictionary.items():
        case_lookup[case_key] = [str(row) for row in case_info["case_rows"]]
    return case_lookup


@dataclass
class PatternState:
    selected_case_keys: list[str | None]
    selected_rows: list[list[str] | None]
    score: int = 0

    def clone_with(self, step_index: int, case_key: str, rows: list[str], incremental_score: int) -> "PatternState":
        new_selected_case_keys = list(self.selected_case_keys)
        new_selected_rows = list(self.selected_rows)
        new_selected_case_keys[step_index] = case_key
        new_selected_rows[step_index] = rows
        return PatternState(
            selected_case_keys=new_selected_case_keys,
            selected_rows=new_selected_rows,
            score=self.score + incremental_score,
        )

    def signature(self) -> tuple[str, ...]:
        return tuple(case_key or "" for case_key in self.selected_case_keys)


def build_step_material_rows(
    step_cell: dict[str, object],
    candidate: dict[str, object],
    assignment_lookup: dict[int, dict[str, str]],
    case_lookup: dict[str, list[str]],
) -> list[str]:
    assignment_index = int(step_cell["assignment_index"])
    assignment_info = assignment_lookup[assignment_index]
    case_key = str(candidate["case_key"])
    case_rows = case_lookup[case_key]
    return materialize_case_rows(case_rows, assignment_info["material_start"], assignment_info["material_end"])


def build_expansion_order(step_count: int, pivot_index: int) -> list[int]:
    order = [pivot_index]
    offset = 1
    while len(order) < step_count:
        left = pivot_index - offset
        right = pivot_index + offset
        if left >= 0:
            order.append(left)
        if right < step_count:
            order.append(right)
        offset += 1
    return order


def choose_top_candidates_for_step(
    step_index: int,
    candidate_matrix: list[dict[str, object]],
    state: PatternState,
    assignment_lookup: dict[int, dict[str, str]],
    case_lookup: dict[str, list[str]],
) -> list[tuple[int, str, list[str], int]]:
    cell = candidate_matrix[step_index]
    candidates = cell.get("candidates", [])
    scored_candidates: list[tuple[int, str, list[str], int]] = []

    for candidate in candidates:
        rows = build_step_material_rows(cell, candidate, assignment_lookup, case_lookup)
        local_score = 0
        if step_index - 1 >= 0 and state.selected_rows[step_index - 1] is not None:
            local_score += weighted_similarity(rows, state.selected_rows[step_index - 1])  # type: ignore[arg-type]
        if step_index + 1 < len(candidate_matrix) and state.selected_rows[step_index + 1] is not None:
            local_score += weighted_similarity(rows, state.selected_rows[step_index + 1])  # type: ignore[arg-type]
        local_score += int(round(float(candidate["eta"]) * 10))
        local_score += int(round(float(candidate["material_start_ratio"]) * 100))
        scored_candidates.append((local_score, str(candidate["case_key"]), rows, int(candidate["material_start_count"])))

    scored_candidates.sort(key=lambda item: (-item[0], item[1]))
    return scored_candidates[: min(BEAM_WIDTH, len(scored_candidates))]


def expand_states(
    states: list[PatternState],
    step_index: int,
    candidate_matrix: list[dict[str, object]],
    assignment_lookup: dict[int, dict[str, str]],
    case_lookup: dict[str, list[str]],
) -> list[PatternState]:
    expanded: list[PatternState] = []

    for state in states:
        top_candidates = choose_top_candidates_for_step(step_index, candidate_matrix, state, assignment_lookup, case_lookup)
        for local_score, case_key, rows, _ in top_candidates:
            cell = candidate_matrix[step_index]
            incremental_score = local_score
            new_state = state.clone_with(step_index, case_key, rows, incremental_score)
            expanded.append(new_state)

    expanded.sort(key=lambda item: (-item.score, item.signature()))

    unique_states: list[PatternState] = []
    seen: set[tuple[str, ...]] = set()
    for state in expanded:
        sig = state.signature()
        if sig in seen:
            continue
        seen.add(sig)
        unique_states.append(state)
        if len(unique_states) >= BEAM_WIDTH:
            break

    return unique_states


def compute_full_matrix(selected_rows: list[list[str]]) -> list[list[str | None]]:
    matrix: list[list[str | None]] = []
    for row_index in range(len(ROW_WEIGHTS)):
        row: list[str | None] = []
        for step_rows in selected_rows:
            if step_rows is None:
                row.append(None)
            else:
                row.append(step_rows[row_index])
        matrix.append(row)
    return matrix


def compute_pattern_metrics(selected_rows: list[list[str]]) -> dict[str, object]:
    matrix = compute_full_matrix(selected_rows)
    total_similarity = 0
    switch_count = 0
    dominant_ratio_score = 0.0

    for step_index in range(len(selected_rows) - 1):
        rows_a = selected_rows[step_index]
        rows_b = selected_rows[step_index + 1]
        if rows_a is None or rows_b is None:
            continue
        total_similarity += weighted_similarity(rows_a, rows_b)

    for row_index, row in enumerate(matrix):
        counts: dict[str, int] = {}
        for value in row:
            if value is None:
                continue
            counts[value] = counts.get(value, 0) + 1
        if counts:
            dominant_count = max(counts.values())
            dominant_ratio_score += dominant_count / len(row)

    for row_index in range(len(matrix)):
        previous_value = None
        for value in matrix[row_index]:
            if value is None:
                continue
            if previous_value is not None and value != previous_value:
                switch_count += 1
            previous_value = value

    return {
        "adjacency_similarity": total_similarity,
        "dominant_ratio_score": round(dominant_ratio_score, 6),
        "switch_count": switch_count,
    }


def format_matrix_text(matrix: list[list[str | None]]) -> str:
    lines: list[str] = []
    for row_index, row in enumerate(matrix, start=1):
        rendered = [value if value is not None else "." for value in row]
        lines.append(f"row_{row_index:02d}: " + " ".join(rendered))
    return "\n".join(lines)


def format_sample_text(sample: dict) -> str:
    lines: list[str] = []
    lines.append(
        "sample_rank: {rank} | score: {score} | adjacency_similarity: {adj} | "
        "dominant_ratio_score: {dom:.6f} | switch_count: {switch_count}".format(
            rank=int(sample["sample_rank"]),
            score=int(sample["score"]),
            adj=int(sample["metrics"]["adjacency_similarity"]),
            dom=float(sample["metrics"]["dominant_ratio_score"]),
            switch_count=int(sample["metrics"]["switch_count"]),
        )
    )
    lines.append("selected_case_keys: " + ", ".join(sample["selected_case_keys"]))
    lines.append("material_matrix:")
    lines.append(format_matrix_text(sample["material_name_matrix"]))
    return "\n".join(lines)


def main() -> None:
    property_program = load_json(PROPERTY_PROGRAM_PATH)
    material_dictionary = load_json(MATERIAL_DICTIONARY_PATH)
    payload = load_json(SOURCE_MATRIX_PATH)

    assignment_lookup = build_assignment_lookup(property_program)
    case_lookup = build_case_lookup(material_dictionary)
    candidate_matrix = payload.get("candidate_matrix", [])

    if not candidate_matrix:
        raise ValueError("candidate_matrix is empty")

    candidate_counts = [int(cell["candidate_count"]) for cell in candidate_matrix]
    pivot_index = min(
        range(len(candidate_counts)),
        key=lambda idx: (candidate_counts[idx], abs(idx - len(candidate_counts) // 2), idx),
    )
    if candidate_counts[pivot_index] <= 1:
        pivot_index = min(
            (idx for idx, count in enumerate(candidate_counts) if count > 1),
            key=lambda idx: (candidate_counts[idx], abs(idx - len(candidate_counts) // 2), idx),
            default=pivot_index,
        )

    expansion_order = build_expansion_order(len(candidate_matrix), pivot_index)
    pivot_candidates = candidate_matrix[pivot_index].get("candidates", [])
    initial_states: list[PatternState] = []
    for candidate in pivot_candidates:
        rows = build_step_material_rows(candidate_matrix[pivot_index], candidate, assignment_lookup, case_lookup)
        selected_case_keys = [None] * len(candidate_matrix)
        selected_rows = [None] * len(candidate_matrix)
        selected_case_keys[pivot_index] = str(candidate["case_key"])
        selected_rows[pivot_index] = rows
        initial_states.append(
            PatternState(
                selected_case_keys=selected_case_keys,
                selected_rows=selected_rows,
                score=int(round(float(candidate["eta"]) * 10)) + int(round(float(candidate["material_start_ratio"]) * 100)),
            )
        )

    if not initial_states:
        raise ValueError("No pivot candidates found")

    beam = initial_states
    for step_index in expansion_order[1:]:
        beam = expand_states(beam, step_index, candidate_matrix, assignment_lookup, case_lookup)

    final_samples: list[dict[str, object]] = []
    for sample_rank, state in enumerate(beam[:TARGET_COUNT], start=1):
        metrics = compute_pattern_metrics([rows if rows is not None else [] for rows in state.selected_rows])  # type: ignore[list-item]
        matrix = compute_full_matrix(state.selected_rows)
        final_samples.append(
            {
                "sample_rank": sample_rank,
                "score": state.score,
                "metrics": metrics,
                "selected_case_keys": [case_key or "" for case_key in state.selected_case_keys],
                "material_name_matrix": matrix,
            }
        )

    report = {
        "source_matrix_path": str(SOURCE_MATRIX_PATH),
        "pivot_index": pivot_index,
        "pivot_step": int(candidate_matrix[pivot_index]["global_step_index"]),
        "sample_count": len(final_samples),
        "samples": final_samples,
    }

    OUTPUT_JSON_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    OUTPUT_TXT_PATH.write_text(
        "\n\n".join(format_sample_text(sample) for sample in final_samples),
        encoding="utf-8",
    )

    print(f"Pivot step: {report['pivot_step']}")
    print(f"Generated samples: {report['sample_count']}")
    print(f"Saved JSON to: {OUTPUT_JSON_PATH}")
    print(f"Saved TXT to: {OUTPUT_TXT_PATH}")


if __name__ == "__main__":
    main()
