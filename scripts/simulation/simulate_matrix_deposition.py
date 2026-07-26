from __future__ import annotations

import ast
import json
import math
import os
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
from matplotlib import animation
from matplotlib.colors import ListedColormap


SOURCE_MATRIX_PATH = Path(
    os.environ.get(
        "SIM_SOURCE_MATRIX_PATH",
        "test_sample/derived/matrices/beam_step_adjacency_candidate01_matrix_only.txt",
    )
)
OUTPUT_JSON_PATH = Path(
    os.environ.get(
        "SIM_OUTPUT_JSON_PATH",
        "test_sample/derived/simulation/beam_step_adjacency_candidate01_simulation_pass1.json",
    )
)
OUTPUT_TXT_PATH = Path(
    os.environ.get(
        "SIM_OUTPUT_TXT_PATH",
        "test_sample/derived/simulation/beam_step_adjacency_candidate01_simulation_pass1.txt",
    )
)
OUTPUT_GIF_PATH = Path(
    os.environ.get(
        "SIM_OUTPUT_GIF_PATH",
        "test_sample/derived/simulation/beam_step_adjacency_candidate01_simulation_pass1.gif",
    )
)
OUTPUT_PNG_PATH = Path(
    os.environ.get(
        "SIM_OUTPUT_PNG_PATH",
        "test_sample/derived/simulation/beam_step_adjacency_candidate01_simulation_pass1.png",
    )
)
MATERIAL_NAME_MATRIX_PATH = os.environ.get("SIM_MATERIAL_NAME_MATRIX_PATH")
MATERIAL_COLORS = {
    "PLA": "#2563eb",
    "CPLA": "#f97316",
    "TPU": "#10b981",
    "PETG": "#8b5cf6",
    "SMP": "#ef4444",
    "CYAN": "#06b6d4",
    "MAGENTA": "#d946ef",
    "YELLOW": "#eab308",
    # Use a visible gray for WHITE in simulation images so it is not confused
    # with an empty/unfilled cell.
    "WHITE": "#cbd5e1",
    "BLACK": "#111827",
    "Other": "#9ca3af",
}
EMPTY_CELL_COLOR = "#ffffff"


def load_matrix_from_txt(path: Path) -> list[list[int]]:
    text = path.read_text(encoding="utf-8").strip()
    prefix = "matrix ="
    if not text.startswith(prefix):
        raise ValueError(f"Expected file to start with '{prefix}'")

    matrix_obj: Any = ast.literal_eval(text[len(prefix):].strip())
    if not isinstance(matrix_obj, list) or not matrix_obj:
        raise ValueError("Matrix must be a non-empty 2D list.")

    matrix: list[list[int]] = []
    expected_cols: int | None = None
    for row_index, row in enumerate(matrix_obj):
        if not isinstance(row, list) or not row:
            raise ValueError(f"Row {row_index} must be a non-empty list.")
        normalized_row: list[int] = []
        for value in row:
            if value not in (0, 1):
                raise ValueError(f"Only 0/1 values are supported, got {value!r}.")
            normalized_row.append(int(value))
        if expected_cols is None:
            expected_cols = len(normalized_row)
        elif len(normalized_row) != expected_cols:
            raise ValueError("All rows must have the same number of columns.")
        matrix.append(normalized_row)

    return matrix


def reverse_matrix_steps(matrix: list[list[int]]) -> list[list[int]]:
    return [list(reversed(row)) for row in matrix]


def load_material_name_matrix(path_text: str | None) -> list[list[str]] | None:
    if not path_text:
        return None
    path = Path(path_text)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    matrix = payload.get("material_name_matrix")
    if not isinstance(matrix, list):
        return None
    return [[str(value) for value in row] for row in matrix]


def material_abbreviation(name: str) -> str:
    if name == "REPEAT":
        return "..."
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


