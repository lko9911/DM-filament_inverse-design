from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile
import xml.etree.ElementTree as ET

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROPERTY_EXCEL_ENV_KEY = "B_FDM_PROPERTY_EXCEL_PATH"
DEFAULT_PROPERTY_EXCEL_PATH = PROJECT_ROOT / "Property.xlsx"
SUPPLEMENTARY_FIGURE_2_SHEET = "Supplementary Figure 2"

def _profile(
    material_start: str,
    material_end: str | None,
    start_count: int,
    end_count: int,
    swatch_hex: str,
    fixed_eta: float | None = None,
) -> dict[str, object]:
    total = start_count + end_count
    profile: dict[str, object] = {
        "material_start": material_start,
        "material_end": material_end,
        "material_start_ratio": 100.0 * start_count / total,
        "material_end_ratio": 100.0 * end_count / total,
        "swatch_hex": swatch_hex,
    }
    if fixed_eta is not None:
        profile["fixed_eta"] = float(fixed_eta)
    return profile


# Figure 3d-f: five intermediate colors between each pair of CMY primaries,
# changing concentration in 8/48 (~16.7%) increments. BLACK and WHITE are
# additional direct-material choices, for 20 choices in total.
COLOR_PROFILE_RECIPES: dict[str, dict[str, object]] = {
    "M100": _profile("MAGENTA", None, 48, 0, "#d82d4b"),
    "M83_Y17": _profile("MAGENTA", "YELLOW", 40, 8, "#dc3524"),
    "M67_Y33": _profile("MAGENTA", "YELLOW", 32, 16, "#df5412"),
    "M50_Y50": _profile("MAGENTA", "YELLOW", 24, 24, "#e57000"),
    "M33_Y67": _profile("MAGENTA", "YELLOW", 16, 32, "#ee9800"),
    "M17_Y83": _profile("MAGENTA", "YELLOW", 8, 40, "#f6c600"),
    "Y100": _profile("YELLOW", None, 48, 0, "#f5ed16"),
    "Y83_C17": _profile("YELLOW", "CYAN", 40, 8, "#9bd400"),
    "Y67_C33": _profile("YELLOW", "CYAN", 32, 16, "#61b600"),
    "Y50_C50": _profile("YELLOW", "CYAN", 24, 24, "#359f13"),
    "Y33_C67": _profile("YELLOW", "CYAN", 16, 32, "#168b48"),
    "Y17_C83": _profile("YELLOW", "CYAN", 8, 40, "#007879"),
    "C100": _profile("CYAN", None, 48, 0, "#1689ca"),
    "C83_M17": _profile("CYAN", "MAGENTA", 40, 8, "#305ab7"),
    "C67_M33": _profile("CYAN", "MAGENTA", 32, 16, "#4b3f9b"),
    "C50_M50": _profile("CYAN", "MAGENTA", 24, 24, "#633279"),
    "C33_M67": _profile("CYAN", "MAGENTA", 16, 32, "#84265f"),
    "C17_M83": _profile("CYAN", "MAGENTA", 8, 40, "#b32763"),
    "PURPLE": _profile("CYAN", "MAGENTA", 10, 38, "#a92862", fixed_eta=1.5),
    "BLACK": _profile("BLACK", None, 48, 0, "#111111"),
    "WHITE": _profile("WHITE", None, 48, 0, "#f2f2f2"),
}
COLOR_PROFILE_OPTIONS = list(COLOR_PROFILE_RECIPES)
LEGACY_COLOR_PROFILE_ALIASES = {
    "MAGENTA": "M100",
    "YELLOW": "Y100",
    "CYAN": "C100",
    "RED": "M50_Y50",
    "ORANGE": "M33_Y67",
    "GREEN": "Y50_C50",
    "LIME": "Y67_C33",
    "TEAL": "Y17_C83",
    "BLUE": "C50_M50",
    "NAVY": "C67_M33",
    "PINK": "C17_M83",
}


def normalize_color_profile_key(value: object) -> str:
    key = str(value or "").strip().upper()
    return LEGACY_COLOR_PROFILE_ALIASES.get(key, key)


def color_profile_swatch(value: object) -> str:
    key = normalize_color_profile_key(value)
    recipe = COLOR_PROFILE_RECIPES.get(key)
    return str(recipe.get("swatch_hex", "#ffffff")) if recipe else "#ffffff"


