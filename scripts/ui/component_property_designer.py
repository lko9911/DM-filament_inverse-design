from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.utils.property_excel_lookup import (
    COLOR_PROFILE_OPTIONS,
    normalize_color_profile_key,
    resolve_color_recipe,
)

DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "input" / "config" / "Property_sample_UIver.json"
UNKNOWN_MATERIAL = "UNKNOWN"
NO_MATERIAL = "NO_MATERIAL"
AUTO_MIXED_ETA = 2.0
MIN_GRADIENT_STEPS = 1
MAX_GRADIENT_STEPS = 99
COLOR_OPTIONS = list(COLOR_PROFILE_OPTIONS)
MATERIAL_OPTIONS = [
    *COLOR_OPTIONS,
    "PLA",
    "CPLA",
    "TPU",
    "PETG",
    "SMP",
    UNKNOWN_MATERIAL,
    NO_MATERIAL,
]
PROPERTY_TYPE_OPTIONS = ["Property", "Gradient"]
GRADIENT_DIRECTION_OPTIONS = ["printing", "layer"]
ASSIGNMENT_MODE_OPTIONS = ["manual", "property_guided"]
PROPERTY_TARGET_OPTIONS = ["Eb", "elongation", "R0", "GF", "color"]
COLOR_LABEL_SEQUENCE = [
    ("custom", None),
    ("pure", "MAGENTA"),
    ("mix", ("MAGENTA", "YELLOW")),
    ("pure", "YELLOW"),
    ("mix", ("YELLOW", "CYAN")),
    ("pure", "CYAN"),
    ("mix", ("CYAN", "MAGENTA")),
    ("pure", "PURPLE"),
]
COLOR_LABEL_SEQUENCE_RANK = {
    key: rank
    for rank, key in enumerate(COLOR_LABEL_SEQUENCE)
}


def normalize_gradient_direction(value: object) -> str:
    normalized = str(value).strip().lower().replace("_", "-").replace(" ", "-")
    if normalized in {"layer", "z", "z-axis", "zaxis"}:
        return "layer"
    return "printing"


def component_display_name(component: "ComponentModel") -> str:
    return str(component.display_name or component.path.stem or component.path.name)


def expand_color_label_token(value: str) -> str:
    return {
        "M": "MAGENTA",
        "C": "CYAN",
        "Y": "YELLOW",
        "P": "PURPLE",
        "W": "WHITE",
        "B": "BLACK",
    }.get(value.upper(), value.upper())


def component_color_percentages(component: "ComponentModel") -> dict[str, float]:
    percentages: dict[str, float] = {}
    name = component_display_name(component).upper()
    tokens = [token for token in re.split(r"[^A-Z0-9.]+", name) if token]
    for token in tokens:
        match = re.fullmatch(r"([MCY])(\d+(?:\.\d+)?)", token)
        if match is None:
            continue
        material = expand_color_label_token(match.group(1))
        percentages[material] = max(percentages.get(material, 0.0), float(match.group(2)))
    if not percentages:
        pure_tokens = [expand_color_label_token(token) for token in tokens if token in {"M", "C", "Y"}]
        if len(set(pure_tokens)) == 1:
            percentages[pure_tokens[0]] = 100.0
    if len(percentages) == 2:
        first_material, first_value = next(iter(percentages.items()))
        second_material, second_value = list(percentages.items())[1]
        if abs((first_value + second_value) - 100.0) > 0.5 and 0.0 <= first_value <= 100.0:
            percentages[second_material] = max(0.0, 100.0 - first_value)
    return percentages


def infer_color_label_from_component_name(component: "ComponentModel") -> str | None:
    name = component_display_name(component).upper()
    tokens = [token for token in re.split(r"[^A-Z0-9.]+", name) if token]
    if "PURPLE" in name or "PUPLE" in name:
        return "PURPLE"
    percentages = component_color_percentages(component)
    for first, second in (("MAGENTA", "YELLOW"), ("YELLOW", "CYAN"), ("CYAN", "MAGENTA")):
        if first in percentages and second in percentages:
            first_key = first[0]
            second_key = second[0]
            return f"{first_key}{int(round(percentages[first]))}_{second_key}{int(round(percentages[second]))}"
    for material, key in (("MAGENTA", "M100"), ("YELLOW", "Y100"), ("CYAN", "C100")):
        if material in name or key in tokens or name.strip() == key:
            return key
    return None


def component_copy_order_rank(component: "ComponentModel") -> int:
    name = component_display_name(component).upper()
    match = re.search(r"\(\s*(\d+)\s*\)", name)
    if match is None:
        return 0
    return -int(match.group(1))


def component_pair_role_rank(component: "ComponentModel") -> int:
    tokens = {token for token in re.split(r"[^A-Z0-9]+", component_display_name(component).upper()) if token}
    if "BLOCK" in tokens:
        return 0
    if "PURGE" in tokens:
        return 1
    return 2