def build_right_to_left_same_value_runs(matrix: list[list[Any]]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    row_count = len(matrix)
    col_count = len(matrix[0])

    for row_index in range(row_count - 1, -1, -1):
        row = matrix[row_index]
        current_value = row[col_count - 1]
        run_right_col = col_count - 1

        for col_index in range(col_count - 2, -1, -1):
            if row[col_index] != current_value:
                runs.append(
                    {
                        "row_index": row_index,
                        "value": current_value,
                        "start_col": run_right_col,
                        "end_col": col_index + 1,
                        "direction": "right_to_left",
                    }
                )
                current_value = row[col_index]
                run_right_col = col_index

        runs.append(
            {
                "row_index": row_index,
                "value": current_value,
                "start_col": run_right_col,
                "end_col": 0,
                "direction": "right_to_left",
            }
        )

    return runs


def reorder_runs_same_color_first(
    matrix: list[list[Any]],
    runs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], Any]:
    start_value = matrix[-1][-1]
    prioritized_runs = [run for run in runs if run["value"] == start_value]
    deferred_runs = [run for run in runs if run["value"] != start_value]
    return prioritized_runs + deferred_runs, start_value


def build_deposition_steps(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    order_index = 1

    for run_index, run in enumerate(runs, start=1):
        row_index = int(run["row_index"])
        value = run["value"]
        start_col = int(run["start_col"])
        end_col = int(run["end_col"])

        for col_index in range(start_col, end_col - 1, -1):
            steps.append(
                {
                    "order_index": order_index,
                    "run_index": run_index,
                    "row_index": row_index,
                    "col_index": col_index,
                    "value": value,
                }
            )
            order_index += 1

    return steps


def iter_matching_cols_right_to_left(row: list[Any], target_value: Any) -> list[int]:
    return [col_index for col_index in range(len(row) - 1, -1, -1) if row[col_index] == target_value]


def deposit_row_cells(
    matrix: list[list[Any]],
    state: np.ndarray,
    row_index: int,
    value: Any,
    order_index: int,
    events: list[dict[str, Any]],
    enforce_support: bool,
) -> tuple[int, bool, int | None, list[int]]:
    placed_cols: list[int] = []
    trigger_col: int | None = None

    for col_index in iter_matching_cols_right_to_left(matrix[row_index], value):
        if state[row_index, col_index] is not None:
            continue
        if enforce_support and row_index < len(matrix) - 1 and state[row_index + 1, col_index] is None:
            trigger_col = col_index
            return order_index, False, trigger_col, placed_cols

        state[row_index, col_index] = value
        placed_cols.append(col_index)
        events.append(
            {
                "event_type": "deposit",
                "order_index": order_index,
                "row_index": row_index,
                "col_index": col_index,
                "value": value,
            }
        )
        order_index += 1

    return order_index, True, trigger_col, placed_cols


def deposit_pass(
    matrix: list[list[Any]],
    state: np.ndarray,
    value: Any,
    order_index: int,
    events: list[dict[str, Any]],
    enforce_support: bool,
) -> tuple[int, int]:
    placed_total = 0
    for row_index in range(len(matrix) - 1, -1, -1):
        before_count = len(events)
        order_index, _, _, _ = deposit_row_cells(
            matrix=matrix,
            state=state,
            row_index=row_index,
            value=value,
            order_index=order_index,
            events=events,
            enforce_support=enforce_support,
        )
        placed_total += len(events) - before_count
    return order_index, placed_total


def find_next_pending_material(
    matrix: list[list[Any]],
    state: np.ndarray,
    exclude_value: Any,
    preferred_row_index: int | None = None,
) -> Any | None:
    first_unfilled_row_index: int | None = None
    for row_index in range(len(matrix) - 1, -1, -1):
        if any(state[row_index, col_index] is None for col_index in range(len(matrix[row_index]))):
            first_unfilled_row_index = row_index
            break

    row_indices: list[int] = []
    if first_unfilled_row_index is not None:
        row_indices.append(first_unfilled_row_index)
    elif preferred_row_index is not None:
        row_indices.append(preferred_row_index)
    row_indices.extend(
        row_index
        for row_index in range(len(matrix) - 1, -1, -1)
        if row_index not in row_indices
    )
    for row_index in row_indices:
        for col_index in range(len(matrix[row_index]) - 1, -1, -1):
            if state[row_index, col_index] is not None:
                continue
            candidate_value = matrix[row_index][col_index]
            if candidate_value == exclude_value:
                continue
            return candidate_value
    return None


def simulate_gravity_first_material_switch(
    matrix: list[list[Any]],
    prioritized_value: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], np.ndarray]:
    row_count = len(matrix)
    state = np.full((row_count, len(matrix[0])), None, dtype=object)
    events: list[dict[str, Any]] = []
    row_summaries_map: dict[int, dict[str, Any]] = {}
    order_index = 1
    current_value = prioritized_value
    max_loops = len(matrix) * len(matrix[0]) * 4
    loop_count = 0

    while np.any(state == None):
        loop_count += 1
        if loop_count > max_loops:
            raise RuntimeError("Simulation exceeded safety loop count.")
        switched_this_round = False
        placed_any = False

        for row_index in range(row_count - 1, -1, -1):
            if not any(matrix[row_index][col_index] == current_value and state[row_index, col_index] is None for col_index in range(len(matrix[0]))):
                continue

            row_summaries_map.setdefault(
                row_index,
                {
                    "row_index": row_index,
                    "reset_happened": 0,
                    "first_material": current_value,
                    "trigger_col_index": -1,
                },
            )

            before_event_count = len(events)
            order_index, success, trigger_col, placed_cols = deposit_row_cells(
                matrix=matrix,
                state=state,
                row_index=row_index,
                value=current_value,
                order_index=order_index,
                events=events,
                enforce_support=True,
            )
            if len(events) > before_event_count:
                placed_any = True

            if success:
                continue

            for col_index in placed_cols:
                state[row_index, col_index] = None

            replacement_value = find_next_pending_material(matrix, state, current_value, preferred_row_index=row_index)
            if replacement_value is None:
                replacement_value = find_next_pending_material(matrix, state, current_value)
            if replacement_value is None:
                break
            events.append(
                {
                    "event_type": "reset",
                    "row_index": row_index,
                    "trigger_col_index": -1 if trigger_col is None else trigger_col,
                    "failed_value": current_value,
                    "replacement_value": replacement_value,
                    "cleared_count": len(placed_cols),
                    "cleared_cols": placed_cols,
                }
            )
            row_summaries_map[row_index] = {
                "row_index": row_index,
                "reset_happened": 1,
                "first_material": replacement_value,
                "trigger_col_index": -1 if trigger_col is None else trigger_col,
            }
            current_value = replacement_value
            switched_this_round = True
            break

        if switched_this_round:
            continue

        next_material = find_next_pending_material(matrix, state, current_value)
        if next_material is not None:
            current_value = next_material
            continue

        if not placed_any:
            break

    row_summaries = sorted(row_summaries_map.values(), key=lambda item: int(item["row_index"]), reverse=True)
    return events, row_summaries, state


