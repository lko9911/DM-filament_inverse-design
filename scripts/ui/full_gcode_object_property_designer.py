from __future__ import annotations

import argparse
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

try:
    from .component_property_designer import (
        DEFAULT_OUTPUT_PATH,
        ComponentModel,
        ExtrusionSegment,
        launch_ui,
        parse_words,
        strip_comment,
    )
except ImportError:
    from component_property_designer import (
        DEFAULT_OUTPUT_PATH,
        ComponentModel,
        ExtrusionSegment,
        launch_ui,
        parse_words,
        strip_comment,
    )


PYVISTA_COLORS = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c"]


OBJECT_COMMENT_RE = re.compile(
    r"^;\s*(?P<action>printing object|stop printing object)\s+"
    r"(?P<name>.+?)\s+id:(?P<id>-?\d+)\s+copy\s+(?P<copy>-?\d+)\s*$",
    re.IGNORECASE,
)
MESH_COMMENT_RE = re.compile(r"^;\s*MESH:(?P<name>.+?)\s*$", re.IGNORECASE)
M486_SELECT_RE = re.compile(r"^M486\s+S(?P<object_id>-?\d+)\b", re.IGNORECASE)
M486_NAME_RE = re.compile(r"^M486\s+A(?P<name>.+?)\s*$", re.IGNORECASE)
LAYER_COMMENT_RE = re.compile(r"^;\s*(?:LAYER\s*:\s*-?\d+|LAYER_CHANGE)\s*$", re.IGNORECASE)
FEATURE_TYPE_RE = re.compile(r"^;\s*TYPE\s*:\s*(?P<name>.+?)\s*$", re.IGNORECASE)
REGION_START_RE = re.compile(
    r"^;\s*REGION_START\s*:\s*(?P<name>[^|]+?)(?:\s*\|\s*PROPERTY\s*:\s*(?P<property>.+?))?\s*$",
    re.IGNORECASE,
)
REGION_END_RE = re.compile(
    r"^;\s*REGION_END\s*:\s*(?P<name>[^|]+?)(?:\s*\|\s*PROPERTY\s*:\s*(?P<property>.+?))?\s*$",
    re.IGNORECASE,
)
BLOCK_COMPONENT_RE = re.compile(r"^BLOCK\s*(?P<number>\d+)(?:[_\-\s]+(?P<rest>.*))?$", re.IGNORECASE)
FOOTER_LINE_RE = re.compile(
    r"^(?:;\s*end model|;\s*End of converted G-code|M400\b|M8[23]\b|G9[01]\b|M10[46]\b|M140\b|M84\b)\b",
    re.IGNORECASE,
)

COLOR_TOKEN_ALIASES = {
    "BLACK": "BLACK",
    "WHITE": "WHITE",
    "GRAY": "GRAY",
    "GREY": "GRAY",
    "RED": "RED",
    "ORANGE": "ORANGE",
    "YELLOW": "YELLOW",
    "GREEN": "GREEN",
    "CYAN": "CYAN",
    "BLUE": "BLUE",
    "MAGENTA": "MAGENTA",
    "PURPLE": "PURPLE",
    "PUPLE": "PURPLE",
    "VIOLET": "PURPLE",
    "PINK": "PINK",
    "BROWN": "BROWN",
    "LIME": "LIME",
    "TEAL": "TEAL",
    "NAVY": "NAVY",
    "GOLD": "GOLD",
    "SILVER": "SILVER",
}


@dataclass(frozen=True)
class ObjectKey:
    object_id: int
    copy: int
    name: str

    @property
    def display_name(self) -> str:
        if self.object_id < 0 and self.copy == 0:
            return self.name
        return f"{self.name} id:{self.object_id} copy:{self.copy}"


def parse_object_comment(raw_line: str) -> tuple[str, ObjectKey] | None:
    match = OBJECT_COMMENT_RE.match(raw_line.strip())
    if match is None:
        return None
    return (
        match.group("action").lower(),
        ObjectKey(
            object_id=int(match.group("id")),
            copy=int(match.group("copy")),
            name=match.group("name").strip(),
        ),
    )


def parse_mesh_comment(raw_line: str) -> str | None:
    match = MESH_COMMENT_RE.match(raw_line.strip())
    if match is None:
        return None
    return match.group("name").strip()


