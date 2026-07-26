from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from openpyxl import load_workbook


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROPERTY_DATA_PATH = PROJECT_ROOT / "Property Data" / "source date.xlsx"


SUPPORTED_PROPERTY_KEYS = {
    "eb": "Eb_MPa",
    "elongation": "elongation_percent",
    "r0": "R0_ohm",
    "gf": "GF",
    "color": "color_value",
}
DEFAULT_RATIO_FAMILY_ETA = 6.0
FALLBACK_SINGLE_MATERIAL_ETA = 0.0


@dataclass
class PropertyCandidate:
    id: str
    material_pair: tuple[str, str]
    material_ratios: dict[str, float]
    eta: float | None
    properties: dict[str, Any]
    source_sheet: str
    source_row: int
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["material_pair"] = list(self.material_pair)
        return payload


@dataclass
class PropertyRequirement:
    required_property_type: str | None = None
    target_Eb_MPa: float | None = None
    Eb_tolerance_percent: float | None = None
    Eb_weight: float = 1.0
    min_elongation_percent: float | None = None
    target_elongation_percent: float | None = None
    elongation_weight: float = 1.0
    max_R0_ohm: float | None = None
    target_R0_ohm: float | None = None
    R0_weight: float = 1.0
    min_GF: float | None = None
    target_GF: float | None = None
    GF_weight: float = 1.0
    target_color: dict[str, float] | str | None = None
    color_tolerance: float | None = None
    color_weight: float = 1.0
    allowed_material_pairs: list[str] = field(default_factory=list)
    gradient_enabled: bool = False
    gradient_property: str | None = None
    gradient_start_value: float | None = None
    gradient_end_value: float | None = None
    gradient_direction: str = "printing"
    gradient_type: str = "linear"
    gradient_steps: int | None = None
    allow_fallback: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SelectedPropertyClass:
    candidate: PropertyCandidate | None
    target_values: dict[str, Any]
    reported_values: dict[str, Any]
    mismatch: dict[str, Any]
    feasible: bool
    failed_constraints: list[str]
    warnings: list[str]
    score: float | None = None
    fallback_selected: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.candidate is not None:
            payload["candidate"] = self.candidate.to_dict()
        return payload