def build_material_switch_support_points(
    simulation_events: list[dict[str, Any]],
    prioritized_value: Any,
    row_count: int,
) -> list[dict[str, int]]:
    switch_seen = False
    support_points: list[dict[str, int]] = []
    seen_coords: set[tuple[int, int]] = set()
    switched_value: Any | None = None

    for event in simulation_events:
        if str(event["event_type"]) == "reset":
            switch_seen = True
            switched_value = event["replacement_value"]
            continue
        if not switch_seen:
            continue
        if switched_value is not None and event["value"] != switched_value:
            continue

        row_index = int(event["row_index"])
        col_index = int(event["col_index"])
        if row_index == row_count - 1:
            continue
        support_coord = (row_index + 1, col_index)
        if support_coord in seen_coords:
            continue
        seen_coords.add(support_coord)
        support_points.append(
            {
                "support_row_index": row_index + 1,
                "support_col_index": col_index,
            }
        )

    support_points.sort(
        key=lambda item: (-int(item["support_row_index"]), -int(item["support_col_index"]))
    )
    return support_points


def build_material_switch_events(
    simulation_events: list[dict[str, Any]],
    material_name_matrix: list[list[str]] | None = None,
) -> list[dict[str, Any]]:
    switch_events: list[dict[str, Any]] = []
    switch_index = 0
    current_value: Any | None = None
    current_material_name: str | None = None
    pending_reset_event: dict[str, Any] | None = None

    for event in simulation_events:
        event_type = str(event["event_type"])
        if event_type == "reset":
            pending_reset_event = event
            continue
        if event_type != "deposit":
            continue

        value = event["value"]
        material_name = (
            material_name_matrix[int(event["row_index"])][int(event["col_index"])]
            if material_name_matrix is not None
            else str(value)
        )
        if current_value is None:
            current_value = value
            current_material_name = material_name
            pending_reset_event = None
            continue

        if value == current_value:
            current_material_name = material_name
            pending_reset_event = None
            continue

        switch_index += 1
        switch_events.append(
            {
                "switch_index": switch_index,
                "row_index": int(event["row_index"]),
                "trigger_col_index": int(event["col_index"]),
                "from_value": current_value,
                "to_value": value,
                "from_material": current_material_name if current_material_name is not None else str(current_value),
                "to_material": material_name,
                "cleared_count": int(pending_reset_event["cleared_count"]) if pending_reset_event else 0,
            }
        )
        current_value = value
        current_material_name = material_name
        pending_reset_event = None

    return switch_events