def parse_m486_select(raw_line: str) -> int | None:
    match = M486_SELECT_RE.match(strip_comment(raw_line))
    if match is None:
        return None
    return int(match.group("object_id"))


def parse_m486_name(raw_line: str) -> str | None:
    match = M486_NAME_RE.match(strip_comment(raw_line))
    if match is None:
        return None
    return match.group("name").strip()


def parse_feature_type(raw_line: str) -> str | None:
    match = FEATURE_TYPE_RE.match(raw_line.strip())
    if match is None:
        return None
    feature_name = match.group("name").strip()
    return feature_name or None


def parse_region_start(raw_line: str) -> tuple[str, str | None] | None:
    match = REGION_START_RE.match(raw_line.strip())
    if match is None:
        return None
    region_name = match.group("name").strip()
    property_name = match.group("property")
    return region_name, property_name.strip() if property_name else None


def parse_region_end(raw_line: str) -> tuple[str, str | None] | None:
    match = REGION_END_RE.match(raw_line.strip())
    if match is None:
        return None
    region_name = match.group("name").strip()
    property_name = match.group("property")
    return region_name, property_name.strip() if property_name else None


def canonical_component_name(name: str) -> str:
    stripped = str(name or "").strip()
    match = BLOCK_COMPONENT_RE.match(stripped)
    if match is None:
        return stripped

    block_number = int(match.group("number"))
    semantic_tokens: list[str] = []
    for token in re.split(r"[^A-Za-z0-9]+", match.group("rest") or ""):
        token = token.strip()
        if not token:
            continue
        upper_token = token.upper()
        if upper_token.isdigit():
            continue
        color_token = COLOR_TOKEN_ALIASES.get(upper_token)
        if color_token:
            if color_token not in semantic_tokens:
                semantic_tokens.append(color_token)
            continue
        step_match = re.match(r"^(?P<count>\d+)STEP$", upper_token)
        if step_match is not None:
            step_token = f"{int(step_match.group('count'))}step"
            if step_token not in semantic_tokens:
                semantic_tokens.append(step_token)
            continue
        if upper_token not in semantic_tokens:
            semantic_tokens.append(upper_token)

    suffix = f"_{'_'.join(semantic_tokens)}" if semantic_tokens else ""
    return f"Block{block_number}{suffix}"


def make_m486_object_key(object_id: int, name: str) -> ObjectKey:
    return ObjectKey(
        object_id=-(object_id + 1),
        copy=0,
        name=canonical_component_name(name or f"Object {object_id}"),
    )


def is_footer_line(raw_line: str) -> bool:
    return FOOTER_LINE_RE.match(raw_line.strip()) is not None


def split_full_gcode_object_blocks(path: Path) -> tuple[list[str], dict[str, list[str]], list[str]]:
    preamble_lines: list[str] = []
    postamble_lines: list[str] = []
    lines_by_object_name: dict[str, list[str]] = {}
    active_object_name: str | None = None
    seen_object_block = False

    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for raw_line in handle:
            region_start = parse_region_start(raw_line)
            if region_start is not None:
                region_name, _property_name = region_start
                object_name = canonical_component_name(region_name)
                seen_object_block = True
                lines_by_object_name.setdefault(object_name, []).append(raw_line)
                active_object_name = object_name
                continue

            region_end = parse_region_end(raw_line)
            if region_end is not None:
                region_name, _property_name = region_end
                object_name = canonical_component_name(region_name)
                seen_object_block = True
                if active_object_name is not None:
                    lines_by_object_name.setdefault(active_object_name, []).append(raw_line)
                elif object_name in lines_by_object_name:
                    lines_by_object_name.setdefault(object_name, []).append(raw_line)
                if active_object_name == object_name:
                    active_object_name = None
                continue

            object_comment = parse_object_comment(raw_line)
            if object_comment is not None:
                action, key = object_comment
                object_name = canonical_component_name(key.display_name)
                seen_object_block = True
                lines_by_object_name.setdefault(object_name, []).append(raw_line)
                if action == "printing object":
                    active_object_name = object_name
                elif active_object_name == object_name:
                    active_object_name = None
                continue

            mesh_name = parse_mesh_comment(raw_line)
            if mesh_name is not None:
                seen_object_block = True
                if mesh_name.upper() == "NONMESH":
                    if active_object_name is not None:
                        lines_by_object_name.setdefault(active_object_name, []).append(raw_line)
                    else:
                        postamble_lines.append(raw_line)
                    active_object_name = None
                    continue

                mesh_name = canonical_component_name(mesh_name)
                lines_by_object_name.setdefault(mesh_name, []).append(raw_line)
                active_object_name = mesh_name
                continue

            if active_object_name is not None:
                lines_by_object_name.setdefault(active_object_name, []).append(raw_line)
            elif not seen_object_block:
                preamble_lines.append(raw_line)
            else:
                postamble_lines.append(raw_line)

    return preamble_lines, lines_by_object_name, postamble_lines