def normalize_property_key(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    aliases = {
        "eb": "Eb_MPa",
        "flexural_modulus": "Eb_MPa",
        "elongation": "elongation_percent",
        "elongation_at_break": "elongation_percent",
        "r0": "R0_ohm",
        "ro": "R0_ohm",
        "gf": "GF",
        "gauge_factor": "GF",
        "color": "color_value",
    }
    return aliases.get(text, SUPPORTED_PROPERTY_KEYS.get(text))


def canonical_material_pair(material_a: str, material_b: str) -> str:
    return "/".join(sorted([str(material_a).upper(), str(material_b).upper()]))


def _safe_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _merge_candidate_property(
    candidate_map: dict[tuple[str, int], PropertyCandidate],
    key: tuple[str, int],
    *,
    material_pair: tuple[str, str],
    material_ratios: dict[str, float],
    eta: float | None,
    properties: dict[str, Any],
    source_sheet: str,
    source_row: int,
    notes: list[str],
) -> None:
    existing = candidate_map.get(key)
    if existing is None:
        candidate_map[key] = PropertyCandidate(
            id=f"{source_sheet.replace(' ', '_').lower()}_{source_row:03d}",
            material_pair=material_pair,
            material_ratios=material_ratios,
            eta=eta,
            properties=dict(properties),
            source_sheet=source_sheet,
            source_row=source_row,
            notes=list(notes),
        )
        return

    existing.properties.update({k: v for k, v in properties.items() if v is not None})
    if existing.eta is None and eta is not None:
        existing.eta = eta
    for note in notes:
        if note not in existing.notes:
            existing.notes.append(note)


def _parse_ratio_family_candidates() -> dict[tuple[str, int], PropertyCandidate]:
    workbook = load_workbook(PROPERTY_DATA_PATH, data_only=True)
    candidate_map: dict[tuple[str, int], PropertyCandidate] = {}

    eb_sheet = workbook["Supplementary Figure 6"]
    for row_index in range(5, 12):
        tpu_fraction = _safe_float(eb_sheet[f"E{row_index}"].value)
        eb_value = _safe_float(eb_sheet[f"F{row_index}"].value)
        if tpu_fraction is None or eb_value is None:
            continue
        cpla_fraction = 100.0 - tpu_fraction
        eta = FALLBACK_SINGLE_MATERIAL_ETA if tpu_fraction in {0.0, 100.0} else DEFAULT_RATIO_FAMILY_ETA
        _merge_candidate_property(
            candidate_map,
            ("ratio_family", int(round(tpu_fraction))),
            material_pair=("CPLA", "TPU"),
            material_ratios={"CPLA": cpla_fraction, "TPU": tpu_fraction},
            eta=eta,
            properties={"Eb_MPa": eb_value},
            source_sheet="Supplementary Figure 6",
            source_row=row_index,
            notes=["Eb candidate from reported CPLA/TPU ratio family."],
        )

    gf_sheet = workbook["Supplementary Figure 6"]
    for row_index in range(5, 11):
        tpu_fraction = _safe_float(gf_sheet[f"Q{row_index}"].value)
        gf_value = _safe_float(gf_sheet[f"R{row_index}"].value)
        if tpu_fraction is None or gf_value is None:
            continue
        cpla_fraction = 100.0 - tpu_fraction
        eta = FALLBACK_SINGLE_MATERIAL_ETA if tpu_fraction in {0.0, 100.0} else DEFAULT_RATIO_FAMILY_ETA
        _merge_candidate_property(
            candidate_map,
            ("ratio_family", int(round(tpu_fraction))),
            material_pair=("CPLA", "TPU"),
            material_ratios={"CPLA": cpla_fraction, "TPU": tpu_fraction},
            eta=eta,
            properties={"GF": gf_value},
            source_sheet="Supplementary Figure 6",
            source_row=row_index,
            notes=["GF candidate from reported CPLA/TPU ratio family."],
        )

    r0_sheet = workbook["Supplementary Figure 5"]
    for row_index in range(5, 11):
        tpu_fraction = _safe_float(r0_sheet[f"E{row_index}"].value)
        r0_value = _safe_float(r0_sheet[f"F{row_index}"].value)
        if tpu_fraction is None or r0_value is None:
            continue
        cpla_fraction = 100.0 - tpu_fraction
        eta = FALLBACK_SINGLE_MATERIAL_ETA if tpu_fraction in {0.0, 100.0} else DEFAULT_RATIO_FAMILY_ETA
        _merge_candidate_property(
            candidate_map,
            ("ratio_family", int(round(tpu_fraction))),
            material_pair=("CPLA", "TPU"),
            material_ratios={"CPLA": cpla_fraction, "TPU": tpu_fraction},
            eta=eta,
            properties={"R0_ohm": r0_value},
            source_sheet="Supplementary Figure 5",
            source_row=row_index,
            notes=["R0 candidate from reported CPLA/TPU ratio family."],
        )

    elong_sheet = workbook["Figure 2"]
    for row_index in range(5, 12):
        tpu_fraction = _safe_float(elong_sheet[f"M{row_index}"].value)
        elongation = _safe_float(elong_sheet[f"N{row_index}"].value)
        if tpu_fraction is None or elongation is None:
            continue
        cpla_fraction = 100.0 - tpu_fraction
        eta = FALLBACK_SINGLE_MATERIAL_ETA if tpu_fraction in {0.0, 100.0} else DEFAULT_RATIO_FAMILY_ETA
        _merge_candidate_property(
            candidate_map,
            ("ratio_family", int(round(tpu_fraction))),
            material_pair=("CPLA", "TPU"),
            material_ratios={"CPLA": cpla_fraction, "TPU": tpu_fraction},
            eta=eta,
            properties={"elongation_percent": elongation},
            source_sheet="Figure 2",
            source_row=row_index,
            notes=["Elongation candidate from reported CPLA/TPU ratio family."],
        )

    eta_sheet = workbook["Figure 2"]
    for row_index in range(5, 10):
        eta_value = _safe_float(eta_sheet[f"E{row_index}"].value)
        elongation = _safe_float(eta_sheet[f"F{row_index}"].value)
        if eta_value is None or elongation is None:
            continue
        _merge_candidate_property(
            candidate_map,
            ("eta_family", row_index),
            material_pair=("CPLA", "TPU"),
            material_ratios={"CPLA": 50.0, "TPU": 50.0},
            eta=eta_value,
            properties={"elongation_percent": elongation},
            source_sheet="Figure 2",
            source_row=row_index,
            notes=[
                "Elongation candidate from reported eta sweep.",
                "The reported eta sweep is mapped to the current CPLA/TPU workflow as a discrete manufacturable class.",
            ],
        )

    color_sheet = workbook["Supplementary Figure 2"]
    for row_index in range(4, 17):
        phi_cyan = _safe_float(color_sheet[f"B{row_index}"].value)
        phi_yellow = _safe_float(color_sheet[f"C{row_index}"].value)
        eta_value = _safe_float(color_sheet[f"D{row_index}"].value)
        c_value = _safe_float(color_sheet[f"G{row_index}"].value)
        m_value = _safe_float(color_sheet[f"H{row_index}"].value)
        y_value = _safe_float(color_sheet[f"I{row_index}"].value)
        profile = color_sheet[f"F{row_index}"].value
        if phi_cyan is None or phi_yellow is None:
            continue
        _merge_candidate_property(
            candidate_map,
            ("color_family", row_index),
            material_pair=("CYAN", "YELLOW"),
            material_ratios={"CYAN": phi_cyan * 100.0, "YELLOW": phi_yellow * 100.0},
            eta=eta_value if eta_value is not None else 2.0,
            properties={
                "color_value": {"C": c_value, "M": m_value, "Y": y_value},
                "color_profile": str(profile) if profile is not None else None,
            },
            source_sheet="Supplementary Figure 2",
            source_row=row_index,
            notes=["Color candidate from reported CYAN/YELLOW family."],
        )

    return candidate_map


def load_property_library() -> list[PropertyCandidate]:
    if not PROPERTY_DATA_PATH.exists():
        raise FileNotFoundError(f"Property source workbook not found: {PROPERTY_DATA_PATH}")
    return list(_parse_ratio_family_candidates().values())


def property_library_summary(library: list[PropertyCandidate]) -> dict[str, Any]:
    counts_by_pair: dict[str, int] = {}
    available_properties: dict[str, int] = {}
    for candidate in library:
        pair_key = canonical_material_pair(*candidate.material_pair)
        counts_by_pair[pair_key] = counts_by_pair.get(pair_key, 0) + 1
        for property_key, property_value in candidate.properties.items():
            if property_value is not None:
                available_properties[property_key] = available_properties.get(property_key, 0) + 1
    return {
        "candidate_count": len(library),
        "counts_by_material_pair": counts_by_pair,
        "available_property_counts": available_properties,
        "source_path": str(PROPERTY_DATA_PATH),
    }


def _candidate_property_range(library: list[PropertyCandidate], property_key: str) -> tuple[float, float] | None:
    values = []
    for candidate in library:
        value = candidate.properties.get(property_key)
        if isinstance(value, (int, float)):
            values.append(float(value))
    if not values:
        return None
    return min(values), max(values)


def _normalized_abs_error(candidate_value: float, target_value: float, value_range: tuple[float, float] | None) -> float:
    if value_range is None:
        denominator = abs(target_value) if abs(target_value) > 1e-9 else 1.0
        return abs(candidate_value - target_value) / denominator
    min_value, max_value = value_range
    span = max(max_value - min_value, 1e-9)
    return abs(candidate_value - target_value) / span


def _color_distance(candidate_color: dict[str, float], target_color: dict[str, float]) -> float:
    channels = sorted(set(candidate_color) | set(target_color))
    return sum(abs(float(candidate_color.get(channel, 0.0)) - float(target_color.get(channel, 0.0))) for channel in channels)


def _matches_allowed_pairs(candidate: PropertyCandidate, allowed_pairs: list[str]) -> bool:
    if not allowed_pairs:
        return True
    candidate_pair = canonical_material_pair(*candidate.material_pair)
    normalized_allowed = {
        canonical_material_pair(*str(item).replace("-", "/").replace(",", "/").split("/")[:2])
        if "/" in str(item) or "-" in str(item) or "," in str(item)
        else str(item).strip().upper()
        for item in allowed_pairs
    }
    return candidate_pair in normalized_allowed


def _supports_requirement(candidate: PropertyCandidate, requirement: PropertyRequirement) -> bool:
    if requirement.target_Eb_MPa is not None and candidate.properties.get("Eb_MPa") is None:
        return False
    if requirement.target_elongation_percent is not None and candidate.properties.get("elongation_percent") is None:
        return False
    if requirement.min_elongation_percent is not None and candidate.properties.get("elongation_percent") is None:
        return False
    if requirement.target_R0_ohm is not None and candidate.properties.get("R0_ohm") is None:
        return False
    if requirement.max_R0_ohm is not None and candidate.properties.get("R0_ohm") is None:
        return False
    if requirement.target_GF is not None and candidate.properties.get("GF") is None:
        return False
    if requirement.min_GF is not None and candidate.properties.get("GF") is None:
        return False
    if requirement.target_color is not None and candidate.properties.get("color_value") is None:
        return False
    return _matches_allowed_pairs(candidate, requirement.allowed_material_pairs)


def _threshold_failures(candidate: PropertyCandidate, requirement: PropertyRequirement) -> list[str]:
    failures: list[str] = []
    elongation = candidate.properties.get("elongation_percent")
    if requirement.min_elongation_percent is not None:
        if elongation is None or float(elongation) < float(requirement.min_elongation_percent):
            failures.append(f"elongation_percent<{requirement.min_elongation_percent}")
    r0_value = candidate.properties.get("R0_ohm")
    if requirement.max_R0_ohm is not None:
        if r0_value is None or float(r0_value) > float(requirement.max_R0_ohm):
            failures.append(f"R0_ohm>{requirement.max_R0_ohm}")
    gf_value = candidate.properties.get("GF")
    if requirement.min_GF is not None:
        if gf_value is None or float(gf_value) < float(requirement.min_GF):
            failures.append(f"GF<{requirement.min_GF}")
    return failures


def _score_candidate(candidate: PropertyCandidate, requirement: PropertyRequirement, library: list[PropertyCandidate]) -> tuple[float, dict[str, Any]]:
    mismatch: dict[str, Any] = {}
    score = 0.0

    if requirement.target_Eb_MPa is not None:
        candidate_value = float(candidate.properties["Eb_MPa"])
        value_range = _candidate_property_range(library, "Eb_MPa")
        error = _normalized_abs_error(candidate_value, float(requirement.target_Eb_MPa), value_range)
        mismatch["Eb_MPa"] = candidate_value - float(requirement.target_Eb_MPa)
        score += requirement.Eb_weight * error

    if requirement.target_elongation_percent is not None:
        candidate_value = float(candidate.properties["elongation_percent"])
        value_range = _candidate_property_range(library, "elongation_percent")
        error = _normalized_abs_error(candidate_value, float(requirement.target_elongation_percent), value_range)
        mismatch["elongation_percent"] = candidate_value - float(requirement.target_elongation_percent)
        score += requirement.elongation_weight * error

    if requirement.target_R0_ohm is not None:
        candidate_value = float(candidate.properties["R0_ohm"])
        value_range = _candidate_property_range(library, "R0_ohm")
        error = _normalized_abs_error(candidate_value, float(requirement.target_R0_ohm), value_range)
        mismatch["R0_ohm"] = candidate_value - float(requirement.target_R0_ohm)
        score += requirement.R0_weight * error

    if requirement.target_GF is not None:
        candidate_value = float(candidate.properties["GF"])
        value_range = _candidate_property_range(library, "GF")
        error = _normalized_abs_error(candidate_value, float(requirement.target_GF), value_range)
        mismatch["GF"] = candidate_value - float(requirement.target_GF)
        score += requirement.GF_weight * error

    if requirement.target_color is not None:
        if isinstance(requirement.target_color, dict):
            candidate_color = candidate.properties.get("color_value") or {}
            color_error = _color_distance(candidate_color, requirement.target_color)
            mismatch["color_value"] = color_error
            score += requirement.color_weight * color_error

    return score, mismatch


def select_property_candidate(
    requirement: PropertyRequirement,
    library: list[PropertyCandidate],
) -> SelectedPropertyClass:
    compatible_candidates = [candidate for candidate in library if _supports_requirement(candidate, requirement)]
    warnings: list[str] = []

    if not compatible_candidates:
        return SelectedPropertyClass(
            candidate=None,
            target_values=requirement.to_dict(),
            reported_values={},
            mismatch={},
            feasible=False,
            failed_constraints=["no_compatible_candidates"],
            warnings=["No library candidates support the requested property combination."],
            score=None,
            fallback_selected=False,
        )

    feasible_candidates: list[tuple[PropertyCandidate, float, dict[str, Any]]] = []
    infeasible_candidates: list[tuple[PropertyCandidate, float, dict[str, Any], list[str]]] = []

    for candidate in compatible_candidates:
        threshold_failures = _threshold_failures(candidate, requirement)
        score, mismatch = _score_candidate(candidate, requirement, library)
        if threshold_failures:
            infeasible_candidates.append((candidate, score, mismatch, threshold_failures))
        else:
            feasible_candidates.append((candidate, score, mismatch))

    selected_candidate: PropertyCandidate | None = None
    selected_score: float | None = None
    selected_mismatch: dict[str, Any] = {}
    failed_constraints: list[str] = []
    fallback_selected = False

    if feasible_candidates:
        feasible_candidates.sort(key=lambda item: (item[1], item[0].id))
        selected_candidate, selected_score, selected_mismatch = feasible_candidates[0]
    elif requirement.allow_fallback and infeasible_candidates:
        infeasible_candidates.sort(key=lambda item: (len(item[3]), item[1], item[0].id))
        selected_candidate, selected_score, selected_mismatch, failed_constraints = infeasible_candidates[0]
        fallback_selected = True
        warnings.append("No candidate satisfied all threshold constraints. Returned the closest reported fallback candidate.")
    else:
        failed_constraints = sorted({failure for _, _, _, failures in infeasible_candidates for failure in failures})

    if selected_candidate is None:
        return SelectedPropertyClass(
            candidate=None,
            target_values=requirement.to_dict(),
            reported_values={},
            mismatch={},
            feasible=False,
            failed_constraints=failed_constraints or ["no_feasible_candidate"],
            warnings=warnings or ["No feasible reported candidate was found."],
            score=None,
            fallback_selected=False,
        )

    if requirement.target_Eb_MPa is not None:
        eb_range = _candidate_property_range(library, "Eb_MPa")
        if eb_range is not None:
            min_eb, max_eb = eb_range
            if float(requirement.target_Eb_MPa) < min_eb or float(requirement.target_Eb_MPa) > max_eb:
                warnings.append(
                    f"Requested Eb target {requirement.target_Eb_MPa} MPa is outside the reported range {min_eb:.3f} to {max_eb:.3f} MPa."
                )

    return SelectedPropertyClass(
        candidate=selected_candidate,
        target_values=requirement.to_dict(),
        reported_values=dict(selected_candidate.properties),
        mismatch=selected_mismatch,
        feasible=not failed_constraints,
        failed_constraints=failed_constraints,
        warnings=warnings,
        score=selected_score,
        fallback_selected=fallback_selected,
    )


def select_gradient_sequence(
    requirement: PropertyRequirement,
    library: list[PropertyCandidate],
) -> dict[str, Any]:
    property_key = normalize_property_key(requirement.gradient_property or requirement.required_property_type)
    if property_key is None:
        raise ValueError("Gradient selection requires a supported gradient_property or required_property_type.")
    if requirement.gradient_start_value is None or requirement.gradient_end_value is None:
        raise ValueError("Gradient selection requires gradient_start_value and gradient_end_value.")

    candidates = [
        candidate
        for candidate in library
        if candidate.properties.get(property_key) is not None and _matches_allowed_pairs(candidate, requirement.allowed_material_pairs)
    ]
    if not candidates:
        return {
            "property_key": property_key,
            "sequence": [],
            "warnings": ["No reported candidates are available for the requested gradient property."],
            "feasible": False,
        }

    candidates.sort(key=lambda item: (float(item.properties[property_key]), item.id))
    step_count = int(requirement.gradient_steps) if requirement.gradient_steps is not None else 0
    if step_count <= 0:
        in_range = [
            candidate
            for candidate in candidates
            if min(float(requirement.gradient_start_value), float(requirement.gradient_end_value))
            <= float(candidate.properties[property_key])
            <= max(float(requirement.gradient_start_value), float(requirement.gradient_end_value))
        ]
        step_count = max(2, len(in_range) if in_range else 2)

    target_values = []
    if step_count == 1:
        target_values = [float(requirement.gradient_start_value)]
    else:
        for step_index in range(step_count):
            alpha = step_index / (step_count - 1)
            target_values.append(
                float(requirement.gradient_start_value)
                + (float(requirement.gradient_end_value) - float(requirement.gradient_start_value)) * alpha
            )

    sequence: list[dict[str, Any]] = []
    last_candidate_id: str | None = None
    for step_index, target_value in enumerate(target_values, start=1):
        nearest = min(
            candidates,
            key=lambda candidate: (
                abs(float(candidate.properties[property_key]) - target_value),
                candidate.id,
            ),
        )
        if nearest.id == last_candidate_id:
            continue
        last_candidate_id = nearest.id
        sequence.append(
            {
                "step_index": step_index,
                "target_value": target_value,
                "candidate": nearest.to_dict(),
                "reported_value": nearest.properties[property_key],
                "mismatch": float(nearest.properties[property_key]) - target_value,
            }
        )

    warnings: list[str] = []
    value_range = _candidate_property_range(candidates, property_key)
    if value_range is not None:
        min_value, max_value = value_range
        if float(requirement.gradient_start_value) < min_value or float(requirement.gradient_end_value) > max_value:
            warnings.append(
                f"Requested gradient range {requirement.gradient_start_value} to {requirement.gradient_end_value} is outside the reported {property_key} range {min_value:.3f} to {max_value:.3f}."
            )

    return {
        "property_key": property_key,
        "sequence": sequence,
        "warnings": warnings,
        "feasible": bool(sequence),
    }

