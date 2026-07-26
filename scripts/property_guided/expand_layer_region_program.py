#!/usr/bin/env python3
"""Expand a component-level property program into chronological layer-region events."""

from __future__ import annotations

import copy
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from scripts.utils.property_program_utils import (
    normalize_ratio_value,
    resolve_assignment_material_pair,
    resolve_gradient_endpoint_compositions,
)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return payload


def _source_assignment_map(assignments: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for position, assignment in enumerate(assignments):
        raw_index = assignment.get("source_component_index", assignment.get("assignment_index", position))
        try:
            result[int(raw_index)] = assignment
        except (TypeError, ValueError):
            result[position] = assignment
    return result


def _event_progress(
    events: list[dict[str, Any]],
    direction: str,
) -> dict[int, float]:
    progress: dict[int, float] = {}
    if not events:
        return progress

    if direction.strip().lower() == "layer":
        layer_values = sorted({int(event.get("layer_index", 0)) for event in events})
        layer_rank = {layer: rank for rank, layer in enumerate(layer_values)}
        count = max(1, len(layer_values))
        for event in events:
            sequence = int(event.get("sequence_index", event.get("sequence", 0)))
            rank = layer_rank[int(event.get("layer_index", 0))]
            progress[sequence] = (rank + 0.5) / count
        return progress

    lengths = [
        max(
            0.0,
            float(event.get("extrusion_e_mm", event.get("deposition_e_mm", 0.0))),
        )
        for event in events
    ]
    total = sum(lengths)
    if total <= 0:
        count = max(1, len(events))
        return {
            int(event.get("sequence_index", event.get("sequence", index))): (index + 0.5) / count
            for index, event in enumerate(events)
        }

    cumulative = 0.0
    for event, length in zip(events, lengths):
        sequence = int(event.get("sequence_index", event.get("sequence", 0)))
        progress[sequence] = (cumulative + 0.5 * length) / total
        cumulative += length
    return progress


def _nearest_resolved_target(
    assignment: dict[str, Any],
    alpha: float,
) -> dict[str, Any] | None:
    targets = assignment.get("resolved_step_targets")
    if not isinstance(targets, list) or not targets:
        return None
    index = min(len(targets) - 1, max(0, int(math.floor(alpha * len(targets)))))
    target = targets[index]
    return copy.deepcopy(target) if isinstance(target, dict) else None


def _interpolated_target(
    program: dict[str, Any],
    assignment: dict[str, Any],
    alpha: float,
) -> dict[str, Any]:
    material_start, material_end = resolve_assignment_material_pair(program, assignment)
    endpoint_compositions = resolve_gradient_endpoint_compositions(program, assignment)
    if endpoint_compositions is None:
        composition_start = {material_start: 1.0}
        composition_end = {material_end: 1.0}
    else:
        composition_start, composition_end = endpoint_compositions
    start_0 = normalize_ratio_value(composition_start.get(material_start, 1.0))
    start_1 = normalize_ratio_value(composition_end.get(material_start, 0.0))
    ratio_start = max(0.0, min(1.0, start_0 + (start_1 - start_0) * alpha))
    return {
        "material_start": material_start,
        "material_end": material_end,
        "ratio_start": ratio_start,
        "ratio_end": 1.0 - ratio_start,
        "eta": assignment.get("eta", assignment.get("max_eta", 0.0)),
        "interpolation_alpha": alpha,
        "target_source": "layer_region_interpolation",
    }


def _configure_gradient_event(
    program: dict[str, Any],
    source: dict[str, Any],
    clone: dict[str, Any],
    alpha: float,
) -> None:
    target = _nearest_resolved_target(source, alpha)
    if target is None:
        target = _interpolated_target(program, source, alpha)

    default_start, default_end = resolve_assignment_material_pair(program, source)
    material_start = str(target.get("material_start", default_start))
    material_end = str(target.get("material_end", default_end))
    target["material_start"] = material_start
    target["material_end"] = material_end
    target.setdefault("interpolation_alpha", alpha)

    clone["material_start"] = material_start
    clone["material_end"] = material_end
    clone["gradient_steps"] = 1
    clone["resolved_step_targets"] = [target]
    clone["eta"] = target.get("eta", source.get("eta", source.get("max_eta", 0.0)))


def expand_layer_region_program(
    program: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    plan = program.get("layer_region_execution_plan")
    events = plan.get("events") if isinstance(plan, dict) else None
    assignments = program.get("assignments")
    if not isinstance(events, list) or not events or not isinstance(assignments, list):
        return program, {
            "expanded_event_count": 0,
            "reason": "No layer-region execution events were embedded in the property program.",
        }

    usable_events = [
        event for event in events
        if isinstance(event, dict)
        and float(event.get("extrusion_e_mm", event.get("deposition_e_mm", 0.0))) > 0
    ]
    usable_events.sort(
        key=lambda event: int(
            event.get("execution_step_index", event.get("sequence_index", event.get("sequence", 0)))
        )
    )
    source_map = _source_assignment_map(assignments)

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    skipped: list[dict[str, Any]] = []
    for event in usable_events:
        try:
            source_index = int(event["source_component_index"])
        except (KeyError, TypeError, ValueError):
            skipped.append({
                "sequence": event.get("sequence_index", event.get("sequence")),
                "reason": "missing source_component_index",
            })
            continue
        if source_index not in source_map:
            skipped.append({
                "sequence": event.get("sequence_index", event.get("sequence")),
                "source_component_index": source_index,
                "reason": "no matching property assignment",
            })
            continue
        grouped[source_index].append(event)

    progress_by_source: dict[int, dict[int, float]] = {}
    for source_index, source_events in grouped.items():
        direction = str(source_map[source_index].get("gradient_direction", "printing"))
        progress_by_source[source_index] = _event_progress(source_events, direction)

    expanded_assignments: list[dict[str, Any]] = []
    expanded_lengths: list[float] = []
    for event in usable_events:
        try:
            source_index = int(event["source_component_index"])
        except (KeyError, TypeError, ValueError):
            continue
        source = source_map.get(source_index)
        if source is None:
            continue

        sequence = int(event.get("sequence_index", event.get("sequence", len(expanded_assignments))))
        alpha = progress_by_source[source_index][sequence]
        clone = copy.deepcopy(source)
        original_assignment_index = source.get("assignment_index", source_index)
        new_index = len(expanded_assignments)
        length = max(
            0.0,
            float(event.get("extrusion_e_mm", event.get("deposition_e_mm", 0.0))),
        )
        layer_index = int(event.get("layer_index", 0))

        clone["assignment_index"] = new_index + 1
        clone["source_definition_assignment_index"] = original_assignment_index
        clone["source_component_index"] = source_index
        clone["start_voxel_index"] = new_index + 1
        clone["end_voxel_index"] = new_index + 1
        clone["start_layer"] = layer_index
        clone["end_layer"] = layer_index
        clone["gradient_steps"] = 1
        clone["execution_filament_e_mm"] = length
        clone["layer_region_event"] = copy.deepcopy(event)
        clone["layer_region_progress"] = alpha
        clone["component_name"] = str(event.get("region_name", clone.get("component_name", "")))
        if str(clone.get("type", "")).strip().lower() == "gradient":
            _configure_gradient_event(program, source, clone, alpha)

        expanded_assignments.append(clone)
        expanded_lengths.append(length)

    expanded = copy.deepcopy(program)
    expanded["source_assignments"] = copy.deepcopy(assignments)
    if "mapped_after_step_lengths_e_mm" in expanded:
        expanded["source_mapped_after_step_lengths_e_mm"] = copy.deepcopy(
            expanded["mapped_after_step_lengths_e_mm"]
        )
    expanded["assignments"] = expanded_assignments
    expanded["mapped_after_step_lengths_e_mm"] = expanded_lengths
    expanded["resolution_mode"] = "layer_region_occurrence"
    expanded["effective_assignment_count"] = len(expanded_assignments)
    expanded["effective_total_region_deposition_e_mm"] = sum(expanded_lengths)
    expanded["layer_region_expansion"] = {
        "source_assignment_count": len(assignments),
        "expanded_event_count": len(expanded_assignments),
        "skipped_events": skipped,
        "ordering": "chronological_gcode_deposition",
        "gradient_sampling": "deposition_midpoint_or_layer_midpoint",
    }

    summary = {
        "source_assignment_count": len(assignments),
        "expanded_event_count": len(expanded_assignments),
        "skipped_event_count": len(skipped),
        "total_region_deposition_e_mm": sum(expanded_lengths),
        "layer_count": len({
            int(assignment["layer_region_event"].get("layer_index", 0))
            for assignment in expanded_assignments
        }),
        "region_count": len({
            str(assignment["layer_region_event"].get("region_name", ""))
            for assignment in expanded_assignments
        }),
    }
    return expanded, summary


def expand_layer_region_program_to_path(
    source_path: Path,
    output_path: Path,
) -> tuple[Path, dict[str, Any]]:
    program = _load_json(source_path)
    expanded, summary = expand_layer_region_program(program)
    if int(summary.get("expanded_event_count", 0)) <= 0:
        return source_path, summary

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(expanded, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    summary_path = output_path.with_name(f"{output_path.stem}_summary.json")
    with summary_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return output_path, summary