def component_anchor(component: ComponentModel) -> tuple[float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    for segment in component.segments:
        xs.extend([segment.x0, segment.x1])
        ys.extend([segment.y0, segment.y1])
        zs.extend([segment.z0, segment.z1])
    if not xs:
        return (0.0, 0.0, 0.0)
    return (
        (min(xs) + max(xs)) / 2.0,
        (min(ys) + max(ys)) / 2.0,
        (min(zs) + max(zs)) / 2.0,
    )


def apply_xyz_offset_to_line(raw_line: str, dx: float, dy: float, dz: float, absolute_xyz: bool) -> tuple[str, bool]:
    stripped = raw_line.rstrip("\r\n")
    newline = raw_line[len(stripped):]
    code_text, comment_sep, comment_text = stripped.partition(";")
    tokens = code_text.split()
    if not tokens:
        return raw_line, absolute_xyz

    g_code: int | None = None
    for token in tokens:
        upper = token.upper()
        if upper == "G90":
            absolute_xyz = True
        elif upper == "G91":
            absolute_xyz = False
        elif upper.startswith("G"):
            try:
                g_code = int(float(upper[1:]))
            except ValueError:
                pass

    if absolute_xyz and g_code in {0, 1, 2, 3, 92}:
        shifted_tokens: list[str] = []
        for token in tokens:
            upper = token.upper()
            prefix = upper[:1]
            if prefix in {"X", "Y", "Z"}:
                try:
                    value = float(token[1:])
                except ValueError:
                    shifted_tokens.append(token)
                    continue
                offset = dx if prefix == "X" else dy if prefix == "Y" else dz
                shifted_tokens.append(f"{token[0]}{value + offset:.5f}")
            else:
                shifted_tokens.append(token)
        code_text = " ".join(shifted_tokens)

    rebuilt = code_text
    if comment_sep:
        rebuilt += f";{comment_text}"
    return rebuilt + newline, absolute_xyz


def apply_xyz_offset_to_block(lines: list[str], dx: float, dy: float, dz: float) -> list[str]:
    absolute_xyz = True
    shifted_lines: list[str] = []
    for raw_line in lines:
        shifted_line, absolute_xyz = apply_xyz_offset_to_line(raw_line, dx, dy, dz, absolute_xyz)
        shifted_lines.append(shifted_line)
    return shifted_lines


def write_reordered_full_gcode(
    source_gcode_path: Path,
    components: list[ComponentModel],
    states: dict[int, dict[str, object]],
    output_path: Path,
    strategy: str = "reorder_mesh_occurrences_within_each_layer_keep_xyz",
) -> Path:
    if strategy in {"preserve_original_gcode_order", "copy_original_keep_xyz"}:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_gcode_path, output_path)
        return output_path

    enabled_components = [
        component for component in components if bool(states[component.index].get("enabled", True))
    ]
    ordered_components = sorted(
        enabled_components,
        key=lambda item: (int(states[item.index]["order"]), item.index),
    )
    ordered_object_names = [component.display_name or component.path.name for component in ordered_components]
    order_rank = {name: index for index, name in enumerate(ordered_object_names)}

    def collect_m486_names(lines: list[str]) -> dict[int, str]:
        names_by_id: dict[int, str] = {}
        pending_object_id: int | None = None
        for line in lines:
            selected_id = parse_m486_select(line)
            if selected_id is not None:
                pending_object_id = selected_id if selected_id >= 0 else None
                continue

            object_name = parse_m486_name(line)
            if object_name is not None and pending_object_id is not None:
                names_by_id[pending_object_id] = canonical_component_name(object_name)
                pending_object_id = None
        return names_by_id

    def read_m486_declaration(lines: list[str], start_index: int) -> tuple[str, list[str], int] | None:
        selected_id = parse_m486_select(lines[start_index])
        if selected_id is None or selected_id < 0:
            return None

        group_lines = [lines[start_index]]
        object_name: str | None = None
        cursor = start_index + 1
        while cursor < len(lines):
            line = lines[cursor]
            next_selected_id = parse_m486_select(line)
            if next_selected_id is not None and next_selected_id >= 0:
                break

            group_lines.append(line)
            parsed_name = parse_m486_name(line)
            if parsed_name is not None:
                object_name = canonical_component_name(parsed_name)

            if next_selected_id == -1:
                cursor += 1
                break
            cursor += 1

        if object_name is None:
            return None
        return object_name, group_lines, cursor

    def reorder_m486_declarations(lines: list[str]) -> list[str]:
        output: list[str] = []
        cursor = 0
        while cursor < len(lines):
            declaration = read_m486_declaration(lines, cursor)
            if declaration is None:
                output.append(lines[cursor])
                cursor += 1
                continue

            declaration_groups: list[tuple[str, list[str]]] = []
            while cursor < len(lines):
                declaration = read_m486_declaration(lines, cursor)
                if declaration is None:
                    break
                object_name, group_lines, next_cursor = declaration
                declaration_groups.append((object_name, group_lines))
                cursor = next_cursor

            for _object_name, group_lines in sorted(
                declaration_groups,
                key=lambda item: (order_rank.get(item[0], 10_000), item[0]),
            ):
                output.extend(group_lines)
        return output

    all_lines = source_gcode_path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
    m486_names_by_id = collect_m486_names(all_lines)

    def is_non_extruding_motion(raw_line: str) -> bool:
        line = strip_comment(raw_line)
        if not line:
            return False
        words = parse_words(line)
        g_code = int(words["G"]) if "G" in words else None
        if g_code not in {0, 1}:
            return False
        return float(words.get("E", 0.0)) <= 0.0

    def split_trailing_travel(lines: list[str]) -> tuple[list[str], list[str]]:
        split_index = len(lines)
        while split_index > 0 and is_non_extruding_motion(lines[split_index - 1]):
            split_index -= 1
        return lines[:split_index], lines[split_index:]

    def split_connector_travel(lines: list[str]) -> tuple[list[str], list[str]]:
        last_positive_extrusion_index: int | None = None
        for index in range(len(lines) - 1, -1, -1):
            line = strip_comment(lines[index])
            if line:
                words = parse_words(line)
                g_code = int(words["G"]) if "G" in words else None
                if g_code in {0, 1, 2, 3} and float(words.get("E", 0.0)) > 0.0:
                    last_positive_extrusion_index = index
                    break

        search_start = last_positive_extrusion_index if last_positive_extrusion_index is not None else -1
        for index in range(len(lines) - 1, search_start, -1):
            if lines[index].strip().upper().startswith(";WIPE_END"):
                tail = lines[index + 1 :]
                if tail and not any(
                    (
                        int(parse_words(strip_comment(line)).get("G", -1)) in {0, 1, 2, 3}
                        and float(parse_words(strip_comment(line)).get("E", 0.0)) > 0.0
                    )
                    for line in tail
                    if strip_comment(line)
                ):
                    return lines[: index + 1], tail
                break
        if last_positive_extrusion_index is not None:
            tail = lines[last_positive_extrusion_index + 1 :]
            if tail and not any(
                (
                    int(parse_words(strip_comment(line)).get("G", -1)) in {0, 1, 2, 3}
                    and float(parse_words(strip_comment(line)).get("E", 0.0)) > 0.0
                )
                for line in tail
                if strip_comment(line)
            ):
                return lines[: last_positive_extrusion_index + 1], tail
        return split_trailing_travel(lines)

    def split_layer_chunk(layer_lines: list[str]) -> tuple[list[str], list[tuple[str, list[str]]], list[str]]:
        if not layer_lines:
            return [], [], []

        first_mesh_start: int | None = None
        for index, line in enumerate(layer_lines):
            mesh_name = parse_mesh_comment(line)
            if mesh_name is not None and mesh_name.upper() != "NONMESH":
                mesh_name = canonical_component_name(mesh_name)
                first_mesh_start = index
                break
            selected_id = parse_m486_select(line)
            if selected_id is not None and selected_id >= 0 and selected_id in m486_names_by_id:
                first_mesh_start = index
                break

        if first_mesh_start is None:
            return list(layer_lines), [], []

        prefix, pending_travel = split_trailing_travel(layer_lines[:first_mesh_start])
        mesh_segments: list[tuple[str, list[str]]] = []
        suffix: list[str] = []
        current_name: str | None = None
        current_lines: list[str] = []
        in_suffix = False
        seen_mesh_segment = False

        segment_lines = layer_lines[first_mesh_start:]
        for line_index, line in enumerate(segment_lines):
            if in_suffix:
                suffix.append(line)
                continue

            has_later_segment_start = any(
                (
                    (parse_mesh_comment(future_line) is not None and parse_mesh_comment(future_line).upper() != "NONMESH")
                    or (
                        (future_selected_id := parse_m486_select(future_line)) is not None
                        and future_selected_id >= 0
                        and future_selected_id in m486_names_by_id
                    )
                )
                for future_line in segment_lines[line_index + 1 :]
            )
            if is_footer_line(line) and not has_later_segment_start:
                if current_name is not None:
                    mesh_segments.append((current_name, current_lines))
                    current_name = None
                    current_lines = []
                suffix.append(line)
                in_suffix = True
                continue

            mesh_name = parse_mesh_comment(line)
            if mesh_name is not None:
                if mesh_name.upper() == "NONMESH":
                    if current_name is not None:
                        mesh_segments.append((current_name, current_lines))
                        current_name = None
                        current_lines = []
                    suffix.append(line)
                    in_suffix = True
                    continue

                mesh_name = canonical_component_name(mesh_name)
                if current_name is not None:
                    current_lines, pending_travel = split_connector_travel(current_lines)
                    mesh_segments.append((current_name, current_lines))
                current_name = mesh_name
                current_lines = [*pending_travel, line]
                pending_travel = []
                seen_mesh_segment = True
                continue

            selected_id = parse_m486_select(line)
            if selected_id is not None:
                if selected_id == -1:
                    if current_name is not None:
                        current_lines.append(line)
                        mesh_segments.append((current_name, current_lines))
                        current_name = None
                        current_lines = []
                    else:
                        suffix.append(line)
                    continue

                m486_name = m486_names_by_id.get(selected_id)
                if m486_name is not None:
                    if current_name is not None:
                        current_lines, pending_travel = split_connector_travel(current_lines)
                        mesh_segments.append((current_name, current_lines))
                    current_name = m486_name
                    current_lines = [*pending_travel, line]
                    pending_travel = []
                    seen_mesh_segment = True
                    continue

            if current_name is None:
                if seen_mesh_segment:
                    suffix.append(line)
                else:
                    prefix.append(line)
            else:
                current_lines.append(line)

        if current_name is not None:
            mesh_segments.append((current_name, current_lines))

        return prefix, mesh_segments, suffix

    def reorder_layer_chunk(layer_lines: list[str]) -> list[str]:
        prefix, mesh_segments, suffix = split_layer_chunk(layer_lines)
        if not mesh_segments:
            return list(prefix) + list(suffix)

        reordered_segments = sorted(
            mesh_segments,
            key=lambda item: (order_rank.get(item[0], 10_000), item[0]),
        )

        output = list(prefix)
        for _mesh_name, segment_lines in reordered_segments:
            output.extend(segment_lines)
        output.extend(suffix)
        return output

    preamble_lines: list[str] = []
    layer_chunks: list[list[str]] = []
    current_chunk: list[str] | None = None

    for raw_line in all_lines:
        if LAYER_COMMENT_RE.match(raw_line.strip()):
            if current_chunk is None:
                current_chunk = [raw_line]
            else:
                layer_chunks.append(current_chunk)
                current_chunk = [raw_line]
            continue

        if current_chunk is None:
            preamble_lines.append(raw_line)
        else:
            current_chunk.append(raw_line)

    if current_chunk is not None:
        layer_chunks.append(current_chunk)

    output_lines: list[str] = reorder_m486_declarations(preamble_lines)
    if strategy == "reorder_mesh_occurrences_globally_keep_xyz":
        parsed_chunks: list[tuple[list[str], list[tuple[str, list[str]]], list[str]]] = [
            split_layer_chunk(layer_chunk)
            for layer_chunk in layer_chunks
        ]
        all_segments: list[tuple[str, list[str]]] = []
        for _prefix, mesh_segments, _suffix in parsed_chunks:
            all_segments.extend(mesh_segments)
        reordered_segments = sorted(
            all_segments,
            key=lambda item: (order_rank.get(item[0], 10_000), item[0]),
        )
        segment_cursor = 0
        for prefix, mesh_segments, suffix in parsed_chunks:
            output_lines.extend(prefix)
            segment_count = len(mesh_segments)
            for _ in range(segment_count):
                if segment_cursor >= len(reordered_segments):
                    break
                _mesh_name, segment_lines = reordered_segments[segment_cursor]
                output_lines.extend(segment_lines)
                segment_cursor += 1
            output_lines.extend(suffix)
    else:
        for layer_chunk in layer_chunks:
            output_lines.extend(reorder_layer_chunk(layer_chunk))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("".join(output_lines), encoding="utf-8")
    return output_path