def count_final_material_cells(final_state: np.ndarray) -> dict[str, Any]:
    counts: dict[str, int] = {}
    total_filled = 0
    for value in final_state.flat:
        if value is None:
            continue
        total_filled += 1
        key = str(value)
        counts[key] = counts.get(key, 0) + 1
    return {
        "total_filled": total_filled,
        "by_value": dict(sorted(counts.items())),
    }


def count_final_named_material_cells(
    final_state: np.ndarray,
    material_name_matrix: list[list[str]] | None,
) -> dict[str, int]:
    if material_name_matrix is None:
        return {}
    counts: dict[str, int] = {}
    for row_index in range(final_state.shape[0]):
        for col_index in range(final_state.shape[1]):
            if final_state[row_index, col_index] is None:
                continue
            name = material_name_matrix[row_index][col_index]
            counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items()))


def build_payload(matrix: list[list[int]], material_name_matrix: list[list[str]] | None = None) -> dict[str, Any]:
    simulation_matrix = reverse_matrix_steps(matrix)
    simulation_material_name_matrix = reverse_matrix_steps(material_name_matrix) if material_name_matrix is not None else None
    simulation_value_matrix: list[list[Any]] = simulation_material_name_matrix if simulation_material_name_matrix is not None else simulation_matrix
    scan_runs = build_right_to_left_same_value_runs(simulation_value_matrix)
    deposition_runs, prioritized_value = reorder_runs_same_color_first(simulation_value_matrix, scan_runs)
    deposition_steps = build_deposition_steps(deposition_runs)
    simulation_events, row_summaries, final_state = simulate_gravity_first_material_switch(
        simulation_value_matrix,
        prioritized_value,
    )
    material_switch_events = build_material_switch_events(simulation_events, simulation_material_name_matrix)
    reset_count = len(material_switch_events)
    material_switch_support_points = build_material_switch_support_points(
        simulation_events,
        prioritized_value,
        len(simulation_matrix),
    )
    final_material_counts = count_final_material_cells(final_state)
    return {
        "source_matrix_path": str(SOURCE_MATRIX_PATH),
        "step_reversed_for_simulation": True,
        "rule": (
            "reverse step order for simulation first; then start from last row; "
            "group contiguous same values while scanning right-to-left; "
            "try the starting color first on each layer during the first pass; if a target cell has "
            "no deposited support directly below, reset that layer and rebuild that layer with the "
            "other material first"
        ),
        "row_count": len(simulation_matrix),
        "col_count": len(simulation_matrix[0]),
        "prioritized_value": prioritized_value,
        "scan_run_count": len(scan_runs),
        "run_count": len(deposition_runs),
        "deposition_step_count": len(deposition_steps),
        "simulation_event_count": len(simulation_events),
        "material_switch_count": reset_count,
        "material_switch_support_point_count": len(material_switch_support_points),
        "scan_runs": scan_runs,
        "runs": deposition_runs,
        "deposition_steps": deposition_steps,
        "row_summaries": row_summaries,
        "simulation_events": simulation_events,
        "material_switch_events": material_switch_events,
        "material_switch_support_points": material_switch_support_points,
        "final_material_counts": final_material_counts,
        "final_named_material_counts": count_final_named_material_cells(final_state, simulation_material_name_matrix),
        "simulation_matrix": simulation_matrix,
        "simulation_material_name_matrix": simulation_material_name_matrix,
        "final_state": final_state.tolist(),
    }


