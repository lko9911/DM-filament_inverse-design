from __future__ import annotations

import json
from itertools import product
from pathlib import Path


MATERIAL_START = "Material_start"
MATERIAL_END = "Material_end"
WHITE = "White"
MATRIX_LENGTH = 14
ROW_WEIGHTS = [2, 3, 3, 4, 4, 4, 4, 4, 4, 4, 4, 3, 3, 2]
OUTPUT_PATH = Path("input/config/material_dictionary.json")
BRIGHTER_WHITE_ROW_INDICES = {0, 1, 2, 3, 10, 11, 12, 13}


def build_case_payload(case_rows: list[str]) -> dict[str, object]:
    if len(case_rows) != len(ROW_WEIGHTS):
        raise ValueError(f"Expected {len(ROW_WEIGHTS)} rows, got {len(case_rows)}")

    material_start_count = 0
    material_end_count = 0
    interface_sum = 0
    for row_index, (material, weight) in enumerate(zip(case_rows, ROW_WEIGHTS), start=1):
        if material == MATERIAL_START:
            material_start_count += weight
        elif material == MATERIAL_END:
            material_end_count += weight
        elif material == WHITE:
            material_start_count += weight
        else:
            raise ValueError(f"Unknown material: {material}")

        if row_index < len(case_rows) and case_rows[row_index - 1] != case_rows[row_index]:
            interface_sum += min(weight, ROW_WEIGHTS[row_index])

    total_count = material_start_count + material_end_count
    return {
        "case_rows": case_rows,
        "material_start_count": material_start_count,
        "material_end_count": material_end_count,
        "total_count": total_count,
        "material_start_ratio": material_start_count / total_count if total_count else 0.0,
        "material_end_ratio": material_end_count / total_count if total_count else 0.0,
        "interface_sum": interface_sum,
        "eta": interface_sum / 4 if interface_sum else 0.0,
    }


def build_material_dictionary() -> dict[str, dict[str, object]]:
    material_dictionary: dict[str, dict[str, object]] = {}

    for case_index, combination in enumerate(product([MATERIAL_START, MATERIAL_END], repeat=MATRIX_LENGTH), start=1):
        case_key = f"case_{case_index:05d}"
        material_dictionary[case_key] = build_case_payload(list(combination))

    next_case_index = len(material_dictionary) + 1
    variable_indices = [index for index in range(MATRIX_LENGTH) if index not in BRIGHTER_WHITE_ROW_INDICES]
    for combination in product([MATERIAL_START, MATERIAL_END], repeat=len(variable_indices)):
        case_rows = [WHITE if index in BRIGHTER_WHITE_ROW_INDICES else MATERIAL_START for index in range(MATRIX_LENGTH)]
        for index, material in zip(variable_indices, combination):
            case_rows[index] = material
        case_key = f"case_{next_case_index:05d}"
        material_dictionary[case_key] = build_case_payload(case_rows)
        next_case_index += 1

    return material_dictionary


def save_material_dictionary(output_path: Path, material_dictionary: dict[str, dict[str, object]]) -> None:
    output_path.write_text(
        json.dumps(material_dictionary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    material_dictionary = build_material_dictionary()
    save_material_dictionary(OUTPUT_PATH, material_dictionary)
    print(f"Generated cases: {len(material_dictionary)}")
    print(f"Saved to: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