@dataclass(frozen=True)
class PropertyExcelRow:
    e_value: float | None
    phi_cyan: float | None
    phi_yellow: float | None
    eta: float | None
    profile: str
    c: float | None
    m: float | None
    y: float | None


def property_excel_path() -> Path:
    configured = os.environ.get(PROPERTY_EXCEL_ENV_KEY, "").strip()
    return Path(configured).expanduser().resolve() if configured else DEFAULT_PROPERTY_EXCEL_PATH


def _column_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha())
    index = 0
    for letter in letters:
        index = (index * 26) + ord(letter.upper()) - 64
    return index


def _float_or_none(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _read_xlsx_sheet_rows(path: Path, sheet_name: str) -> list[list[str]]:
    if not path.exists():
        return []

    main_ns = {
        "a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    rel_ns = {"rel": "http://schemas.openxmlformats.org/package/2006/relationships"}

    with ZipFile(path) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in shared_root.findall("a:si", main_ns):
                shared_strings.append("".join(node.text or "" for node in item.findall(".//a:t", main_ns)))

        workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
        rels_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_targets = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in rels_root.findall("rel:Relationship", rel_ns)
        }

        sheet_path: str | None = None
        for sheet in workbook_root.findall("a:sheets/a:sheet", main_ns):
            if sheet.attrib.get("name") != sheet_name:
                continue
            rel_id = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            target = rel_targets.get(str(rel_id), "")
            sheet_path = "xl/" + target.lstrip("/") if not target.startswith("xl/") else target
            break

        if sheet_path is None:
            return []

        sheet_root = ET.fromstring(archive.read(sheet_path))
        rows: list[list[str]] = []
        for row in sheet_root.findall("a:sheetData/a:row", main_ns):
            values: list[str] = []
            previous_index = 0
            for cell in row.findall("a:c", main_ns):
                column_index = _column_index(cell.attrib.get("r", "A1"))
                while previous_index + 1 < column_index:
                    values.append("")
                    previous_index += 1

                value = ""
                node = cell.find("a:v", main_ns)
                if node is not None and node.text is not None:
                    if cell.attrib.get("t") == "s":
                        shared_index = int(node.text)
                        value = shared_strings[shared_index] if shared_index < len(shared_strings) else node.text
                    else:
                        value = node.text
                inline = cell.find("a:is/a:t", main_ns)
                if inline is not None and inline.text is not None:
                    value = inline.text

                values.append(value)
                previous_index = column_index
            rows.append(values)
    return rows


def load_supplementary_figure_2_rows(path: Path | None = None) -> list[PropertyExcelRow]:
    rows = _read_xlsx_sheet_rows(path or property_excel_path(), SUPPLEMENTARY_FIGURE_2_SHEET)
    data: list[PropertyExcelRow] = []
    for row in rows[2:]:
        padded = row + [""] * 9
        if not any(str(item).strip() for item in padded[:9]):
            continue
        data.append(
            PropertyExcelRow(
                e_value=_float_or_none(padded[0]),
                phi_cyan=_float_or_none(padded[1]),
                phi_yellow=_float_or_none(padded[2]),
                eta=_float_or_none(padded[3]),
                profile=str(padded[5] or "").strip(),
                c=_float_or_none(padded[6]),
                m=_float_or_none(padded[7]),
                y=_float_or_none(padded[8]),
            )
        )
    return data


def _nearest_excel_row(cmy: tuple[float, float, float], rows: list[PropertyExcelRow]) -> PropertyExcelRow | None:
    candidates = [row for row in rows if row.c is not None and row.m is not None and row.y is not None]
    if not candidates:
        return None
    target_norm = math.sqrt(sum(value * value for value in cmy)) or 1.0
    normalized_target = tuple(value / target_norm for value in cmy)

    def distance(row: PropertyExcelRow) -> float:
        row_values = (float(row.c or 0.0), float(row.m or 0.0), float(row.y or 0.0))
        row_norm = math.sqrt(sum(value * value for value in row_values)) or 1.0
        normalized_row = tuple(value / row_norm for value in row_values)
        return sum((left - right) ** 2 for left, right in zip(normalized_target, normalized_row))

    return min(candidates, key=distance)


def _nearest_ratio_eta_row(start_ratio: float, rows: list[PropertyExcelRow]) -> PropertyExcelRow | None:
    candidates = [row for row in rows if row.phi_cyan is not None and row.eta is not None]
    if not candidates:
        return None
    target = max(0.0, min(1.0, float(start_ratio)))
    return min(candidates, key=lambda row: abs(float(row.phi_cyan or 0.0) - target))


def _eta_lookup_cyan_ratio(
    material_start: str,
    material_end: str | None,
    start_ratio: float,
    end_ratio: float,
) -> float:
    total_ratio = float(start_ratio) + float(end_ratio)
    if total_ratio <= 0.0:
        return 0.0
    if material_start == "CYAN":
        return float(start_ratio) / total_ratio
    if material_end == "CYAN":
        return float(end_ratio) / total_ratio
    return float(start_ratio) / total_ratio


def resolve_color_recipe(
    color_name: str,
    *,
    brighter_mode: bool = False,
    target_mpa: float | None = None,
    target_gf: float | None = None,
) -> dict[str, object]:
    color_key = normalize_color_profile_key(color_name)
    if color_key not in COLOR_PROFILE_RECIPES:
        raise KeyError(f"Unknown color recipe: {color_name}")

    profile = COLOR_PROFILE_RECIPES[color_key]
    material_start = str(profile["material_start"])
    material_end_value = profile.get("material_end")
    material_end = str(material_end_value) if material_end_value else None
    start_ratio = float(profile["material_start_ratio"])
    end_ratio = float(profile["material_end_ratio"])
    target_cmy = (
        (start_ratio / 100.0 if material_start == "CYAN" else 0.0)
        + (end_ratio / 100.0 if material_end == "CYAN" else 0.0),
        (start_ratio / 100.0 if material_start == "MAGENTA" else 0.0)
        + (end_ratio / 100.0 if material_end == "MAGENTA" else 0.0),
        (start_ratio / 100.0 if material_start == "YELLOW" else 0.0)
        + (end_ratio / 100.0 if material_end == "YELLOW" else 0.0),
    )
    if color_key == "BLACK":
        target_cmy = (1.0, 1.0, 1.0)
    elif color_key == "WHITE":
        target_cmy = (0.0, 0.0, 0.0)
    rows = load_supplementary_figure_2_rows()
    matched_row = _nearest_ratio_eta_row(
        _eta_lookup_cyan_ratio(material_start, material_end, start_ratio, end_ratio),
        rows,
    )
    profile_row = _nearest_excel_row(target_cmy, rows)
    material_count = 1 if material_end is None or end_ratio <= 1e-9 else 2
    eta = 0.0 if material_count == 1 else float(matched_row.eta if matched_row and matched_row.eta is not None else 0.0)
    fixed_eta = None if brighter_mode else profile.get("fixed_eta")
    if fixed_eta is not None:
        eta = float(fixed_eta)

    recipe = {
        "requested_color": color_key,
        "brighter_mode": bool(brighter_mode),
        "target_mpa": None if target_mpa is None else float(target_mpa),
        "target_gf": None if target_gf is None else float(target_gf),
        "source": str(property_excel_path()),
        "sheet": SUPPLEMENTARY_FIGURE_2_SHEET,
        "target_cmy": {
            "CYAN": float(target_cmy[0]),
            "MAGENTA": float(target_cmy[1]),
            "YELLOW": float(target_cmy[2]),
        },
        "material_count": material_count,
        "material_start": material_start,
        "material_end": material_end,
        "material_start_ratio": start_ratio,
        "material_end_ratio": end_ratio,
        "eta": eta,
        "fixed_eta": None if fixed_eta is None else float(fixed_eta),
        "swatch_hex": profile["swatch_hex"],
        "matched_excel_row": (
            {
                "E": matched_row.e_value,
                "phi_cyan": matched_row.phi_cyan,
                "phi_yellow": matched_row.phi_yellow,
                "eta": matched_row.eta,
                "profile": matched_row.profile,
                "C": matched_row.c,
                "M": matched_row.m,
                "Y": matched_row.y,
            }
            if matched_row is not None
            else None
        ),
        "matched_profile_row": (
            {
                "E": profile_row.e_value,
                "phi_cyan": profile_row.phi_cyan,
                "phi_yellow": profile_row.phi_yellow,
                "eta": profile_row.eta,
                "profile": profile_row.profile,
                "C": profile_row.c,
                "M": profile_row.m,
                "Y": profile_row.y,
            }
            if profile_row is not None
            else None
        ),
    }
    return recipe