def format_payload(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"source_matrix_path: {payload['source_matrix_path']}")
    lines.append(f"step_reversed_for_simulation: {payload['step_reversed_for_simulation']}")
    lines.append(f"rule: {payload['rule']}")
    lines.append(f"shape: {payload['row_count']} x {payload['col_count']}")
    lines.append(f"prioritized_value: {payload['prioritized_value']}")
    lines.append(f"scan_run_count: {payload['scan_run_count']}")
    lines.append(f"run_count: {payload['run_count']}")
    lines.append(f"deposition_step_count: {payload['deposition_step_count']}")
    lines.append(f"simulation_event_count: {payload['simulation_event_count']}")
    lines.append(f"material_switch_count: {payload['material_switch_count']}")
    lines.append(f"material_switch_support_point_count: {payload['material_switch_support_point_count']}")
    lines.append(f"final_material_counts: {payload['final_material_counts']}")
    if payload.get("final_named_material_counts"):
        lines.append(f"final_named_material_counts: {payload['final_named_material_counts']}")
    lines.append("")
    lines.append("material_switch_events:")
    if payload["material_switch_events"]:
        for item in payload["material_switch_events"]:
            lines.append(
                f"switch_{item['switch_index']:03d}: row={item['row_index']} "
                f"trigger_col={item['trigger_col_index']} "
                f"from_value={item['from_value']} to_value={item['to_value']} "
                f"from_material={item.get('from_material', item['from_value'])} "
                f"to_material={item.get('to_material', item['to_value'])} "
                f"cleared_count={item['cleared_count']}"
            )
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("row_summaries:")
    for item in payload["row_summaries"]:
        lines.append(
            f"row={item['row_index']} reset={item['reset_happened']} "
            f"first_material={item['first_material']} trigger_col={item['trigger_col_index']}"
        )
    lines.append("")
    lines.append("material_switch_support_points:")
    if payload["material_switch_support_points"]:
        for item in payload["material_switch_support_points"]:
            lines.append(
                f"support_row={item['support_row_index']} support_col={item['support_col_index']}"
            )
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("deposition_runs:")
    for index, run in enumerate(payload["runs"], start=1):
        lines.append(
            f"{index:03d}. row={run['row_index']} value={run['value']} "
            f"start_col={run['start_col']} end_col={run['end_col']} "
            f"direction={run['direction']}"
        )
    return "\n".join(lines) + "\n"


def build_material_palette(material_name_matrix: list[list[str]] | None) -> tuple[list[str], ListedColormap]:
    ordered_materials = ["EMPTY"]
    if material_name_matrix is not None:
        for row in material_name_matrix:
            for value in row:
                if value not in ordered_materials:
                    ordered_materials.append(value)
    else:
        ordered_materials.extend(["0", "1"])
    colors = [EMPTY_CELL_COLOR] + [MATERIAL_COLORS.get(name, MATERIAL_COLORS["Other"]) for name in ordered_materials[1:]]
    return ordered_materials, ListedColormap(colors)


def make_display_grid(state: np.ndarray, material_name_matrix: list[list[str]] | None) -> np.ndarray:
    display = np.zeros(state.shape, dtype=int)
    if material_name_matrix is None:
        display[state == 0] = 1
        display[state == 1] = 2
        return display
    ordered_materials, _ = build_material_palette(material_name_matrix)
    for row_index in range(state.shape[0]):
        for col_index in range(state.shape[1]):
            if state[row_index, col_index] is None:
                continue
            display[row_index, col_index] = ordered_materials.index(material_name_matrix[row_index][col_index])
    return display


