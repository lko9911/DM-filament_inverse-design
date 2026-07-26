#!/usr/bin/env python3
"""
Convert a manual-filament-change G-code file to Prusa XL tool-change G-code.

The converter reads the material order from po.txt, inserts the initial tool
before the first extrusion move, and replaces each M600 with a Prusa XL tool
command plus an optional purge/prime block.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Iterable


material_to_tool = {
    400: "T0",  # White
    500: "T1",  # Black
    100: "T2",  # Cyan
    200: "T3",  # Magenta
    300: "T4",  # Yellow
}


COMMAND_RE = re.compile(r"^\s*([GMT]\d+(?:\.\d+)?)\b", re.IGNORECASE)
M600_RE = re.compile(r"^\s*M600\b", re.IGNORECASE)
AXIS_RE = re.compile(r"([XYZE])\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))", re.IGNORECASE)
G28_W_RE = re.compile(r"^\s*G28\s+W\b", re.IGNORECASE)
G28_XY_RE = re.compile(r"^\s*G28\b(?=.*\bX\s*0?\b)(?=.*\bY\s*0?\b)", re.IGNORECASE)
G28_Z_RE = re.compile(r"^\s*G28\b(?=.*\bZ\s*0?\b)", re.IGNORECASE)
G80_RE = re.compile(r"^\s*G80\b", re.IGNORECASE)
TEMP_RE = re.compile(r"^\s*M10[49]\b.*\bS\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))", re.IGNORECASE)
BARE_TEMP_RE = re.compile(r"^\s*(M10[49])\s+S([-+]?(?:\d+(?:\.\d*)?|\.\d+))(.*)$", re.IGNORECASE)


def _strip_comment(line: str) -> str:
    return line.split(";", 1)[0]


def _next_significant_line(lines: list[str], start_index: int) -> str | None:
    for line in lines[start_index:]:
        code = _strip_comment(line).strip()
        if code:
            return line
    return None


def _parse_numeric_rows(text: str) -> list[list[int]]:
    rows: list[list[int]] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].split(";", 1)[0].strip()
        if not line:
            continue
        numbers = re.findall(r"[-+]?\d+", line)
        if numbers:
            rows.append([int(value) for value in numbers])
    return rows


def _coerce_po_object(value: object) -> list[int]:
    if not isinstance(value, list):
        raise ValueError("PO data must be a list or rows of numbers.")

    sequence: list[int] = []
    for index, item in enumerate(value):
        if isinstance(item, (list, tuple)):
            if not item:
                raise ValueError(f"PO row {index + 1} is empty.")
            material = item[0]
        else:
            material = item

        if not isinstance(material, (int, float)):
            raise ValueError(f"PO row {index + 1} has a non-numeric material code: {material!r}")
        sequence.append(int(material))

    if not sequence:
        raise ValueError("PO material sequence is empty.")
    return sequence


def parse_po_file(po_path: str | Path) -> list[int]:
    """
    Read po.txt and return a list of material codes.
    Example return: [400, 500, 100, 500, 400]
    """
    path = Path(po_path)
    text = path.read_text(encoding="utf-8-sig")
    stripped = text.strip()
    if not stripped:
        raise ValueError(f"PO file is empty: {path}")

    candidates = [stripped]
    if "=" in stripped:
        candidates.append(stripped.split("=", 1)[1].strip())

    for candidate in candidates:
        try:
            return _coerce_po_object(ast.literal_eval(candidate))
        except (SyntaxError, ValueError):
            pass

        try:
            return _coerce_po_object(json.loads(candidate))
        except (json.JSONDecodeError, ValueError):
            pass

    segment_materials: list[int] = []
    for raw_line in stripped.splitlines():
        match = re.match(r"\s*segment_\d+\s*:\s+.*?\(([-+]?\d+)\)", raw_line, re.IGNORECASE)
        if match:
            segment_materials.append(int(match.group(1)))
    if segment_materials:
        return segment_materials

    numeric_rows = _parse_numeric_rows(stripped)
    if numeric_rows:
        return _coerce_po_object(numeric_rows)

    raise ValueError(f"Could not parse PO file: {path}")


def build_tool_sequence(material_sequence: Iterable[int], mapping: dict[int, str]) -> list[str]:
    """
    Convert material codes to Prusa XL tool commands.
    Example: [400, 500, 100] -> ['T0', 'T1', 'T2']
    """
    tools: list[str] = []
    for material in material_sequence:
        if material not in mapping:
            known = ", ".join(str(code) for code in sorted(mapping))
            raise ValueError(f"No tool mapping for material {material}. Known materials: {known}")
        tools.append(mapping[material])
    return tools


def parse_gcode_position(line: str, current_position: dict[str, float | None]) -> dict[str, float | None]:
    """
    Update last known X/Y/Z/E position from a G-code line.
    """
    code = _strip_comment(line)
    match = COMMAND_RE.match(code)
    if not match or match.group(1).upper() not in {"G0", "G1", "G92"}:
        return current_position

    for axis, value in AXIS_RE.findall(code):
        current_position[axis.upper()] = float(value)
    return current_position


def is_m600_line(line: str) -> bool:
    """
    Return True if the line is an M600 command.
    """
    return bool(M600_RE.match(_strip_comment(line)))


def is_extrusion_line(line: str) -> bool:
    """
    Return True if the line is a G0/G1 command containing an E value.
    Used to insert the initial tool command before the first extrusion.
    """
    code = _strip_comment(line)
    match = COMMAND_RE.match(code)
    return bool(match and match.group(1).upper() in {"G0", "G1"} and re.search(r"\bE\s*[-+]?(?:\d+(?:\.\d*)?|\.\d+)", code, re.IGNORECASE))


def estimate_print_area(lines: Iterable[str], margin: float = 5.0) -> tuple[float, float, float, float] | None:
    """
    Estimate the printed XY area from extrusion moves, excluding intro-line comments.
    Returns (x, y, width, height) for Prusa XL M555.
    """
    xs: list[float] = []
    ys: list[float] = []
    position: dict[str, float | None] = {"X": None, "Y": None, "Z": None, "E": None}

    for line in lines:
        if "intro line" in line.lower():
            parse_gcode_position(line, position)
            continue

        code = _strip_comment(line)
        match = COMMAND_RE.match(code)
        if not match or match.group(1).upper() not in {"G0", "G1"}:
            parse_gcode_position(line, position)
            continue

        values = {axis.upper(): float(value) for axis, value in AXIS_RE.findall(code)}
        parse_gcode_position(line, position)
        if "E" not in values or values["E"] < 0:
            continue
        if position["X"] is None or position["Y"] is None:
            continue
        xs.append(position["X"])
        ys.append(position["Y"])

    if not xs or not ys:
        return None

    min_x = max(0.0, min(xs) - margin)
    min_y = max(0.0, min(ys) - margin)
    max_x = max(xs) + margin
    max_y = max(ys) + margin
    return min_x, min_y, max_x - min_x, max_y - min_y


def estimate_print_bounds(lines: Iterable[str]) -> tuple[float, float, float, float] | None:
    area = estimate_print_area(lines, margin=0.0)
    if area is None:
        return None
    x, y, width, height = area
    return x, x + width, y, y + height


def calculate_center_offset(bounds: tuple[float, float, float, float], bed_size_x: float, bed_size_y: float) -> tuple[float, float]:
    min_x, max_x, min_y, max_y = bounds
    return calculate_target_center_offset(bounds, bed_size_x / 2.0, bed_size_y / 2.0)


def calculate_target_center_offset(bounds: tuple[float, float, float, float], target_center_x: float, target_center_y: float) -> tuple[float, float]:
    min_x, max_x, min_y, max_y = bounds
    model_width = max_x - min_x
    model_height = max_y - min_y
    target_min_x = target_center_x - model_width / 2.0
    target_min_y = target_center_y - model_height / 2.0
    return target_min_x - min_x, target_min_y - min_y


def find_model_shift_start_line(lines: list[str]) -> int | None:
    """
    Return a 1-based source line where model XY shifting should begin.
    Includes the travel move immediately before the first non-intro extrusion.
    """
    last_xy_move_line: int | None = None
    for index, line in enumerate(lines, 1):
        code = _strip_comment(line)
        match = COMMAND_RE.match(code)
        if match and match.group(1).upper() in {"G0", "G1"}:
            values = {axis.upper(): float(value) for axis, value in AXIS_RE.findall(code)}
            if "X" in values or "Y" in values:
                last_xy_move_line = index
            if "E" in values and values["E"] >= 0 and "intro line" not in line.lower():
                return last_xy_move_line or index
    return None


def shift_xy_line(line: str, offset_x: float, offset_y: float) -> str:
    code, separator, comment = line.partition(";")
    match = COMMAND_RE.match(code)
    if not match or match.group(1).upper() not in {"G0", "G1"}:
        return line

    def replace_axis(match: re.Match[str]) -> str:
        axis = match.group(1).upper()
        value = float(match.group(2))
        if axis == "X":
            return f"X{_format_float(value + offset_x)}"
        if axis == "Y":
            return f"Y{_format_float(value + offset_y)}"
        return match.group(0)

    shifted_code = re.sub(r"\b([XY])\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))", replace_axis, code, flags=re.IGNORECASE)
    if separator:
        return shifted_code.rstrip() + " ;" + comment
    return shifted_code.rstrip()


def clamp_min_y_line(line: str, min_y: float) -> str:
    code, separator, comment = line.partition(";")
    match = COMMAND_RE.match(code)
    if not match or match.group(1).upper() not in {"G0", "G1"}:
        return line

    def replace_y(match: re.Match[str]) -> str:
        value = float(match.group(1))
        if value < min_y:
            return f"Y{_format_float(min_y)}"
        return match.group(0)

    clamped_code = re.sub(r"\bY\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))", replace_y, code, flags=re.IGNORECASE)
    if separator:
        return clamped_code.rstrip() + " ;" + comment
    return clamped_code.rstrip()


def _format_float(value: float) -> str:
    return f"{value:.3f}"


def _format_tool_command(tool: str, tool_command_style: str) -> str:
    if tool_command_style == "xl":
        return f"{tool} S1 L0 D0"
    return tool


def _tool_number(tool: str) -> str:
    return tool[1:]


def _format_temperature_command(line: str, tool: str) -> str:
    """
    Rewrite bare M104/M109 S... commands to target a specific Prusa XL tool.
    """
    code = _strip_comment(line)
    comment = ""
    if ";" in line:
        comment = " ;" + line.split(";", 1)[1]
    match = BARE_TEMP_RE.match(code)
    if not match:
        return line
    command, temperature, tail = match.groups()
    if re.search(r"\bT\s*\d+\b", code, re.IGNORECASE):
        return line
    return f"{command.upper()} {tool} S{temperature}{tail.rstrip()}{comment}"


def make_purge_block(
    tool: str,
    material: int,
    last_position: dict[str, float | None],
    purge_x: float = 220.0,
    purge_y: float = 20.0,
    purge_length: float = 60.0,
    purge_e: float = 20.0,
    purge_wipe_e: float = 15.0,
    safe_z_lift: float = 5.0,
    restore_e: bool = True,
    purge_index: int | None = None,
) -> list[str]:
    """
    Return a list of G-code lines for purge/prime after tool change.
    """
    last_x = last_position.get("X")
    last_y = last_position.get("Y")
    last_z = last_position.get("Z")
    last_e = last_position.get("E")

    purge_x2 = purge_x + purge_length
    safe_z = (last_z if last_z is not None else 0.0) + safe_z_lift
    purge_total_e = purge_e + purge_wipe_e

    lines = [
        "; ---- Purge / Prime Start ----",
        f"; tool {tool}, material {material}",
        f"; purge position X{_format_float(purge_x)} Y{_format_float(purge_y)}"
        + (f", index {purge_index}" if purge_index is not None else ""),
        f"; purge total E{_format_float(purge_total_e)} = prime E{_format_float(purge_e)} + wipe E{_format_float(purge_wipe_e)}",
        "G92 E0",
        f"G1 Z{_format_float(safe_z)} F1200",
        f"G1 X{_format_float(purge_x)} Y{_format_float(purge_y)} F9000",
        f"G1 E{_format_float(purge_e)} F300",
        f"G1 X{_format_float(purge_x2)} Y{_format_float(purge_y)} E{_format_float(purge_total_e)} F600",
        "G92 E0",
    ]

    if last_x is not None and last_y is not None:
        lines.append(f"G1 X{_format_float(last_x)} Y{_format_float(last_y)} F9000")
    if last_z is not None:
        lines.append(f"G1 Z{_format_float(last_z)} F1200")
    if restore_e and last_e is not None:
        lines.append(f"G92 E{_format_float(last_e)}")

    lines.append("; ---- Purge / Prime End ----")
    return lines


def convert_gcode_to_prusa_xl(
    input_gcode_path: str | Path,
    po_path: str | Path,
    output_gcode_path: str | Path,
    material_to_tool: dict[int, str],
    purge_enabled: bool = True,
    strict: bool = True,
    purge_x: float = 220.0,
    purge_y: float = 20.0,
    purge_length: float = 60.0,
    purge_e: float = 20.0,
    purge_wipe_e: float = 15.0,
    purge_step_x: float = 0.0,
    purge_step_y: float = 10.0,
    safe_z_lift: float = 5.0,
    tool_command_style: str = "xl",
    xl_bed_leveling_fix: bool = True,
    print_area_margin: float = 5.0,
    center_model_on_bed: bool = True,
    bed_size_x: float = 360.0,
    bed_size_y: float = 360.0,
    target_center_x: float | None = None,
    target_center_y: float | None = None,
    model_offset_x: float | None = None,
    model_offset_y: float | None = None,
    min_intro_y: float = 3.0,
    park_before_tool_change: bool = True,
) -> dict[str, object]:
    """
    Main conversion function.
    """
    input_path = Path(input_gcode_path)
    output_path = Path(output_gcode_path)
    material_sequence = parse_po_file(po_path)
    tool_sequence = build_tool_sequence(material_sequence, material_to_tool)

    source_lines = input_path.read_text(encoding="utf-8-sig").splitlines()
    print_bounds = estimate_print_bounds(source_lines)
    auto_offset_x = 0.0
    auto_offset_y = 0.0
    if center_model_on_bed and print_bounds:
        if target_center_x is not None and target_center_y is not None:
            auto_offset_x, auto_offset_y = calculate_target_center_offset(print_bounds, target_center_x, target_center_y)
        else:
            auto_offset_x, auto_offset_y = calculate_center_offset(print_bounds, bed_size_x, bed_size_y)
    offset_x = model_offset_x if model_offset_x is not None else auto_offset_x
    offset_y = model_offset_y if model_offset_y is not None else auto_offset_y
    shift_start_line = find_model_shift_start_line(source_lines) if (offset_x or offset_y) else None
    if xl_bed_leveling_fix and print_bounds:
        min_x, max_x, min_y, max_y = print_bounds
        print_area = (
            max(0.0, min_x + offset_x - print_area_margin),
            max(0.0, min_y + offset_y - print_area_margin),
            (max_x - min_x) + 2 * print_area_margin,
            (max_y - min_y) + 2 * print_area_margin,
        )
    else:
        print_area = None
    found_m600_count = sum(1 for line in source_lines if is_m600_line(line))
    expected_m600_count = len(material_sequence) - 1

    if found_m600_count != expected_m600_count:
        message = (
            f"Found M600 count ({found_m600_count}) does not match expected count "
            f"len(po)-1 ({expected_m600_count})."
        )
        if strict:
            raise ValueError(message)
        print(f"WARNING: {message}", file=sys.stderr)

    output_lines: list[str] = []
    position: dict[str, float | None] = {"X": None, "Y": None, "Z": None, "E": None}
    initial_tool_inserted = False
    replaced_m600_count = 0
    inserted_print_area = False
    last_hotend_temperature: float | None = None
    absolute_xy_mode = True

    for source_line_number, line in enumerate(source_lines, 1):
        command_match = COMMAND_RE.match(_strip_comment(line))
        if command_match and command_match.group(1).upper() == "G90":
            absolute_xy_mode = True
        elif command_match and command_match.group(1).upper() == "G91":
            absolute_xy_mode = False

        temp_match = TEMP_RE.match(_strip_comment(line))
        if temp_match:
            last_hotend_temperature = float(temp_match.group(1))

        if xl_bed_leveling_fix and not initial_tool_inserted and BARE_TEMP_RE.match(_strip_comment(line)):
            output_lines.append(_format_temperature_command(line, tool_sequence[0]))
            continue

        if initial_tool_inserted and BARE_TEMP_RE.match(_strip_comment(line)):
            next_line = _next_significant_line(source_lines, source_line_number)
            if next_line is not None and is_m600_line(next_line) and last_hotend_temperature and last_hotend_temperature > 0:
                continue

        if xl_bed_leveling_fix and not initial_tool_inserted and (
            G28_W_RE.match(_strip_comment(line)) or G28_XY_RE.match(_strip_comment(line))
        ):
            if print_area and not inserted_print_area:
                x, y, width, height = print_area
                output_lines.append("; ---- XL bed leveling fix: print area estimated from extrusion bounds ----")
                output_lines.append(
                    f"M555 X{_format_float(x)} Y{_format_float(y)} W{_format_float(width)} H{_format_float(height)}"
                )
                inserted_print_area = True
            output_lines.append(f"; ---- XL bed leveling fix: replaced legacy {line.strip()} ----")
            output_lines.append("G28 XY")
            if not initial_tool_inserted:
                material = material_sequence[0]
                tool = tool_sequence[0]
                output_lines.append(f"; ---- Initial tool selected before XL Z home / MBL: material {material} -> {tool} ----")
                output_lines.append(_format_tool_command(tool, tool_command_style))
                initial_tool_inserted = True
            output_lines.append("G28 Z")
            if not any(G80_RE.match(_strip_comment(candidate)) for candidate in source_lines[source_line_number:]):
                output_lines.append("; ---- XL bed leveling fix: inserted mesh bed leveling for non-PrusaSlicer start ----")
                output_lines.append("G29 G")
                output_lines.append("G29 P1")
                output_lines.append("G29 A")
            continue

        if xl_bed_leveling_fix and initial_tool_inserted and G28_Z_RE.match(_strip_comment(line)):
            output_lines.append(f"; ---- XL bed leveling fix: skipped duplicate {line.strip()} ----")
            continue

        if xl_bed_leveling_fix and initial_tool_inserted and G28_XY_RE.match(_strip_comment(line)):
            output_lines.append(f"; ---- XL end-gcode fix: replaced legacy {line.strip()} ----")
            output_lines.append("P0 S1 ; park current tool")
            continue

        if xl_bed_leveling_fix and G80_RE.match(_strip_comment(line)):
            output_lines.append("; ---- XL bed leveling fix: replaced legacy G80 mesh bed leveling ----")
            output_lines.append("G29 G")
            output_lines.append("G29 P1")
            output_lines.append("G29 A")
            continue

        if is_m600_line(line):
            replaced_m600_count += 1
            sequence_index = replaced_m600_count
            if sequence_index >= len(tool_sequence):
                message = f"M600 #{replaced_m600_count} has no matching PO entry."
                if strict:
                    raise ValueError(message)
                print(f"WARNING: {message}", file=sys.stderr)
                continue

            material = material_sequence[sequence_index]
            tool = tool_sequence[sequence_index]
            purge_index = replaced_m600_count - 1
            current_purge_x = purge_x + purge_index * purge_step_x
            current_purge_y = purge_y + purge_index * purge_step_y
            safe_z = (position["Z"] if position["Z"] is not None else 0.0) + safe_z_lift
            output_lines.append(
                f"; ---- Replaced M600 #{replaced_m600_count}: material {material} -> {tool} ----"
            )
            output_lines.append("; ---- Move away from printed part before tool change ----")
            output_lines.append(f"G1 Z{_format_float(safe_z)} F1200")
            output_lines.append(f"G1 X{_format_float(current_purge_x)} Y{_format_float(current_purge_y)} F9000")
            if park_before_tool_change:
                output_lines.append("P0 S1 L2 D0 ; park current tool before picking next tool")
            output_lines.append(_format_tool_command(tool, tool_command_style))
            if purge_enabled:
                if last_hotend_temperature is not None and last_hotend_temperature > 0:
                    output_lines.insert(-1, f"M109 {tool} S{_format_float(last_hotend_temperature)}")
                output_lines.extend(
                    make_purge_block(
                        tool=tool,
                        material=material,
                        last_position=position.copy(),
                        purge_x=current_purge_x,
                        purge_y=current_purge_y,
                        purge_length=purge_length,
                        purge_e=purge_e,
                        purge_wipe_e=purge_wipe_e,
                        safe_z_lift=safe_z_lift,
                        purge_index=replaced_m600_count,
                    )
                )
            continue

        if shift_start_line is not None and source_line_number < shift_start_line:
            line = clamp_min_y_line(line, min_intro_y)

        if shift_start_line is not None and source_line_number >= shift_start_line and absolute_xy_mode:
            line = shift_xy_line(line, offset_x, offset_y)

        if not initial_tool_inserted and is_extrusion_line(line):
            material = material_sequence[0]
            tool = tool_sequence[0]
            output_lines.append(f"; ---- Initial tool selected from PO: material {material} -> {tool} ----")
            output_lines.append(_format_tool_command(tool, tool_command_style))
            initial_tool_inserted = True

        output_lines.append(line)
        parse_gcode_position(line, position)

    if not initial_tool_inserted:
        raise ValueError("Could not find a G0/G1 extrusion line for initial tool insertion.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")

    return {
        "input_gcode": str(input_path),
        "po_file": str(po_path),
        "output_gcode": str(output_path),
        "material_sequence": material_sequence,
        "tool_sequence": tool_sequence,
        "found_m600_count": found_m600_count,
        "expected_m600_count": expected_m600_count,
        "inserted_initial_tool": tool_sequence[0],
        "replaced_m600_commands": replaced_m600_count,
        "purge_enabled": purge_enabled,
        "purge_start": [purge_x, purge_y],
        "purge_step": [purge_step_x, purge_step_y],
        "purge_e": purge_e,
        "purge_wipe_e": purge_wipe_e,
        "purge_total_e": purge_e + purge_wipe_e,
        "xl_bed_leveling_fix": xl_bed_leveling_fix,
        "center_model_on_bed": center_model_on_bed,
        "print_bounds_before_offset": print_bounds,
        "model_offset": [offset_x, offset_y],
        "model_shift_start_line": shift_start_line,
        "bed_size": [bed_size_x, bed_size_y],
        "target_center": [target_center_x, target_center_y] if target_center_x is not None and target_center_y is not None else [bed_size_x / 2.0, bed_size_y / 2.0],
        "estimated_print_area": print_area,
        "min_intro_y": min_intro_y,
        "park_before_tool_change": park_before_tool_change,
        "strict": strict,
        "tool_command_style": tool_command_style,
    }


def _parse_material_mapping(values: list[str]) -> dict[int, str]:
    mapping = dict(material_to_tool)
    for value in values:
        if "=" not in value:
            raise argparse.ArgumentTypeError(f"Invalid mapping {value!r}; use MATERIAL=TOOL, for example 400=T0.")
        material_text, tool = value.split("=", 1)
        try:
            material = int(material_text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"Invalid material code in mapping {value!r}.") from exc
        tool = tool.strip().upper()
        if not re.fullmatch(r"T\d+", tool):
            raise argparse.ArgumentTypeError(f"Invalid tool in mapping {value!r}; expected T0, T1, ...")
        mapping[material] = tool
    return mapping


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Input G-code file containing M600 commands.")
    parser.add_argument("--po", required=True, help="po.txt file containing the material sequence.")
    parser.add_argument("--output", required=True, help="Output Prusa XL G-code path.")
    parser.add_argument("--map", action="append", default=[], help="Override/add material mapping, e.g. --map 400=T0")
    parser.add_argument("--non-strict", action="store_true", help="Warn instead of failing when M600 count mismatches PO.")
    parser.add_argument("--no-purge", action="store_true", help="Replace M600 with tool commands only.")
    parser.add_argument("--purge-x", type=float, default=220.0)
    parser.add_argument("--purge-y", type=float, default=20.0)
    parser.add_argument("--purge-length", type=float, default=60.0)
    parser.add_argument("--purge-e", type=float, default=20.0)
    parser.add_argument("--purge-wipe-e", type=float, default=15.0, help="Extra extrusion during the purge line after prime.")
    parser.add_argument("--purge-step-x", type=float, default=0.0, help="X offset added for each replaced M600 purge.")
    parser.add_argument("--purge-step-y", type=float, default=10.0, help="Y offset added for each replaced M600 purge.")
    parser.add_argument("--safe-z-lift", type=float, default=5.0)
    parser.add_argument("--no-xl-bed-leveling-fix", action="store_true", help="Keep legacy G28 W and G80 commands unchanged.")
    parser.add_argument("--print-area-margin", type=float, default=5.0, help="Margin added to auto-estimated M555 print area.")
    parser.add_argument("--no-center-model-on-bed", action="store_true", help="Keep original model XY coordinates instead of centering on the XL bed.")
    parser.add_argument("--bed-size-x", type=float, default=360.0)
    parser.add_argument("--bed-size-y", type=float, default=360.0)
    parser.add_argument("--target-center-x", type=float, default=None, help="Target model center X. Defaults to bed center.")
    parser.add_argument("--target-center-y", type=float, default=None, help="Target model center Y. Defaults to bed center.")
    parser.add_argument("--model-offset-x", type=float, default=None, help="Manual X offset for original model coordinates.")
    parser.add_argument("--model-offset-y", type=float, default=None, help="Manual Y offset for original model coordinates.")
    parser.add_argument("--min-intro-y", type=float, default=3.0, help="Clamp pre-model intro-line Y moves to this minimum.")
    parser.add_argument("--no-park-before-tool-change", action="store_true", help="Do not emit P0 before each generated tool change.")
    parser.add_argument(
        "--tool-command-style",
        choices=("bare", "xl"),
        default="xl",
        help="Use bare T0/T1/T2 or Prusa XL reference style 'T0 S1 L0 D0'.",
    )
    args = parser.parse_args(argv)

    mapping = _parse_material_mapping(args.map)
    summary = convert_gcode_to_prusa_xl(
        input_gcode_path=args.input,
        po_path=args.po,
        output_gcode_path=args.output,
        material_to_tool=mapping,
        purge_enabled=not args.no_purge,
        strict=not args.non_strict,
        purge_x=args.purge_x,
        purge_y=args.purge_y,
        purge_length=args.purge_length,
        purge_e=args.purge_e,
        purge_wipe_e=args.purge_wipe_e,
        purge_step_x=args.purge_step_x,
        purge_step_y=args.purge_step_y,
        safe_z_lift=args.safe_z_lift,
        tool_command_style=args.tool_command_style,
        xl_bed_leveling_fix=not args.no_xl_bed_leveling_fix,
        print_area_margin=args.print_area_margin,
        center_model_on_bed=not args.no_center_model_on_bed,
        bed_size_x=args.bed_size_x,
        bed_size_y=args.bed_size_y,
        target_center_x=args.target_center_x,
        target_center_y=args.target_center_y,
        model_offset_x=args.model_offset_x,
        model_offset_y=args.model_offset_y,
        min_intro_y=args.min_intro_y,
        park_before_tool_change=not args.no_park_before_tool_change,
    )

    print(f"Input G-code: {summary['input_gcode']}")
    print(f"PO file: {summary['po_file']}")
    print(f"Output G-code: {summary['output_gcode']}")
    print(f"Detected PO sequence: {summary['material_sequence']}")
    print(f"Tool sequence: {summary['tool_sequence']}")
    print(f"Found M600 count: {summary['found_m600_count']}")
    print(f"Expected M600 count: {summary['expected_m600_count']}")
    print(f"Inserted initial tool: {summary['inserted_initial_tool']}")
    print(f"Replaced M600 commands: {summary['replaced_m600_commands']}")
    print(f"Purge enabled: {summary['purge_enabled']}")
    print(f"Purge start: {summary['purge_start']}")
    print(f"Purge step: {summary['purge_step']}")
    print(f"Purge E: {summary['purge_e']}")
    print(f"Purge wipe E: {summary['purge_wipe_e']}")
    print(f"Purge total E: {summary['purge_total_e']}")
    print(f"XL bed leveling fix: {summary['xl_bed_leveling_fix']}")
    print(f"Center model on bed: {summary['center_model_on_bed']}")
    print(f"Print bounds before offset: {summary['print_bounds_before_offset']}")
    print(f"Target center: {summary['target_center']}")
    print(f"Model offset: {summary['model_offset']}")
    print(f"Model shift start line: {summary['model_shift_start_line']}")
    print(f"Estimated print area: {summary['estimated_print_area']}")
    print(f"Min intro Y: {summary['min_intro_y']}")
    print(f"Park before tool change: {summary['park_before_tool_change']}")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