def parse_full_gcode_objects(path: Path) -> list[ComponentModel]:
    x = y = z = e = 0.0
    absolute_xyz = True
    absolute_e = True
    active_object: ObjectKey | None = None
    object_order: list[ObjectKey] = []
    segments_by_object: dict[ObjectKey, list[ExtrusionSegment]] = {}
    total_e_by_object: dict[ObjectKey, float] = {}
    z_values_by_object: dict[ObjectKey, set[float]] = {}
    mesh_keys_by_name: dict[str, ObjectKey] = {}
    m486_keys_by_id: dict[int, ObjectKey] = {}
    region_keys_by_name: dict[str, ObjectKey] = {}
    pending_m486_object_id: int | None = None
    current_feature_type: str | None = None
    active_non_object_key: ObjectKey | None = None
    non_object_run_index = 0
    fallback_segments: list[ExtrusionSegment] = []
    fallback_total_e = 0.0
    fallback_z_values: set[float] = set()
    first_extrusion_order_by_object: dict[ObjectKey, int] = {}
    extrusion_order = 0

    def ensure_object(key: ObjectKey) -> None:
        if key not in segments_by_object:
            object_order.append(key)
            segments_by_object[key] = []
            total_e_by_object[key] = 0.0
            z_values_by_object[key] = set()

    def make_non_object_key(feature_type: str | None) -> ObjectKey:
        nonlocal non_object_run_index
        non_object_run_index += 1
        feature_label = (feature_type or "Unassigned extrusion").strip() or "Unassigned extrusion"
        return ObjectKey(
            object_id=-(10_000 + non_object_run_index),
            copy=0,
            name=feature_label,
        )

    def reset_non_object_run() -> None:
        nonlocal active_non_object_key
        active_non_object_key = None

    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for raw_line in handle:
            feature_type = parse_feature_type(raw_line)
            if feature_type is not None:
                if feature_type != current_feature_type:
                    reset_non_object_run()
                current_feature_type = feature_type

            region_start = parse_region_start(raw_line)
            if region_start is not None:
                region_name, _property_name = region_start
                canonical_name = canonical_component_name(region_name)
                key = region_keys_by_name.get(canonical_name)
                if key is None:
                    key = ObjectKey(
                        object_id=-(20_000 + len(region_keys_by_name) + 1),
                        copy=0,
                        name=canonical_name,
                    )
                    region_keys_by_name[canonical_name] = key
                ensure_object(key)
                active_object = key
                reset_non_object_run()
                continue

            region_end = parse_region_end(raw_line)
            if region_end is not None:
                region_name, _property_name = region_end
                canonical_name = canonical_component_name(region_name)
                key = region_keys_by_name.get(canonical_name)
                if key is not None and active_object == key:
                    active_object = None
                reset_non_object_run()
                continue

            object_comment = parse_object_comment(raw_line)
            if object_comment is not None:
                action, key = object_comment
                key = ObjectKey(key.object_id, key.copy, canonical_component_name(key.name))
                ensure_object(key)
                if action == "printing object":
                    active_object = key
                    reset_non_object_run()
                elif active_object is not None and active_object.object_id == key.object_id and active_object.copy == key.copy:
                    active_object = None
                    reset_non_object_run()
                continue

            mesh_name = parse_mesh_comment(raw_line)
            if mesh_name is not None:
                if mesh_name.upper() == "NONMESH":
                    active_object = None
                    reset_non_object_run()
                    continue
                mesh_name = canonical_component_name(mesh_name)
                key = mesh_keys_by_name.get(mesh_name)
                if key is None:
                    key = ObjectKey(
                        object_id=-(len(mesh_keys_by_name) + 1),
                        copy=0,
                        name=mesh_name,
                    )
                    mesh_keys_by_name[mesh_name] = key
                ensure_object(key)
                active_object = key
                reset_non_object_run()
                continue

            m486_select = parse_m486_select(raw_line)
            if m486_select is not None:
                pending_m486_object_id = m486_select
                if m486_select < 0:
                    active_object = None
                    reset_non_object_run()
                    continue
                active_object = m486_keys_by_id.get(m486_select)
                if active_object is not None:
                    ensure_object(active_object)
                reset_non_object_run()
                continue

            m486_name = parse_m486_name(raw_line)
            if m486_name is not None and pending_m486_object_id is not None and pending_m486_object_id >= 0:
                key = make_m486_object_key(pending_m486_object_id, m486_name)
                m486_keys_by_id[pending_m486_object_id] = key
                ensure_object(key)
                reset_non_object_run()
                continue

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
                extrusion_order += 1
                segment = ExtrusionSegment(x, y, z, next_x, next_y, next_z, e_delta)
                fallback_segments.append(segment)
                fallback_total_e += e_delta
                fallback_z_values.add(round(next_z, 5))
                if active_object is not None:
                    segments_by_object[active_object].append(segment)
                    total_e_by_object[active_object] += e_delta
                    z_values_by_object[active_object].add(round(next_z, 5))
                else:
                    if active_non_object_key is None:
                        active_non_object_key = make_non_object_key(current_feature_type)
                        ensure_object(active_non_object_key)
                    segments_by_object[active_non_object_key].append(segment)
                    total_e_by_object[active_non_object_key] += e_delta
                    z_values_by_object[active_non_object_key].add(round(next_z, 5))
                effective_object = active_object or active_non_object_key
                if effective_object is not None:
                    first_extrusion_order_by_object.setdefault(
                        effective_object,
                        extrusion_order,
                    )

            x, y, z, e = next_x, next_y, next_z, next_e

    components: list[ComponentModel] = []
    for key in object_order:
        segments = segments_by_object[key]
        if not segments:
            continue
        z_values = sorted(z_values_by_object[key])
        components.append(
            ComponentModel(
                index=len(components) + 1,
                path=path,
                segments=segments,
                total_e=total_e_by_object[key],
                layer_count=len(z_values),
                min_z=z_values[0] if z_values else None,
                max_z=z_values[-1] if z_values else None,
                display_name=key.display_name,
            )
        )
    if not components and fallback_segments:
        z_values = sorted(fallback_z_values)
        components.append(
            ComponentModel(
                index=1,
                path=path,
                segments=fallback_segments,
                total_e=fallback_total_e,
                layer_count=len(z_values),
                min_z=z_values[0] if z_values else None,
                max_z=z_values[-1] if z_values else None,
                display_name=path.stem or "Full G-code",
            )
        )
    return components