def save_final_stack_image(
    state: np.ndarray,
    matrix: list[list[int]],
    material_name_matrix: list[list[str]] | None,
    prioritized_value: Any,
    material_switch_events: list[dict[str, Any]],
    path: Path,
    *,
    x_tick_labels: list[str] | None = None,
    summary_extra_lines: list[str] | None = None,
) -> None:
    row_count = len(matrix)
    col_count = len(matrix[0])
    matrix_width = min(24.0, max(6.0, col_count * 0.08))
    text_width = 4.8
    figure_height = min(12.0, max(4.8, row_count * 0.42))
    fig, (ax, summary_ax) = plt.subplots(
        1,
        2,
        figsize=(matrix_width + text_width, figure_height),
        gridspec_kw={"width_ratios": [matrix_width, text_width]},
        constrained_layout=True,
    )
    ordered_materials, cmap = build_material_palette(material_name_matrix)
    image = make_display_grid(state, material_name_matrix)
    ax.imshow(image, origin="upper", cmap=cmap, vmin=0, vmax=len(ordered_materials) - 1)

    if col_count <= 120:
        ax.set_xticks(np.arange(-0.5, col_count, 1), minor=True)
        ax.set_yticks(np.arange(-0.5, row_count, 1), minor=True)
        ax.grid(which="minor", color="#cbd5e1", linewidth=0.8)
        ax.tick_params(which="minor", bottom=False, left=False)
    tick_stride = max(1, math.ceil(col_count / 30))
    x_ticks = list(range(0, col_count, tick_stride))
    ax.set_xticks(x_ticks)
    if x_tick_labels is not None and len(x_tick_labels) == col_count:
        ax.set_xticklabels([x_tick_labels[index] for index in x_ticks])
        plt.setp(ax.get_xticklabels(), rotation=25, ha="right")
    else:
        ax.set_xticklabels([str(index + 1) for index in x_ticks])
    ax.set_yticks(range(row_count))
    ax.set_xlabel("Step")
    ax.set_ylabel("Layer")
    ax.set_yticklabels([str(row_count - row_index) for row_index in range(row_count)])
    ax.set_title(f"Final Stacked State | start value={prioritized_value}")

    summary_lines = [f"material switch count: {len(material_switch_events)}"]
    if summary_extra_lines:
        summary_lines.extend(str(line) for line in summary_extra_lines)
    if material_switch_events:
        for item in material_switch_events:
            summary_lines.append(
                f"switch {item['switch_index']}: row {item['row_index']}, col {item['trigger_col_index']}, "
                f"{item.get('from_material', item['from_value'])}->{item.get('to_material', item['to_value'])}"
            )
    else:
        summary_lines.append("switch events: none")

    summary_ax.axis("off")
    summary_ax.text(
        0.0,
        1.0,
        "\n".join(summary_lines),
        transform=summary_ax.transAxes,
        ha="left",
        va="top",
        fontsize=8,
        color="#111827",
        linespacing=1.25,
    )

    if col_count <= 100:
        for row_index in range(row_count):
            for col_index in range(col_count):
                if state[row_index, col_index] is None:
                    continue
                display_name = (
                    material_name_matrix[row_index][col_index]
                    if material_name_matrix is not None
                    else str(state[row_index, col_index])
                )
                ax.text(
                    col_index,
                    row_index,
                    material_abbreviation(display_name),
                    ha="center",
                    va="center",
                    color="black" if display_name in {"WHITE", "YELLOW"} else "white",
                    fontsize=8,
                    fontweight="bold",
                )

    fig.savefig(path, dpi=180)
    plt.close(fig)


