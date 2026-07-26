from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from pathlib import Path
import json
import os

import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap

KNOWN_MATERIALS = {"PLA", "CPLA", "TPU", "PETG", "SMP", "CYAN", "MAGENTA", "YELLOW", "WHITE", "BLACK"}
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


SOURCE_MATRIX_PATH = Path("test_sample/derived/matrices/assignment_candidate_matrix_max_eta.json")
MATERIAL_DICTIONARY_PATH = Path(os.environ.get("B_FDM_MATERIAL_DICTIONARY_PATH", "input/config/material_dictionary.json"))
PROPERTY_PROGRAM_PATH = Path("input/config/Property_sample.json")
OUTPUT_JSON_PATH = Path("test_sample/derived/continuity/best_pattern_continuity_first.json")
OUTPUT_TXT_PATH = Path("test_sample/derived/continuity/best_pattern_continuity_first.txt")
OUTPUT_PNG_PATH = Path("test_sample/derived/continuity/best_pattern_continuity_first.png")
TOP_K = 20


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


def normalize_material_name(name: str) -> str:
    normalized = str(name).strip().upper()
    return normalized if normalized in KNOWN_MATERIALS else "Other"


def material_abbreviation(name: str) -> str:
    abbreviations = {
        "PLA": "P",
        "CPLA": "CP",
        "TPU": "T",
        "PETG": "PG",
        "SMP": "S",
        "CYAN": "C",
        "MAGENTA": "M",
        "YELLOW": "Y",
        "WHITE": "W",
        "BLACK": "B",
        "Other": "O",
    }
    return abbreviations.get(name, "O")


def build_assignment_lookup(property_program: dict) -> dict[int, dict[str, str]]:
    lookup: dict[int, dict[str, str]] = {}
    for assignment in property_program.get("assignments", []):
        assignment_index = int(assignment["assignment_index"])
        lookup[assignment_index] = {
            "material_start": normalize_material_name(str(assignment["material_start"])),
            "material_end": normalize_material_name(str(assignment["material_end"])),
        }
    return lookup


def build_case_lookup(material_dictionary: dict[str, dict[str, object]]) -> dict[str, list[str]]:
    case_lookup: dict[str, list[str]] = {}
    for case_key, case_info in material_dictionary.items():
        case_lookup[case_key] = [str(row) for row in case_info["case_rows"]]
    return case_lookup


def build_step_options(
    candidate_matrix: list[dict[str, object]],
    assignment_lookup: dict[int, dict[str, str]],
    case_lookup: dict[str, list[str]],
) -> list[list[dict[str, object]]]:
    # We materialize the full 14-row matrix for each candidate once.
    step_options: list[list[dict[str, object]]] = []
    for cell in candidate_matrix:
        assignment_index = int(cell["assignment_index"])
        assignment_info = assignment_lookup[assignment_index]
        options: list[dict[str, object]] = []

        for candidate in cell.get("candidates", []):
            case_key = str(candidate["case_key"])
            case_rows = case_lookup[case_key]
            material_rows = materialize_case_rows(
                case_rows,
                assignment_info["material_start"],
                assignment_info["material_end"],
            )
            options.append(
                {
                    "case_key": case_key,
                    "rows": material_rows,
                    "eta": float(candidate["eta"]),
                    "material_start_ratio": float(candidate["material_start_ratio"]),
                    "material_end_ratio": float(candidate["material_end_ratio"]),
                }
            )
        step_options.append(options)

    return step_options


def count_horizontal_groups(matrix: list[list[str]]) -> int:
    # A "group" is one contiguous horizontal run of the same material.
    group_count = 0
    for row in matrix:
        if not row:
            continue
        group_count += 1
        for left, right in zip(row, row[1:]):
            if left != right:
                group_count += 1
    return group_count


