from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.property_guided.property_library import (
    PropertyRequirement,
    canonical_material_pair,
    load_property_library,
    property_library_summary,
    select_gradient_sequence,
    select_property_candidate,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESOLVED_OUTPUT_PATH = PROJECT_ROOT / "test_sample" / "derived" / "property_guided" / "resolved_property_program.json"
DEFAULT_SUMMARY_PATH = PROJECT_ROOT / "test_sample" / "derived" / "property_guided" / "resolved_property_program_summary.json"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def normalize_assignment_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"property-guided", "property_guided", "propertyguided", "guided"}:
        return "property_guided"
    return "manual"


def _requirement_from_guided_payload(payload: dict[str, Any], assignment_type: str) -> PropertyRequirement:
    return PropertyRequirement(
        required_property_type=payload.get("required_property_type"),
        target_Eb_MPa=payload.get("target_Eb_MPa"),
        Eb_tolerance_percent=payload.get("Eb_tolerance_percent"),
        Eb_weight=float(payload.get("Eb_weight", 1.0)),
        min_elongation_percent=payload.get("min_elongation_percent"),
        target_elongation_percent=payload.get("target_elongation_percent"),
        elongation_weight=float(payload.get("elongation_weight", 1.0)),
        max_R0_ohm=payload.get("max_R0_ohm"),
        target_R0_ohm=payload.get("target_R0_ohm"),
        R0_weight=float(payload.get("R0_weight", 1.0)),
        min_GF=payload.get("min_GF"),
        target_GF=payload.get("target_GF"),
        GF_weight=float(payload.get("GF_weight", 1.0)),
        target_color=payload.get("target_color"),
        color_tolerance=payload.get("color_tolerance"),
        color_weight=float(payload.get("color_weight", 1.0)),
        allowed_material_pairs=list(payload.get("allowed_material_pairs", [])),
        gradient_enabled=assignment_type == "Gradient",
        gradient_property=payload.get("gradient_property"),
        gradient_start_value=payload.get("gradient_start_value"),
        gradient_end_value=payload.get("gradient_end_value"),
        gradient_direction=str(payload.get("gradient_direction", "printing")),
        gradient_type=str(payload.get("gradient_type", "linear")),
        gradient_steps=payload.get("gradient_steps"),
        allow_fallback=bool(payload.get("allow_fallback", True)),
    )


def _to_manual_property_assignment(assignment: dict[str, Any], selection_payload: dict[str, Any]) -> dict[str, Any]:
    resolved = dict(assignment)
    candidate = selection_payload["candidate"]
    material_pair = candidate["material_pair"]
    ratios = candidate["material_ratios"]
    material_start = material_pair[0]
    material_end = material_pair[1]
    start_ratio = float(ratios.get(material_start, 100.0))
    end_ratio = float(ratios.get(material_end, 0.0))
    resolved["material_start"] = material_start
    resolved["material_end"] = material_end
    resolved["material_start_ratio"] = start_ratio
    resolved["material_end_ratio"] = end_ratio
    resolved["material_count"] = 1 if end_ratio <= 0.0 or material_start == material_end else 2
    resolved["eta"] = float(candidate["eta"] or 0.0)
    resolved["property_guided_resolution"] = selection_payload
    return resolved


def _to_manual_gradient_assignment(assignment: dict[str, Any], gradient_payload: dict[str, Any]) -> dict[str, Any]:
    resolved = dict(assignment)
    sequence = gradient_payload.get("sequence", [])
    if not sequence:
        resolved["property_guided_resolution"] = gradient_payload
        return resolved

    first_candidate = sequence[0]["candidate"]
    material_pair = first_candidate["material_pair"]
    resolved["Property_type"] = "Gradient"
    resolved["material_start"] = material_pair[0]
    resolved["material_end"] = material_pair[1]
    resolved["material_count"] = 2 if material_pair[0] != material_pair[1] else 1
    resolved["gradient_steps"] = len(sequence)
    resolved["gradient_direction"] = str(
        assignment.get(
            "gradient_direction",
            gradient_payload.get("gradient_direction", "printing"),
        )
    )
    resolved["resolved_step_targets"] = []
    eta_values = []
    for item in sequence:
        candidate = item["candidate"]
        material_pair = candidate["material_pair"]
        ratios = candidate["material_ratios"]
        material_start = material_pair[0]
        material_end = material_pair[1]
        eta_value = float(candidate["eta"] or 0.0)
        eta_values.append(eta_value)
        resolved["resolved_step_targets"].append(
            {
                "step_index": int(item["step_index"]),
                "material_start": material_start,
                "material_end": material_end,
                "material_start_ratio": float(ratios.get(material_start, 100.0)),
                "material_end_ratio": float(ratios.get(material_end, 0.0)),
                "eta": eta_value,
                "target_value": item["target_value"],
                "reported_value": item["reported_value"],
                "candidate_id": candidate["id"],
                "candidate_source_sheet": candidate["source_sheet"],
                "candidate_source_row": candidate["source_row"],
            }
        )
    resolved["eta"] = max(eta_values) if eta_values else float(assignment.get("eta", 0.0))
    resolved["property_guided_resolution"] = gradient_payload
    return resolved


