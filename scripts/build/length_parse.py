from pathlib import Path
import json
import re

# =========================
# Input G-code file path
# =========================
GCODE_PATH = r"input\gcode\vase.gcode"
VOXEL_THRESHOLD_E = 2.0
VOXEL_JSON_PATH = r"input\config\sample_info.json"


MOVE_RE = re.compile(r'([A-Z])([-+]?\d*\.?\d+)')


def parse_words(code_part: str) -> dict[str, float]:
    words = {}
    for key, value in MOVE_RE.findall(code_part.upper()):
        try:
            words[key] = float(value)
        except ValueError:
            pass
    return words


def calculate_total_filament(gcode_path: str) -> float:
    path = Path(gcode_path)

    if not path.exists():
        raise FileNotFoundError(f"G-code file not found: {path}")

    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()

    absolute_extrusion = True   # Assume absolute extrusion mode (M82) by default
    current_e = 0.0
    segment_start_e = 0.0
    segment_max_e = 0.0
    total_filament = 0.0

    def finalize_segment() -> None:
        nonlocal total_filament, segment_start_e, segment_max_e
        total_filament += max(0.0, segment_max_e - segment_start_e)

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        # Strip comments
        code_part = line.split(";", 1)[0].strip()
        if not code_part:
            continue

        upper = code_part.upper()

        # Extrusion mode switch
        if upper.startswith("M82"):
            absolute_extrusion = True
            continue

        if upper.startswith("M83"):
            absolute_extrusion = False
            continue

        # Reset position / extrusion reference
        if upper.startswith("G92"):
            finalize_segment()
            words = parse_words(code_part)
            if "E" in words:
                current_e = words["E"]
                segment_start_e = current_e
                segment_max_e = current_e
            continue

        # Movement commands including arc extrusion (G2/G3)
        words = parse_words(code_part)
        g_code = int(words["G"]) if "G" in words else None
        if g_code in {0, 1, 2, 3}:
            if "E" not in words:
                continue

            e_value = words["E"]

            if absolute_extrusion:
                current_e = e_value
            else:
                current_e += e_value

            if current_e > segment_max_e:
                segment_max_e = current_e

    finalize_segment()

    return total_filament


def iter_deposited_extrusion_deltas(gcode_path: str):
    path = Path(gcode_path)

    if not path.exists():
        raise FileNotFoundError(f"G-code file not found: {path}")

    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()

    absolute_extrusion = True
    current_e = 0.0
    segment_max_e = 0.0
    current_layer = 0
    preheat_mode = True

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("M107"):
            preheat_mode = False
            current_layer = 1
            continue

        if line.startswith(";LAYER:"):
            try:
                if preheat_mode:
                    current_layer = 0
                else:
                    current_layer = int(line.split(":", 1)[1].strip()) + 1
                    if current_layer < 1:
                        current_layer = 1
            except ValueError:
                pass
            continue

        # Strip comments
        code_part = line.split(";", 1)[0].strip()
        if not code_part:
            continue

        upper = code_part.upper()

        # Extrusion mode switch
        if upper.startswith("M82"):
            absolute_extrusion = True
            continue

        if upper.startswith("M83"):
            absolute_extrusion = False
            continue

        # Reset position / extrusion reference
        if upper.startswith("G92"):
            words = parse_words(code_part)
            if "E" in words:
                current_e = words["E"]
                segment_max_e = current_e
            continue

        # Movement commands including arc extrusion (G2/G3)
        words = parse_words(code_part)
        g_code = int(words["G"]) if "G" in words else None
        if g_code in {0, 1, 2, 3}:
            if "E" not in words:
                continue

            e_value = words["E"]
            if absolute_extrusion:
                current_e = e_value
                if current_e > segment_max_e:
                    yield current_e - segment_max_e, current_layer
                    segment_max_e = current_e
            else:
                current_e += e_value
                if e_value > 0:
                    yield e_value, current_layer


def build_voxel_table(gcode_path: str, voxel_threshold_e: float = VOXEL_THRESHOLD_E) -> list[dict[str, float | int]]:
    if voxel_threshold_e <= 0:
        raise ValueError("voxel_threshold_e must be greater than 0")

    voxel_table: list[dict[str, float]] = []
    voxel_id = 1
    voxel_running_e = 0.0
    last_layer_num = 0

    for e_delta, layer_num in iter_deposited_extrusion_deltas(gcode_path):
        last_layer_num = layer_num
        voxel_running_e += e_delta

        if voxel_running_e >= voxel_threshold_e:
            voxel_table.append(
                {
                    "voxel_id": voxel_id,
                    "voxel_filament_e_mm": voxel_running_e,
                    "layer_num": layer_num,
                }
            )
            voxel_id += 1
            voxel_running_e = 0.0

    if voxel_running_e > 0:
        voxel_table.append(
            {
                "voxel_id": voxel_id,
                "voxel_filament_e_mm": voxel_running_e,
                "layer_num": last_layer_num,
            }
        )

    return voxel_table


def calculate_voxel_information(gcode_path: str, voxel_threshold_e: float = VOXEL_THRESHOLD_E) -> tuple[float, list[dict[str, float | int]]]:
    total_filament = calculate_total_filament(gcode_path)
    voxel_table = build_voxel_table(gcode_path, voxel_threshold_e)
    return total_filament, voxel_table


def save_voxel_information_json(
    gcode_path: str,
    json_path: str,
    voxel_threshold_e: float = VOXEL_THRESHOLD_E,
) -> None:
    total_filament, voxel_table = calculate_voxel_information(gcode_path, voxel_threshold_e)
    payload = {
        "source_gcode": gcode_path,
        "voxel_threshold_e": voxel_threshold_e,
        "total_filament_e_mm": total_filament,
        "voxel_count": len(voxel_table),
        "voxels": voxel_table,
    }
    Path(json_path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    total, voxel_table = calculate_voxel_information(GCODE_PATH)
    save_voxel_information_json(GCODE_PATH, VOXEL_JSON_PATH)
    print(f"Total filament extrusion: {total:.6f}")
    print(f"Voxel count: {len(voxel_table)}")
    print(f"Voxel JSON saved to: {VOXEL_JSON_PATH}")
