from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
from typing import Any

try:
    from .component_property_designer import parse_words, strip_comment
    from .full_gcode_object_property_designer import (
        canonical_component_name,
        parse_feature_type,
        parse_mesh_comment,
        parse_m486_name,
        parse_m486_select,
        parse_object_comment,
        parse_region_end,
        parse_region_start,
    )
except ImportError:
    from component_property_designer import parse_words, strip_comment
    from full_gcode_object_property_designer import (
        canonical_component_name,
        parse_feature_type,
        parse_mesh_comment,
        parse_m486_name,
        parse_m486_select,
        parse_object_comment,
        parse_region_end,
        parse_region_start,
    )


LAYER_NUMBER_RE = re.compile(r"^;\s*LAYER\s*:\s*(?P<number>-?\d+)\s*$", re.IGNORECASE)
LAYER_CHANGE_RE = re.compile(r"^;\s*LAYER_CHANGE\s*$", re.IGNORECASE)
LAYER_Z_RE = re.compile(
    r"^;\s*Z\s*:\s*(?P<value>[-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*$",
    re.IGNORECASE,
)

REGION_COLORS = [
    "#2563eb",
    "#dc2626",
    "#16a34a",
    "#9333ea",
    "#ea580c",
    "#0891b2",
    "#ca8a04",
    "#db2777",
    "#4f46e5",
    "#059669",
]


@dataclass
class LayerRegionSegment:
    x0: float
    y0: float
    z0: float
    x1: float
    y1: float
    z1: float
    e_delta: float
    source_line: int
    feature_type: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "x0": self.x0,
            "y0": self.y0,
            "z0": self.z0,
            "x1": self.x1,
            "y1": self.y1,
            "z1": self.z1,
            "e_delta": self.e_delta,
            "source_line": self.source_line,
            "feature_type": self.feature_type,
        }


@dataclass
class LayerRegionOccurrence:
    sequence_index: int
    layer_index: int
    layer_label: int
    layer_z: float | None
    region_name: str
    occurrence_index: int
    source_line_start: int
    source_line_end: int
    segments: list[LayerRegionSegment] = field(default_factory=list)

    @property
    def extrusion_e_mm(self) -> float:
        return sum(segment.e_delta for segment in self.segments)

    @property
    def segment_count(self) -> int:
        return len(self.segments)

    @property
    def feature_types(self) -> list[str]:
        return sorted(
            {
                str(segment.feature_type)
                for segment in self.segments
                if segment.feature_type
            }
        )

    @property
    def bounds_xy(self) -> list[float] | None:
        if not self.segments:
            return None
        xs = [coordinate for segment in self.segments for coordinate in (segment.x0, segment.x1)]
        ys = [coordinate for segment in self.segments for coordinate in (segment.y0, segment.y1)]
        return [min(xs), max(xs), min(ys), max(ys)]

    def to_dict(self, *, include_segments: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "sequence_index": self.sequence_index,
            "layer_index": self.layer_index,
            "layer_label": self.layer_label,
            "layer_z": self.layer_z,
            "region_name": self.region_name,
            "occurrence_index": self.occurrence_index,
            "source_line_start": self.source_line_start,
            "source_line_end": self.source_line_end,
            "extrusion_e_mm": self.extrusion_e_mm,
            "segment_count": self.segment_count,
            "feature_types": self.feature_types,
            "bounds_xy": self.bounds_xy,
        }
        if include_segments:
            payload["segments"] = [segment.to_dict() for segment in self.segments]
        return payload


