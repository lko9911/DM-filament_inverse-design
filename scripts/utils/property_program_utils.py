from __future__ import annotations

import os
from pathlib import Path

RATIO_EPSILON = 1e-9
PROPERTY_PATH_ENV_KEY = "B_FDM_PROPERTY_PATH"


def resolve_property_program_path(default_path: str | Path = "input/config/Property_sample.json") -> Path:
    env_path = os.environ.get(PROPERTY_PATH_ENV_KEY)
    if env_path and env_path.strip():
        return Path(env_path).expanduser().resolve()

    project_root = Path(__file__).resolve().parents[2]
    config_dir = project_root / "input" / "config"
    property_candidates = sorted(
        config_dir.glob("Property*.json"),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    if property_candidates:
        return property_candidates[0].resolve()

    return (project_root / Path(default_path)).resolve()


def normalize_property_type(value: object) -> str:
    text = str(value or "").strip().lower()
    if text == "property":
        return "Property"
    if text == "gradient":
        return "Gradient"
    return ""


def get_assignment_property_type(
    property_program: dict[str, object],
    assignment: dict[str, object],
) -> str:
    assignment_type = normalize_property_type(assignment.get("Property_type"))
    if assignment_type:
        return assignment_type
    program_type = normalize_property_type(property_program.get("Property_type"))
    return program_type or "Gradient"


def get_property_type(property_program: dict[str, object]) -> str:
    normalized = normalize_property_type(property_program.get("Property_type"))
    return normalized or "Gradient"


def normalize_material_name(value: object) -> str:
    return str(value or "").strip().upper()


def normalize_requested_color(value: object) -> str:
    return str(value or "").strip().upper()


def get_assignment_requested_color(assignment: dict[str, object]) -> str:
    requested_color = normalize_requested_color(assignment.get("requested_color"))
    if requested_color:
        return requested_color
    color_recipe = assignment.get("color_recipe")
    if isinstance(color_recipe, dict):
        return normalize_requested_color(color_recipe.get("requested_color"))
    return ""


def get_fixed_requested_color_case_rows(assignment: dict[str, object]) -> list[str] | None:
    return None


def get_fixed_requested_color_material_pair(assignment: dict[str, object]) -> tuple[str, str] | None:
    return None


def get_fixed_requested_color_composition(assignment: dict[str, object]) -> dict[str, float] | None:
    return None


def normalize_ratio_value(value: object) -> float:
    ratio = float(value)
    return ratio / 100.0 if ratio > 1.0 else ratio


def build_assignment_lookup(property_program: dict[str, object]) -> dict[int, dict[str, object]]:
    lookup: dict[int, dict[str, object]] = {}
    for assignment in property_program.get("assignments", []):
        lookup[int(assignment["assignment_index"])] = assignment
    return lookup


def assignment_spatial_sort_key(assignment: dict[str, object]) -> tuple[int, int, int, int, int]:
    start_voxel_index = int(assignment.get("start_voxel_index", 10**9))
    end_voxel_index = int(assignment.get("end_voxel_index", 10**9))
    start_layer = int(assignment.get("start_layer", 10**9))
    end_layer = int(assignment.get("end_layer", 10**9))
    assignment_index = int(assignment.get("assignment_index", 10**9))
    return (start_voxel_index, end_voxel_index, start_layer, end_layer, assignment_index)


def get_assignments_in_spatial_order(property_program: dict[str, object]) -> list[dict[str, object]]:
    assignments = list(property_program.get("assignments", []))
    assignments.sort(key=assignment_spatial_sort_key)
    return assignments


def resolve_property_assignment_pure_material(
    property_program: dict[str, object],
    assignment_index: int,
) -> str:
    assignment_lookup = build_assignment_lookup(property_program)
    assignment = assignment_lookup.get(int(assignment_index))
    if assignment is None:
        raise ValueError(f"Referenced Property assignment not found: assignment_index={assignment_index}")

    assignment_type = get_assignment_property_type(property_program, assignment)
    if assignment_type != "Property":
        raise ValueError(
            "Gradient assignment references a non-Property assignment: "
            f"assignment_index={assignment_index}, Property_type={assignment_type or 'Gradient'}"
        )

    fixed_pair = get_fixed_requested_color_material_pair(assignment)
    if fixed_pair is not None:
        return fixed_pair[1]

    start_material = normalize_material_name(assignment.get("material_start"))
    end_material = normalize_material_name(assignment.get("material_end"))
    material_count = int(assignment.get("material_count", 2))

    if material_count <= 1 or not end_material:
        if not start_material:
            raise ValueError(f"Property assignment {assignment_index} has no material_start.")
        return start_material

    start_ratio_raw = assignment.get("material_start_ratio")
    end_ratio_raw = assignment.get("material_end_ratio")
    if start_ratio_raw is None or end_ratio_raw is None:
        raise ValueError(
            "Gradient currently requires referenced Property assignments to have explicit "
            f"material_start_ratio/material_end_ratio: assignment_index={assignment_index}"
        )

    start_ratio = normalize_ratio_value(start_ratio_raw)
    end_ratio = normalize_ratio_value(end_ratio_raw)

    if abs(start_ratio - 1.0) <= RATIO_EPSILON and end_ratio <= RATIO_EPSILON:
        return start_material
    if start_ratio <= RATIO_EPSILON and abs(end_ratio - 1.0) <= RATIO_EPSILON:
        return end_material
    if start_material and start_material == end_material:
        return start_material

    if start_ratio >= end_ratio:
        return start_material
    return end_material


def resolve_property_assignment_composition(
    property_program: dict[str, object],
    assignment_index: int,
) -> dict[str, float]:
    assignment_lookup = build_assignment_lookup(property_program)
    assignment = assignment_lookup.get(int(assignment_index))
    if assignment is None:
        raise ValueError(f"Referenced Property assignment not found: assignment_index={assignment_index}")

    assignment_type = get_assignment_property_type(property_program, assignment)
    if assignment_type != "Property":
        raise ValueError(
            "Gradient assignment references a non-Property assignment: "
            f"assignment_index={assignment_index}, Property_type={assignment_type or 'Gradient'}"
        )

    fixed_composition = get_fixed_requested_color_composition(assignment)
    if fixed_composition is not None:
        return fixed_composition

    start_material = normalize_material_name(assignment.get("material_start"))
    end_material = normalize_material_name(assignment.get("material_end"))
    material_count = int(assignment.get("material_count", 2))
    if not start_material:
        raise ValueError(f"Property assignment {assignment_index} has no material_start.")

    if material_count <= 1 or not end_material:
        return {start_material: 1.0}

    start_ratio_raw = assignment.get("material_start_ratio")
    end_ratio_raw = assignment.get("material_end_ratio")
    if start_ratio_raw is None or end_ratio_raw is None:
        raise ValueError(
            "Gradient currently requires referenced Property assignments to have explicit "
            f"material_start_ratio/material_end_ratio: assignment_index={assignment_index}"
        )

    start_ratio = normalize_ratio_value(start_ratio_raw)
    end_ratio = normalize_ratio_value(end_ratio_raw)
    total_ratio = start_ratio + end_ratio
    if total_ratio <= RATIO_EPSILON:
        return {start_material: 1.0}

    composition: dict[str, float] = {}
    composition[start_material] = composition.get(start_material, 0.0) + start_ratio / total_ratio
    if end_material:
        composition[end_material] = composition.get(end_material, 0.0) + end_ratio / total_ratio

    return {
        material: ratio
        for material, ratio in composition.items()
        if ratio > RATIO_EPSILON
    }


def resolve_gradient_endpoint_compositions(
    property_program: dict[str, object],
    assignment: dict[str, object],
) -> tuple[dict[str, float], dict[str, float]] | None:
    assignment_type = get_assignment_property_type(property_program, assignment)
    if assignment_type != "Gradient":
        return None

    direct_start_material = normalize_material_name(assignment.get("material_start"))
    direct_end_material = normalize_material_name(assignment.get("material_end"))
    if direct_start_material and direct_end_material:
        return {direct_start_material: 1.0}, {direct_end_material: 1.0}

    property_start = assignment.get("Property_start")
    property_end = assignment.get("Property_end")
    if property_start is None or property_end is None:
        return None

    return (
        resolve_property_assignment_composition(property_program, int(property_start)),
        resolve_property_assignment_composition(property_program, int(property_end)),
    )


def choose_material_pair_from_compositions(
    start_composition: dict[str, float],
    end_composition: dict[str, float],
    assignment_index: object = None,
) -> tuple[str, str]:
    material_names = sorted(
        {
            material
            for material, ratio in start_composition.items()
            if ratio > RATIO_EPSILON
        }
        | {
            material
            for material, ratio in end_composition.items()
            if ratio > RATIO_EPSILON
        }
    )
    if len(material_names) == 2:
        start_material = max(
            material_names,
            key=lambda material: (
                float(start_composition.get(material, 0.0))
                - float(end_composition.get(material, 0.0)),
                material == "CYAN",
                material,
            ),
        )
        end_material = material_names[0] if material_names[1] == start_material else material_names[1]
        return str(start_material), str(end_material)

    def dominant_material(composition: dict[str, float]) -> str | None:
        ranked = [
            (material, ratio)
            for material, ratio in sorted(composition.items(), key=lambda item: (-item[1], item[0]))
            if ratio > RATIO_EPSILON
        ]
        return ranked[0][0] if ranked else None

    start_material = dominant_material(start_composition)
    end_material = dominant_material(end_composition)
    if start_material is None and end_material is None:
        raise ValueError(f"Gradient assignment has no effective endpoint materials: assignment_index={assignment_index}")
    if start_material is None:
        start_material = end_material
    if end_material is None:
        end_material = start_material
    return str(start_material), str(end_material)


def resolve_assignment_material_pair(
    property_program: dict[str, object],
    assignment: dict[str, object],
) -> tuple[str, str]:
    assignment_type = get_assignment_property_type(property_program, assignment)
    if assignment_type == "Gradient":
        direct_start_material = normalize_material_name(assignment.get("material_start"))
        direct_end_material = normalize_material_name(assignment.get("material_end"))
        if direct_start_material and direct_end_material:
            return direct_start_material, direct_end_material
        property_start = assignment.get("Property_start")
        property_end = assignment.get("Property_end")
        if property_start is None or property_end is None:
            raise ValueError(
                "Gradient assignment requires Property_start/Property_end or direct material_start/material_end. "
                f"assignment_index={assignment.get('assignment_index')}"
            )
        return choose_material_pair_from_compositions(
            resolve_property_assignment_composition(property_program, int(property_start)),
            resolve_property_assignment_composition(property_program, int(property_end)),
            assignment.get("assignment_index"),
        )

    fixed_pair = get_fixed_requested_color_material_pair(assignment)
    if fixed_pair is not None:
        return fixed_pair

    start_material = normalize_material_name(assignment.get("material_start"))
    end_material = normalize_material_name(assignment.get("material_end"))
    material_count = int(assignment.get("material_count", 2))
    if material_count <= 1 or not end_material:
        end_material = start_material
    return start_material, end_material


def get_effective_gradient_steps(
    property_program: dict[str, object],
    assignment: dict[str, object],
) -> int:
    property_type = get_assignment_property_type(property_program, assignment)
    if property_type == "Property":
        return 1
    return int(assignment.get("gradient_steps", 0))