def resolve_property_guided_program(
    property_program: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    resolved_program = json.loads(json.dumps(property_program))
    assignments = resolved_program.get("assignments", [])
    guided_assignment_indices: list[int] = []
    for index, assignment in enumerate(assignments):
        assignment_mode = normalize_assignment_mode(
            assignment.get("assignment_mode") or assignment.get("Assignment_mode")
        )
        assignment["assignment_mode"] = assignment_mode
        if assignment_mode == "property_guided":
            guided_assignment_indices.append(index)

    resolution_summary: dict[str, Any] = {
        "library_summary": None,
        "library_loaded": False,
        "guided_assignment_count": len(guided_assignment_indices),
        "resolved_assignments": [],
    }
    if not guided_assignment_indices:
        resolution_summary["skip_reason"] = (
            "No property-guided assignments; the source workbook is not required."
        )
        return resolved_program, resolution_summary

    library = load_property_library()
    resolution_summary["library_summary"] = property_library_summary(library)
    resolution_summary["library_loaded"] = True

    for index, assignment in enumerate(assignments):
        if index not in guided_assignment_indices:
            continue
        assignment_mode = "property_guided"

        assignment_type = str(assignment.get("Property_type", "Property"))
        guided_payload = dict(assignment.get("property_guided") or {})
        requirement = _requirement_from_guided_payload(guided_payload, assignment_type)
        if assignment_type == "Gradient":
            gradient_payload = select_gradient_sequence(requirement, library)
            gradient_payload["gradient_direction"] = requirement.gradient_direction
            gradient_payload["requirement"] = requirement.to_dict()
            resolved_assignment = _to_manual_gradient_assignment(assignment, gradient_payload)
            resolved_program["assignments"][index] = resolved_assignment
            resolution_summary["resolved_assignments"].append(
                {
                    "assignment_index": assignment.get("assignment_index"),
                    "assignment_type": assignment_type,
                    "assignment_mode": assignment_mode,
                    "resolved_material_pair": canonical_material_pair(
                        resolved_assignment.get("material_start", ""),
                        resolved_assignment.get("material_end", ""),
                    ),
                    "gradient_step_count": len(resolved_assignment.get("resolved_step_targets", [])),
                    "resolution": gradient_payload,
                }
            )
            continue

        selection = select_property_candidate(requirement, library)
        selection_payload = selection.to_dict()
        if selection.candidate is not None:
            resolved_program["assignments"][index] = _to_manual_property_assignment(assignment, selection_payload)
        else:
            resolved_program["assignments"][index]["property_guided_resolution"] = selection_payload
        resolution_summary["resolved_assignments"].append(
            {
                "assignment_index": assignment.get("assignment_index"),
                "assignment_type": assignment_type,
                "assignment_mode": assignment_mode,
                "resolved_material_pair": (
                    canonical_material_pair(*selection.candidate.material_pair)
                    if selection.candidate is not None
                    else None
                ),
                "resolution": selection_payload,
            }
        )

    return resolved_program, resolution_summary


def resolve_property_guided_program_to_path(
    source_path: Path,
    resolved_output_path: Path = DEFAULT_RESOLVED_OUTPUT_PATH,
    summary_output_path: Path = DEFAULT_SUMMARY_PATH,
) -> tuple[Path, dict[str, Any]]:
    property_program = load_json(source_path)
    resolved_program, summary = resolve_property_guided_program(property_program)
    summary["source_property_program"] = str(source_path)
    summary["resolved_property_program"] = str(resolved_output_path)
    save_json(resolved_output_path, resolved_program)
    save_json(summary_output_path, summary)
    return resolved_output_path, summary


if __name__ == "__main__":
    resolved_path, summary = resolve_property_guided_program_to_path(
        PROJECT_ROOT / "input" / "config" / "Property_sample.json"
    )
    print(f"Saved resolved property program to: {resolved_path}")
    print(f"Resolved assignments: {len(summary.get('resolved_assignments', []))}")
