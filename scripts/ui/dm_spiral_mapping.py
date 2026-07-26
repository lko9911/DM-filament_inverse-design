from __future__ import annotations

from dataclasses import dataclass
import math
import os

try:
    from .layer_region_analysis import LayerRegionAnalysis, LayerRegionOccurrence
except ImportError:
    from layer_region_analysis import LayerRegionAnalysis, LayerRegionOccurrence


DEFAULT_INNER_RADIUS_MM = 50.0
DEFAULT_PITCH_MM = 2.15
DEFAULT_PREVIEW_SAMPLE_MM = 0.75
DEFAULT_FEED_START_MM = 200.0
DEFAULT_FEED_END_MM = 60.0
SPIRAL_FEED_START_ENV_KEY = "B_FDM_SPIRAL_FEED_START_MM"
SPIRAL_FEED_END_ENV_KEY = "B_FDM_SPIRAL_FEED_END_MM"


def _env_float(name: str, fallback: float) -> float:
    try:
        return float(os.environ.get(name, fallback))
    except (TypeError, ValueError):
        return fallback


@dataclass(frozen=True)
class SpiralMappingSegment:
    sequence_index: int | None
    occurrence: LayerRegionOccurrence | None
    role: str
    filament_start_mm: float
    filament_end_mm: float
    points: tuple[tuple[float, float, float], ...]

    @property
    def length_mm(self) -> float:
        return self.filament_end_mm - self.filament_start_mm


@dataclass(frozen=True)
class SpiralMapping:
    segments: tuple[SpiralMappingSegment, ...]
    mapped_length_mm: float
    total_length_with_feed_mm: float
    inner_radius_mm: float
    outer_radius_mm: float
    pitch_mm: float
    reverse_execution_for_manufacturing: bool

    def occurrence_segment(self, sequence_index: int) -> SpiralMappingSegment | None:
        return next(
            (
                segment
                for segment in self.segments
                if segment.sequence_index == sequence_index
            ),
            None,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": "b_fdm.object_spiral_mapping.v1",
            "mapped_length_mm": self.mapped_length_mm,
            "total_length_with_feed_mm": self.total_length_with_feed_mm,
            "inner_radius_mm": self.inner_radius_mm,
            "outer_radius_mm": self.outer_radius_mm,
            "pitch_mm": self.pitch_mm,
            "manufacturing_order": (
                "reverse_execution"
                if self.reverse_execution_for_manufacturing
                else "execution"
            ),
            "consumption_order": (
                "execution"
                if self.reverse_execution_for_manufacturing
                else "reverse_execution"
            ),
            "segments": [
                {
                    "role": segment.role,
                    "sequence_index": segment.sequence_index,
                    "layer_index": (
                        segment.occurrence.layer_index
                        if segment.occurrence is not None
                        else None
                    ),
                    "layer_label": (
                        segment.occurrence.layer_label
                        if segment.occurrence is not None
                        else None
                    ),
                    "region_name": (
                        segment.occurrence.region_name
                        if segment.occurrence is not None
                        else None
                    ),
                    "occurrence_index": (
                        segment.occurrence.occurrence_index
                        if segment.occurrence is not None
                        else None
                    ),
                    "filament_start_mm": segment.filament_start_mm,
                    "filament_end_mm": segment.filament_end_mm,
                    "length_mm": segment.length_mm,
                    "spiral_start_xyz": list(segment.points[0]),
                    "spiral_end_xyz": list(segment.points[-1]),
                }
                for segment in self.segments
            ],
        }


def _advance_spiral(
    radius: float,
    phi: float,
    length_mm: float,
    *,
    pitch_mm: float,
    sample_mm: float,
) -> tuple[float, float, tuple[tuple[float, float, float], ...]]:
    b = pitch_mm / (2.0 * math.pi)
    points = [(radius * math.cos(phi), radius * math.sin(phi), 0.0)]
    remaining = max(0.0, float(length_mm))
    while remaining > 1e-12:
        step = min(sample_mm, remaining)
        phi += step / radius
        radius = b * phi
        points.append((radius * math.cos(phi), radius * math.sin(phi), 0.0))
        remaining -= step
    return radius, phi, tuple(points)


def build_spiral_mapping(
    analysis: LayerRegionAnalysis,
    *,
    feed_start_mm: float | None = None,
    feed_end_mm: float | None = None,
    inner_radius_mm: float = DEFAULT_INNER_RADIUS_MM,
    pitch_mm: float = DEFAULT_PITCH_MM,
    sample_mm: float = DEFAULT_PREVIEW_SAMPLE_MM,
    included_sequence_indices: set[int] | None = None,
    reverse_execution_for_manufacturing: bool = True,
) -> SpiralMapping:
    if inner_radius_mm <= 0.0:
        raise ValueError("inner_radius_mm must be positive.")
    if pitch_mm <= 0.0:
        raise ValueError("pitch_mm must be positive.")
    if sample_mm <= 0.0:
        raise ValueError("sample_mm must be positive.")

    feed_start_mm = (
        _env_float(SPIRAL_FEED_START_ENV_KEY, DEFAULT_FEED_START_MM)
        if feed_start_mm is None
        else float(feed_start_mm)
    )
    feed_end_mm = (
        _env_float(SPIRAL_FEED_END_ENV_KEY, DEFAULT_FEED_END_MM)
        if feed_end_mm is None
        else float(feed_end_mm)
    )
    radius = float(inner_radius_mm)
    b = pitch_mm / (2.0 * math.pi)
    phi = radius / b
    cursor = 0.0
    segments: list[SpiralMappingSegment] = []

    def append_segment(
        length_mm: float,
        *,
        role: str,
        occurrence: LayerRegionOccurrence | None = None,
    ) -> None:
        nonlocal radius, phi, cursor
        start = cursor
        radius, phi, points = _advance_spiral(
            radius,
            phi,
            length_mm,
            pitch_mm=pitch_mm,
            sample_mm=sample_mm,
        )
        cursor += float(length_mm)
        segments.append(
            SpiralMappingSegment(
                sequence_index=occurrence.sequence_index if occurrence else None,
                occurrence=occurrence,
                role=role,
                filament_start_mm=start,
                filament_end_mm=cursor,
                points=points,
            )
        )

    if feed_start_mm > 0.0:
        append_segment(feed_start_mm, role="start_feed")
    mapped_occurrences = [
        occurrence
        for occurrence in analysis.occurrences
        if included_sequence_indices is None
        or occurrence.sequence_index in included_sequence_indices
    ]
    if reverse_execution_for_manufacturing:
        mapped_occurrences.reverse()
    for occurrence in mapped_occurrences:
        append_segment(
            occurrence.extrusion_e_mm,
            role="mapped_occurrence",
            occurrence=occurrence,
        )
    if feed_end_mm > 0.0:
        append_segment(feed_end_mm, role="end_feed")

    return SpiralMapping(
        segments=tuple(segments),
        mapped_length_mm=sum(
            occurrence.extrusion_e_mm
            for occurrence in analysis.occurrences
            if included_sequence_indices is None
            or occurrence.sequence_index in included_sequence_indices
        ),
        total_length_with_feed_mm=cursor,
        inner_radius_mm=float(inner_radius_mm),
        outer_radius_mm=radius,
        pitch_mm=float(pitch_mm),
        reverse_execution_for_manufacturing=reverse_execution_for_manufacturing,
    )