def make_component_polydata(component: ComponentModel, pv):
    if not component.segments:
        return pv.PolyData()

    points = []
    lines = []
    for segment in component.segments:
        start_index = len(points)
        points.append((segment.x0, segment.y0, segment.z0))
        points.append((segment.x1, segment.y1, segment.z1))
        lines.extend([2, start_index, start_index + 1])
    return pv.PolyData(points, lines=lines)


def launch_pyvista_preview(components: list[ComponentModel]) -> None:
    try:
        import pyvista as pv
    except ImportError as exc:
        raise SystemExit(
            "PyVista preview is not available in this Python environment. "
            "Install pyvista/vtk, or run without --pyvista-preview."
        ) from exc

    plotter = pv.Plotter(window_size=(1400, 900))
    actors = {}
    active_index = {"value": components[0].index}

    def redraw() -> None:
        plotter.clear()
        active_component = next(component for component in components if component.index == active_index["value"])
        mesh = make_component_polydata(active_component, pv)
        color = PYVISTA_COLORS[(active_component.index - 1) % len(PYVISTA_COLORS)]
        if mesh.n_points:
            actors[active_component.index] = plotter.add_mesh(mesh, color=color, line_width=3, render_lines_as_tubes=True)
        plotter.add_text(
            f"C{active_component.index}: {active_component.display_name or active_component.path.name}\n"
            f"E={active_component.total_e:.6f} | layers={active_component.layer_count}",
            position="upper_left",
            font_size=12,
        )
        plotter.add_axes()
        plotter.reset_camera()
        plotter.render()

    def select_component(component_index: int) -> None:
        active_index["value"] = component_index
        redraw()

    for component in components:
        plotter.add_key_event(str(component.index), lambda idx=component.index: select_component(idx))

    redraw()
    print("PyVista preview: press number keys 1-5 to show one component at a time.")
    plotter.show()