@dataclass
class LayerRegionAnalysis:
    source_gcode: str
    occurrences: list[LayerRegionOccurrence]
    total_deposition_e_mm: float
    region_deposition_e_mm: float
    non_region_deposition_e_mm: float
    layer_count: int
    region_names: list[str]
    warnings: list[str] = field(default_factory=list)

    def occurrences_for_layer(self, layer_index: int) -> list[LayerRegionOccurrence]:
        return [
            occurrence
            for occurrence in self.occurrences
            if occurrence.layer_index == layer_index
        ]

    def layer_indices(self) -> list[int]:
        return sorted({occurrence.layer_index for occurrence in self.occurrences})

    def to_payload(self, *, include_segments: bool = True) -> dict[str, object]:
        region_totals: dict[str, float] = {}
        layer_totals: dict[str, float] = {}
        for occurrence in self.occurrences:
            region_totals[occurrence.region_name] = (
                region_totals.get(occurrence.region_name, 0.0)
                + occurrence.extrusion_e_mm
            )
            layer_key = str(occurrence.layer_label)
            layer_totals[layer_key] = (
                layer_totals.get(layer_key, 0.0)
                + occurrence.extrusion_e_mm
            )

        return {
            "schema": "b_fdm.layer_region_analysis.v1",
            "source_gcode": self.source_gcode,
            "resolution_mode": "layer_region_occurrence",
            "layer_count": self.layer_count,
            "region_count": len(self.region_names),
            "region_names": self.region_names,
            "occurrence_count": len(self.occurrences),
            "total_deposition_e_mm": self.total_deposition_e_mm,
            "region_deposition_e_mm": self.region_deposition_e_mm,
            "non_region_deposition_e_mm": self.non_region_deposition_e_mm,
            "region_coverage_ratio": (
                self.region_deposition_e_mm / self.total_deposition_e_mm
                if self.total_deposition_e_mm > 0.0
                else 0.0
            ),
            "region_totals_e_mm": region_totals,
            "layer_totals_e_mm": layer_totals,
            "warnings": list(self.warnings),
            "occurrences": [
                occurrence.to_dict(include_segments=include_segments)
                for occurrence in self.occurrences
            ],
        }

    def save_json(self, output_path: Path, *, include_segments: bool = True) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(
                self.to_payload(include_segments=include_segments),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return output_path


def _collect_m486_names(lines: list[str]) -> dict[int, str]:
    names_by_id: dict[int, str] = {}
    pending_id: int | None = None
    for raw_line in lines:
        selected_id = parse_m486_select(raw_line)
        if selected_id is not None:
            pending_id = selected_id if selected_id >= 0 else None
            continue
        object_name = parse_m486_name(raw_line)
        if object_name is not None and pending_id is not None:
            names_by_id[pending_id] = canonical_component_name(object_name)
            pending_id = None
    return names_by_id


def analyze_layer_regions(gcode_path: Path | str) -> LayerRegionAnalysis:
    path = Path(gcode_path).resolve()
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    m486_names = _collect_m486_names(lines)

    x = y = z = e = 0.0
    absolute_xyz = True
    absolute_e = True
    current_feature_type: str | None = None
    active_region: str | None = None
    current_layer_index = -1
    current_layer_label = -1
    current_layer_z: float | None = None
    next_layer_index = 0
    occurrences: list[LayerRegionOccurrence] = []
    active_occurrence: LayerRegionOccurrence | None = None
    occurrence_counts: dict[tuple[int, str], int] = {}
    total_deposition_e = 0.0
    region_deposition_e = 0.0
    non_region_deposition_e = 0.0
    seen_layer_indices: set[int] = set()
    seen_region_names: list[str] = []
    warnings: list[str] = []

    def close_occurrence(source_line_end: int) -> None:
        nonlocal active_occurrence
        if active_occurrence is None:
            return
        active_occurrence.source_line_end = max(
            active_occurrence.source_line_start,
            source_line_end,
        )
        if active_occurrence.segments:
            active_occurrence.sequence_index = len(occurrences) + 1
            occurrences.append(active_occurrence)
        active_occurrence = None

    def set_active_region(region_name: str | None, source_line: int) -> None:
        nonlocal active_region
        canonical_name = (
            canonical_component_name(region_name)
            if region_name is not None
            else None
        )
        if canonical_name == active_region:
            return
        close_occurrence(source_line - 1)
        active_region = canonical_name

    for source_line, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        layer_number_match = LAYER_NUMBER_RE.match(stripped)
        if layer_number_match is not None:
            close_occurrence(source_line - 1)
            current_layer_label = int(layer_number_match.group("number"))
            current_layer_index = next_layer_index
            next_layer_index += 1
            current_layer_z = None
            seen_layer_indices.add(current_layer_index)
            continue
        if LAYER_CHANGE_RE.match(stripped):
            close_occurrence(source_line - 1)
            current_layer_index = next_layer_index
            current_layer_label = next_layer_index
            next_layer_index += 1
            current_layer_z = None
            seen_layer_indices.add(current_layer_index)
            continue

        layer_z_match = LAYER_Z_RE.match(stripped)
        if layer_z_match is not None and current_layer_index >= 0:
            current_layer_z = float(layer_z_match.group("value"))
            continue

        feature_type = parse_feature_type(raw_line)
        if feature_type is not None:
            current_feature_type = feature_type

        region_start = parse_region_start(raw_line)
        if region_start is not None:
            set_active_region(region_start[0], source_line)
            continue
        region_end = parse_region_end(raw_line)
        if region_end is not None:
            set_active_region(None, source_line)
            continue

        object_comment = parse_object_comment(raw_line)
        if object_comment is not None:
            action, object_key = object_comment
            set_active_region(
                object_key.display_name if action == "printing object" else None,
                source_line,
            )
            continue

        mesh_name = parse_mesh_comment(raw_line)
        if mesh_name is not None:
            set_active_region(
                None if mesh_name.upper() == "NONMESH" else mesh_name,
                source_line,
            )
            continue

        m486_select = parse_m486_select(raw_line)
        if m486_select is not None:
            set_active_region(
                m486_names.get(m486_select) if m486_select >= 0 else None,
                source_line,
            )
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
        next_e = (
            words["E"] if absolute_e else e + words["E"]
        ) if "E" in words else e
        e_delta = next_e - e
        has_spatial_motion = (next_x, next_y, next_z) != (x, y, z)

        if e_delta > 1e-12 and has_spatial_motion:
            total_deposition_e += e_delta
            if active_region is not None and current_layer_index >= 0:
                if active_region not in seen_region_names:
                    seen_region_names.append(active_region)
                if active_occurrence is None:
                    occurrence_key = (current_layer_index, active_region)
                    occurrence_index = occurrence_counts.get(occurrence_key, 0) + 1
                    occurrence_counts[occurrence_key] = occurrence_index
                    active_occurrence = LayerRegionOccurrence(
                        sequence_index=0,
                        layer_index=current_layer_index,
                        layer_label=current_layer_label,
                        layer_z=current_layer_z if current_layer_z is not None else next_z,
                        region_name=active_region,
                        occurrence_index=occurrence_index,
                        source_line_start=source_line,
                        source_line_end=source_line,
                    )
                active_occurrence.segments.append(
                    LayerRegionSegment(
                        x0=x,
                        y0=y,
                        z0=z,
                        x1=next_x,
                        y1=next_y,
                        z1=next_z,
                        e_delta=e_delta,
                        source_line=source_line,
                        feature_type=current_feature_type,
                    )
                )
                active_occurrence.source_line_end = source_line
                if active_occurrence.layer_z is None:
                    active_occurrence.layer_z = next_z
                region_deposition_e += e_delta
            else:
                non_region_deposition_e += e_delta

        x, y, z, e = next_x, next_y, next_z, next_e

    close_occurrence(len(lines))

    if not occurrences:
        warnings.append(
            "No layer-region deposition occurrences were found. "
            "Enable slicer object/mesh labels such as ;MESH, M486, or ; printing object."
        )
    if non_region_deposition_e > 1e-9:
        warnings.append(
            "Some deposition is outside labeled regions. Prime, skirt, support, wipe, "
            "or unlabeled model extrusion must be handled as synchronization consumption."
        )

    return LayerRegionAnalysis(
        source_gcode=str(path),
        occurrences=occurrences,
        total_deposition_e_mm=total_deposition_e,
        region_deposition_e_mm=region_deposition_e,
        non_region_deposition_e_mm=non_region_deposition_e,
        layer_count=len(seen_layer_indices),
        region_names=seen_region_names,
        warnings=warnings,
    )


def build_execution_plan(
    analysis: LayerRegionAnalysis,
    region_to_component_index: dict[str, int],
    enabled_component_indices: set[int],
) -> dict[str, object]:
    events: list[dict[str, object]] = []
    skipped_regions: dict[str, float] = {}
    for occurrence in analysis.occurrences:
        component_index = region_to_component_index.get(occurrence.region_name)
        if component_index is None or component_index not in enabled_component_indices:
            skipped_regions[occurrence.region_name] = (
                skipped_regions.get(occurrence.region_name, 0.0)
                + occurrence.extrusion_e_mm
            )
            continue
        event = occurrence.to_dict(include_segments=False)
        event["source_component_index"] = component_index
        events.append(event)

    for sequence_index, event in enumerate(events, start=1):
        event["execution_step_index"] = sequence_index

    return {
        "schema": "b_fdm.layer_region_execution_plan.v1",
        "resolution_mode": "layer_region_occurrence",
        "source_gcode": analysis.source_gcode,
        "event_count": len(events),
        "mapped_deposition_e_mm": sum(float(event["extrusion_e_mm"]) for event in events),
        "skipped_region_deposition_e_mm": skipped_regions,
        "non_region_deposition_e_mm": analysis.non_region_deposition_e_mm,
        "events": events,
    }


class LayerRegionPreviewDialog:
    def __init__(
        self,
        analysis: LayerRegionAnalysis,
        *,
        parent: Any = None,
        region_properties: dict[str, str] | None = None,
        region_display_colors: dict[str, str] | None = None,
        mapped_sequence_indices: set[int] | None = None,
    ):
        from PyQt5 import QtCore, QtGui, QtWidgets

        self.QtCore = QtCore
        self.QtGui = QtGui
        self.QtWidgets = QtWidgets
        self.analysis = analysis
        self.region_properties = region_properties or {}
        self.layer_indices = analysis.layer_indices()
        self.region_colors = {
            region_name: (region_display_colors or {}).get(
                region_name,
                REGION_COLORS[index % len(REGION_COLORS)],
            )
            for index, region_name in enumerate(analysis.region_names)
        }
        try:
            from .dm_spiral_mapping import build_spiral_mapping
        except ImportError:
            from dm_spiral_mapping import build_spiral_mapping

        self.spiral_mapping = build_spiral_mapping(
            analysis,
            included_sequence_indices=mapped_sequence_indices,
        )
        self.current_occurrences: list[LayerRegionOccurrence] = []
        self.object_highlight_actor = None
        self.spiral_highlight_actor = None
        self._vtk_finalized = False

        self.dialog = QtWidgets.QDialog(parent)
        self.dialog.setWindowTitle("Layer x Region Resolution Preview")
        self.dialog.resize(1220, 820)
        root = QtWidgets.QVBoxLayout(self.dialog)

        header = QtWidgets.QLabel(
            "Each colored block is one chronological Layer x Region occurrence. "
            "Only positive-E spatial deposition is shown."
        )
        header.setWordWrap(True)
        root.addWidget(header)

        navigation = QtWidgets.QHBoxLayout()
        self.layer_label = QtWidgets.QLabel("")
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.setMinimum(0)
        self.slider.setMaximum(max(0, len(self.layer_indices) - 1))
        self.slider.setValue(0)
        navigation.addWidget(QtWidgets.QLabel("Layer"))
        navigation.addWidget(self.slider, stretch=1)
        navigation.addWidget(self.layer_label)
        root.addLayout(navigation)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        root.addWidget(splitter, stretch=1)

        self.preview_tabs = QtWidgets.QTabWidget()
        splitter.addWidget(self.preview_tabs)

        self.scene = QtWidgets.QGraphicsScene()
        self.view = QtWidgets.QGraphicsView(self.scene)
        self.view.setRenderHint(QtGui.QPainter.Antialiasing, True)
        self.view.setBackgroundBrush(QtGui.QColor("#fafaf9"))
        self.view.setDragMode(QtWidgets.QGraphicsView.ScrollHandDrag)
        self.preview_tabs.addTab(self.view, "Layer 2D")

        self.three_d_container = QtWidgets.QWidget()
        three_d_layout = QtWidgets.QVBoxLayout(self.three_d_container)
        three_d_layout.setContentsMargins(0, 0, 0, 0)
        self.three_d_title = QtWidgets.QLabel(
            "3D target toolpath (left) ↔ predicted spiral DM filament (right). "
            "The spiral is shown as a 1.75 mm filament envelope using the same "
            "R=50 mm / pitch=2.15 mm geometry as Source_DM_filament. "
            "Manufacturing runs from the inner radius in reverse execution order; "
            "the finished filament is consumed from the outer end in execution order. "
            "Select a table row to highlight both locations."
        )
        self.three_d_title.setWordWrap(True)
        three_d_layout.addWidget(self.three_d_title)
        self._initialize_3d_mapping_view(three_d_layout)
        self.preview_tabs.addTab(self.three_d_container, "3D ↔ Spiral DM")

        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)
        self.summary = QtWidgets.QLabel("")
        self.summary.setWordWrap(True)
        right_layout.addWidget(self.summary)
        self.mapping_summary = QtWidgets.QLabel("")
        self.mapping_summary.setWordWrap(True)
        right_layout.addWidget(self.mapping_summary)
        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Color", "Region", "Occurrence", "E (mm)", "Segments", "Property"]
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        right_layout.addWidget(self.table, stretch=1)
        self.export_button = QtWidgets.QPushButton("Export Current Layer PNG")
        right_layout.addWidget(self.export_button)
        self.export_3d_button = QtWidgets.QPushButton("Export 3D ↔ Spiral PNG")
        right_layout.addWidget(self.export_3d_button)
        splitter.addWidget(right)
        splitter.setSizes([780, 420])

        self.slider.valueChanged.connect(self.render_current_layer)
        self.export_button.clicked.connect(self.export_current_layer)
        self.export_3d_button.clicked.connect(self.export_3d_mapping)
        self.table.itemSelectionChanged.connect(self.update_3d_selection)
        self.dialog.finished.connect(self._finalize_3d_mapping_view)
        application = QtWidgets.QApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(self._finalize_3d_mapping_view)
        self.render_current_layer()

    def _initialize_3d_mapping_view(self, layout: Any) -> None:
        from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor
        from vtkmodules.vtkRenderingCore import vtkRenderer

        self.vtk_widget = QVTKRenderWindowInteractor(self.three_d_container)
        layout.addWidget(self.vtk_widget, stretch=1)
        self.mapping_renderer = vtkRenderer()
        self.mapping_renderer.SetBackground(0.97, 0.98, 0.99)
        self.vtk_widget.GetRenderWindow().AddRenderer(self.mapping_renderer)

        object_points = [
            coordinate
            for occurrence in self.analysis.occurrences
            for segment in occurrence.segments
            for coordinate in (
                (segment.x0, segment.y0, segment.z0),
                (segment.x1, segment.y1, segment.z1),
            )
        ]
        if object_points:
            object_max_x = max(point[0] for point in object_points)
            object_mid_y = (
                min(point[1] for point in object_points)
                + max(point[1] for point in object_points)
            ) / 2.0
            object_min_z = min(point[2] for point in object_points)
        else:
            object_max_x, object_mid_y, object_min_z = 0.0, 0.0, 0.0

        self.spiral_offset = (
            object_max_x + self.spiral_mapping.outer_radius_mm + 30.0,
            object_mid_y,
            object_min_z,
        )
        object_polylines = []
        for occurrence in self.analysis.occurrences:
            color = self.region_colors.get(occurrence.region_name, "#64748b")
            for segment in occurrence.segments:
                object_polylines.append(
                    (
                        [
                            (segment.x0, segment.y0, segment.z0),
                            (segment.x1, segment.y1, segment.z1),
                        ],
                        color,
                    )
                )
        spiral_polylines = []
        for segment in self.spiral_mapping.segments:
            if segment.occurrence is None:
                color = "#94a3b8"
            else:
                color = self.region_colors.get(
                    segment.occurrence.region_name,
                    "#64748b",
                )
            spiral_polylines.append(
                (
                    [self._offset_spiral_point(point) for point in segment.points],
                    color,
                )
            )

        self.object_actor = self._make_colored_tube_actor(
            object_polylines,
            radius=0.16,
        )
        self.spiral_actor = self._make_colored_tube_actor(
            spiral_polylines,
            radius=0.875,
        )
        self.mapping_renderer.AddActor(self.object_actor)
        self.mapping_renderer.AddActor(self.spiral_actor)
        self.mapping_renderer.ResetCamera()
        camera = self.mapping_renderer.GetActiveCamera()
        camera.Elevation(35)
        camera.Azimuth(-25)
        self.mapping_renderer.ResetCameraClippingRange()
        self.vtk_widget.Initialize()
        self.vtk_widget.GetRenderWindow().Render()

    def _finalize_3d_mapping_view(self, *_args: Any) -> None:
        """Release the VTK OpenGL context before Qt destroys its native window."""
        if self._vtk_finalized:
            return
        self._vtk_finalized = True

        vtk_widget = getattr(self, "vtk_widget", None)
        if vtk_widget is None:
            return

        try:
            render_window = vtk_widget.GetRenderWindow()
            interactor = render_window.GetInteractor()
            if interactor is not None:
                interactor.Disable()

            renderer = getattr(self, "mapping_renderer", None)
            if renderer is not None:
                renderer.RemoveAllViewProps()
                render_window.RemoveRenderer(renderer)

            vtk_widget.Finalize()
        except RuntimeError:
            # Qt may already have removed the native window during application
            # shutdown. In that case there is no remaining VTK resource to use.
            pass

    def _offset_spiral_point(
        self,
        point: tuple[float, float, float],
    ) -> tuple[float, float, float]:
        return (
            point[0] + self.spiral_offset[0],
            point[1] + self.spiral_offset[1],
            point[2] + self.spiral_offset[2],
        )

    def _make_colored_tube_actor(
        self,
        polylines: list[tuple[list[tuple[float, float, float]], str]],
        *,
        radius: float,
    ) -> Any:
        from vtkmodules.vtkCommonCore import vtkPoints, vtkUnsignedCharArray
        from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData, vtkPolyLine
        from vtkmodules.vtkFiltersCore import vtkTubeFilter
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

        points = vtkPoints()
        lines = vtkCellArray()
        colors = vtkUnsignedCharArray()
        colors.SetName("segment_colors")
        colors.SetNumberOfComponents(3)

        for coordinates, color_hex in polylines:
            if len(coordinates) < 2:
                continue
            polyline = vtkPolyLine()
            polyline.GetPointIds().SetNumberOfIds(len(coordinates))
            for point_index, coordinate in enumerate(coordinates):
                vtk_id = points.InsertNextPoint(*coordinate)
                polyline.GetPointIds().SetId(point_index, vtk_id)
            lines.InsertNextCell(polyline)
            color = self.QtGui.QColor(color_hex)
            colors.InsertNextTuple3(color.red(), color.green(), color.blue())

        polydata = vtkPolyData()
        polydata.SetPoints(points)
        polydata.SetLines(lines)
        polydata.GetCellData().SetScalars(colors)
        tube = vtkTubeFilter()
        tube.SetInputData(polydata)
        tube.SetRadius(radius)
        tube.SetNumberOfSides(8)
        tube.CappingOn()
        mapper = vtkPolyDataMapper()
        mapper.SetInputConnection(tube.GetOutputPort())
        mapper.SetScalarModeToUseCellData()
        mapper.ScalarVisibilityOn()
        actor = vtkActor()
        actor.SetMapper(mapper)
        return actor

    def _make_highlight_actor(
        self,
        polylines: list[list[tuple[float, float, float]]],
        *,
        radius: float,
    ) -> Any:
        return self._make_colored_tube_actor(
            [(points, "#facc15") for points in polylines],
            radius=radius,
        )

    def show(self) -> None:
        self.dialog.show()

    def exec_(self) -> int:
        return self.dialog.exec_()

    def render_current_layer(self) -> None:
        if not self.layer_indices:
            self.layer_label.setText("No labeled layers")
            self.summary.setText("\n".join(self.analysis.warnings))
            return

        layer_index = self.layer_indices[self.slider.value()]
        occurrences = self.analysis.occurrences_for_layer(layer_index)
        self.current_occurrences = occurrences
        layer_label = occurrences[0].layer_label if occurrences else layer_index
        layer_z = occurrences[0].layer_z if occurrences else None
        self.layer_label.setText(
            f"{layer_label}  |  Z={layer_z:.5g}" if layer_z is not None else str(layer_label)
        )
        self.scene.clear()
        self.table.setRowCount(len(occurrences))

        for row, occurrence in enumerate(occurrences):
            color_hex = self.region_colors.get(occurrence.region_name, "#64748b")
            color = self.QtGui.QColor(color_hex)
            pen = self.QtGui.QPen(color)
            pen.setWidthF(1.8)
            for segment in occurrence.segments:
                item = self.scene.addLine(
                    segment.x0,
                    -segment.y0,
                    segment.x1,
                    -segment.y1,
                    pen,
                )
                item.setToolTip(
                    f"{occurrence.region_name} | occurrence {occurrence.occurrence_index} | "
                    f"E={segment.e_delta:.6f} | line {segment.source_line}"
                )

            swatch = self.QtWidgets.QTableWidgetItem("")
            swatch.setBackground(color)
            self.table.setItem(row, 0, swatch)
            self.table.setItem(row, 1, self.QtWidgets.QTableWidgetItem(occurrence.region_name))
            self.table.setItem(
                row,
                2,
                self.QtWidgets.QTableWidgetItem(str(occurrence.occurrence_index)),
            )
            self.table.setItem(
                row,
                3,
                self.QtWidgets.QTableWidgetItem(f"{occurrence.extrusion_e_mm:.6f}"),
            )
            self.table.setItem(
                row,
                4,
                self.QtWidgets.QTableWidgetItem(str(occurrence.segment_count)),
            )
            self.table.setItem(
                row,
                5,
                self.QtWidgets.QTableWidgetItem(
                    self.region_properties.get(occurrence.region_name, "")
                ),
            )

        bounds = self.scene.itemsBoundingRect()
        if not bounds.isNull():
            margin = max(bounds.width(), bounds.height(), 1.0) * 0.05
            self.scene.setSceneRect(bounds.adjusted(-margin, -margin, margin, margin))
            self.view.fitInView(self.scene.sceneRect(), self.QtCore.Qt.KeepAspectRatio)

        layer_e = sum(occurrence.extrusion_e_mm for occurrence in occurrences)
        warning_text = (
            "\nWarning: " + " ".join(self.analysis.warnings)
            if self.analysis.warnings
            else ""
        )
        self.summary.setText(
            f"Layer occurrences: {len(occurrences)}\n"
            f"Layer region deposition: {layer_e:.6f} mm\n"
            f"Total analyzed occurrences: {len(self.analysis.occurrences)}\n"
            f"Region coverage: "
            f"{(100.0 * self.analysis.region_deposition_e_mm / self.analysis.total_deposition_e_mm) if self.analysis.total_deposition_e_mm else 0.0:.2f}%"
            f"{warning_text}"
        )
        self.table.resizeColumnsToContents()
        if occurrences:
            self.table.selectRow(0)

    def update_3d_selection(self) -> None:
        if self._vtk_finalized:
            return
        selected_rows = self.table.selectionModel().selectedRows()
        if not selected_rows:
            return
        row = selected_rows[0].row()
        if row < 0 or row >= len(self.current_occurrences):
            return
        occurrence = self.current_occurrences[row]
        mapping_segment = self.spiral_mapping.occurrence_segment(
            occurrence.sequence_index
        )
        if mapping_segment is None:
            self.mapping_summary.setText(
                f"Layer {occurrence.layer_label}, region {occurrence.region_name} is "
                "disabled or excluded from the DM filament, so it has no spiral interval."
            )
            return

        if self.object_highlight_actor is not None:
            self.mapping_renderer.RemoveActor(self.object_highlight_actor)
        if self.spiral_highlight_actor is not None:
            self.mapping_renderer.RemoveActor(self.spiral_highlight_actor)

        self.object_highlight_actor = self._make_highlight_actor(
            [
                [
                    (segment.x0, segment.y0, segment.z0),
                    (segment.x1, segment.y1, segment.z1),
                ]
                for segment in occurrence.segments
            ],
            radius=0.28,
        )
        self.spiral_highlight_actor = self._make_highlight_actor(
            [
                [
                    self._offset_spiral_point(point)
                    for point in mapping_segment.points
                ]
            ],
            radius=1.15,
        )
        self.mapping_renderer.AddActor(self.object_highlight_actor)
        self.mapping_renderer.AddActor(self.spiral_highlight_actor)
        self.mapping_renderer.ResetCameraClippingRange()
        self.vtk_widget.GetRenderWindow().Render()

        self.mapping_summary.setText(
            f"Selected execution step: {occurrence.sequence_index}\n"
            f"Layer {occurrence.layer_label}, region {occurrence.region_name}, "
            f"occurrence {occurrence.occurrence_index}\n"
            f"Object consumption: {occurrence.extrusion_e_mm:.6f} mm\n"
            f"Physical spiral interval: "
            f"{mapping_segment.filament_start_mm:.3f} → "
            f"{mapping_segment.filament_end_mm:.3f} mm\n"
            f"Spiral radius interval: "
            f"{math.hypot(*mapping_segment.points[0][:2]):.3f} → "
            f"{math.hypot(*mapping_segment.points[-1][:2]):.3f} mm\n"
            f"Manufacturing direction: inner → outer, reverse execution order\n"
            f"Final-print consumption: outer → inner, execution order\n"
            f"Predicted spiral: inner R={self.spiral_mapping.inner_radius_mm:.2f} mm, "
            f"outer R={self.spiral_mapping.outer_radius_mm:.2f} mm, "
            f"pitch={self.spiral_mapping.pitch_mm:.2f} mm\n"
            f"Mapped object={self.spiral_mapping.mapped_length_mm:.3f} mm, "
            f"with purge/feed={self.spiral_mapping.total_length_with_feed_mm:.3f} mm"
        )

    def export_current_layer(self) -> None:
        if not self.layer_indices:
            return
        default_name = f"layer_{self.layer_indices[self.slider.value()]:04d}_regions.png"
        output_path, _selected_filter = self.QtWidgets.QFileDialog.getSaveFileName(
            self.dialog,
            "Export Layer x Region Preview",
            default_name,
            "PNG image (*.png)",
        )
        if not output_path:
            return
        image = self.QtGui.QImage(
            max(1, self.view.viewport().width()),
            max(1, self.view.viewport().height()),
            self.QtGui.QImage.Format_ARGB32,
        )
        image.fill(self.QtGui.QColor("#fafaf9"))
        painter = self.QtGui.QPainter(image)
        self.view.render(painter)
        painter.end()
        image.save(output_path)

    def export_3d_mapping(self) -> None:
        if self._vtk_finalized:
            return
        output_path, _selected_filter = self.QtWidgets.QFileDialog.getSaveFileName(
            self.dialog,
            "Export 3D Object to Spiral Mapping",
            "object_to_spiral_dm_mapping.png",
            "PNG image (*.png)",
        )
        if not output_path:
            return
        self.vtk_widget.grab().save(output_path)


def show_layer_region_preview(
    analysis: LayerRegionAnalysis,
    *,
    parent: Any = None,
    region_properties: dict[str, str] | None = None,
    region_display_colors: dict[str, str] | None = None,
    mapped_sequence_indices: set[int] | None = None,
) -> LayerRegionPreviewDialog:
    dialog = LayerRegionPreviewDialog(
        analysis,
        parent=parent,
        region_properties=region_properties,
        region_display_colors=region_display_colors,
        mapped_sequence_indices=mapped_sequence_indices,
    )
    dialog.show()
    return dialog
