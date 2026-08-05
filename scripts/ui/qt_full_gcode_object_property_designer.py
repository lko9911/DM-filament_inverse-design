from __future__ import annotations

import argparse
import json
import os
import sys
from pprint import pformat
from pathlib import Path

try:
    from PyQt5 import QtCore, QtGui, QtWidgets
    from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor
    from vtkmodules.vtkCommonColor import vtkNamedColors
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
    from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTrackballCamera
    from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper, vtkRenderer
    from vtkmodules.vtkFiltersCore import vtkTubeFilter

    import vtkmodules.vtkInteractionStyle  # noqa: F401
    import vtkmodules.vtkRenderingFreeType  # noqa: F401
    import vtkmodules.vtkRenderingOpenGL2  # noqa: F401
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Qt property designer requires PyQt5 and vtkmodules.\n"
        f"Missing module: {exc.name}\n"
        f"Python executable: {sys.executable}\n"
        "Install the missing package in this environment, or launch through all_in_one_pipeline_ui.py "
        "so it can auto-select a Python environment with Qt dependencies."
    ) from exc

try:
    from .component_property_designer import (
        ASSIGNMENT_MODE_OPTIONS,
        DEFAULT_OUTPUT_PATH,
        COLOR_OPTIONS,
        GRADIENT_DIRECTION_OPTIONS,
        NO_MATERIAL,
        PROPERTY_TARGET_OPTIONS,
        PROPERTY_TYPE_OPTIONS,
        apply_color_label_default_order,
        build_property_payload,
        component_voxel_count,
        default_state,
        normalize_gradient_direction,
    )
    from .full_gcode_object_property_designer import (
        canonical_component_name,
        parse_full_gcode_objects,
        write_reordered_full_gcode,
    )
    from .layer_region_analysis import (
        analyze_layer_regions,
        build_execution_plan,
        show_layer_region_preview,
    )
except ImportError:
    from component_property_designer import (
        ASSIGNMENT_MODE_OPTIONS,
        DEFAULT_OUTPUT_PATH,
        COLOR_OPTIONS,
        GRADIENT_DIRECTION_OPTIONS,
        NO_MATERIAL,
        PROPERTY_TARGET_OPTIONS,
        PROPERTY_TYPE_OPTIONS,
        apply_color_label_default_order,
        build_property_payload,
        component_voxel_count,
        default_state,
        normalize_gradient_direction,
    )
    from full_gcode_object_property_designer import (
        canonical_component_name,
        parse_full_gcode_objects,
        write_reordered_full_gcode,
    )
    from layer_region_analysis import (
        analyze_layer_regions,
        build_execution_plan,
        show_layer_region_preview,
    )

from scripts.utils.property_excel_lookup import (
    color_profile_swatch,
    normalize_color_profile_key,
)


INPUT_GCODE = r"Sample_compenent\origami_gripper - base^origami_gripper-2_0.15mm_ABS_MK3S_55m.gcode"
OUTPUT_JSON = r"input\config\Property_sample.json"
VOXEL_THRESHOLD_E = 2.0
REORDER_GCODE_STRATEGY_ENV_KEY = "B_FDM_REORDERED_GCODE_STRATEGY"
BRIGHTER_MODE_ENV_KEY = "B_FDM_BRIGHTER_MODE"
REGION_RECOGNITION_MODE_ENV_KEY = "B_FDM_REGION_RECOGNITION_MODE"
DEFAULT_REORDER_GCODE_STRATEGY = "reorder_mesh_occurrences_within_each_layer_keep_xyz"

PREVIEW_COLORS = [
    (0.15, 0.39, 0.92),
    (0.86, 0.15, 0.15),
    (0.09, 0.64, 0.29),
    (0.58, 0.20, 0.92),
    (0.92, 0.35, 0.07),
]


def build_layer_based_step_lengths(component, step_count: int) -> list[float]:
    if step_count <= 0:
        return []

    layer_totals: dict[float, float] = {}
    for segment in component.segments:
        layer_key = round(float(segment.z1), 5)
        layer_totals[layer_key] = layer_totals.get(layer_key, 0.0) + float(segment.e_delta)

    ordered_layers = sorted(layer_totals.items(), key=lambda item: item[0])
    if not ordered_layers:
        return [0.0] * step_count

    layer_values = [value for _z, value in ordered_layers]
    layer_count = len(layer_values)
    step_lengths: list[float] = []
    for step_index in range(step_count):
        seg_start = (step_index * layer_count) // step_count
        seg_end = ((step_index + 1) * layer_count) // step_count
        step_lengths.append(sum(layer_values[seg_start:seg_end]))
    return step_lengths


def make_vtk_polydata(component):
    points = vtkPoints()
    lines = vtkCellArray()
    for segment in component.segments:
        start_id = points.InsertNextPoint(segment.x0, segment.y0, segment.z0)
        end_id = points.InsertNextPoint(segment.x1, segment.y1, segment.z1)
        lines.InsertNextCell(2)
        lines.InsertCellPoint(start_id)
        lines.InsertCellPoint(end_id)
    polydata = vtkPolyData()
    polydata.SetPoints(points)
    polydata.SetLines(lines)
    return polydata


def make_tube_actor(component, active: bool = True):
    polydata = make_vtk_polydata(component)
    bounds = polydata.GetBounds()
    if bounds is None:
        radius = 0.05
    else:
        span = max(
            abs(bounds[1] - bounds[0]),
            abs(bounds[3] - bounds[2]),
            abs(bounds[5] - bounds[4]),
            1.0,
        )
        radius = max(0.03, span * 0.0025)

    tube = vtkTubeFilter()
    tube.SetInputData(polydata)
    tube.SetRadius(radius)
    tube.SetNumberOfSides(10)
    tube.CappingOn()
    tube.Update()

    mapper = vtkPolyDataMapper()
    mapper.SetInputConnection(tube.GetOutputPort())

    actor = vtkActor()
    actor.SetMapper(mapper)
    actor.GetProperty().SetColor(PREVIEW_COLORS[(component.index - 1) % len(PREVIEW_COLORS)])
    actor.GetProperty().SetOpacity(1.0 if active else 0.58)
    actor.GetProperty().SetSpecular(0.25)
    actor.GetProperty().SetSpecularPower(12)
    return actor