class PyVistaComponentPreview:
    def __init__(self, components: list[ComponentModel]):
        try:
            import pyvista as pv
        except ImportError as exc:
            raise SystemExit(
                "PyVista preview is not available in this Python environment. "
                "Install pyvista/vtk, or run without PyVista preview enabled."
            ) from exc

        self.pv = pv
        self.components = components
        self.plotter = pv.Plotter(window_size=(1050, 760))
        self.closed = False
        self.plotter.add_axes()
        self.plotter.show(auto_close=False, interactive_update=True)

    def show_component(self, component_index: int) -> None:
        if self.closed:
            return
        component = next((item for item in self.components if item.index == component_index), None)
        if component is None:
            return
        try:
            self.plotter.clear()
            self.plotter.add_axes()
            mesh = make_component_polydata(component, self.pv)
            color = PYVISTA_COLORS[(component.index - 1) % len(PYVISTA_COLORS)]
            if mesh.n_points:
                self.plotter.add_mesh(mesh, color=color, line_width=4, render_lines_as_tubes=True)
            self.plotter.add_text(
                f"C{component.index}: {component.display_name or component.path.name}\n"
                f"E={component.total_e:.6f} | layers={component.layer_count}",
                position="upper_left",
                font_size=11,
            )
            self.plotter.reset_camera()
            self.plotter.update()
        except Exception:
            self.closed = True

    def close(self) -> None:
        if self.closed:
            return
        try:
            self.plotter.close()
        finally:
            self.closed = True