def component_leading_purge_rank(component: "ComponentModel") -> int:
    tokens = {token for token in re.split(r"[^A-Z0-9]+", component_display_name(component).upper()) if token}
    if "CUSTOM" in tokens:
        return 0
    if "PURGE" in tokens and tokens.intersection({"INITIAL", "INIT", "START", "FIRST", "PRIME"}):
        return 0
    return 1


def component_layer_order_rank(component: "ComponentModel") -> int:
    name = component_display_name(component).upper()
    match = re.search(r"\bLAYER\s*[_\-\s]*(\d+)(?=\D|$)", name)
    if match is None:
        match = re.search(r"\bL\s*[_\-\s]*(\d+)(?=\D|$)", name)
    if match is not None:
        return int(match.group(1))

    tokens = [token for token in re.split(r"[^A-Z0-9]+", name) if token]
    for index, token in enumerate(tokens[:-1]):
        if token in {"LAYER", "L"} and tokens[index + 1].isdigit():
            return int(tokens[index + 1])
    return 10000


def component_color_order_key(component: "ComponentModel") -> tuple[int, ...]:
    name = component_display_name(component).upper()
    percentages = component_color_percentages(component)
    copy_rank = component_copy_order_rank(component)
    pair_role_rank = component_pair_role_rank(component)
    leading_purge_rank = component_leading_purge_rank(component)
    layer_rank = component_layer_order_rank(component)

    if "CUSTOM" in name:
        return (
            leading_purge_rank,
            layer_rank,
            COLOR_LABEL_SEQUENCE_RANK[("custom", None)],
            0,
            0,
            copy_rank,
            pair_role_rank,
            component.index,
        )
    if "PURPLE" in name or "PUPLE" in name:
        return (
            leading_purge_rank,
            layer_rank,
            COLOR_LABEL_SEQUENCE_RANK[("pure", "PURPLE")],
            0,
            0,
            copy_rank,
            pair_role_rank,
            component.index,
        )
    if "MAGENTA" in name or percentages.get("MAGENTA") == 100.0 or name.strip() in {"M", "M100"}:
        return (
            leading_purge_rank,
            layer_rank,
            COLOR_LABEL_SEQUENCE_RANK[("pure", "MAGENTA")],
            0,
            0,
            copy_rank,
            pair_role_rank,
            component.index,
        )
    if "YELLOW" in name or percentages.get("YELLOW") == 100.0 or name.strip() in {"Y", "Y100"}:
        return (
            leading_purge_rank,
            layer_rank,
            COLOR_LABEL_SEQUENCE_RANK[("pure", "YELLOW")],
            0,
            0,
            copy_rank,
            pair_role_rank,
            component.index,
        )
    if "CYAN" in name or percentages.get("CYAN") == 100.0 or name.strip() in {"C", "C100"}:
        return (
            leading_purge_rank,
            layer_rank,
            COLOR_LABEL_SEQUENCE_RANK[("pure", "CYAN")],
            0,
            0,
            copy_rank,
            pair_role_rank,
            component.index,
        )

    if {"MAGENTA", "YELLOW"}.issubset(percentages):
        rank = COLOR_LABEL_SEQUENCE_RANK[("mix", ("MAGENTA", "YELLOW"))]
        return (
            leading_purge_rank,
            layer_rank,
            rank,
            -int(round(percentages["MAGENTA"] * 1000.0)),
            int(round(percentages["YELLOW"] * 1000.0)),
            copy_rank,
            pair_role_rank,
            component.index,
        )
    if {"YELLOW", "CYAN"}.issubset(percentages):
        rank = COLOR_LABEL_SEQUENCE_RANK[("mix", ("YELLOW", "CYAN"))]
        return (
            leading_purge_rank,
            layer_rank,
            rank,
            -int(round(percentages["YELLOW"] * 1000.0)),
            int(round(percentages["CYAN"] * 1000.0)),
            copy_rank,
            pair_role_rank,
            component.index,
        )
    if {"CYAN", "MAGENTA"}.issubset(percentages):
        rank = COLOR_LABEL_SEQUENCE_RANK[("mix", ("CYAN", "MAGENTA"))]
        return (
            leading_purge_rank,
            layer_rank,
            rank,
            -int(round(percentages["CYAN"] * 1000.0)),
            int(round(percentages["MAGENTA"] * 1000.0)),
            copy_rank,
            pair_role_rank,
            component.index,
        )

    return (leading_purge_rank, layer_rank, len(COLOR_LABEL_SEQUENCE), 0, 0, copy_rank, pair_role_rank, component.index)


def apply_color_label_default_order(
    components: list["ComponentModel"],
    states: dict[int, dict[str, object]],
) -> None:
    for order, component in enumerate(sorted(components, key=component_color_order_key), start=1):
        states[component.index]["order"] = order