class QtFullGcodeObjectDesigner(QtWidgets.QMainWindow):
    def __init__(self, components, output_path: Path, voxel_threshold_e: float):
        super().__init__()
        self.components = components
        self.output_path = output_path
        self.voxel_threshold_e = voxel_threshold_e
        self.states = {component.index: default_state(component) for component in components}
        apply_color_label_default_order(self.components, self.states)
        configured_recognition_mode = os.environ.get(
            REGION_RECOGNITION_MODE_ENV_KEY
        )
        self.region_recognition_mode_from_env = configured_recognition_mode is not None
        self.region_recognition_mode = (
            "z-axis"
            if str(configured_recognition_mode).strip().lower() == "z-axis"
            else "layer-region"
        )
        self.restore_states_from_existing_output()
        self.active_index = components[0].index
        self.syncing = False
        self.layer_region_dialogs = []

        self.setWindowTitle("b-FDM G-code Component Property Designer")
        self.resize(1500, 900)
        self.apply_style()

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QHBoxLayout(central)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(14)

        self.vtk_widget = QVTKRenderWindowInteractor(central)
        layout.addWidget(self.vtk_widget, stretch=3)

        self.renderer = vtkRenderer()
        self.renderer.SetBackground(0.98, 0.98, 0.96)
        self.vtk_widget.GetRenderWindow().AddRenderer(self.renderer)
        self.interactor = self.vtk_widget.GetRenderWindow().GetInteractor()
        self.interactor.SetInteractorStyle(vtkInteractorStyleTrackballCamera())

        controls = QtWidgets.QWidget()
        controls.setMinimumWidth(420)
        controls_layout = QtWidgets.QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(10)
        layout.addWidget(controls, stretch=1)

        title = QtWidgets.QLabel("Component Property")
        title.setObjectName("titleLabel")
        controls_layout.addWidget(title)

        hint = QtWidgets.QLabel("Check rows to view together. Use in DM controls whether the active object is saved to JSON.")
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        controls_layout.addWidget(hint)

        self.component_list = QtWidgets.QListWidget()
        self.component_list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.component_list.setAlternatingRowColors(True)
        self.component_list.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.component_list.setDefaultDropAction(QtCore.Qt.MoveAction)
        for component in components:
            item = QtWidgets.QListWidgetItem("")
            item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.Checked if component.index == self.active_index else QtCore.Qt.Unchecked)
            item.setData(QtCore.Qt.UserRole, component.index)
            self.component_list.addItem(item)
        self.component_list.setCurrentRow(0)
        self.component_list.item(0).setSelected(True)
        controls_layout.addWidget(self.component_list, stretch=1)

        order_button_row = QtWidgets.QHBoxLayout()
        self.move_up_button = QtWidgets.QPushButton("Move Up")
        self.move_down_button = QtWidgets.QPushButton("Move Down")
        order_button_row.addWidget(self.move_up_button)
        order_button_row.addWidget(self.move_down_button)
        controls_layout.addLayout(order_button_row)

        editor_group = QtWidgets.QGroupBox("Active Object Settings")
        editor_layout = QtWidgets.QVBoxLayout(editor_group)
        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignLeft)
        editor_layout.addLayout(form)
        controls_layout.addWidget(editor_group)
        self.form_rows = {}

        def add_form_row(key: str, label_text: str, widget) -> None:
            label = QtWidgets.QLabel(label_text)
            form.addRow(label, widget)
            self.form_rows[key] = (label, widget)

        self.order_spin = QtWidgets.QSpinBox()
        self.order_spin.setRange(1, 99)
        add_form_row("order", "Order", self.order_spin)

        self.enabled_checkbox = QtWidgets.QCheckBox("Use in DM")
        self.enabled_checkbox.setToolTip("Include this object in the Property JSON, ordered G-code, length matrix, and DM filament.")
        add_form_row("enabled", "Output", self.enabled_checkbox)

        self.property_type_combo = QtWidgets.QComboBox()
        self.property_type_combo.addItems(PROPERTY_TYPE_OPTIONS)
        add_form_row("property_type", "Property Type", self.property_type_combo)

        self.assignment_mode_combo = QtWidgets.QComboBox()
        self.assignment_mode_combo.addItems(ASSIGNMENT_MODE_OPTIONS)
        add_form_row("assignment_mode", "Design Mode", self.assignment_mode_combo)

        self.required_property_type_combo = QtWidgets.QComboBox()
        self.required_property_type_combo.addItems(PROPERTY_TARGET_OPTIONS)
        add_form_row("required_property_type", "Target Property", self.required_property_type_combo)

        def make_large_double_spin() -> QtWidgets.QDoubleSpinBox:
            widget = QtWidgets.QDoubleSpinBox()
            widget.setRange(-1.0, 999999999.0)
            widget.setDecimals(4)
            widget.setSpecialValueText("")
            return widget

        self.target_eb_spin = make_large_double_spin()
        add_form_row("target_Eb_MPa", "Target Eb (MPa)", self.target_eb_spin)

        self.min_elongation_spin = make_large_double_spin()
        add_form_row("min_elongation_percent", "Min Elongation (%)", self.min_elongation_spin)

        self.target_elongation_spin = make_large_double_spin()
        add_form_row("target_elongation_percent", "Target Elongation (%)", self.target_elongation_spin)

        self.max_r0_spin = make_large_double_spin()
        add_form_row("max_R0_ohm", "Max R0 (ohm)", self.max_r0_spin)

        self.target_r0_spin = make_large_double_spin()
        add_form_row("target_R0_ohm", "Target R0 (ohm)", self.target_r0_spin)

        self.min_gf_spin = make_large_double_spin()
        add_form_row("min_GF", "Min GF", self.min_gf_spin)

        self.target_gf_spin = make_large_double_spin()
        add_form_row("target_GF", "Target GF", self.target_gf_spin)

        self.gradient_property_combo = QtWidgets.QComboBox()
        self.gradient_property_combo.addItems(PROPERTY_TARGET_OPTIONS)
        add_form_row("gradient_property", "Gradient Property", self.gradient_property_combo)

        self.gradient_start_value_spin = make_large_double_spin()
        add_form_row("gradient_start_value", "Gradient Start", self.gradient_start_value_spin)

        self.gradient_end_value_spin = make_large_double_spin()
        add_form_row("gradient_end_value", "Gradient End", self.gradient_end_value_spin)

        self.material_start_combo = QtWidgets.QComboBox()
        self.material_start_combo.addItems(COLOR_OPTIONS)
        for state in self.states.values():
            material_start = str(state.get("material_start", "")).strip()
            if material_start and self.material_start_combo.findText(material_start) < 0:
                self.material_start_combo.addItem(material_start)
        for color_index, color_key in enumerate(COLOR_OPTIONS):
            swatch = QtGui.QColor(color_profile_swatch(color_key))
            self.material_start_combo.setItemData(color_index, swatch, QtCore.Qt.DecorationRole)
            self.material_start_combo.setItemData(
                color_index,
                f"{color_key} | {color_profile_swatch(color_key)}",
                QtCore.Qt.ToolTipRole,
            )
        add_form_row("material_start", "Color", self.material_start_combo)

        self.property_mpa_spin = QtWidgets.QDoubleSpinBox()
        self.property_mpa_spin.setRange(0.0, 99999.0)
        self.property_mpa_spin.setDecimals(3)
        add_form_row("property_mpa", "MPa", self.property_mpa_spin)

        self.property_gf_spin = QtWidgets.QDoubleSpinBox()
        self.property_gf_spin.setRange(0.0, 99999.0)
        self.property_gf_spin.setDecimals(3)
        add_form_row("property_gf", "GF", self.property_gf_spin)

        self.material_end_combo = QtWidgets.QComboBox()
        self.material_end_combo.addItems(COLOR_OPTIONS)
        add_form_row("material_end", "Material End", self.material_end_combo)

        self.ratio_start_spin = QtWidgets.QDoubleSpinBox()
        self.ratio_start_spin.setRange(0.0, 100.0)
        self.ratio_start_spin.setDecimals(2)
        add_form_row("ratio_start", "Start Ratio (%)", self.ratio_start_spin)

        self.ratio_end_spin = QtWidgets.QDoubleSpinBox()
        self.ratio_end_spin.setRange(0.0, 100.0)
        self.ratio_end_spin.setDecimals(2)
        add_form_row("ratio_end", "End Ratio (%)", self.ratio_end_spin)

        self.gradient_steps_spin = QtWidgets.QSpinBox()
        self.gradient_steps_spin.setRange(1, 99)
        add_form_row("gradient_steps", "Gradient Steps", self.gradient_steps_spin)

        self.gradient_direction_combo = QtWidgets.QComboBox()
        self.gradient_direction_combo.addItems(GRADIENT_DIRECTION_OPTIONS)
        add_form_row("gradient_direction", "Gradient Direction", self.gradient_direction_combo)

        self.eta_mode_combo = QtWidgets.QComboBox()
        self.eta_mode_combo.addItem("Auto (mixed = 2)", "auto")
        self.eta_mode_combo.addItem("Manual", "manual")
        self.eta_mode_combo.setToolTip(
            "Auto uses eta 0 for one material and eta 2 for a mixed-material color. "
            "Brighter adds a 50% WHITE shell, so physical eta becomes 2 for a pure color "
            "and 4 for a mixed color. "
            "Manual uses the Eta value below independently of the color recipe."
        )
        add_form_row("eta_mode", "Eta Mode", self.eta_mode_combo)

        self.eta_spin = QtWidgets.QDoubleSpinBox()
        self.eta_spin.setRange(0.0, 999.0)
        self.eta_spin.setDecimals(3)
        add_form_row("eta", "Eta (manual)", self.eta_spin)

        self.property_start_combo = QtWidgets.QComboBox()
        add_form_row("property_start", "Gradient Start Property", self.property_start_combo)

        self.property_end_combo = QtWidgets.QComboBox()
        add_form_row("property_end", "Gradient End Property", self.property_end_combo)

        self.brighter_checkbox = QtWidgets.QCheckBox("Brighter")
        self.brighter_checkbox.setToolTip("Use fixed WHITE rows at the outside layers during candidate generation.")
        self.brighter_checkbox.setChecked(
            os.environ.get(BRIGHTER_MODE_ENV_KEY, "").strip().lower() in {"1", "true", "yes", "on", "brighter"}
        )
        add_form_row("brighter", "Bright Color", self.brighter_checkbox)

        action_row = QtWidgets.QHBoxLayout()
        self.fit_button = QtWidgets.QPushButton("Fit View")
        self.view_checked_button = QtWidgets.QPushButton("Fit Checked")
        self.layer_region_button = QtWidgets.QPushButton("Preview Layer x Region")
        action_row.addWidget(self.fit_button)
        action_row.addWidget(self.view_checked_button)
        action_row.addWidget(self.layer_region_button)
        controls_layout.addLayout(action_row)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setWordWrap(True)
        controls_layout.addWidget(self.status_label)

        self.save_button = QtWidgets.QPushButton("Save Property + G-code")
        self.save_button.setObjectName("saveButton")
        controls_layout.addWidget(self.save_button)

        self.component_list.currentItemChanged.connect(self.on_component_changed)
        self.component_list.itemSelectionChanged.connect(self.update_preview)
        self.component_list.itemChanged.connect(self.on_component_checked)
        self.component_list.model().rowsMoved.connect(self.on_component_rows_moved)
        self.move_up_button.clicked.connect(lambda: self.move_active_component(-1))
        self.move_down_button.clicked.connect(lambda: self.move_active_component(1))
        self.fit_button.clicked.connect(self.fit_view)
        self.view_checked_button.clicked.connect(self.view_checked_components)
        self.layer_region_button.clicked.connect(self.preview_layer_regions)
        self.save_button.clicked.connect(self.save_json)
        for widget in [
            self.order_spin,
            self.target_eb_spin,
            self.min_elongation_spin,
            self.target_elongation_spin,
            self.max_r0_spin,
            self.target_r0_spin,
            self.min_gf_spin,
            self.target_gf_spin,
            self.gradient_start_value_spin,
            self.gradient_end_value_spin,
            self.ratio_start_spin,
            self.ratio_end_spin,
            self.gradient_steps_spin,
            self.eta_spin,
            self.property_mpa_spin,
            self.property_gf_spin,
        ]:
            widget.valueChanged.connect(self.on_controls_changed)
        self.enabled_checkbox.stateChanged.connect(self.on_controls_changed)
        self.brighter_checkbox.stateChanged.connect(self.on_controls_changed)
        for widget in [
            self.property_type_combo,
            self.assignment_mode_combo,
            self.required_property_type_combo,
            self.gradient_property_combo,
            self.material_start_combo,
            self.material_end_combo,
            self.gradient_direction_combo,
            self.eta_mode_combo,
            self.property_start_combo,
            self.property_end_combo,
        ]:
            widget.currentTextChanged.connect(self.on_controls_changed)

        self.refresh_property_reference_controls()
        self.refresh_component_labels()
        self.sync_component_list_order()
        self.load_active_state()
        self.interactor.Initialize()
        QtCore.QTimer.singleShot(0, self.update_preview)

    def restore_states_from_existing_output(self) -> None:
        if not self.output_path.exists():
            return
        try:
            payload = json.loads(self.output_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not self.region_recognition_mode_from_env:
            recognition_mode = str(
                payload.get("region_recognition_mode", "layer-region")
            ).strip().lower()
            self.region_recognition_mode = (
                "z-axis" if recognition_mode == "z-axis" else "layer-region"
            )

        components_by_name = {
            component.display_name or component.path.name: component
            for component in self.components
        }
        components_by_index = {component.index: component for component in self.components}
        restored_count = 0
        for assignment in payload.get("assignments", []):
            component = components_by_name.get(str(assignment.get("source_component_name", "")))
            if component is None:
                try:
                    source_component_index = int(assignment.get("source_component_index", -1))
                except (TypeError, ValueError):
                    source_component_index = -1
                component = components_by_index.get(source_component_index)
            if component is None:
                continue

            state = self.states[component.index]
            state["enabled"] = True
            property_type = str(assignment.get("Property_type", state.get("property_type", "Property")))
            state["property_type"] = property_type
            state["gradient_steps"] = int(assignment.get("gradient_steps", state.get("gradient_steps", 1)))
            state["gradient_direction"] = normalize_gradient_direction(
                assignment.get(
                    "gradient_direction",
                    state.get("gradient_direction", "printing"),
                )
            )
            state["eta"] = float(assignment.get("eta", state.get("eta", 0.0)))
            state["eta_mode"] = str(assignment.get("eta_mode", state.get("eta_mode", "auto")))
            state["property_mpa"] = float(assignment.get("target_mpa", state.get("property_mpa", 0.0)))
            state["property_gf"] = float(assignment.get("target_gf", state.get("property_gf", 0.0)))
            state["brighter_mode"] = bool(assignment.get("brighter_mode", state.get("brighter_mode", False)))

            requested_color = assignment.get("requested_color")
            if requested_color:
                state["material_start"] = normalize_color_profile_key(requested_color)
            elif assignment.get("material_start"):
                state["material_start"] = normalize_color_profile_key(
                    assignment.get("material_start", state.get("material_start"))
                )
            if assignment.get("material_end"):
                state["material_end"] = str(assignment.get("material_end"))
            state["material_start_ratio"] = float(assignment.get("material_start_ratio", state.get("material_start_ratio", 100.0)))
            state["material_end_ratio"] = float(assignment.get("material_end_ratio", state.get("material_end_ratio", 0.0)))
            if "Property_start" in assignment:
                state["property_start"] = int(assignment["Property_start"])
            if "Property_end" in assignment:
                state["property_end"] = int(assignment["Property_end"])
            restored_count += 1

        if "brighter_mode" in payload and not any("brighter_mode" in item for item in payload.get("assignments", [])):
            default_brighter = bool(payload.get("brighter_mode"))
            for state in self.states.values():
                state["brighter_mode"] = default_brighter

        if restored_count:
            apply_color_label_default_order(self.components, self.states)

        disabled_indices = payload.get("disabled_source_component_indices", [])
        if isinstance(disabled_indices, list):
            for raw_index in disabled_indices:
                try:
                    component_index = int(raw_index)
                except (TypeError, ValueError):
                    continue
                if component_index in self.states:
                    self.states[component_index]["enabled"] = False

    def apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #f3f4f6;
                color: #111827;
                font-family: Segoe UI, Arial;
                font-size: 10pt;
            }
            QLabel#titleLabel {
                font-size: 20px;
                font-weight: 700;
                color: #0f172a;
            }
            QLabel#hintLabel {
                color: #64748b;
                font-size: 9pt;
            }
            QListWidget {
                background: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
                padding: 6px;
            }
            QListWidget::item {
                padding: 7px;
                border-radius: 5px;
            }
            QListWidget::item:selected {
                background: #dbeafe;
                color: #0f172a;
            }
            QGroupBox {
                background: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
                margin-top: 12px;
                padding: 12px 10px 10px 10px;
                font-weight: 700;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 5px;
            }
            QComboBox, QSpinBox, QDoubleSpinBox {
                background: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                min-height: 26px;
                padding: 2px 6px;
            }
            QCheckBox {
                background: transparent;
                spacing: 6px;
                padding: 0;
            }
            QCheckBox:hover {
                background: transparent;
            }
            QCheckBox::indicator {
                width: 14px;
                height: 14px;
                border: none;
                background: #e2e8f0;
            }
            QCheckBox::indicator:checked {
                background: #2563eb;
            }
            QPushButton {
                background: #e2e8f0;
                border: 1px solid #cbd5e1;
                border-radius: 7px;
                padding: 8px 10px;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #dbeafe;
                border-color: #93c5fd;
            }
            QPushButton#saveButton {
                background: #2563eb;
                color: white;
                border-color: #1d4ed8;
            }
            QPushButton#saveButton:hover {
                background: #1d4ed8;
            }
            QLabel#statusLabel {
                background: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
                padding: 10px;
                color: #334155;
            }
            """
        )

    def active_component(self):
        return next(component for component in self.components if component.index == self.active_index)

    def ordered_components(self):
        return sorted(
            self.components,
            key=lambda item: (int(self.states[item.index]["order"]), item.index),
        )

    def output_components(self):
        return [
            component
            for component in self.ordered_components()
            if bool(self.states[component.index].get("enabled", True))
        ]

    def component_list_order_indices(self) -> list[int]:
        return [
            int(self.component_list.item(row).data(QtCore.Qt.UserRole))
            for row in range(self.component_list.count())
        ]

    def apply_component_list_order(self) -> None:
        for order, component_index in enumerate(self.component_list_order_indices(), start=1):
            self.states[component_index]["order"] = order

    def output_index_by_component(self) -> dict[int, int]:
        return {
            component.index: output_index
            for output_index, component in enumerate(self.output_components(), start=1)
        }

    def property_reference_label(self, component) -> str:
        output_index = self.output_index_by_component().get(component.index, component.index)
        name = component.display_name or component.path.name
        return f"P{output_index} / C{component.index}: {name}"

    def property_reference_components(self):
        property_components = [
            component
            for component in self.output_components()
            if str(self.states[component.index].get("property_type", "Property")) == "Property"
        ]
        return property_components or self.output_components() or self.ordered_components()

    def sync_component_list_order(self) -> None:
        selected_indices = {item.index for item in self.selected_components()}
        checked_indices = self.checked_component_indices()
        current_index = self.active_index

        self.component_list.blockSignals(True)
        items_by_index = {}
        while self.component_list.count():
            item = self.component_list.takeItem(0)
            items_by_index[int(item.data(QtCore.Qt.UserRole))] = item

        for row, component in enumerate(self.ordered_components()):
            item = items_by_index[component.index]
            self.component_list.addItem(item)
            item.setCheckState(QtCore.Qt.Checked if component.index in checked_indices else QtCore.Qt.Unchecked)
            item.setSelected(component.index in selected_indices)
            if component.index == current_index:
                self.component_list.setCurrentRow(row)

        self.component_list.blockSignals(False)

    def refresh_component_labels(self) -> None:
        output_index_by_component = self.output_index_by_component()
        for row in range(self.component_list.count()):
            item = self.component_list.item(row)
            component_index = int(item.data(QtCore.Qt.UserRole))
            component = next(component for component in self.components if component.index == component_index)
            output_index = output_index_by_component.get(component.index, component.index)
            property_type = str(self.states[component.index].get("property_type", "Property"))
            assignment_mode = str(self.states[component.index].get("assignment_mode", "manual"))
            use_label = "DM" if bool(self.states[component.index].get("enabled", True)) else "CUT"
            prefix = f"P{output_index}" if bool(self.states[component.index].get("enabled", True)) else "OFF"
            item.setText(
                f"{prefix}  |  C{component.index}  |  {use_label}  |  {property_type} / {assignment_mode}  |  "
                f"{component.display_name or component.path.name}"
            )

    def on_component_rows_moved(self, *_args) -> None:
        if self.syncing:
            return
        self.apply_component_list_order()
        self.load_active_state()
        self.refresh_property_reference_controls()
        self.refresh_component_labels()
        self.update_status()

    def move_active_component(self, direction: int) -> None:
        current_row = self.component_list.currentRow()
        target_row = current_row + int(direction)
        if current_row < 0 or target_row < 0 or target_row >= self.component_list.count():
            return
        item = self.component_list.takeItem(current_row)
        self.component_list.insertItem(target_row, item)
        self.component_list.setCurrentItem(item)
        item.setSelected(True)
        self.apply_component_list_order()
        self.load_active_state()
        self.refresh_property_reference_controls()
        self.refresh_component_labels()
        self.update_status()

    def refresh_property_reference_controls(self) -> None:
        previous_start = int(self.states[self.active_index].get("property_start", 1))
        previous_end = int(self.states[self.active_index].get("property_end", 1))
        previous_syncing = self.syncing
        self.syncing = True
        self.property_start_combo.clear()
        self.property_end_combo.clear()
        for component in self.property_reference_components():
            label = self.property_reference_label(component)
            self.property_start_combo.addItem(label, component.index)
            self.property_end_combo.addItem(label, component.index)
        self.set_combo_by_component_index(self.property_start_combo, previous_start)
        self.set_combo_by_component_index(self.property_end_combo, previous_end)
        self.syncing = previous_syncing

    @staticmethod
    def set_combo_by_component_index(combo: QtWidgets.QComboBox, component_index: int) -> None:
        for index in range(combo.count()):
            if int(combo.itemData(index)) == int(component_index):
                combo.setCurrentIndex(index)
                return
        if combo.count() > 0:
            combo.setCurrentIndex(0)

    @staticmethod
    def combo_component_index(combo: QtWidgets.QComboBox, default: int = 1) -> int:
        data = combo.currentData()
        return int(data) if data is not None else default

    def selected_components(self):
        selected_indices = self.checked_component_indices()
        return [component for component in self.ordered_components() if component.index in selected_indices]

    def checked_component_indices(self) -> set[int]:
        checked_indices = {
            int(self.component_list.item(row).data(QtCore.Qt.UserRole))
            for row in range(self.component_list.count())
            if self.component_list.item(row).checkState() == QtCore.Qt.Checked
        }
        return checked_indices or {self.active_index}

    def states_for_output(self) -> dict[int, dict[str, object]]:
        output_states = {index: dict(state) for index, state in self.states.items()}
        for state in output_states.values():
            state["visible"] = True
        return output_states

    def view_checked_components(self) -> None:
        selection_model = self.component_list.selectionModel()
        selection_model.clearSelection()
        for row in range(self.component_list.count()):
            item = self.component_list.item(row)
            if item.checkState() == QtCore.Qt.Checked:
                item.setSelected(True)
        if not self.component_list.selectedItems():
            self.component_list.item(0).setSelected(True)
        self.update_preview()

    def load_active_state(self) -> None:
        state = self.states[self.active_index]
        self.syncing = True
        self.order_spin.setValue(int(state["order"]))
        self.enabled_checkbox.setChecked(bool(state.get("enabled", True)))
        self.property_type_combo.setCurrentText(str(state["property_type"]))
        self.assignment_mode_combo.setCurrentText(str(state.get("assignment_mode", "manual")))
        self.required_property_type_combo.setCurrentText(str(state.get("required_property_type", "Eb")))
        self.target_eb_spin.setValue(float(state.get("target_Eb_MPa") or 0.0))
        self.min_elongation_spin.setValue(float(state.get("min_elongation_percent") or 0.0))
        self.target_elongation_spin.setValue(float(state.get("target_elongation_percent") or 0.0))
        self.max_r0_spin.setValue(float(state.get("max_R0_ohm") or 0.0))
        self.target_r0_spin.setValue(float(state.get("target_R0_ohm") or 0.0))
        self.min_gf_spin.setValue(float(state.get("min_GF") or 0.0))
        self.target_gf_spin.setValue(float(state.get("target_GF") or 0.0))
        self.gradient_property_combo.setCurrentText(str(state.get("gradient_property", "Eb")))
        self.gradient_start_value_spin.setValue(float(state.get("gradient_start_value") or 0.0))
        self.gradient_end_value_spin.setValue(float(state.get("gradient_end_value") or 0.0))
        self.material_start_combo.setCurrentText(str(state["material_start"]))
        self.material_end_combo.setCurrentText(str(state["material_end"]))
        self.ratio_start_spin.setValue(float(state["material_start_ratio"]))
        self.ratio_end_spin.setValue(float(state["material_end_ratio"]))
        self.gradient_steps_spin.setValue(int(state["gradient_steps"]))
        self.gradient_direction_combo.setCurrentText(
            normalize_gradient_direction(state["gradient_direction"])
        )
        eta_mode = str(state.get("eta_mode", "auto")).strip().lower()
        eta_mode_index = self.eta_mode_combo.findData(eta_mode)
        self.eta_mode_combo.setCurrentIndex(max(0, eta_mode_index))
        self.eta_spin.setValue(float(state["eta"]))
        self.property_mpa_spin.setValue(float(state.get("property_mpa", 0.0)))
        self.property_gf_spin.setValue(float(state.get("property_gf", 0.0)))
        self.brighter_checkbox.setChecked(bool(state.get("brighter_mode", False)))
        self.refresh_property_reference_controls()
        self.set_combo_by_component_index(self.property_start_combo, int(state["property_start"]))
        self.set_combo_by_component_index(self.property_end_combo, int(state["property_end"]))
        self.syncing = False
        self.update_control_visibility()

    def save_active_state(self) -> None:
        def optional_spin_value(widget: QtWidgets.QDoubleSpinBox) -> float | None:
            value = float(widget.value())
            return None if abs(value) <= 1e-12 else value

        state = self.states[self.active_index]
        if "order" not in state:
            state["order"] = int(self.order_spin.value())
        state["enabled"] = bool(self.enabled_checkbox.isChecked())
        state["property_type"] = self.property_type_combo.currentText()
        state["assignment_mode"] = self.assignment_mode_combo.currentText()
        state["required_property_type"] = self.required_property_type_combo.currentText()
        state["target_Eb_MPa"] = optional_spin_value(self.target_eb_spin)
        state["min_elongation_percent"] = optional_spin_value(self.min_elongation_spin)
        state["target_elongation_percent"] = optional_spin_value(self.target_elongation_spin)
        state["max_R0_ohm"] = optional_spin_value(self.max_r0_spin)
        state["target_R0_ohm"] = optional_spin_value(self.target_r0_spin)
        state["min_GF"] = optional_spin_value(self.min_gf_spin)
        state["target_GF"] = optional_spin_value(self.target_gf_spin)
        state["gradient_property"] = self.gradient_property_combo.currentText()
        state["gradient_start_value"] = optional_spin_value(self.gradient_start_value_spin)
        state["gradient_end_value"] = optional_spin_value(self.gradient_end_value_spin)
        state["material_start"] = self.material_start_combo.currentText()
        state["material_end"] = self.material_end_combo.currentText()
        state["material_start_ratio"] = float(self.ratio_start_spin.value())
        state["material_end_ratio"] = float(self.ratio_end_spin.value())
        if state["material_end"] == NO_MATERIAL:
            state["material_end_ratio"] = 0.0
            self.syncing = True
            self.ratio_end_spin.setValue(0.0)
            self.syncing = False
        state["gradient_steps"] = 1 if state["property_type"] == "Property" else int(self.gradient_steps_spin.value())
        state["gradient_direction"] = normalize_gradient_direction(
            self.gradient_direction_combo.currentText()
        )
        state["eta_mode"] = str(self.eta_mode_combo.currentData() or "auto")
        state["eta"] = float(self.eta_spin.value())
        state["property_mpa"] = float(self.property_mpa_spin.value())
        state["property_gf"] = float(self.property_gf_spin.value())
        state["brighter_mode"] = bool(self.brighter_checkbox.isChecked())
        state["property_start"] = self.combo_component_index(self.property_start_combo, int(state.get("property_start", 1)))
        state["property_end"] = self.combo_component_index(self.property_end_combo, int(state.get("property_end", 1)))

    def on_component_checked(self, item) -> None:
        item.setSelected(item.checkState() == QtCore.Qt.Checked)
        if item.checkState() == QtCore.Qt.Checked:
            self.component_list.setCurrentItem(item)
        self.update_status()
        self.update_preview()

    def on_component_changed(self, current, _previous) -> None:
        if current is None:
            return
        self.save_active_state()
        self.active_index = int(current.data(QtCore.Qt.UserRole))
        self.load_active_state()
        self.update_preview()

    def on_controls_changed(self, *_args) -> None:
        if self.syncing:
            return
        self.save_active_state()
        self.refresh_property_reference_controls()
        self.refresh_component_labels()
        self.update_control_visibility()
        self.update_status()

    def update_preview(self) -> None:
        view_components = self.selected_components()
        self.renderer.RemoveAllViewProps()

        for component in view_components:
            self.renderer.AddActor(make_tube_actor(component, active=component.index == self.active_index))

        colors = vtkNamedColors()
        self.renderer.SetBackground(colors.GetColor3d("WhiteSmoke"))
        self.fit_view()
        self.vtk_widget.GetRenderWindow().Render()
        self.update_status()

    def fit_view(self) -> None:
        self.renderer.ResetCamera()
        self.renderer.ResetCameraClippingRange()
        camera = self.renderer.GetActiveCamera()
        camera.Elevation(25)
        camera.Azimuth(35)
        self.vtk_widget.GetRenderWindow().Render()

    def update_control_visibility(self) -> None:
        is_gradient = self.property_type_combo.currentText() == "Gradient"
        is_guided = self.assignment_mode_combo.currentText() == "property_guided"
        manual_property_keys = {"material_start", "property_mpa", "property_gf", "brighter", "eta_mode", "eta"}
        internal_keys = {"order", "material_end", "ratio_start", "ratio_end"}
        gradient_keys = {"gradient_steps", "gradient_direction", "property_start", "property_end"}
        guided_property_keys = {
            "required_property_type",
            "target_Eb_MPa",
            "min_elongation_percent",
            "target_elongation_percent",
            "max_R0_ohm",
            "target_R0_ohm",
            "min_GF",
            "target_GF",
            "gradient_property",
            "gradient_start_value",
            "gradient_end_value",
        }
        for key in manual_property_keys:
            label, widget = self.form_rows[key]
            label.setVisible((not is_gradient) and (not is_guided))
            widget.setVisible((not is_gradient) and (not is_guided))
        for key in internal_keys:
            label, widget = self.form_rows[key]
            label.setVisible(False)
            widget.setVisible(False)
        for key in gradient_keys:
            label, widget = self.form_rows[key]
            label.setVisible(is_gradient and (not is_guided))
            widget.setVisible(is_gradient and (not is_guided))
        for key in guided_property_keys:
            label, widget = self.form_rows[key]
            show = is_guided and (not is_gradient or key in {"gradient_property", "gradient_start_value", "gradient_end_value"})
            label.setVisible(show)
            widget.setVisible(show)
        eta_is_manual = str(self.eta_mode_combo.currentData() or "auto") == "manual"
        self.eta_spin.setEnabled(is_gradient or eta_is_manual)
        if is_gradient:
            self.eta_spin.setVisible(not is_guided)
            self.form_rows["eta"][0].setVisible(not is_guided)

    def update_status(self) -> None:
        component = self.active_component()
        active_output_index = self.output_index_by_component().get(component.index)
        active_output_label = f"P{active_output_index}" if active_output_index is not None else "OFF"
        active_state = self.states[component.index]
        view_labels = ", ".join(f"C{item.index}" for item in self.selected_components())
        is_guided = str(active_state.get("assignment_mode", "manual")) == "property_guided"
        payload = build_property_payload(
            self.components,
            self.states_for_output(),
            self.voxel_threshold_e,
            brighter_mode=bool(active_state.get("brighter_mode", False)),
        )
        gradient_ref_text = ""
        if str(active_state.get("property_type")) == "Gradient":
            start_component = int(active_state.get("property_start", 1))
            end_component = int(active_state.get("property_end", 1))
            output_indices = self.output_index_by_component()
            gradient_ref_text = (
                f"\nGradient refs: Property_start=P{output_indices.get(start_component, start_component)} "
                f"(C{start_component}), Property_end=P{output_indices.get(end_component, end_component)} "
                f"(C{end_component})"
            )
        guided_resolution_text = ""
        if is_guided:
            target_property = str(active_state.get("required_property_type", "Eb"))
            if str(active_state.get("property_type")) == "Gradient":
                guided_resolution_text = (
                    f"\nGuided input: {active_state.get('gradient_property', target_property)} "
                    f"{active_state.get('gradient_start_value')} -> {active_state.get('gradient_end_value')}"
                    "\nResolved later from experimental library to base material / ratio / eta."
                )
            else:
                target_value = (
                    active_state.get("target_Eb_MPa")
                    or active_state.get("target_elongation_percent")
                    or active_state.get("min_elongation_percent")
                    or active_state.get("target_R0_ohm")
                    or active_state.get("max_R0_ohm")
                    or active_state.get("target_GF")
                    or active_state.get("min_GF")
                )
                guided_resolution_text = (
                    f"\nGuided input: {target_property} = {target_value}"
                    "\nResolved later from experimental library to base material / ratio / eta."
                )
        resolved_text = ""
        if (not is_guided) and payload["assignments"]:
            active_assignment = next(
                (
                    assignment
                    for assignment in payload["assignments"]
                    if int(assignment.get("source_component_index", -1)) == component.index
                ),
                None,
            )
            if active_assignment is not None and str(active_assignment.get("Property_type")) == "Property":
                material_end = active_assignment.get("material_end")
                material_text = str(active_assignment.get("material_start"))
                if material_end:
                    material_text += f" / {material_end}"
                ratio_text = ", ".join(
                    f"{material}={float(ratio):.2f}%"
                    for material, ratio in active_assignment.get("final_material_ratios", {}).items()
                )
                resolved_text = (
                    "\nResolved: "
                    f"{active_assignment.get('requested_color', active_state.get('material_start'))} "
                    f"MPa={float(active_state.get('property_mpa', 0.0)):.3f} "
                    f"GF={float(active_state.get('property_gf', 0.0)):.3f} -> "
                    f"{material_text}, eta[{active_assignment.get('eta_mode', 'manual')}]="
                    f"{float(active_assignment.get('eta', 0.0)):.3f}"
                    f"\nFinal ratio: {ratio_text}"
                )
        self.status_label.setText(
            f"Active Property {active_output_label} / C{component.index}\n"
            f"{component.display_name or component.path.name}\n"
            f"Checked view: {view_labels}\n"
            f"Order: {active_state.get('order')} | Use in DM: {'on' if bool(active_state.get('enabled', True)) else 'off'}"
            f" | Type: {active_state.get('property_type')}"
            f" | Mode: {active_state.get('assignment_mode', 'manual')}"
            f" | Brighter: {'on' if bool(active_state.get('brighter_mode', False)) else 'off'}"
            f"{gradient_ref_text}{guided_resolution_text}{resolved_text}\n"
            f"E={component.total_e:.6f}, layers={component.layer_count}, segments={len(component.segments)}\n"
            f"Output assignments={len(payload['assignments'])}, voxels={payload['voxel_count']}"
        )

    def region_component_index_map(self) -> dict[str, int]:
        mapping: dict[str, int] = {}
        for component in self.components:
            name = canonical_component_name(
                component.display_name or component.path.name
            )
            mapping[name] = int(component.index)
        return mapping

    def region_property_label_map(self) -> dict[str, str]:
        labels: dict[str, str] = {}
        for component in self.components:
            state = self.states[component.index]
            region_name = canonical_component_name(
                component.display_name or component.path.name
            )
            property_type = str(state.get("property_type", "Property"))
            if str(state.get("assignment_mode", "manual")) == "property_guided":
                if property_type == "Gradient":
                    label = (
                        f"Guided {state.get('gradient_property', 'Property')} "
                        f"{state.get('gradient_start_value')} -> {state.get('gradient_end_value')}"
                    )
                else:
                    label = f"Guided {state.get('required_property_type', 'Property')}"
            elif property_type == "Gradient":
                label = (
                    f"Gradient P{state.get('property_start')} -> P{state.get('property_end')} "
                    f"({state.get('gradient_direction', 'printing')})"
                )
            else:
                label = str(state.get("material_start", "Property"))
            if not bool(state.get("enabled", True)):
                label = f"OFF - {label}"
            labels[region_name] = label
        return labels

    def region_property_color_map(self) -> dict[str, str]:
        colors: dict[str, str] = {}
        component_by_index = {
            component.index: component for component in self.components
        }
        for region_name, component_index in self.region_component_index_map().items():
            component = component_by_index.get(component_index)
            if component is None:
                continue
            state = self.states[component.index]
            if str(state.get("property_type", "Property")) == "Property":
                colors[region_name] = color_profile_swatch(
                    state.get("material_start", "WHITE")
                )
            else:
                colors[region_name] = "#64748b"
        return colors

    def preview_layer_regions(self) -> None:
        self.save_active_state()
        self.apply_component_list_order()
        source_gcode_path = self.components[0].path.resolve()
        preview_dir = (
            Path(__file__).resolve().parents[2]
            / "out"
            / self.output_path.stem
            / "input"
        )
        preview_dir.mkdir(parents=True, exist_ok=True)
        preview_gcode_path = (
            preview_dir
            / f"{self.output_path.stem}_layer_region_preview_ordered.gcode"
        )
        reorder_gcode_strategy = os.environ.get(
            REORDER_GCODE_STRATEGY_ENV_KEY,
            DEFAULT_REORDER_GCODE_STRATEGY,
        ).strip() or DEFAULT_REORDER_GCODE_STRATEGY
        write_reordered_full_gcode(
            source_gcode_path,
            self.components,
            self.states_for_output(),
            preview_gcode_path,
            strategy=reorder_gcode_strategy,
        )
        analysis = analyze_layer_regions(preview_gcode_path)
        preview_execution_plan = build_execution_plan(
            analysis,
            self.region_component_index_map(),
            {
                int(component.index)
                for component in self.output_components()
            },
        )
        dialog = show_layer_region_preview(
            analysis,
            parent=self,
            region_properties=self.region_property_label_map(),
            region_display_colors=self.region_property_color_map(),
            mapped_sequence_indices={
                int(event["sequence_index"])
                for event in preview_execution_plan["events"]
            },
        )
        self.layer_region_dialogs.append(dialog)
        self.status_label.setText(
            self.status_label.text()
            + "\n"
            + f"Layer x Region preview: {len(analysis.occurrences)} occurrences, "
            + f"{analysis.layer_count} layers, {len(analysis.region_names)} regions"
        )

    def save_json(self) -> None:
        self.save_active_state()
        self.apply_component_list_order()
        payload = build_property_payload(
            self.components,
            self.states_for_output(),
            self.voxel_threshold_e,
            brighter_mode=bool(self.brighter_checkbox.isChecked()),
        )
        payload["region_recognition_mode"] = self.region_recognition_mode
        payload["brighter_mode"] = any(bool(state.get("brighter_mode", False)) for state in self.states_for_output().values())
        payload["disabled_source_component_indices"] = [
            int(component.index)
            for component in self.ordered_components()
            if not bool(self.states[component.index].get("enabled", True))
        ]
        source_gcode_path = self.components[0].path.resolve()
        reorder_gcode_strategy = os.environ.get(
            REORDER_GCODE_STRATEGY_ENV_KEY,
            DEFAULT_REORDER_GCODE_STRATEGY,
        ).strip() or DEFAULT_REORDER_GCODE_STRATEGY
        ordered_gcode_dir = Path(__file__).resolve().parents[2] / "out" / self.output_path.stem / "input"
        ordered_gcode_dir.mkdir(parents=True, exist_ok=True)
        before_gcode_path = ordered_gcode_dir / f"{self.output_path.stem}_before_order.gcode"
        after_gcode_path = ordered_gcode_dir / f"{self.output_path.stem}_after_order.gcode"
        ordered_gcode_path = ordered_gcode_dir / f"{self.output_path.stem}_ordered.gcode"
        before_gcode_path.write_text(source_gcode_path.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
        write_reordered_full_gcode(
            source_gcode_path,
            self.components,
            self.states_for_output(),
            after_gcode_path,
            strategy=reorder_gcode_strategy,
        )
        ordered_gcode_path.write_text(after_gcode_path.read_text(encoding="utf-8"), encoding="utf-8")
        layer_region_analysis = analyze_layer_regions(ordered_gcode_path)
        layer_region_analysis_path = (
            ordered_gcode_dir
            / f"{self.output_path.stem}_layer_region_analysis.json"
        )
        layer_region_analysis.save_json(
            layer_region_analysis_path,
            include_segments=True,
        )
        enabled_component_indices = {
            int(component.index)
            for component in self.output_components()
        }
        layer_region_execution_plan = build_execution_plan(
            layer_region_analysis,
            self.region_component_index_map(),
            enabled_component_indices,
        )
        layer_region_execution_plan_path = (
            ordered_gcode_dir
            / f"{self.output_path.stem}_layer_region_execution_plan.json"
        )
        layer_region_execution_plan_path.write_text(
            json.dumps(
                layer_region_execution_plan,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        try:
            from .dm_spiral_mapping import build_spiral_mapping
        except ImportError:
            from dm_spiral_mapping import build_spiral_mapping
        spiral_mapping = build_spiral_mapping(
            layer_region_analysis,
            included_sequence_indices={
                int(event["sequence_index"])
                for event in layer_region_execution_plan["events"]
            },
        )
        spiral_mapping_path = (
            ordered_gcode_dir
            / f"{self.output_path.stem}_object_spiral_mapping.json"
        )
        spiral_mapping_path.write_text(
            json.dumps(
                spiral_mapping.to_payload(),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        before_component_lengths = []
        for component in self.components:
            before_component_lengths.append(
                {
                    "assignment_index": component.index,
                    "source_component_index": component.index,
                    "source_component_name": component.display_name or component.path.name,
                    "enabled_in_dm": bool(self.states[component.index].get("enabled", True)),
                    "component_total_filament_e_mm": float(component.total_e),
                    "component_voxel_count": int(component_voxel_count(component.total_e, self.voxel_threshold_e)),
                }
            )
        ordered_component_lengths = []
        mapped_after_step_lengths = []
        for output_index, component in enumerate(self.output_components(), start=1):
            assignment = payload["assignments"][output_index - 1]
            property_type = str(assignment.get("Property_type", "Property"))
            effective_steps = 1 if property_type == "Property" else int(assignment.get("gradient_steps", 1))
            gradient_direction = normalize_gradient_direction(
                assignment.get("gradient_direction", "printing")
            )
            component_total_e = float(component.total_e)
            if property_type == "Property" or gradient_direction != "layer":
                mapped_step_lengths = [component_total_e / effective_steps] * effective_steps
            else:
                mapped_step_lengths = build_layer_based_step_lengths(component, effective_steps)
            ordered_component_lengths.append(
                {
                    "assignment_index": output_index,
                    "source_component_index": component.index,
                    "source_component_name": component.display_name or component.path.name,
                    "property_type": property_type,
                    "gradient_direction": gradient_direction,
                    "effective_step_count": effective_steps,
                    "component_total_filament_e_mm": component_total_e,
                    "component_voxel_count": int(component_voxel_count(component_total_e, self.voxel_threshold_e)),
                    "mapped_step_lengths_e_mm": mapped_step_lengths,
                }
            )
            mapped_after_step_lengths.extend(mapped_step_lengths)
        payload["source_gcode"] = str(source_gcode_path)
        payload["reordered_full_gcode"] = str(ordered_gcode_path)
        payload["before_order_gcode"] = str(before_gcode_path)
        payload["after_order_gcode"] = str(after_gcode_path)
        payload["reordered_full_gcode_strategy"] = reorder_gcode_strategy
        payload["before_component_lengths"] = before_component_lengths
        payload["ordered_component_lengths"] = ordered_component_lengths
        payload["mapped_after_step_lengths_e_mm"] = mapped_after_step_lengths
        payload["resolution_mode"] = "layer_region_occurrence"
        payload["layer_region_analysis_path"] = str(layer_region_analysis_path)
        payload["layer_region_execution_plan_path"] = str(
            layer_region_execution_plan_path
        )
        payload["layer_region_execution_plan"] = layer_region_execution_plan
        payload["layer_region_mapped_step_lengths_e_mm"] = [
            float(event["extrusion_e_mm"])
            for event in layer_region_execution_plan["events"]
        ]
        payload["layer_region_summary"] = {
            "layer_count": layer_region_analysis.layer_count,
            "region_count": len(layer_region_analysis.region_names),
            "occurrence_count": len(layer_region_analysis.occurrences),
            "execution_event_count": int(
                layer_region_execution_plan["event_count"]
            ),
            "total_deposition_e_mm": layer_region_analysis.total_deposition_e_mm,
            "region_deposition_e_mm": layer_region_analysis.region_deposition_e_mm,
            "non_region_deposition_e_mm": (
                layer_region_analysis.non_region_deposition_e_mm
            ),
            "warnings": list(layer_region_analysis.warnings),
        }
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
        before_lengths_path = ordered_gcode_dir / f"{self.output_path.stem}_before_order_lengths.json"
        before_lengths_path.write_text(json.dumps(before_component_lengths, indent=2, ensure_ascii=True), encoding="utf-8")
        after_lengths_path = ordered_gcode_dir / f"{self.output_path.stem}_after_order_lengths.json"
        after_lengths_path.write_text(json.dumps(ordered_component_lengths, indent=2, ensure_ascii=True), encoding="utf-8")
        after_step_lengths_path = ordered_gcode_dir / f"{self.output_path.stem}_after_order_step_lengths.json"
        after_step_lengths_path.write_text(json.dumps(mapped_after_step_lengths, indent=2, ensure_ascii=True), encoding="utf-8")
        mapped_length_txt_path = ordered_gcode_dir / "length.txt"
        mapped_length_txt_path.write_text(
            f"length = {pformat(mapped_after_step_lengths, width=120)}\n",
            encoding="utf-8",
        )
        ordered_lengths_path = ordered_gcode_dir / f"{self.output_path.stem}_ordered_lengths.json"
        ordered_lengths_path.write_text(json.dumps(ordered_component_lengths, indent=2, ensure_ascii=True), encoding="utf-8")
        self.status_label.setText(
            self.status_label.text()
            + f"\nSaved JSON: {self.output_path}"
            + f"\nSaved before-order G-code: {before_gcode_path}"
            + f"\nSaved after-order G-code: {after_gcode_path}"
            + f"\nSaved ordered G-code: {ordered_gcode_path}"
            + f"\nOrdered G-code strategy: {reorder_gcode_strategy}"
            + f"\nSaved before-order lengths: {before_lengths_path}"
            + f"\nSaved after-order lengths: {after_lengths_path}"
            + f"\nSaved after-order step lengths: {after_step_lengths_path}"
            + f"\nSaved mapped length.txt: {mapped_length_txt_path}"
            + f"\nSaved ordered lengths: {ordered_lengths_path}"
            + f"\nSaved Layer x Region analysis: {layer_region_analysis_path}"
            + f"\nSaved Layer x Region execution plan: {layer_region_execution_plan_path}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Qt/VTK designer: edit Property_sample.json from object sections in one G-code file."
    )
    parser.add_argument(
        "gcode_file",
        nargs="?",
        default=INPUT_GCODE,
        help="Full G-code file containing either '; printing object ... id:N copy M' or ';MESH:part_name.STL' comments.",
    )
    parser.add_argument("--output", default=OUTPUT_JSON or str(DEFAULT_OUTPUT_PATH), help="Output JSON path.")
    parser.add_argument("--voxel-threshold-e", type=float, default=VOXEL_THRESHOLD_E, help="E amount represented by one output voxel.")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[2]
    gcode_path = Path(args.gcode_file)
    if not gcode_path.is_absolute():
        gcode_path = project_root / gcode_path
    gcode_path = gcode_path.resolve()
    if not gcode_path.exists():
        raise SystemExit(f"G-code file not found: {gcode_path}")
    components = parse_full_gcode_objects(gcode_path)
    if not components:
        raise SystemExit("No object components found in the G-code comments.")

    app = QtWidgets.QApplication(sys.argv)
    window = QtFullGcodeObjectDesigner(
        components,
        (Path(args.output) if Path(args.output).is_absolute() else project_root / Path(args.output)).resolve(),
        float(args.voxel_threshold_e),
    )
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