def make_live_pyvista_preview(components: list[ComponentModel]) -> PyVistaComponentPreview:
    preview = PyVistaComponentPreview(components)
    preview.show_component(components[0].index)
    return preview


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create input/config/Property_sample.json from object sections inside one full G-code file."
    )
    parser.add_argument("gcode_file", help="Full G-code file containing '; printing object ... id:N copy M' comments.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH), help="Output JSON path.")
    parser.add_argument("--voxel-threshold-e", type=float, default=2.0, help="E amount represented by one output voxel.")
    parser.add_argument("--pyvista-preview", action="store_true", help="Open an optional PyVista preview before the designer UI.")
    args = parser.parse_args()

    if args.voxel_threshold_e <= 0.0:
        raise SystemExit("--voxel-threshold-e must be greater than 0.")

    gcode_path = Path(args.gcode_file).resolve()
    if not gcode_path.exists():
        raise SystemExit(f"G-code file not found: {gcode_path}")

    components = parse_full_gcode_objects(gcode_path)
    if not components:
        raise SystemExit(
            "No printable extrusion was found. Object comments are optional, but the file must contain "
            "positive-E G0/G1 extrusion moves."
        )

    for component in components:
        print(
            f"C{component.index}: {component.display_name} | "
            f"E={component.total_e:.6f} | layers={component.layer_count} | segments={len(component.segments)}"
        )
    if args.pyvista_preview:
        preview_controller = make_live_pyvista_preview(components)
    else:
        preview_controller = None
    launch_ui(
        components,
        Path(args.output).resolve(),
        float(args.voxel_threshold_e),
        preview_controller=preview_controller,
    )


if __name__ == "__main__":
    main()