def count_material_components(matrix: list[list[str]]) -> int:
    # Count connected components in the full 2D 14 x step grid.
    if not matrix or not matrix[0]:
        return 0

    row_count = len(matrix)
    col_count = len(matrix[0])
    visited: set[tuple[int, int]] = set()
    component_count = 0

    for row_index in range(row_count):
        for col_index in range(col_count):
            if (row_index, col_index) in visited:
                continue

            component_count += 1
            target_material = matrix[row_index][col_index]
            stack = [(row_index, col_index)]
            visited.add((row_index, col_index))

            while stack:
                current_row, current_col = stack.pop()
                for next_row, next_col in (
                    (current_row - 1, current_col),
                    (current_row + 1, current_col),
                    (current_row, current_col - 1),
                    (current_row, current_col + 1),
                ):
                    if not (0 <= next_row < row_count and 0 <= next_col < col_count):
                        continue
                    if (next_row, next_col) in visited:
                        continue
                    if matrix[next_row][next_col] != target_material:
                        continue
                    visited.add((next_row, next_col))
                    stack.append((next_row, next_col))

    return component_count


def count_assignment_row_groups(
    selected_rows_per_step: list[list[str]],
    step_assignment_indices: list[int],
) -> tuple[int, list[dict[str, int]]]:
    if not selected_rows_per_step or not selected_rows_per_step[0]:
        return 0, []

    row_count = len(selected_rows_per_step[0])
    total_group_count = 0
    breakdown: list[dict[str, int]] = []

    block_start = 0
    while block_start < len(step_assignment_indices):
        assignment_index = step_assignment_indices[block_start]
        block_end = block_start
        while block_end < len(step_assignment_indices) and step_assignment_indices[block_end] == assignment_index:
            block_end += 1

        block_matrix: list[list[str]] = []
        for row_index in range(row_count):
            block_matrix.append(
                [selected_rows_per_step[step_index][row_index] for step_index in range(block_start, block_end)]
            )
        for row in block_matrix:
            total_group_count += count_row_groups(row)
        breakdown.append(
            {
                "assignment_index": assignment_index,
                "assignment_row_group_count": sum(count_row_groups(row) for row in block_matrix),
            }
        )
        block_start = block_end

    return total_group_count, breakdown


def longest_run_length(row: list[str]) -> int:
    if not row:
        return 0
    best = 1
    current = 1
    for left, right in zip(row, row[1:]):
        if left == right:
            current += 1
        else:
            best = max(best, current)
            current = 1
    return max(best, current)


def count_row_groups(row: list[str]) -> int:
    if not row:
        return 0
    group_count = 1
    for left, right in zip(row, row[1:]):
        if left != right:
            group_count += 1
    return group_count


def get_start_material(gravity_rows: list[list[str]]) -> str:
    if not gravity_rows or not gravity_rows[0]:
        return ""
    return str(gravity_rows[0][-1])


def simulate_material_restarts(
    gravity_rows: list[list[str]],
) -> tuple[int, list[int], list[int]]:
    # Restart logic is disabled in this step. We keep the function for
    # compatibility, but it now returns a zero-cost placeholder.
    return 0, [], []


@dataclass
class PatternScore:
    # 1) Prefer the fewest same-material connected components in the full 2D grid.
    material_component_count: int
    # 2) Prefer the fewest horizontal material groups inside each assignment block.
    assignment_row_group_count: int
    # Optional tie-breakers kept for reporting only.
    first_layer_dominant_ratio: float
    row_group_count: int
    support_restart_count: int
    support_reset_penalty: int
    horizontal_transition_count: int
    dominant_ratio_variation: float
    run_score: int
    support_score: float
    early_switch_penalty: int

    def sort_key(self) -> tuple:
        return (self.material_component_count, self.assignment_row_group_count)