def save_stacking_animation(
    matrix: list[list[int]],
    material_name_matrix: list[list[str]] | None,
    simulation_events: list[dict[str, Any]],
    prioritized_value: Any,
    material_switch_events: list[dict[str, Any]],
    path: Path,
) -> None:
    row_count = len(matrix)
    col_count = len(matrix[0])
    state = np.full((row_count, col_count), None, dtype=object)
    ordered_materials, cmap = build_material_palette(material_name_matrix)

    fig, ax = plt.subplots(
        figsize=(
            min(16.0, max(7.0, col_count * 0.05)),
            min(8.0, max(4.8, row_count * 0.35)),
        )
    )
    image = ax.imshow(make_display_grid(state, material_name_matrix), origin="upper", cmap=cmap, vmin=0, vmax=len(ordered_materials) - 1)
    marker, = ax.plot([], [], marker="s", markersize=10, color="#111827", markeredgecolor="white")
    switch_marker, = ax.plot(
        [],
        [],
        marker="o",
        markersize=20,
        markerfacecolor="none",
        markeredgecolor="#dc2626",
        markeredgewidth=2.5,
        linestyle="None",
    )
    title = ax.set_title(f"Stacking Simulation | start value={prioritized_value}")
    header_text = ax.text(
        0.02,
        0.98,
        "",
        transform=ax.transAxes,
        ha="left",
        va="top",
        fontsize=10,
        color="#111827",
        fontweight="bold",
        bbox={"facecolor": "white", "alpha": 0.9, "edgecolor": "#cbd5e1"},
    )
    switch_info_text = ax.text(
        0.62,
        0.98,
        f"material switch: 0/{len(material_switch_events)}",
        transform=ax.transAxes,
        ha="left",
        va="top",
        fontsize=9,
        color="#111827",
        bbox={"facecolor": "white", "alpha": 0.9, "edgecolor": "#cbd5e1"},
    )
    switch_lookup = {
        (int(item["row_index"]), int(item["trigger_col_index"]), item["to_value"]): item
        for item in material_switch_events
    }
    current_switch_index = [0]
    persistent_switch_points: list[tuple[int, int]] = []

    ax.set_xticks(np.arange(-0.5, col_count, 1), minor=True)
    ax.set_yticks(np.arange(-0.5, row_count, 1), minor=True)
    ax.grid(which="minor", color="#cbd5e1", linewidth=0.8)
    ax.tick_params(which="minor", bottom=False, left=False)
    ax.set_xticks(range(col_count))
    ax.set_yticks(range(row_count))
    ax.set_xlabel("Step")
    ax.set_ylabel("Layer")
    ax.set_yticklabels([str(row_count - row_index) for row_index in range(row_count)])
    row_highlight = plt.Rectangle(
        (-0.5, -0.5),
        col_count,
        1.0,
        facecolor="#fca5a5",
        edgecolor="#dc2626",
        linewidth=2.0,
        alpha=0.0,
    )
    ax.add_patch(row_highlight)
    persistent_switch_scatter = ax.scatter(
        [],
        [],
        s=20,
        c="#dc2626",
        marker="s",
        alpha=0.95,
    )

    def update(frame_index: int):
        if frame_index == 0:
            title.set_text(f"Stacking Simulation | start value={prioritized_value}")
            marker.set_data([], [])
            switch_marker.set_data([], [])
            row_highlight.set_alpha(0.0)
            persistent_switch_scatter.set_offsets(np.empty((0, 2)))
            image.set_data(make_display_grid(state, material_name_matrix))
            header_text.set_text("")
            switch_info_text.set_text(f"material switch: 0/{len(material_switch_events)}")
            return image, marker, switch_marker, persistent_switch_scatter, row_highlight, title, header_text, switch_info_text

        event = simulation_events[frame_index - 1]
        event_type = str(event["event_type"])

        if event_type == "deposit":
            row_index = int(event["row_index"])
            col_index = int(event["col_index"])
            value = event["value"]
            order_index = int(event["order_index"])
            state[row_index, col_index] = value
            image.set_data(make_display_grid(state, material_name_matrix))
            marker.set_data([col_index], [row_index])
            switch_event = switch_lookup.get((row_index, col_index, value))
            if switch_event is not None:
                current_switch_index[0] = int(switch_event["switch_index"])
                switch_marker.set_data([col_index], [row_index])
                if (col_index, row_index) not in persistent_switch_points:
                    persistent_switch_points.append((col_index, row_index))
                persistent_switch_scatter.set_offsets(np.array(persistent_switch_points, dtype=float))
                switch_info_text.set_text(
                    f"material switch: {current_switch_index[0]}/{len(material_switch_events)}\n"
                    f"switch at row {row_index}, col {col_index}\n"
                    f"{switch_event.get('from_material', switch_event['from_value'])} -> {switch_event.get('to_material', switch_event['to_value'])}"
                )
                row_highlight.set_alpha(0.0)
                header_text.set_text("")
            else:
                switch_marker.set_data([], [])
                row_highlight.set_alpha(0.0)
                header_text.set_text("")
                switch_info_text.set_text(
                    f"material switch: {current_switch_index[0]}/{len(material_switch_events)}"
                )
                if persistent_switch_points:
                    persistent_switch_scatter.set_offsets(np.array(persistent_switch_points, dtype=float))
                else:
                    persistent_switch_scatter.set_offsets(np.empty((0, 2)))
            display_name = (
                material_name_matrix[row_index][col_index]
                if material_name_matrix is not None
                else str(value)
            )
            title.set_text(
                f"Stacking Simulation | deposit {order_index}/{len(simulation_events)} "
                f"| row {row_index} col {col_index} | material {display_name}"
            )
        else:
            row_index = int(event["row_index"])
            trigger_col = int(event["trigger_col_index"])
            replacement_value = event["replacement_value"]
            failed_value = event["failed_value"]
            cleared_cols = event.get("cleared_cols")
            if isinstance(cleared_cols, list):
                for col_index in cleared_cols:
                    state[row_index, int(col_index)] = None
            image.set_data(make_display_grid(state, material_name_matrix))
            marker.set_data([trigger_col], [row_index])
            row_highlight.set_y(row_index - 0.5)
            row_highlight.set_alpha(0.35)
            title.set_text(
                f"Stacking Simulation | reset row {row_index} at col {trigger_col} "
                f"| switch first material to {replacement_value}"
            )
            switch_marker.set_data([], [])
            header_text.set_text("")
            switch_info_text.set_text(
                f"material switch: {current_switch_index[0]}/{len(material_switch_events)}\n"
                f"reset row {row_index}, col {trigger_col}\n"
                f"{failed_value} -> {replacement_value} pending"
            )
            if persistent_switch_points:
                persistent_switch_scatter.set_offsets(np.array(persistent_switch_points, dtype=float))
            else:
                persistent_switch_scatter.set_offsets(np.empty((0, 2)))
        return image, marker, switch_marker, persistent_switch_scatter, row_highlight, title, header_text, switch_info_text

    frame_count = len(simulation_events) + 1
    ani = animation.FuncAnimation(
        fig,
        update,
        frames=frame_count,
        interval=220,
        blit=False,
        repeat=False,
    )
    writer = animation.PillowWriter(fps=5)
    ani.save(path, writer=writer)
    plt.close(fig)