def infer_material_from_component_name(component: "ComponentModel") -> str | None:
    inferred_color_label = infer_color_label_from_component_name(component)
    if inferred_color_label:
        return normalize_color_profile_key(inferred_color_label)

    name = component_display_name(component).upper()
    tokens = [token for token in re.split(r"[^A-Z]+", name) if token]
    token_set = set(tokens)
    abbreviation_map = {
        "B": "BLACK",
        "W": "WHITE",
        "C": "CYAN",
        "M": "MAGENTA",
        "Y": "YELLOW",
    }
    expanded_tokens = {abbreviation_map.get(token, token) for token in token_set}

    aliases = {
        "BLACK": ("BLACK", "BLK"),
        "WHITE": ("WHITE",),
        "M100": ("MAGENTA",),
        "Y100": ("YELLOW",),
        "C100": ("CYAN",),
    }
    for material, tokens in aliases.items():
        if any(token in name for token in tokens):
            return material
    if len(expanded_tokens) == 1:
        only_token = next(iter(expanded_tokens))
        normalized = normalize_color_profile_key(only_token)
        if normalized in COLOR_OPTIONS:
            return normalized
    return None


def infer_gradient_steps_from_component_name(component: "ComponentModel") -> int | None:
    name = component_display_name(component).upper()
    match = re.search(r"\b(\d+)\s*STEP\b", name)
    if match is None:
        match = re.search(r"_(\d+)\s*STEP\b", name)
    if match is None:
        return None
    try:
        steps = int(match.group(1))
    except ValueError:
        return None
    return steps if steps > 1 else None


@dataclass
class ExtrusionSegment:
    x0: float
    y0: float
    z0: float
    x1: float
    y1: float
    z1: float
    e_delta: float


@dataclass
class ComponentModel:
    index: int
    path: Path
    segments: list[ExtrusionSegment]
    total_e: float
    layer_count: int
    min_z: float | None
    max_z: float | None
    display_name: str | None = None

    @property
    def label(self) -> str:
        return f"C{self.index}: {self.display_name or self.path.name}"


def strip_comment(line: str) -> str:
    return line.split(";", 1)[0].strip()


def parse_words(line: str) -> dict[str, float]:
    words: dict[str, float] = {}
    for token in line.split():
        if len(token) < 2:
            continue
        key = token[0].upper()
        if key in {"G", "M", "X", "Y", "Z", "E", "F"}:
            try:
                words[key] = float(token[1:])
            except ValueError:
                pass
    return words


def parse_component_gcode(path: Path, index: int) -> ComponentModel:
    x = y = z = e = 0.0
    absolute_xyz = True
    absolute_e = True
    segments: list[ExtrusionSegment] = []
    total_e = 0.0
    z_values: set[float] = set()

    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for raw_line in handle:
            line = strip_comment(raw_line)
            if not line:
                continue
            words = parse_words(line)
            g_code = int(words["G"]) if "G" in words else None
            m_code = int(words["M"]) if "M" in words else None

            if g_code == 90:
                absolute_xyz = True
                continue
            if g_code == 91:
                absolute_xyz = False
                continue
            if m_code == 82:
                absolute_e = True
                continue
            if m_code == 83:
                absolute_e = False
                continue
            if g_code == 92:
                if "X" in words:
                    x = words["X"]
                if "Y" in words:
                    y = words["Y"]
                if "Z" in words:
                    z = words["Z"]
                if "E" in words:
                    e = words["E"]
                continue
            if g_code not in {0, 1, 2, 3}:
                continue

            next_x = words["X"] if "X" in words and absolute_xyz else x + words.get("X", 0.0)
            next_y = words["Y"] if "Y" in words and absolute_xyz else y + words.get("Y", 0.0)
            next_z = words["Z"] if "Z" in words and absolute_xyz else z + words.get("Z", 0.0)
            if "E" in words:
                next_e = words["E"] if absolute_e else e + words["E"]
            else:
                next_e = e

            e_delta = next_e - e
            if e_delta > 0.0 and ((next_x, next_y, next_z) != (x, y, z)):
                segments.append(ExtrusionSegment(x, y, z, next_x, next_y, next_z, e_delta))
                total_e += e_delta
                z_values.add(round(next_z, 5))

            x, y, z, e = next_x, next_y, next_z, next_e

    ordered_z = sorted(z_values)
    return ComponentModel(
        index=index,
        path=path,
        segments=segments,
        total_e=total_e,
        layer_count=len(ordered_z),
        min_z=ordered_z[0] if ordered_z else None,
        max_z=ordered_z[-1] if ordered_z else None,
    )


def clamp_int(value: object, default: int, min_value: int = 0, max_value: int = 9999) -> int:
    try:
        return max(min_value, min(max_value, int(float(str(value).strip()))))
    except (TypeError, ValueError):
        return default


def clamp_float(value: object, default: float, min_value: float = 0.0, max_value: float = 9999.0) -> float:
    try:
        return max(min_value, min(max_value, float(str(value).strip())))
    except (TypeError, ValueError):
        return default


