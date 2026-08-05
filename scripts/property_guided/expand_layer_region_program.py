#!/usr/bin/env python3
"""Expand a component-level property program into chronological layer-region events."""

from __future__ import annotations

import copy
import json
import math
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from scripts.utils.property_program_utils import (
    normalize_ratio_value,
    resolve_assignment_material_pair,
    resolve_gradient_endpoint_compositions,
)

REGION_RECOGNITION_MODE_ENV_KEY = "B_FDM_REGION_RECOGNITION_MODE"


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


def _uses_z_axis_region_recognition(program: dict[str, Any]) -> bool:
    normalized = str(
        os.environ.get(
            REGION_RECOGNITION_MODE_ENV_KEY,
            program.get("region_recognition_mode", "layer-region"),
        )
    ).strip().lower().replace("_", "-").replace(" ", "-")
    return normalized in {"z", "z-axis", "zaxis"}


def _effective_step_count(assignment: dict[str, Any]) -> int:
    property_type = str(
        assignment.get("Property_type", assignment.get("type", "Property"))
    ).strip().lower()
    if property_type != "gradient":
        return 1
    try:
        return max(1, int(assignment.get("gradient_steps", 1)))
    except (TypeError, ValueError):
        return 1


def _z_axis_step_lengths(
    events: list[dict[str, Any]],
    step_count: int,
) -> list[float]:
    layer_totals: dict[int, float] = defaultdict(float)
    for event in events:
        layer_index = int(event.get("layer_index", 0))
        layer_totals[layer_index] += max(
            0.0,
            float(event.get("extrusion_e_mm", event.get("deposition_e_mm", 0.0))),
        )

    ordered_layer_lengths = [
        layer_totals[layer_index] for layer_index in sorted(layer_totals)
    ]
    layer_count = len(ordered_layer_lengths)
    if layer_count == 0:
        return [0.0] * step_count

    return [
        sum(
            ordered_layer_lengths[
                (step_index * layer_count) // step_count :
                ((step_index + 1) * layer_count) // step_count
            ]
        )
        for step_index in range(step_count)
    ]


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
    if _uses_z_axis_region_recognition(program):
        return program, {
            "source_assignment_count": len(assignments),
            "expanded_event_count": 0,
            "reason": (
                "z-axis mode uses the component-level property program directly, "
                "matching the b-FDM_main2 workflow."
            ),
        }

    usable_events = [
        event for event in events
        if isinstance(event, dict)
        and float(event.get("extrusion_e_mm", event.get("deposition_e_mm", 0.0))) > 0
    ]
    source_map = _source_assignment_map(assignments)
    source_order_by_component_index = {
        source_component_index: position
        for position, source_component_index in enumerate(source_map)
    }
    use_z_axis_region_recognition = _uses_z_axis_region_recognition(program)
    if use_z_axis_region_recognition:
        usable_events.sort(
            key=lambda event: int(
                event.get("execution_step_index", event.get("sequence_index", event.get("sequence", 0)))
            )
        )
        layer_region_ordering = "chronological_gcode_deposition"
    else:
        usable_events.sort(
            key=lambda event: (
                int(event.get("layer_index", 0)),
                source_order_by_component_index.get(
                    int(event.get("source_component_index", -1)),
                    10**9,
                ),
                int(
                    event.get(
                        "execution_step_index",
                        event.get("sequence_index", event.get("sequence", 0)),
                    )
                ),
            )
        )
        layer_region_ordering = "layer_then_property_assignment_order"

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

    expanded_assignments: list[dict[str, Any]] = []
    expanded_lengths: list[float] = []
    emitted_z_axis_sources: set[int] = set()
    z_axis_assignment_index_remap: dict[int, int] = {}
    for event in usable_events:
        try:
            source_index = int(event["source_component_index"])
        except (KeyError, TypeError, ValueError):
            continue
        source = source_map.get(source_index)
        if source is None:
            continue

        if use_z_axis_region_recognition:
            if source_index in emitted_z_axis_sources:
                continue
            emitted_z_axis_sources.add(source_index)

            source_events = grouped[source_index]
            layer_indices = [
                int(source_event.get("layer_index", 0))
                for source_event in source_events
            ]
            step_count = _effective_step_count(source)
            step_lengths = _z_axis_step_lengths(source_events, step_count)
            clone = copy.deepcopy(source)
            original_assignment_index = source.get("assignment_index", source_index)
            new_index = len(expanded_assignments)

            clone["assignment_index"] = new_index + 1
            clone["source_definition_assignment_index"] = original_assignment_index
            clone["source_component_index"] = source_index
            z_axis_assignment_index_remap[int(original_assignment_index)] = new_index + 1
            clone["start_layer"] = min(layer_indices)
            clone["end_layer"] = max(layer_indices)
            clone["gradient_steps"] = step_count
            clone["execution_filament_e_mm"] = sum(step_lengths)
            clone.pop("layer_region_event", None)
            clone["layer_region_group"] = {
                "resolution_mode": "z_axis_region",
                "region_name": str(
                    source_events[0].get(
                        "region_name",
                        clone.get("component_name", ""),
                    )
                ),
                "occurrence_count": len(source_events),
                "layer_count": len(set(layer_indices)),
                "layer_start": min(layer_indices),
                "layer_end": max(layer_indices),
                "source_event_sequences": [
                    int(
                        source_event.get(
                            "sequence_index",
                            source_event.get("sequence", 0),
                        )
                    )
                    for source_event in source_events
                ],
            }
            expanded_assignments.append(clone)
            expanded_lengths.extend(step_lengths)
            continue

        # A layer-region is one physical step. Repeated occurrences of the
        # same region reuse one ratio pattern; only their measured E lengths
        # differ. A Gradient region therefore uses its central target.
        alpha = 0.5
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
        property_type = str(
            clone.get("Property_type", clone.get("type", "Property"))
        ).strip().lower()
        if property_type == "gradient":
            _configure_gradient_event(program, source, clone, alpha)

        expanded_assignments.append(clone)
        expanded_lengths.append(length)

    if use_z_axis_region_recognition:
        for assignment in expanded_assignments:
            for reference_key in ("Property_start", "Property_end"):
                raw_reference = assignment.get(reference_key)
                if raw_reference is None:
                    continue
                try:
                    original_reference = int(raw_reference)
                except (TypeError, ValueError):
                    continue
                remapped_reference = z_axis_assignment_index_remap.get(
                    original_reference
                )
                if remapped_reference is not None:
                    assignment[reference_key] = remapped_reference

    expanded = copy.deepcopy(program)
    expanded["source_assignments"] = copy.deepcopy(assignments)
    if "mapped_after_step_lengths_e_mm" in expanded:
        expanded["source_mapped_after_step_lengths_e_mm"] = copy.deepcopy(
            expanded["mapped_after_step_lengths_e_mm"]
        )
    expanded["assignments"] = expanded_assignments
    expanded["mapped_after_step_lengths_e_mm"] = expanded_lengths
    expanded["region_recognition_mode"] = (
        "z-axis" if use_z_axis_region_recognition else "layer-region"
    )
    occurrence_count = sum(
        1
        for assignment in expanded_assignments
        if isinstance(assignment.get("layer_region_event"), dict)
    )
    z_axis_region_count = sum(
        1
        for assignment in expanded_assignments
        if isinstance(assignment.get("layer_region_group"), dict)
    )
    if z_axis_region_count and occurrence_count:
        expanded["resolution_mode"] = "mixed_region_component_and_layer_region_occurrence"
    elif z_axis_region_count:
        expanded["resolution_mode"] = "z_axis_region"
    else:
        expanded["resolution_mode"] = "layer_region_occurrence"
    expanded["effective_assignment_count"] = len(expanded_assignments)
    expanded["effective_total_region_deposition_e_mm"] = sum(expanded_lengths)
    expanded["layer_region_expansion"] = {
        "source_assignment_count": len(assignments),
        "expanded_event_count": len(expanded_assignments),
        "layer_region_occurrence_assignment_count": occurrence_count,
        "z_axis_region_assignment_count": z_axis_region_count,
        "z_axis_assignment_index_remap": {
            str(source_index): target_index
            for source_index, target_index in sorted(
                z_axis_assignment_index_remap.items()
            )
        },
        "skipped_events": skipped,
        "ordering": layer_region_ordering,
        "gradient_sampling": "deposition_midpoint_or_layer_midpoint",
    }

    summary = {
        "source_assignment_count": len(assignments),
        "expanded_event_count": len(expanded_assignments),
        "layer_region_occurrence_assignment_count": occurrence_count,
        "z_axis_region_assignment_count": z_axis_region_count,
        "skipped_event_count": len(skipped),
        "total_region_deposition_e_mm": sum(expanded_lengths),
        "layer_count": len({
            int(event.get("layer_index", 0))
            for event in usable_events
        }),
        "region_count": len({
            str(event.get("region_name", ""))
            for event in usable_events
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