def score_pattern(
    selected_rows_per_step: list[list[str]],
    step_assignment_indices: list[int],
) -> tuple[PatternScore, list[list[str]], list[dict[str, object]], list[int], list[dict[str, int]]]:
    row_count = len(selected_rows_per_step[0])
    step_count = len(selected_rows_per_step)

    gravity_rows: list[list[str]] = []
    for gravity_row_index in range(row_count):
        original_row_index = row_count - 1 - gravity_row_index
        gravity_rows.append(
            [selected_rows_per_step[step_index][original_row_index] for step_index in range(step_count)]
        )

    assignment_row_group_count, assignment_row_group_breakdown = count_assignment_row_groups(
        selected_rows_per_step,
        step_assignment_indices,
    )
    material_component_count = count_material_components(selected_rows_per_step)
    horizontal_transition_count = 0
    run_score = 0
    support_score = 0.0
    row_group_count = 0
    support_reset_penalty = 0
    dominant_ratios: list[float] = []
    dominant_materials: list[str] = []
    row_summaries: list[dict[str, object]] = []
    first_layer_dominant_ratio = 0.0

    for gravity_row_index, row_values in enumerate(gravity_rows):
        counts: dict[str, int] = {}
        for value in row_values:
            counts[value] = counts.get(value, 0) + 1

        dominant_material, dominant_count = max(counts.items(), key=lambda item: (item[1], item[0]))
        dominant_ratio = dominant_count / len(row_values)
        layer_weight = row_count - gravity_row_index
        support_prefix_length = step_count

        if gravity_row_index > 0:
            below_row = gravity_rows[gravity_row_index - 1]
            for col_index, value in enumerate(row_values):
                if value != below_row[col_index]:
                    support_prefix_length = col_index
                    break
        if support_prefix_length < step_count:
            support_reset_penalty += (step_count - support_prefix_length) * layer_weight

        dominant_ratios.append(dominant_ratio)
        dominant_materials.append(dominant_material)
        row_group_count += count_row_groups(row_values)
        horizontal_transition_count += sum(1 for left, right in zip(row_values, row_values[1:]) if left != right)
        run_score += longest_run_length(row_values) * layer_weight
        support_score += dominant_ratio * layer_weight
        if gravity_row_index == 0:
            first_layer_dominant_ratio = dominant_ratio

        row_summaries.append(
            {
                "layer_index_from_bottom": gravity_row_index + 1,
                "dominant_material": dominant_material,
                "dominant_ratio": dominant_ratio,
                "layer_weight": layer_weight,
                "row_group_count": count_row_groups(row_values),
                "support_prefix_length": support_prefix_length,
                "support_reset_suffix": step_count - support_prefix_length,
                "support_restart": support_prefix_length < step_count,
                "horizontal_transitions": sum(1 for left, right in zip(row_values, row_values[1:]) if left != right),
            }
        )

    dominant_ratio_variation = sum(abs(a - b) for a, b in zip(dominant_ratios, dominant_ratios[1:]))

    support_restart_count, support_restart_layers_from_bottom, support_restart_columns_from_left = 0, [], []

    score = PatternScore(
        assignment_row_group_count=assignment_row_group_count,
        material_component_count=material_component_count,
        first_layer_dominant_ratio=round(first_layer_dominant_ratio, 6),
        row_group_count=row_group_count,
        support_restart_count=support_restart_count,
        support_reset_penalty=support_reset_penalty,
        horizontal_transition_count=horizontal_transition_count,
        dominant_ratio_variation=round(dominant_ratio_variation, 6),
        run_score=run_score,
        support_score=round(support_score, 6),
        early_switch_penalty=0,
    )
    return (
        score,
        gravity_rows,
        row_summaries,
        support_restart_layers_from_bottom,
        support_restart_columns_from_left,
        assignment_row_group_breakdown,
    )


def build_selected_case_keys(selected_indices: tuple[int, ...], step_options: list[list[dict[str, object]]]) -> list[str]:
    return [str(step_options[step_index][option_index]["case_key"]) for step_index, option_index in enumerate(selected_indices)]


def format_matrix_text(matrix: list[list[str]]) -> str:
    lines: list[str] = []
    for row_index, row in enumerate(matrix, start=1):
        lines.append(f"row_{row_index:02d}: " + " ".join(row))
    return "\n".join(lines)


def build_numeric_matrix(material_name_matrix: list[list[object]]) -> tuple[list[list[int]], list[str]]:
    ordered_materials: list[str] = []
    for row in material_name_matrix:
        for value in row:
            normalized = normalize_material_name(str(value))
            if normalized not in ordered_materials:
                ordered_materials.append(normalized)
    numeric_matrix = [
        [ordered_materials.index(normalize_material_name(str(value))) for value in row]
        for row in material_name_matrix
    ]
    return numeric_matrix, ordered_materials