def clamp_gradient_steps(value: object, default: int = MIN_GRADIENT_STEPS) -> int:
    return clamp_int(value, default, MIN_GRADIENT_STEPS, MAX_GRADIENT_STEPS)


def component_voxel_count(total_e: float, voxel_threshold_e: float) -> int:
    if voxel_threshold_e <= 0.0:
        return 1
    return max(1, int(math.ceil(max(total_e, 0.0) / voxel_threshold_e)))


def build_property_payload(
    components: list[ComponentModel],
    states: dict[int, dict[str, object]],
    voxel_threshold_e: float,
    brighter_mode: bool = False,
    resolve_color_properties: bool = True,
) -> dict[str, object]:
    ordered_components = sorted(
        [component for component in components if bool(states[component.index].get("enabled", True))],
        key=lambda item: (int(states[item.index]["order"]), item.index),
    )
    property_components = [
        component
        for component in ordered_components
        if str(states[component.index].get("property_type", "Property")) == "Property"
    ]
    output_index_by_component = {
        component.index: output_index
        for output_index, component in enumerate(ordered_components, start=1)
    }
    assignments: list[dict[str, object]] = []
    next_voxel = 1
    layer_start = 1

    for component in ordered_components:
        state = states[component.index]
        voxels = component_voxel_count(component.total_e, voxel_threshold_e)
        start_voxel = next_voxel
        end_voxel = next_voxel + voxels - 1
        layer_count = max(1, component.layer_count)
        assignment: dict[str, object] = {
            "assignment_index": len(assignments) + 1,
            "source_component_index": component.index,
            "source_component_name": component.display_name or component.path.name,
            "start_voxel_index": start_voxel,
            "end_voxel_index": end_voxel,
            "start_layer": layer_start,
            "end_layer": layer_start + layer_count - 1,
            "Property_type": str(state["property_type"]),
            "gradient_steps": (
                clamp_gradient_steps(state["gradient_steps"])
                if str(state["property_type"]) == "Gradient"
                else 1
            ),
            "gradient_direction": normalize_gradient_direction(state["gradient_direction"]),
            "eta": clamp_float(state["eta"], 0.0, 0.0, 999.0),
            "eta_mode": str(state.get("eta_mode", "auto")).strip().lower(),
            "assignment_mode": str(state.get("assignment_mode", "manual")),
        }

        if assignment["assignment_mode"] == "property_guided":
            assignment["eta_mode"] = "guided"
            assignment["property_guided"] = {
                "required_property_type": str(state.get("required_property_type", "Eb")),
                "target_Eb_MPa": clamp_float(state.get("target_Eb_MPa"), 0.0, 0.0, 999999.0)
                if state.get("target_Eb_MPa") not in {None, ""}
                else None,
                "min_elongation_percent": clamp_float(state.get("min_elongation_percent"), 0.0, 0.0, 999999.0)
                if state.get("min_elongation_percent") not in {None, ""}
                else None,
                "target_elongation_percent": clamp_float(state.get("target_elongation_percent"), 0.0, 0.0, 999999.0)
                if state.get("target_elongation_percent") not in {None, ""}
                else None,
                "max_R0_ohm": clamp_float(state.get("max_R0_ohm"), 0.0, 0.0, 999999999.0)
                if state.get("max_R0_ohm") not in {None, ""}
                else None,
                "target_R0_ohm": clamp_float(state.get("target_R0_ohm"), 0.0, 0.0, 999999999.0)
                if state.get("target_R0_ohm") not in {None, ""}
                else None,
                "min_GF": clamp_float(state.get("min_GF"), 0.0, 0.0, 999999.0)
                if state.get("min_GF") not in {None, ""}
                else None,
                "target_GF": clamp_float(state.get("target_GF"), 0.0, 0.0, 999999.0)
                if state.get("target_GF") not in {None, ""}
                else None,
                "gradient_property": str(state.get("gradient_property", "Eb")),
                "gradient_start_value": clamp_float(state.get("gradient_start_value"), 0.0, 0.0, 999999.0)
                if state.get("gradient_start_value") not in {None, ""}
                else None,
                "gradient_end_value": clamp_float(state.get("gradient_end_value"), 0.0, 0.0, 999999.0)
                if state.get("gradient_end_value") not in {None, ""}
                else None,
                "gradient_direction": normalize_gradient_direction(
                    state.get("gradient_direction", "printing")
                ),
                "gradient_type": "linear",
                "gradient_steps": clamp_gradient_steps(state.get("gradient_steps")),
                "allow_fallback": bool(state.get("allow_fallback", True)),
            }
        elif assignment["Property_type"] == "Gradient":
            assignment["eta_mode"] = "manual"
            if not property_components:
                raise ValueError("At least one Property assignment is required before saving a Gradient assignment.")
            property_component_indices = {component.index for component in property_components}
            fallback_start = property_components[0].index
            fallback_end = property_components[-1].index
            start_component = clamp_int(state["property_start"], fallback_start, 1, max(1, len(components)))
            end_component = clamp_int(state["property_end"], fallback_end, 1, max(1, len(components)))
            if start_component not in property_component_indices:
                start_component = fallback_start
            if end_component not in property_component_indices:
                end_component = fallback_end
            assignment["Property_start"] = output_index_by_component.get(start_component, 1)
            assignment["Property_end"] = output_index_by_component.get(end_component, assignment["Property_start"])
        else:
            assignment_brighter_mode = bool(state.get("brighter_mode", brighter_mode))
            start_ratio = clamp_float(state["material_start_ratio"], 100.0, 0.0, 100.0)
            end_ratio = clamp_float(state["material_end_ratio"], 0.0, 0.0, 100.0)
            material_start = normalize_color_profile_key(state["material_start"])
            material_end = str(state["material_end"]).strip().upper()
            if material_start == NO_MATERIAL:
                material_start = UNKNOWN_MATERIAL
            if material_end == NO_MATERIAL:
                end_ratio = 0.0
            material_count = 1 if end_ratio <= 0.0 or material_end == NO_MATERIAL else 2
            if material_count == 1:
                start_ratio = 100.0
                end_ratio = 0.0
            color_recipe: dict[str, object] | None = None
            if resolve_color_properties and material_start not in {UNKNOWN_MATERIAL, NO_MATERIAL}:
                try:
                    color_recipe = resolve_color_recipe(
                        material_start,
                        brighter_mode=assignment_brighter_mode,
                        target_mpa=clamp_float(state.get("property_mpa", 0.0), 0.0, 0.0, 99999.0),
                        target_gf=clamp_float(state.get("property_gf", 0.0), 0.0, 0.0, 99999.0),
                    )
                except KeyError:
                    color_recipe = None
            if color_recipe is not None:
                material_count = int(color_recipe["material_count"])
                material_start = str(color_recipe["material_start"])
                material_end_value = color_recipe.get("material_end")
                material_end = str(material_end_value) if material_end_value else NO_MATERIAL
                start_ratio = float(color_recipe["material_start_ratio"])
                end_ratio = float(color_recipe["material_end_ratio"])
            eta_mode = "manual" if assignment["eta_mode"] == "manual" else "auto"
            fixed_recipe_eta = (
                color_recipe.get("fixed_eta")
                if color_recipe is not None
                else None
            )
            if fixed_recipe_eta is not None:
                eta_mode = "manual"
            assignment["eta_mode"] = eta_mode
            if fixed_recipe_eta is not None:
                assignment["eta"] = float(fixed_recipe_eta)
            elif eta_mode == "auto":
                if assignment_brighter_mode and material_start != "WHITE":
                    assignment["eta"] = 4.0 if material_count >= 2 else 2.0
                else:
                    assignment["eta"] = AUTO_MIXED_ETA if material_count >= 2 else 0.0
            assignment.update(
                {
                    "material_count": material_count,
                    "material_start": material_start,
                    "material_start_ratio": start_ratio,
                    "material_end_ratio": end_ratio,
                    "brighter_mode": assignment_brighter_mode,
                }
            )
            base_ratios = {material_start: start_ratio}
            if material_count >= 2:
                base_ratios[material_end] = end_ratio
            assignment["base_material_ratios"] = base_ratios
            if assignment_brighter_mode and material_start != "WHITE":
                final_ratios = {
                    material: ratio * 0.5
                    for material, ratio in base_ratios.items()
                }
                final_ratios["WHITE"] = final_ratios.get("WHITE", 0.0) + 50.0
                assignment["final_material_ratios"] = final_ratios
            else:
                assignment["final_material_ratios"] = dict(base_ratios)
            if material_count >= 2:
                assignment["material_end"] = material_end
            if color_recipe is not None:
                assignment["requested_color"] = color_recipe["requested_color"]
                assignment["target_mpa"] = color_recipe["target_mpa"]
                assignment["target_gf"] = color_recipe["target_gf"]
                assignment["color_recipe"] = color_recipe
                if color_recipe.get("fixed_case_rows") is not None:
                    assignment["fixed_case_rows"] = list(color_recipe["fixed_case_rows"])

        assignments.append(assignment)
        next_voxel = end_voxel + 1
        layer_start = assignment["end_layer"] + 1

    return {
        "voxel_threshold_e": voxel_threshold_e,
        "voxel_count": max(0, next_voxel - 1),
        "preheat_prime_e": 0.0,
        "assignments": assignments,
    }