def main() -> None:
    source_matrix = load_matrix_from_txt(SOURCE_MATRIX_PATH)
    material_name_matrix = load_material_name_matrix(MATERIAL_NAME_MATRIX_PATH)
    payload = build_payload(source_matrix, material_name_matrix)

    OUTPUT_JSON_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    OUTPUT_TXT_PATH.write_text(format_payload(payload), encoding="utf-8")

    final_state = np.array(payload["final_state"], dtype=object)
    simulation_matrix = payload["simulation_matrix"]
    simulation_material_name_matrix = payload.get("simulation_material_name_matrix")

    save_final_stack_image(
        final_state,
        simulation_matrix,
        simulation_material_name_matrix,
        payload["prioritized_value"],
        payload["material_switch_events"],
        OUTPUT_PNG_PATH,
    )
    save_stacking_animation(
        simulation_matrix,
        simulation_material_name_matrix,
        payload["simulation_events"],
        payload["prioritized_value"],
        payload["material_switch_events"],
        OUTPUT_GIF_PATH,
    )

    print(f"Loaded matrix: {len(source_matrix)} x {len(source_matrix[0])}")
    print("Applied reversed step order for simulation.")
    print(f"Generated runs: {payload['run_count']}")
    print(f"Generated deposition steps: {payload['deposition_step_count']}")
    print(f"Generated simulation events: {payload['simulation_event_count']}")
    print(f"Generated material switches: {payload['material_switch_count']}")
    print(f"Saved JSON: {OUTPUT_JSON_PATH}")
    print(f"Saved TXT: {OUTPUT_TXT_PATH}")
    print(f"Saved PNG: {OUTPUT_PNG_PATH}")
    print(f"Saved GIF: {OUTPUT_GIF_PATH}")


if __name__ == "__main__":
    main()
