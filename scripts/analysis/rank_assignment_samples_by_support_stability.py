from __future__ import annotations

from collections import Counter
from pathlib import Path
import json


SOURCE_PATH = Path("test_sample/derived/continuity/assignment_samples_100_matrix.json")
OUTPUT_JSON_PATH = Path("test_sample/derived/continuity/best_assignment_pattern_support.json")
OUTPUT_TXT_PATH = Path("test_sample/derived/continuity/best_assignment_pattern_support.txt")
TOP_K = 10


def load_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def count_row_materials(matrix: list[list[object]], row_index: int) -> Counter[str]:
    counts: Counter[str] = Counter()
    for value in matrix[row_index]:
        if value is None:
            continue
        counts[str(value)] += 1
    return counts


def analyze_row(row_values: list[object], row_index: int, total_rows: int) -> dict[str, object]:
    counts = Counter(str(value) for value in row_values if value is not None)
    total_count = sum(counts.values())
    ranked: list[dict[str, object]] = []
    for material, count in counts.items():
        ratio = (count / total_count) if total_count else 0.0
        ranked.append(
            {
                "material": material,
                "count": int(count),
                "ratio": float(ratio),
            }
        )
    ranked.sort(key=lambda item: (-float(item["ratio"]), str(item["material"])))

    dominant = ranked[0] if ranked else {"material": None, "count": 0, "ratio": 0.0}
    layer_weight = total_rows - row_index
    if layer_weight <= 0:
        layer_weight = 1

    return {
        "row_index": row_index + 1,
        "material_rankings": ranked,
        "dominant_material": dominant["material"],
        "dominant_count": int(dominant["count"]),
        "dominant_ratio": float(dominant["ratio"]),
        "layer_weight": layer_weight,
    }


def evaluate_sample(sample: dict) -> dict[str, object]:
    matrix = sample.get("material_name_matrix", [])
    if not matrix:
        raise ValueError("material_name_matrix is missing or empty")

    evaluation_matrix = list(reversed(matrix))
    row_count = len(evaluation_matrix)
    col_count = len(evaluation_matrix[0]) if evaluation_matrix[0] else 0
    if col_count == 0:
        raise ValueError("material_name_matrix has zero columns")

    row_analysis = [
        analyze_row(row_values, row_index, row_count)
        for row_index, row_values in enumerate(evaluation_matrix)
    ]

    switch_count = 0
    early_switch_penalty = 0
    support_penalty = 0.0
    support_score = 0.0
    dominant_ratio_score = 0.0

    previous_material: str | None = None
    for row in row_analysis:
        current_material = row["dominant_material"]
        dominant_ratio = float(row["dominant_ratio"])
        layer_weight = int(row["layer_weight"])

        dominant_ratio_score += dominant_ratio
        support_score += layer_weight * dominant_ratio
        support_penalty += layer_weight * (1.0 - dominant_ratio)

        if previous_material is not None and current_material != previous_material:
            switch_count += 1
            early_switch_penalty += layer_weight
        previous_material = current_material

    return {
        "sample_rank": int(sample["sample_rank"]),
        "pattern_index": int(sample["pattern_index"]),
        "score": {
            "switch_count": switch_count,
            "dominant_ratio_score": round(dominant_ratio_score, 6),
            "support_penalty": round(support_penalty, 6),
            "support_score": round(support_score, 6),
            "early_switch_penalty": early_switch_penalty,
        },
        "row_analysis": row_analysis,
        "material_name_matrix": sample.get("material_name_matrix", []),
        "material_code_matrix": sample.get("material_code_matrix", []),
    }


def format_sample_text(sample_result: dict) -> str:
    score = sample_result["score"]
    lines: list[str] = []
    lines.append(
        "sample_rank: {rank} | pattern_index: {pattern_index} | switch_count: {switch_count} | "
        "dominant_ratio_score: {dominant_ratio_score:.6f} | support_penalty: {support_penalty:.6f} | "
        "support_score: {support_score:.6f} | early_switch_penalty: {early_switch_penalty}".format(
            rank=int(sample_result["sample_rank"]),
            pattern_index=int(sample_result["pattern_index"]),
            switch_count=int(score["switch_count"]),
            dominant_ratio_score=float(score["dominant_ratio_score"]),
            support_penalty=float(score["support_penalty"]),
            support_score=float(score["support_score"]),
            early_switch_penalty=int(score["early_switch_penalty"]),
        )
    )
    lines.append("row analysis:")
    for row in sample_result.get("row_analysis", []):
        rankings = row.get("material_rankings", [])
        ranking_text = ", ".join(f"{item['material']}={float(item['ratio']):.6f}" for item in rankings)
        lines.append(
            "  row_{row_index:02d} | dominant {dominant} | ratio {ratio:.6f} | weight {weight} | {rankings}".format(
                row_index=int(row["row_index"]),
                dominant=row["dominant_material"],
                ratio=float(row["dominant_ratio"]),
                weight=int(row["layer_weight"]),
                rankings=ranking_text,
            )
        )
    return "\n".join(lines)


def build_report(payload: dict, best_result: dict, ranked_results: list[dict[str, object]]) -> dict:
    return {
        "source_path": str(SOURCE_PATH),
        "sample_count": len(payload.get("samples", [])),
        "top_k": TOP_K,
        "best_sample": best_result,
        "ranking": ranked_results[:TOP_K],
    }


def main() -> None:
    payload = load_json(SOURCE_PATH)
    sample_results = [evaluate_sample(sample) for sample in payload.get("samples", [])]
    sample_results.sort(
        key=lambda item: (
            -float(item["score"]["dominant_ratio_score"]),
            -float(item["score"]["support_score"]),
            int(item["score"]["switch_count"]),
            float(item["score"]["support_penalty"]),
            int(item["score"]["early_switch_penalty"]),
            int(item["pattern_index"]),
        )
    )

    best_result = sample_results[0] if sample_results else {}
    report = build_report(payload, best_result, sample_results)

    OUTPUT_JSON_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    text_lines: list[str] = []
    text_lines.append(f"source_path: {report['source_path']}")
    text_lines.append(f"sample_count: {report['sample_count']}")
    text_lines.append("")
    if best_result:
        text_lines.append("best sample:")
        text_lines.append(format_sample_text(best_result))
        text_lines.append("")
    text_lines.append("top ranking:")
    for idx, item in enumerate(sample_results[:TOP_K], start=1):
        score = item["score"]
        text_lines.append(
            "  {rank:02d}. sample_{sample_rank:03d} | pattern_index {pattern_index} | "
            "switch_count {switch_count} | dominant_ratio_score {dominant_ratio_score:.6f} | "
            "support_penalty {support_penalty:.6f} | support_score {support_score:.6f} | "
            "early_switch_penalty {early_switch_penalty}".format(
                rank=idx,
                sample_rank=int(item["sample_rank"]),
                pattern_index=int(item["pattern_index"]),
                switch_count=int(score["switch_count"]),
                dominant_ratio_score=float(score["dominant_ratio_score"]),
                support_penalty=float(score["support_penalty"]),
                support_score=float(score["support_score"]),
                early_switch_penalty=int(score["early_switch_penalty"]),
            )
        )

    OUTPUT_TXT_PATH.write_text("\n".join(text_lines), encoding="utf-8")

    print(f"Best sample rank: {best_result.get('sample_rank')}")
    print(f"Best pattern index: {best_result.get('pattern_index')}")
    print(f"Saved JSON to: {OUTPUT_JSON_PATH}")
    print(f"Saved TXT to: {OUTPUT_TXT_PATH}")


if __name__ == "__main__":
    main()