def save_visualization(best_pattern: dict[str, object]) -> None:
    matrix = best_pattern["material_name_matrix"]
    numeric_matrix, ordered_materials = build_numeric_matrix(matrix)
    row_summaries = best_pattern.get("row_summaries", [])
    support_restart_layers = best_pattern.get("support_restart_layers_from_bottom", [])
    support_restart_columns = best_pattern.get("support_restart_columns_from_left", [])
    row_count = len(matrix)

    fig, (ax_heat, ax_text) = plt.subplots(
        1,
        2,
        figsize=(18, 10),
        gridspec_kw={"width_ratios": [1.35, 0.75]},
        constrained_layout=True,
    )

    cmap = ListedColormap([MATERIAL_COLORS.get(name, MATERIAL_COLORS["Other"]) for name in ordered_materials])
    im = ax_heat.imshow(
        numeric_matrix,
        aspect="auto",
        cmap=cmap,
        interpolation="nearest",
        vmin=-0.5,
        vmax=len(ordered_materials) - 0.5,
    )
    ax_heat.set_title("Best Continuity-First Pattern", fontsize=14, weight="bold")
    ax_heat.set_xlabel("Step Index")
    ax_heat.set_ylabel("Layer Index")
    ax_heat.set_xticks(range(len(matrix[0])))
    ax_heat.set_xticklabels([str(i) for i in range(1, len(matrix[0]) + 1)])
    ax_heat.set_yticks(range(len(matrix)))
    ax_heat.set_yticklabels([str(i) for i in range(1, len(matrix) + 1)])

    cbar = fig.colorbar(im, ax=ax_heat, fraction=0.046, pad=0.04, ticks=range(len(ordered_materials)))
    cbar.ax.set_yticklabels(ordered_materials)

    for y, row in enumerate(matrix):
        for x, value in enumerate(row):
            ax_heat.text(
                x,
                y,
                material_abbreviation(normalize_material_name(str(value))),
                ha="center",
                va="center",
                fontsize=7,
                color="black" if normalize_material_name(str(value)) in {"WHITE", "YELLOW"} else "white",
            )

    restart_labels: list[str] = []
    for restart_index, (layer_from_bottom, column_from_left) in enumerate(
        zip(support_restart_layers, support_restart_columns),
        start=1,
    ):
        row_index = row_count - layer_from_bottom
        col_index = column_from_left - 1
        ax_heat.scatter(
            col_index,
            row_index,
            marker="x",
            s=180,
            color="#2ca02c",
            linewidths=3.0,
            zorder=5,
        )
        ax_heat.annotate(
            str(restart_index),
            (col_index, row_index),
            textcoords="offset points",
            xytext=(10, -12),
            fontsize=10,
            fontweight="bold",
            color="white",
            ha="center",
            va="center",
            bbox=dict(boxstyle="circle,pad=0.25", fc="#2ca02c", ec="white", lw=1.5),
        )
        restart_labels.append(f"{restart_index}: L{layer_from_bottom}/C{column_from_left}")

    ax_text.axis("off")
    score = best_pattern["score"]
    summary_lines = [
        f"pattern_index: {best_pattern['pattern_index']}",
        f"support_restart_count: {score.get('support_restart_count')}",
        f"material_component_count: {score.get('material_component_count', score.get('horizontal_group_count'))}",
        f"support_restart_layers_from_bottom: {best_pattern.get('support_restart_layers_from_bottom', [])}",
        f"support_restart_columns_from_left: {best_pattern.get('support_restart_columns_from_left', [])}",
        f"horizontal_transition_count: {score['horizontal_transition_count']}",
        f"dominant_ratio_variation: {score['dominant_ratio_variation']}",
        f"run_score: {score['run_score']}",
        f"support_score: {score['support_score']}",
        f"early_switch_penalty: {score['early_switch_penalty']}",
        "",
        "restart_map:",
    ]
    summary_lines.append("legend: green X + number = material restart point")
    summary_lines.extend(f"- {label}" for label in restart_labels)
    summary_lines.extend([
        "",
        "row_group_counts:",
    ])
    for row_summary, row in zip(row_summaries, matrix):
        horizontal_groups = 1
        for left, right in zip(row, row[1:]):
            if left != right:
                horizontal_groups += 1
        summary_lines.append(
            f"- layer {row_summary.get('layer_index_from_bottom', '?'):>2}: "
            f"groups={horizontal_groups}, transitions={row_summary.get('horizontal_transitions', '?')}, "
            f"dominant={row_summary.get('dominant_material', '?')}, "
            f"ratio={row_summary.get('dominant_ratio', '?')}"
        )
    summary_lines.extend([
        "",
        "selected_case_keys:",
    ])
    summary_lines.extend(f"- {case_key}" for case_key in best_pattern["selected_case_keys"])
    ax_text.text(
        0.0,
        1.0,
        "\n".join(summary_lines),
        va="top",
        ha="left",
        family="monospace",
        fontsize=10,
    )

    fig.suptitle("Best Continuity-First Pattern Visualization", fontsize=18, weight="bold")
    fig.savefig(OUTPUT_PNG_PATH, dpi=200, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    property_program = load_json(PROPERTY_PROGRAM_PATH)
    material_dictionary = load_json(MATERIAL_DICTIONARY_PATH)
    payload = load_json(SOURCE_MATRIX_PATH)

    assignment_lookup = build_assignment_lookup(property_program)
    case_lookup = build_case_lookup(material_dictionary)
    candidate_matrix = payload.get("candidate_matrix", [])
    if not candidate_matrix:
        raise ValueError("candidate_matrix is empty")
    step_assignment_indices = [int(cell["assignment_index"]) for cell in candidate_matrix]

    step_options = build_step_options(candidate_matrix, assignment_lookup, case_lookup)
    step_counts = [len(options) for options in step_options]
    total_pattern_count = 1
    for count in step_counts:
        total_pattern_count *= count

    best_result: dict[str, object] | None = None
    ranking: list[dict[str, object]] = []

    for pattern_index, selected_indices in enumerate(product(*[range(count) for count in step_counts]), start=1):
        selected_rows_per_step = [
            step_options[step_index][option_index]["rows"]
            for step_index, option_index in enumerate(selected_indices)
        ]
        (
            score,
            gravity_rows,
            row_summaries,
            support_restart_layers_from_bottom,
            support_restart_columns_from_left,
            assignment_row_group_breakdown,
        ) = score_pattern(selected_rows_per_step, step_assignment_indices)
        selected_case_keys = build_selected_case_keys(selected_indices, step_options)
        material_matrix = list(reversed(gravity_rows))
        start_material = get_start_material(gravity_rows)

        result = {
            "pattern_index": pattern_index - 1,
            "selected_case_keys": selected_case_keys,
            "start_material": start_material,
            "score": {
                "assignment_row_group_count": score.assignment_row_group_count,
                "material_component_count": score.material_component_count,
                "first_layer_dominant_ratio": score.first_layer_dominant_ratio,
                "row_group_count": score.row_group_count,
                "support_restart_count": score.support_restart_count,
                "support_reset_penalty": score.support_reset_penalty,
                "horizontal_transition_count": score.horizontal_transition_count,
                "dominant_ratio_variation": score.dominant_ratio_variation,
                "run_score": score.run_score,
                "support_score": score.support_score,
                "early_switch_penalty": score.early_switch_penalty,
            },
            "row_summaries": row_summaries,
            "support_restart_layers_from_bottom": support_restart_layers_from_bottom,
            "support_restart_columns_from_left": support_restart_columns_from_left,
            "assignment_row_group_breakdown": assignment_row_group_breakdown,
            "material_name_matrix": material_matrix,
            "sort_key": score.sort_key(),
        }
        ranking.append(result)

        if best_result is None or result["sort_key"] < best_result["sort_key"]:
            best_result = result

        if pattern_index % 50000 == 0:
            print(f"Evaluated {pattern_index}/{total_pattern_count}")

    if best_result is None:
        raise RuntimeError("No patterns were evaluated")

    ranking.sort(key=lambda item: item["sort_key"])

    report = {
        "source_matrix_path": str(SOURCE_MATRIX_PATH),
        "total_pattern_count": total_pattern_count,
        "step_counts": step_counts,
        "best_pattern": best_result,
        "top_patterns": ranking[:TOP_K],
    }

    OUTPUT_JSON_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    text_lines: list[str] = []
    text_lines.append(f"source_matrix_path: {report['source_matrix_path']}")
    text_lines.append(f"total_pattern_count: {report['total_pattern_count']}")
    text_lines.append(f"step_counts: {step_counts}")
    text_lines.append(f"full_matrix_pattern_count: {total_pattern_count}")
    text_lines.append("")
    text_lines.append("best_pattern:")
    text_lines.append(
        "  pattern_index: {pattern_index}\n"
        "  start_material: {start_material}\n"
        "  selected_case_keys: {selected_case_keys}\n"
        "  score: {score}\n"
        "  assignment_row_group_breakdown: {assignment_row_group_breakdown}\n"
        "  support_restart_layers_from_bottom: {support_restart_layers_from_bottom}\n"
        "  support_restart_columns_from_left: {support_restart_columns_from_left}\n"
        "  material_name_matrix:\n{matrix}".format(
            pattern_index=best_result["pattern_index"],
            start_material=best_result.get("start_material", ""),
            selected_case_keys=", ".join(best_result["selected_case_keys"]),
            score=best_result["score"],
            assignment_row_group_breakdown=best_result.get("assignment_row_group_breakdown", []),
            support_restart_layers_from_bottom=best_result.get("support_restart_layers_from_bottom", []),
            support_restart_columns_from_left=best_result.get("support_restart_columns_from_left", []),
            matrix="\n".join("    " + line for line in format_matrix_text(best_result["material_name_matrix"]).splitlines()),
        )
    )
    text_lines.append("")
    text_lines.append("top_patterns:")
    for rank, item in enumerate(ranking[:TOP_K], start=1):
        text_lines.append(
            "start_material {start_material} | "
            "assignment_row_group_count {assignment_row_group_count} | "
            "support_restart_count {support_restart_count} | "
            "material_component_count {material_component_count} | "
            "first_layer_dominant_ratio {first_layer_dominant_ratio:.6f} | "
            "row_group_count {row_group_count} | "
            "support_reset_penalty {support_reset_penalty} | "
            "horizontal_transition_count {horizontal_transition_count} | "
            "dominant_ratio_variation {dominant_ratio_variation:.6f} | run_score {run_score} | "
            "support_score {support_score:.6f}".format(
                rank=rank,
            pattern_index=item["pattern_index"],
            start_material=item.get("start_material", ""),
            assignment_row_group_count=item["score"].get("assignment_row_group_count", 0),
            support_restart_count=item["score"]["support_restart_count"],
            material_component_count=item["score"]["material_component_count"],
            first_layer_dominant_ratio=item["score"]["first_layer_dominant_ratio"],
            row_group_count=item["score"]["row_group_count"],
            support_reset_penalty=item["score"]["support_reset_penalty"],
                horizontal_transition_count=item["score"]["horizontal_transition_count"],
                dominant_ratio_variation=item["score"]["dominant_ratio_variation"],
                run_score=item["score"]["run_score"],
                support_score=item["score"]["support_score"],
            )
        )

    OUTPUT_TXT_PATH.write_text("\n".join(text_lines), encoding="utf-8")
    save_visualization(best_result)

    print(f"Total pattern count: {total_pattern_count}")
    print(f"Best pattern index: {best_result['pattern_index']}")
    print(f"Saved JSON to: {OUTPUT_JSON_PATH}")
    print(f"Saved TXT to: {OUTPUT_TXT_PATH}")
    print(f"Saved PNG to: {OUTPUT_PNG_PATH}")


if __name__ == "__main__":
    main()