def default_state(component: ComponentModel) -> dict[str, object]:
    inferred_material = infer_material_from_component_name(component)
    inferred_gradient_steps = infer_gradient_steps_from_component_name(component)
    material_start = inferred_material or UNKNOWN_MATERIAL
    return {
        "visible": True,
        "enabled": True,
        "order": component.index,
        "property_type": "Gradient" if inferred_gradient_steps else "Property",
        "material_start": material_start,
        "material_end": MATERIAL_OPTIONS[component.index % len(MATERIAL_OPTIONS)],
        "material_start_ratio": 100.0,
        "material_end_ratio": 0.0,
        "gradient_steps": clamp_gradient_steps(inferred_gradient_steps) if inferred_gradient_steps else 1,
        "gradient_direction": "printing",
        "eta": AUTO_MIXED_ETA,
        "eta_mode": "auto",
        "property_mpa": 0.0,
        "property_gf": 0.0,
        "brighter_mode": False,
        "property_start": 1,
        "property_end": min(2, component.index),
        "assignment_mode": "manual",
        "required_property_type": "Eb",
        "target_Eb_MPa": None,
        "min_elongation_percent": None,
        "target_elongation_percent": None,
        "max_R0_ohm": None,
        "target_R0_ohm": None,
        "min_GF": None,
        "target_GF": None,
        "gradient_property": "Eb",
        "gradient_start_value": None,
        "gradient_end_value": None,
        "allow_fallback": True,
    }


def launch_ui(
    components: list[ComponentModel],
    output_path: Path,
    voxel_threshold_e: float,
    show_only_active: bool = True,
    preview_controller: object | None = None,
) -> None:
    try:
        import matplotlib.pyplot as plt
        from matplotlib.widgets import Button, CheckButtons, RadioButtons, TextBox
    except ImportError as exc:  # pragma: no cover - interactive dependency guard
        raise SystemExit("matplotlib is required for the component property designer UI.") from exc

    states = {component.index: default_state(component) for component in components}
    active_index = components[0].index

    fig = plt.figure(figsize=(15, 9), dpi=120, facecolor="#f5f5f2")
    preview_ax = fig.add_axes([0.04, 0.16, 0.58, 0.74], facecolor="#ffffff")
    check_ax = fig.add_axes([0.04, 0.04, 0.22, 0.08], facecolor="#ffffff")
    component_ax = fig.add_axes([0.66, 0.72, 0.28, 0.18], facecolor="#ffffff")
    type_ax = fig.add_axes([0.66, 0.58, 0.12, 0.09], facecolor="#ffffff")
    mat_start_ax = fig.add_axes([0.81, 0.45, 0.12, 0.22], facecolor="#ffffff")
    mat_end_ax = fig.add_axes([0.66, 0.29, 0.12, 0.22], facecolor="#ffffff")
    direction_ax = fig.add_axes([0.81, 0.29, 0.12, 0.09], facecolor="#ffffff")
    order_ax = fig.add_axes([0.66, 0.20, 0.07, 0.04], facecolor="#f5f5f2")
    steps_ax = fig.add_axes([0.75, 0.20, 0.07, 0.04], facecolor="#f5f5f2")
    eta_ax = fig.add_axes([0.84, 0.20, 0.07, 0.04], facecolor="#f5f5f2")
    ratio_start_ax = fig.add_axes([0.66, 0.12, 0.07, 0.04], facecolor="#f5f5f2")
    ratio_end_ax = fig.add_axes([0.75, 0.12, 0.07, 0.04], facecolor="#f5f5f2")
    prop_start_ax = fig.add_axes([0.84, 0.12, 0.07, 0.04], facecolor="#f5f5f2")
    prop_end_ax = fig.add_axes([0.84, 0.05, 0.07, 0.04], facecolor="#f5f5f2")
    save_ax = fig.add_axes([0.66, 0.04, 0.12, 0.05])
    status_ax = fig.add_axes([0.04, 0.91, 0.90, 0.06], facecolor="#f5f5f2")
    status_ax.axis("off")

    component_labels = [component.label for component in components]
    visible_labels = [f"C{component.index}" for component in components]
    visible_checks = CheckButtons(check_ax, visible_labels, [True] * len(components))
    component_radio = RadioButtons(component_ax, component_labels, active=0)
    type_radio = RadioButtons(type_ax, PROPERTY_TYPE_OPTIONS, active=0)
    mat_start_radio = RadioButtons(mat_start_ax, MATERIAL_OPTIONS, active=0)
    mat_end_radio = RadioButtons(mat_end_ax, MATERIAL_OPTIONS, active=1)
    direction_radio = RadioButtons(direction_ax, GRADIENT_DIRECTION_OPTIONS, active=0)
    order_box = TextBox(order_ax, "", initial="1", color="#ffffff", hovercolor="#eef2ff")
    steps_box = TextBox(steps_ax, "", initial="1", color="#ffffff", hovercolor="#eef2ff")
    eta_box = TextBox(eta_ax, "", initial="0", color="#ffffff", hovercolor="#eef2ff")
    ratio_start_box = TextBox(ratio_start_ax, "", initial="100", color="#ffffff", hovercolor="#eef2ff")
    ratio_end_box = TextBox(ratio_end_ax, "", initial="0", color="#ffffff", hovercolor="#eef2ff")
    prop_start_box = TextBox(prop_start_ax, "", initial="1", color="#ffffff", hovercolor="#eef2ff")
    prop_end_box = TextBox(prop_end_ax, "", initial="1", color="#ffffff", hovercolor="#eef2ff")
    save_button = Button(save_ax, "Save JSON", color="#dbeafe", hovercolor="#bfdbfe")
    status_text = status_ax.text(0.0, 0.5, "", ha="left", va="center", fontsize=10, family="monospace", color="#111827")

    for ax, title in [
        (component_ax, "Active Component"),
        (type_ax, "Property Type"),
        (mat_start_ax, "Start Material"),
        (mat_end_ax, "End Material"),
        (direction_ax, "Direction"),
    ]:
        ax.set_title(title, fontsize=10, color="#111827")
    for ax, title in [
        (order_ax, "order"),
        (steps_ax, "steps"),
        (eta_ax, "eta"),
        (ratio_start_ax, "start %"),
        (ratio_end_ax, "end %"),
        (prop_start_ax, "C start"),
        (prop_end_ax, "C end"),
    ]:
        ax.text(0.5, 1.18, title, transform=ax.transAxes, ha="center", va="bottom", fontsize=8.5, color="#374151")

    syncing = {"value": False}

    def active_component() -> ComponentModel:
        return next(component for component in components if component.index == active_index)

    def set_radio(radio: RadioButtons, labels: list[str], value: str) -> None:
        if value in labels and radio.value_selected != value:
            radio.set_active(labels.index(value))

    def sync_controls() -> None:
        state = states[active_index]
        syncing["value"] = True
        set_radio(type_radio, PROPERTY_TYPE_OPTIONS, str(state["property_type"]))
        set_radio(mat_start_radio, MATERIAL_OPTIONS, str(state["material_start"]))
        set_radio(mat_end_radio, MATERIAL_OPTIONS, str(state["material_end"]))
        set_radio(direction_radio, GRADIENT_DIRECTION_OPTIONS, str(state["gradient_direction"]))
        order_box.set_val(str(state["order"]))
        steps_box.set_val(str(state["gradient_steps"]))
        eta_box.set_val(str(state["eta"]))
        ratio_start_box.set_val(str(state["material_start_ratio"]))
        ratio_end_box.set_val(str(state["material_end_ratio"]))
        prop_start_box.set_val(str(state["property_start"]))
        prop_end_box.set_val(str(state["property_end"]))
        syncing["value"] = False

    def update_status(saved: bool = False) -> None:
        component = active_component()
        state = states[component.index]
        payload = build_property_payload(components, states, voxel_threshold_e)
        prefix = "Saved" if saved else "Ready"
        status_text.set_text(
            f"{prefix}: active C{component.index} | E={component.total_e:.3f} | "
            f"layers={component.layer_count} | use={state['enabled']} | order={state['order']} | "
            f"assignments={len(payload['assignments'])} | voxels={payload['voxel_count']}"
        )

    def refresh_plot(saved: bool = False) -> None:
        if preview_controller is not None and hasattr(preview_controller, "show_component"):
            preview_controller.show_component(active_index)
        preview_ax.clear()
        if preview_controller is not None:
            preview_ax.set_title("PyVista Component Preview", fontsize=13, fontweight="bold", color="#111827")
            preview_ax.axis("off")
            component = active_component()
            preview_ax.text(
                0.5,
                0.58,
                f"C{component.index}",
                transform=preview_ax.transAxes,
                ha="center",
                va="center",
                fontsize=30,
                fontweight="bold",
                color="#111827",
            )
            preview_ax.text(
                0.5,
                0.46,
                component.display_name or component.path.name,
                transform=preview_ax.transAxes,
                ha="center",
                va="center",
                fontsize=11,
                color="#374151",
                wrap=True,
            )
            preview_ax.text(
                0.5,
                0.36,
                "Object preview is shown in the PyVista window.",
                transform=preview_ax.transAxes,
                ha="center",
                va="center",
                fontsize=10,
                color="#6b7280",
            )
            update_status(saved=saved)
            fig.canvas.draw_idle()
            return

        preview_ax.set_title("G-code Component Preview", fontsize=13, fontweight="bold", color="#111827")
        preview_ax.set_aspect("equal", adjustable="datalim")
        preview_ax.grid(True, color="#e5e7eb", linewidth=0.6)
        colors = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c"]
        has_segment = False
        for component in components:
            if show_only_active and component.index != active_index:
                continue
            if not states[component.index]["visible"]:
                continue
            color = colors[(component.index - 1) % len(colors)]
            linewidth = 1.8 if component.index == active_index else 0.9
            alpha = 0.95 if component.index == active_index else 0.45
            for segment in component.segments:
                preview_ax.plot(
                    [segment.x0, segment.x1],
                    [segment.y0, segment.y1],
                    color=color,
                    linewidth=linewidth,
                    alpha=alpha,
                )
                has_segment = True
        if not has_segment:
            preview_ax.text(0.5, 0.5, "No extrusion paths found", transform=preview_ax.transAxes, ha="center", va="center")
        preview_ax.set_xlabel("X")
        preview_ax.set_ylabel("Y")
        update_status(saved=saved)
        fig.canvas.draw_idle()

    def save_active_text_fields() -> None:
        state = states[active_index]
        state["order"] = clamp_int(order_box.text, int(state["order"]), 1, 99)
        if str(state.get("property_type")) == "Gradient":
            state["gradient_steps"] = clamp_gradient_steps(steps_box.text, int(state["gradient_steps"]))
        else:
            state["gradient_steps"] = 1
        state["eta"] = clamp_float(eta_box.text, float(state["eta"]), 0.0, 999.0)
        state["material_start_ratio"] = clamp_float(ratio_start_box.text, float(state["material_start_ratio"]), 0.0, 100.0)
        state["material_end_ratio"] = clamp_float(ratio_end_box.text, float(state["material_end_ratio"]), 0.0, 100.0)
        state["property_start"] = clamp_int(prop_start_box.text, int(state["property_start"]), 1, len(components))
        state["property_end"] = clamp_int(prop_end_box.text, int(state["property_end"]), 1, len(components))

    def on_component(label: str) -> None:
        nonlocal active_index
        if syncing["value"]:
            return
        save_active_text_fields()
        active_index = components[component_labels.index(label)].index
        if show_only_active:
            for component in components:
                states[component.index]["visible"] = component.index == active_index
        sync_controls()
        refresh_plot()

    def on_visible(label: str) -> None:
        index = int(label[1:])
        states[index]["enabled"] = not bool(states[index].get("enabled", True))
        states[index]["visible"] = bool(states[index]["enabled"])
        refresh_plot()

    def on_type(label: str) -> None:
        if syncing["value"]:
            return
        states[active_index]["property_type"] = label
        if label == "Gradient":
            states[active_index]["gradient_steps"] = clamp_gradient_steps(states[active_index]["gradient_steps"])
        else:
            states[active_index]["gradient_steps"] = 1
        sync_controls()
        refresh_plot()

    def on_start_material(label: str) -> None:
        if syncing["value"]:
            return
        states[active_index]["material_start"] = label
        refresh_plot()

    def on_end_material(label: str) -> None:
        if syncing["value"]:
            return
        states[active_index]["material_end"] = label
        refresh_plot()

    def on_direction(label: str) -> None:
        if syncing["value"]:
            return
        states[active_index]["gradient_direction"] = normalize_gradient_direction(label)
        refresh_plot()

    def on_text_submit(_text: str) -> None:
        if syncing["value"]:
            return
        save_active_text_fields()
        sync_controls()
        refresh_plot()

    def on_save(_event=None) -> None:
        save_active_text_fields()
        payload = build_property_payload(components, states, voxel_threshold_e)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
        refresh_plot(saved=True)
        print(f"Saved property JSON: {output_path}")

    component_radio.on_clicked(on_component)
    visible_checks.on_clicked(on_visible)
    type_radio.on_clicked(on_type)
    mat_start_radio.on_clicked(on_start_material)
    mat_end_radio.on_clicked(on_end_material)
    direction_radio.on_clicked(on_direction)
    for box in [order_box, steps_box, eta_box, ratio_start_box, ratio_end_box, prop_start_box, prop_end_box]:
        box.on_submit(on_text_submit)
    save_button.on_clicked(on_save)

    fig.text(0.04, 0.97, "b-FDM Component Property Designer", fontsize=17, fontweight="bold", color="#111827")
    sync_controls()
    refresh_plot()
    try:
        plt.show()
    finally:
        if preview_controller is not None and hasattr(preview_controller, "close"):
            preview_controller.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create input/config/Property_sample.json from 1-5 component G-code files."
    )
    parser.add_argument("gcode_files", nargs="+", help="Component G-code files, in any initial order. Pass 1 to 5 files.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH), help="Output JSON path.")
    parser.add_argument("--voxel-threshold-e", type=float, default=2.0, help="E amount represented by one output voxel.")
    args = parser.parse_args()

    if not (1 <= len(args.gcode_files) <= 5):
        raise SystemExit("Pass between 1 and 5 component G-code files.")
    if args.voxel_threshold_e <= 0.0:
        raise SystemExit("--voxel-threshold-e must be greater than 0.")

    paths = [Path(item).resolve() for item in args.gcode_files]
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise SystemExit(f"G-code file not found: {', '.join(missing)}")

    components = [parse_component_gcode(path, index + 1) for index, path in enumerate(paths)]
    for component in components:
        print(
            f"C{component.index}: {component.path.name} | "
            f"E={component.total_e:.6f} | layers={component.layer_count} | segments={len(component.segments)}"
        )
    launch_ui(components, Path(args.output).resolve(), float(args.voxel_threshold_e))


if __name__ == "__main__":
    main()
