"""
PyVista selector for G-code-reconstructed mesh faces and path voxels.

This does not load STL/OBJ/PLY files.
It reconstructs a smooth-ish triangular surface from the G-code path samples,
then lets you click those mesh faces to select the corresponding path voxels.
"""

import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

try:
    import pyvista as pv
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
    from vtkmodules.vtkInteractionStyle import vtkInteractorStyleUser
    from vtkmodules.vtkRenderingCore import vtkActor2D, vtkCellPicker, vtkCoordinate, vtkPolyDataMapper2D
except ImportError as exc:
    raise SystemExit(
        "PyVista is not installed in this Python environment. "
        "Install pyvista/vtk or run the matplotlib selector instead."
    ) from exc

from interactive_voxel_selector import (
    annotate_voxels_with_layers,
    build_virtual_voxel_sample_cache,
    build_voxel_selection_cache,
    compute_selected_voxel_filament_e,
    estimate_rectangular_region_size,
    group_segments_into_voxels,
    layers_from_selected_voxels_cached,
    parse_gcode_extrusion_segments,
    point_in_triangle_3d,
)


def make_path_polydata(flat_segments: np.ndarray, voxel_ids: Optional[Set[int]] = None) -> pv.PolyData:
    """Build one PolyData line mesh from flat segment rows."""
    if flat_segments.size == 0:
        return pv.PolyData()

    rows = flat_segments
    if voxel_ids is not None:
        if not voxel_ids:
            return pv.PolyData()
        mask = np.isin(rows[:, 0].astype(int), np.array(sorted(voxel_ids), dtype=int))
        rows = rows[mask]

    if rows.size == 0:
        return pv.PolyData()

    points = np.empty((rows.shape[0] * 2, 3), dtype=float)
    points[0::2, :] = rows[:, 3:6]
    points[1::2, :] = rows[:, 6:9]

    lines = np.empty((rows.shape[0], 3), dtype=np.int64)
    lines[:, 0] = 2
    lines[:, 1] = np.arange(0, rows.shape[0] * 2, 2)
    lines[:, 2] = lines[:, 1] + 1

    mesh = pv.PolyData(points, lines=lines.ravel())
    mesh.cell_data["voxel_id"] = rows[:, 0].astype(int)
    mesh.point_data["voxel_id"] = np.repeat(rows[:, 0].astype(int), 2)
    return mesh


def dilate_occupancy(occupied: np.ndarray, iterations: int) -> np.ndarray:
    """Expand occupied grid points without requiring scipy."""
    result = occupied.astype(bool, copy=True)
    for _iteration in range(max(0, int(iterations))):
        padded = np.pad(result, 1, mode="constant", constant_values=False)
        expanded = np.zeros_like(result, dtype=bool)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    expanded |= padded[
                        1 + dx:1 + dx + result.shape[0],
                        1 + dy:1 + dy + result.shape[1],
                        1 + dz:1 + dz + result.shape[2],
                    ]
        result = expanded
    return result


def build_smooth_surface_mesh_from_samples(
    sample_points: np.ndarray,
    grid_spacing_mm: float,
    dilation_iterations: int = 1,
    smoothing_iterations: int = 25,
) -> pv.PolyData:
    """Reconstruct a triangular contour surface from sampled G-code path points."""
    if sample_points.size == 0:
        return pv.PolyData()

    spacing = max(float(grid_spacing_mm), 1e-6)
    padding = spacing * (2.0 + max(0, int(dilation_iterations)))
    min_point = np.min(sample_points, axis=0) - padding
    max_point = np.max(sample_points, axis=0) + padding
    dimensions = np.ceil((max_point - min_point) / spacing).astype(int) + 1
    dimensions = np.maximum(dimensions, 3)

    indices = np.floor((sample_points - min_point) / spacing).astype(int)
    indices = np.clip(indices, 0, dimensions - 1)

    occupied = np.zeros(tuple(int(value) for value in dimensions), dtype=bool)
    occupied[indices[:, 0], indices[:, 1], indices[:, 2]] = True
    occupied = dilate_occupancy(occupied, dilation_iterations)

    image = pv.ImageData()
    image.dimensions = tuple(int(value) for value in dimensions)
    image.origin = tuple(float(value) for value in min_point)
    image.spacing = (spacing, spacing, spacing)
    image.point_data["occupied"] = occupied.astype(float).ravel(order="F")

    surface = image.contour([0.5], scalars="occupied").extract_surface().triangulate()
    if surface.n_cells == 0:
        return surface

    surface.cell_data["surface_face_id"] = np.arange(surface.n_cells, dtype=int)
    if smoothing_iterations > 0:
        surface = surface.smooth(
            n_iter=int(smoothing_iterations),
            relaxation_factor=0.08,
            boundary_smoothing=True,
            feature_smoothing=False,
        ).triangulate()
        surface.cell_data["surface_face_id"] = np.arange(surface.n_cells, dtype=int)
    return surface


def make_selected_surface_mesh(surface_mesh: pv.PolyData, face_ids: Set[int]) -> pv.PolyData:
    """Extract selected triangular surface faces as a highlight mesh."""
    if not face_ids or surface_mesh.n_cells == 0:
        return pv.PolyData()
    selected = surface_mesh.extract_cells(sorted(int(face_id) for face_id in face_ids))
    return selected.extract_surface().triangulate()


def triangle_points_from_mesh(surface_mesh: pv.PolyData, face_id: int) -> np.ndarray:
    """Return three points for one triangular mesh face."""
    faces = np.asarray(surface_mesh.faces, dtype=np.int64).reshape((-1, 4))
    vertex_indices = faces[int(face_id), 1:4]
    return np.asarray(surface_mesh.points[vertex_indices], dtype=float)


def voxel_ids_near_triangle_from_samples(
    sample_points: np.ndarray,
    sample_voxel_ids: np.ndarray,
    triangle_points: np.ndarray,
    selection_tolerance_mm: float,
    barycentric_tolerance: float = 0.15,
) -> Set[int]:
    """Select voxel ids using virtual samples around one clicked surface triangle."""
    if sample_points.size == 0 or sample_voxel_ids.size == 0:
        return set()

    tri_a = triangle_points[0]
    tri_b = triangle_points[1]
    tri_c = triangle_points[2]
    normal = np.cross(tri_b - tri_a, tri_c - tri_a)
    normal_norm = float(np.linalg.norm(normal))
    if normal_norm < 1e-12:
        return set()
    normal = normal / normal_norm

    tolerance = max(float(selection_tolerance_mm), 1e-6)
    tri_min = np.min(triangle_points, axis=0) - tolerance
    tri_max = np.max(triangle_points, axis=0) + tolerance
    box_mask = np.all((sample_points >= tri_min) & (sample_points <= tri_max), axis=1)
    if not np.any(box_mask):
        return set()

    candidate_indices = np.where(box_mask)[0]
    candidate_points = sample_points[candidate_indices]
    signed_distances = (candidate_points - tri_a) @ normal
    near_plane = np.abs(signed_distances) <= tolerance

    selected: Set[int] = set()
    for local_index in np.where(near_plane)[0]:
        source_index = int(candidate_indices[int(local_index)])
        point = sample_points[source_index]
        projected = point - signed_distances[int(local_index)] * normal
        if point_in_triangle_3d(projected, tri_a, tri_b, tri_c, tolerance=barycentric_tolerance):
            selected.add(int(sample_voxel_ids[source_index]))
    return selected


class PyVistaGcodeSurfaceFaceSelector:
    """Select G-code-reconstructed triangular surface faces and linked voxels."""

    def __init__(
        self,
        gcode_path: str,
        voxel_threshold_e: float = 0.1,
        output_dir: Optional[str] = None,
        virtual_sample_spacing_mm: float = 0.2,
        rectangular_region_size_mm: Optional[Tuple[float, float, float]] = None,
        surface_grid_spacing_mm: Optional[float] = None,
        face_selection_tolerance_mm: Optional[float] = None,
    ):
        self.gcode_path = Path(gcode_path)
        self.output_dir = Path(output_dir) if output_dir else self.gcode_path.parent
        self.voxel_threshold_e = float(voxel_threshold_e)
        self.virtual_sample_spacing_mm = float(virtual_sample_spacing_mm)
        self.rectangular_region_size_mm = rectangular_region_size_mm
        self.surface_grid_spacing_mm = surface_grid_spacing_mm
        self.face_selection_tolerance_mm = face_selection_tolerance_mm

        self.segments: List[Dict] = []
        self.voxels: List[Dict] = []
        self.flat_segments = np.empty((0, 10), dtype=float)
        self.preprint_e = 0.0

        self.selection_cache: Dict[str, np.ndarray] = {}
        self.virtual_sample_cache: Dict[str, np.ndarray] = {}
        self.surface_mesh = pv.PolyData()
        self.path_mesh = pv.PolyData()

        self.plotter: Optional[pv.Plotter] = None
        self.left_renderer = None
        self.right_renderer = None
        self.surface_actor = None
        self.path_actor = None
        self.pick_observer_registered = False
        self.pick_observer_id: Optional[int] = None
        self.release_observer_id: Optional[int] = None
        self.move_observer_id: Optional[int] = None
        self.drag_selection_active = False
        self.drag_start_pos: Optional[Tuple[int, int]] = None
        self.drag_current_pos: Optional[Tuple[int, int]] = None
        self.drag_box_actor = None
        self.drag_box_renderer = None
        self.drag_camera_state: Dict[str, Dict[str, object]] = {}
        self.initial_camera_state: Dict[str, Dict[str, object]] = {}
        self.surface_face_centers = np.empty((0, 3), dtype=float)

        self.selected_face_ids: Set[int] = set()
        self.selected_voxel_ids: Set[int] = set()
        self.assignments: List[Dict] = []

    def parse(self) -> None:
        """Parse G-code and reconstruct an outer triangular surface mesh."""
        print("=" * 60)
        print("PyVista G-code Surface Face Selector")
        print("=" * 60)

        start = time.time()
        self.segments, self.preprint_e = parse_gcode_extrusion_segments(str(self.gcode_path))
        self.voxels, self.flat_segments = group_segments_into_voxels(
            self.segments,
            self.voxel_threshold_e,
        )
        annotate_voxels_with_layers(self.voxels)
        self.selection_cache = build_voxel_selection_cache(self.voxels)
        self.virtual_sample_cache = build_virtual_voxel_sample_cache(
            self.voxels,
            self.virtual_sample_spacing_mm,
        )

        estimated_region_size = estimate_rectangular_region_size(self.voxels)
        if self.rectangular_region_size_mm is None:
            self.rectangular_region_size_mm = estimated_region_size
        if self.surface_grid_spacing_mm is None:
            xy_spacing = min(float(estimated_region_size[0]), float(estimated_region_size[1]))
            z_spacing = float(estimated_region_size[2])
            self.surface_grid_spacing_mm = max(0.30, min(xy_spacing, z_spacing) * 2.0)
        if self.face_selection_tolerance_mm is None:
            self.face_selection_tolerance_mm = max(float(self.surface_grid_spacing_mm) * 2.25, 0.30)

        self.surface_mesh = build_smooth_surface_mesh_from_samples(
            self.virtual_sample_cache["points"],
            float(self.surface_grid_spacing_mm),
            dilation_iterations=1,
            smoothing_iterations=18,
        )
        if self.surface_mesh.n_cells > 0:
            self.surface_face_centers = np.asarray(self.surface_mesh.cell_centers().points, dtype=float)
            self.surface_mesh.cell_data["z_height"] = self.surface_face_centers[:, 2]
        self.path_mesh = make_path_polydata(self.flat_segments)

        print(f"Parsed in {time.time() - start:.2f}s")
        print(f"  G-code file: {self.gcode_path}")
        print(f"  Path voxels: {len(self.voxels):,} @ E {self.voxel_threshold_e:.3f}")
        print(
            f"  Estimated region size: {self.rectangular_region_size_mm[0]:.3f} x "
            f"{self.rectangular_region_size_mm[1]:.3f} x "
            f"{self.rectangular_region_size_mm[2]:.3f} mm"
        )
        print(f"  Smooth surface grid spacing: {float(self.surface_grid_spacing_mm):.3f} mm")
        print(f"  Face selection tolerance: {float(self.face_selection_tolerance_mm):.3f} mm")
        print(f"  Smooth reconstructed surface triangles: {self.surface_mesh.n_cells:,}")

    def setup_plotter(self) -> None:
        """Create the PyVista interface."""
        self.plotter = pv.Plotter(shape=(1, 2), window_size=(1800, 900))

        self.plotter.subplot(0, 0)
        self.left_renderer = self.plotter.renderer
        self.plotter.add_text(
            "Mesh view: click faces, or press B then drag",
            position="upper_left",
            font_size=10,
            name="left_title_text",
        )
        self.surface_actor = self.plotter.add_mesh(
            self.surface_mesh,
            scalars="z_height" if "z_height" in self.surface_mesh.cell_data else None,
            cmap="turbo",
            opacity=0.70,
            show_edges=True,
            line_width=1,
            pickable=True,
            name="smooth_reconstructed_surface",
        )
        self.plotter.add_axes()
        self.plotter.reset_camera()

        self.plotter.subplot(0, 1)
        self.right_renderer = self.plotter.renderer
        self.plotter.add_text(
            "Voxel view: selected path voxels",
            position="upper_left",
            font_size=10,
            name="right_title_text",
        )
        self.path_actor = self.plotter.add_mesh(
            self.path_mesh,
            scalars="voxel_id" if "voxel_id" in self.path_mesh.cell_data else None,
            cmap="viridis",
            line_width=1.0,
            opacity=0.45,
            pickable=False,
            name="path_voxels",
        )
        self.plotter.add_axes()
        self.plotter.reset_camera()
        self.plotter.link_views()
        self.initial_camera_state = self._snapshot_camera_state()
        self.plotter.add_text(
            "Click = toggle multi-select. B = box-drag faces. A = add, W = save, C = clear",
            position="lower_left",
            font_size=9,
            name="status_text",
        )
        self.plotter.add_key_event("a", self.add_current_assignment)
        self.plotter.add_key_event("A", self.add_current_assignment)
        self.plotter.add_key_event("w", self.save_all_assignments_to_json)
        self.plotter.add_key_event("W", self.save_all_assignments_to_json)
        self.plotter.add_key_event("c", self.clear_selection)
        self.plotter.add_key_event("C", self.clear_selection)
        self.plotter.add_key_event("b", self.enable_drag_face_selection)
        self.plotter.add_key_event("B", self.enable_drag_face_selection)
        self._register_pick_observer()

    def _register_pick_observer(self) -> None:
        """Register a VTK left-click observer for reconstructed face picking."""
        if self.plotter is None or self.pick_observer_registered:
            return

        interactor = getattr(self.plotter.iren, "interactor", None)
        if interactor is not None and hasattr(interactor, "AddObserver"):
            observer_id = interactor.AddObserver("LeftButtonPressEvent", self._on_left_button_press, 1.0)
            release_id = interactor.AddObserver("LeftButtonReleaseEvent", self._on_left_button_release, 1.0)
            move_id = interactor.AddObserver("MouseMoveEvent", self._on_mouse_move, 1.0)
        else:
            observer_id = self.plotter.iren.add_observer("LeftButtonPressEvent", self._on_left_button_press)
            release_id = self.plotter.iren.add_observer("LeftButtonReleaseEvent", self._on_left_button_release)
            move_id = self.plotter.iren.add_observer("MouseMoveEvent", self._on_mouse_move)
        self.pick_observer_id = int(observer_id)
        self.release_observer_id = int(release_id)
        self.move_observer_id = int(move_id)
        self.pick_observer_registered = True

    def _event_position(self, interactor) -> Tuple[int, int]:
        """Return the current VTK display position."""
        if interactor is not None and hasattr(interactor, "GetEventPosition"):
            x, y = interactor.GetEventPosition()
            return int(x), int(y)
        if self.plotter is not None:
            x, y = self.plotter.iren.get_event_position()
            return int(x), int(y)
        return 0, 0

    def _display_y_candidates(self, y: int) -> Tuple[int, int]:
        """Return bottom-left and top-left display Y coordinate candidates."""
        if self.plotter is None:
            return int(y), int(y)
        height = int(self.plotter.window_size[1])
        return int(y), height - int(y)

    def _snapshot_camera_state(self) -> Dict[str, Dict[str, object]]:
        """Capture camera state for both views before interaction-style changes."""
        state: Dict[str, Dict[str, object]] = {}
        renderers = {"left": self.left_renderer, "right": self.right_renderer}
        for key, renderer in renderers.items():
            if renderer is None:
                continue
            camera = renderer.GetActiveCamera()
            if camera is None:
                continue
            state[key] = {
                "position": tuple(camera.GetPosition()),
                "focal_point": tuple(camera.GetFocalPoint()),
                "view_up": tuple(camera.GetViewUp()),
                "clipping_range": tuple(camera.GetClippingRange()),
                "parallel_scale": float(camera.GetParallelScale()),
                "parallel_projection": int(camera.GetParallelProjection()),
            }
        return state

    def _restore_camera_state(self, state: Dict[str, Dict[str, object]]) -> None:
        """Restore camera state after changing interaction style."""
        if not state:
            return
        renderers = {"left": self.left_renderer, "right": self.right_renderer}
        for key, camera_state in state.items():
            renderer = renderers.get(key)
            if renderer is None:
                continue
            camera = renderer.GetActiveCamera()
            if camera is None:
                continue
            camera.SetPosition(*camera_state["position"])
            camera.SetFocalPoint(*camera_state["focal_point"])
            camera.SetViewUp(*camera_state["view_up"])
            camera.SetClippingRange(*camera_state["clipping_range"])
            camera.SetParallelScale(float(camera_state["parallel_scale"]))
            camera.SetParallelProjection(int(camera_state["parallel_projection"]))
        if self.plotter is not None:
            self.plotter.render()

    def _enable_drag_interactor_style(self) -> None:
        """Switch left-drag away from camera rotation while custom drag is active."""
        if self.plotter is None:
            return
        self.drag_camera_state = self._snapshot_camera_state()
        try:
            self.plotter.disable_picking()
        except Exception:
            pass
        try:
            self.plotter.iren.interactor.SetInteractorStyle(vtkInteractorStyleUser())
        except Exception:
            pass
        self._restore_camera_state(self.drag_camera_state)

    def _restore_trackball_interactor_style(self) -> None:
        """Restore normal camera interaction after custom drag selection."""
        if self.plotter is None:
            return
        camera_state = self.drag_camera_state or self._snapshot_camera_state()
        try:
            self.plotter.enable_trackball_style()
        except Exception:
            try:
                self.plotter.iren.enable_trackball_style()
            except Exception:
                pass
        self._restore_camera_state(camera_state)
        self.drag_camera_state = {}

    def _clear_drag_box_actor(self, render: bool = False) -> None:
        """Remove the custom screen-space drag rectangle."""
        if self.drag_box_actor is None or self.drag_box_renderer is None:
            self.drag_box_actor = None
            self.drag_box_renderer = None
            return
        try:
            self.drag_box_renderer.RemoveActor(self.drag_box_actor)
        except Exception:
            pass
        self.drag_box_actor = None
        self.drag_box_renderer = None
        if render and self.plotter is not None:
            self.plotter.render()

    def _draw_drag_box_actor(self, start_pos: Tuple[int, int], end_pos: Tuple[int, int]) -> None:
        """Draw a 2D white rectangle in display coordinates."""
        if self.left_renderer is None or self.plotter is None:
            return

        self._clear_drag_box_actor(render=False)

        x0, x1 = sorted((int(start_pos[0]), int(end_pos[0])))
        y0, y1 = sorted((int(start_pos[1]), int(end_pos[1])))

        points = vtkPoints()
        points.InsertNextPoint(float(x0), float(y0), 0.0)
        points.InsertNextPoint(float(x1), float(y0), 0.0)
        points.InsertNextPoint(float(x1), float(y1), 0.0)
        points.InsertNextPoint(float(x0), float(y1), 0.0)

        lines = vtkCellArray()
        lines.InsertNextCell(5)
        for point_id in (0, 1, 2, 3, 0):
            lines.InsertCellPoint(point_id)

        polydata = vtkPolyData()
        polydata.SetPoints(points)
        polydata.SetLines(lines)

        coordinate = vtkCoordinate()
        coordinate.SetCoordinateSystemToDisplay()

        mapper = vtkPolyDataMapper2D()
        mapper.SetInputData(polydata)
        mapper.SetTransformCoordinate(coordinate)

        actor = vtkActor2D()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(1.0, 1.0, 1.0)
        actor.GetProperty().SetLineWidth(2.0)

        self.left_renderer.AddActor2D(actor)
        self.drag_box_actor = actor
        self.drag_box_renderer = self.left_renderer
        self.plotter.render()

    def _position_in_left_view(self, pos: Tuple[int, int]) -> bool:
        """Return True when a display coordinate is inside the mesh view."""
        if self.plotter is None or self.left_renderer is None:
            return False
        width, height = self.plotter.window_size
        vx0, vy0, vx1, vy1 = self.left_renderer.GetViewport()
        x, y = pos
        x_inside = (vx0 * width) <= x <= (vx1 * width)
        y_inside = any((vy0 * height) <= candidate <= (vy1 * height) for candidate in self._display_y_candidates(y))
        return bool(x_inside and y_inside)

    def _pick_surface_face_id(self, pos: Tuple[int, int]) -> Optional[int]:
        """Pick one reconstructed triangular surface cell at display position."""
        if self.plotter is None or self.surface_actor is None or self.left_renderer is None:
            return None

        picker = vtkCellPicker()
        picker.SetTolerance(0.0008)
        picker.PickFromListOn()
        picker.AddPickList(self.surface_actor)

        candidates = [pos]
        flipped_y = self._display_y_candidates(pos[1])[1]
        if flipped_y != pos[1]:
            candidates.append((pos[0], flipped_y))

        renderer = self.left_renderer
        for x, y in candidates:
            picked = picker.Pick(float(x), float(y), 0.0, renderer)
            if not picked:
                continue
            cell_id = int(picker.GetCellId())
            if 0 <= cell_id < self.surface_mesh.n_cells:
                return cell_id
        return None

    def _on_left_button_press(self, interactor, event_name) -> None:
        """Toggle the clicked reconstructed surface triangle."""
        pos = self._event_position(interactor)
        if self.drag_selection_active:
            if self._position_in_left_view(pos):
                self.drag_start_pos = pos
                self.drag_current_pos = pos
                print(f"Mesh face drag started: start={pos}")
            return
        face_id = self._pick_surface_face_id(pos)
        if face_id is None:
            return
        self.toggle_face_selection(face_id)

    def _on_mouse_move(self, interactor, event_name) -> None:
        """Track drag movement while mesh-face box selection is active."""
        if not self.drag_selection_active or self.drag_start_pos is None:
            return
        self.drag_current_pos = self._event_position(interactor)
        self._draw_drag_box_actor(self.drag_start_pos, self.drag_current_pos)

    def _on_left_button_release(self, interactor, event_name) -> None:
        """Apply drag-box mesh face selection on release."""
        if not self.drag_selection_active:
            return
        end_pos = self._event_position(interactor)
        start_pos = self.drag_start_pos
        self.drag_start_pos = None
        self.drag_current_pos = end_pos
        self._clear_drag_box_actor(render=False)
        if start_pos is None:
            return
        face_ids = self._face_ids_from_display_rectangle(start_pos, end_pos)
        if not face_ids:
            print("Mesh face drag found no faces.")
            self._finish_drag_mode()
            return
        self.selected_face_ids.update(face_ids)
        self.refresh_selection()
        print(f"Mesh face drag added {len(face_ids)} faces.")
        self._finish_drag_mode()

    def enable_drag_face_selection(self) -> None:
        """Toggle mesh-view drag selection mode."""
        if self.drag_selection_active:
            self._finish_drag_mode()
            print("Mesh face box-drag disabled.")
            return
        self.drag_selection_active = True
        self.drag_start_pos = None
        self.drag_current_pos = None
        self._enable_drag_interactor_style()
        if self.plotter is not None:
            message = "Mesh face box-drag ON. Drag over the left mesh view."
            self.plotter.add_text(message, position="lower_left", font_size=9, name="status_text")
            self.plotter.render()
        print("Mesh face box-drag enabled.")

    def _finish_drag_mode(self) -> None:
        """Return to normal click/camera mode after drag selection."""
        self.drag_selection_active = False
        self.drag_start_pos = None
        self.drag_current_pos = None
        self._clear_drag_box_actor(render=False)
        self._restore_trackball_interactor_style()
        if self.plotter is not None:
            self.plotter.add_text(
                "Click = toggle multi-select. B = box-drag faces. A = add, W = save, C = clear",
                position="lower_left",
                font_size=9,
                name="status_text",
            )
            self.plotter.render()

    def _project_face_footprints_to_display(self) -> np.ndarray:
        """Project each triangle's screen-space footprint in the mesh view."""
        if self.left_renderer is None or self.surface_mesh.n_cells == 0:
            return np.empty((0, 6), dtype=float)

        faces = np.asarray(self.surface_mesh.faces, dtype=np.int64).reshape((-1, 4))
        projected = np.empty((faces.shape[0], 6), dtype=float)

        for face_id, face in enumerate(faces):
            display_points = []
            for vertex_index in face[1:4]:
                point = self.surface_mesh.points[int(vertex_index)]
                self.left_renderer.SetWorldPoint(float(point[0]), float(point[1]), float(point[2]), 1.0)
                self.left_renderer.WorldToDisplay()
                display_points.append(self.left_renderer.GetDisplayPoint())

            display_array = np.asarray(display_points, dtype=float)
            projected[face_id, :] = [
                float(np.min(display_array[:, 0])),
                float(np.max(display_array[:, 0])),
                float(np.min(display_array[:, 1])),
                float(np.max(display_array[:, 1])),
                float(np.min(display_array[:, 2])),
                float(face_id),
            ]
        return projected

    def _face_ids_from_display_rectangle(
        self,
        start_pos: Tuple[int, int],
        end_pos: Tuple[int, int],
    ) -> Set[int]:
        """Return face ids whose projected triangle footprint overlaps a drag rectangle."""
        projected = self._project_face_footprints_to_display()
        if projected.size == 0:
            return set()

        x0, x1 = sorted((int(start_pos[0]), int(end_pos[0])))
        y0, y1 = sorted((int(start_pos[1]), int(end_pos[1])))
        if abs(x1 - x0) < 3 or abs(y1 - y0) < 3:
            return set()

        face_ids = self._face_ids_from_projected_rectangle(projected, x0, x1, y0, y1)
        if not face_ids and self.plotter is not None:
            height = int(self.plotter.window_size[1])
            flipped_y0, flipped_y1 = sorted((height - y0, height - y1))
            face_ids = self._face_ids_from_projected_rectangle(projected, x0, x1, flipped_y0, flipped_y1)
        return face_ids

    def _face_ids_from_projected_rectangle(
        self,
        projected: np.ndarray,
        x0: int,
        x1: int,
        y0: int,
        y1: int,
    ) -> Set[int]:
        """Return projected face ids whose screen-space footprint overlaps a rectangle."""
        mask = (
            (projected[:, 1] >= x0) & (projected[:, 0] <= x1)
            & (projected[:, 3] >= y0) & (projected[:, 2] <= y1)
        )
        return set(int(face_id) for face_id in projected[mask, 5].astype(int))

    def toggle_face_selection(self, face_id: int) -> None:
        """Toggle one triangular face and refresh linked voxels."""
        if face_id in self.selected_face_ids:
            self.selected_face_ids.remove(face_id)
        else:
            self.selected_face_ids.add(face_id)
        self.refresh_selection()

    def refresh_selection(self) -> None:
        """Refresh selected reconstructed faces and linked path voxel highlights."""
        camera_state = self._snapshot_camera_state()
        voxel_ids: Set[int] = set()
        sample_points = self.virtual_sample_cache.get("points", np.empty((0, 3), dtype=float))
        sample_voxel_ids = self.virtual_sample_cache.get("voxel_ids", np.empty((0,), dtype=int))
        for face_id in self.selected_face_ids:
            triangle_points = triangle_points_from_mesh(self.surface_mesh, int(face_id))
            voxel_ids.update(
                voxel_ids_near_triangle_from_samples(
                    sample_points,
                    sample_voxel_ids,
                    triangle_points,
                    float(self.face_selection_tolerance_mm),
                )
            )
        self.selected_voxel_ids = voxel_ids

        assert self.plotter is not None
        self.plotter.subplot(0, 0)
        self.plotter.remove_actor("selected_surface_faces", render=False)
        self.plotter.subplot(0, 1)
        self.plotter.remove_actor("selected_path_voxels", render=False)

        self.plotter.subplot(0, 0)
        selected_surface = make_selected_surface_mesh(self.surface_mesh, self.selected_face_ids)
        if selected_surface.n_cells > 0:
            self.plotter.add_mesh(
                selected_surface,
                color="#ffffff",
                opacity=0.95,
                show_edges=True,
                line_width=2,
                pickable=False,
                name="selected_surface_faces",
            )

        self.plotter.subplot(0, 1)
        selected_path = make_path_polydata(self.flat_segments, self.selected_voxel_ids)
        if selected_path.n_cells > 0:
            self.plotter.add_mesh(
                selected_path,
                color="#ef4444",
                line_width=5,
                opacity=1.0,
                pickable=False,
                name="selected_path_voxels",
            )

        message = (
            f"Selected {len(self.selected_face_ids)} smooth mesh faces -> "
            f"{len(self.selected_voxel_ids)} path voxels. Press A to add."
        )
        self.plotter.add_text(message, position="lower_left", font_size=9, name="status_text")
        self._restore_camera_state(camera_state)
        print(message)

    def clear_selection(self) -> None:
        """Clear current reconstructed-face and path-voxel selection."""
        self.selected_face_ids.clear()
        self.selected_voxel_ids.clear()
        if self.plotter is not None:
            camera_state = self._snapshot_camera_state()
            self.plotter.subplot(0, 0)
            self.plotter.remove_actor("selected_surface_faces", render=False)
            self.plotter.subplot(0, 1)
            self.plotter.remove_actor("selected_path_voxels", render=False)
            self.plotter.add_text(
                "Selection cleared. Click reconstructed surface faces to select voxels.",
                position="lower_left",
                font_size=9,
                name="status_text",
            )
            self._restore_camera_state(camera_state)
        print("Selection cleared.")

    def add_current_assignment(self) -> None:
        """Add selected reconstructed surface faces as one material assignment unit."""
        if not self.selected_face_ids:
            print("No reconstructed surface faces selected.")
            return
        if not self.selected_voxel_ids:
            print("Selected faces found no path voxels. Assignment was not added.")
            return

        voxel_ids = sorted(self.selected_voxel_ids)
        selected_e, start_cum, end_cum = compute_selected_voxel_filament_e(
            self.selection_cache,
            set(voxel_ids),
        )
        layer_range = layers_from_selected_voxels_cached(self.selection_cache, set(voxel_ids))

        face_payload = []
        for face_id in sorted(self.selected_face_ids):
            triangle_points = triangle_points_from_mesh(self.surface_mesh, int(face_id))
            face_payload.append(
                {
                    "surface_face_id": int(face_id),
                    "triangle_points": triangle_points.round(6).tolist(),
                }
            )

        assignment_index = len(self.assignments) + 1
        assignment = {
            "assignment_index": assignment_index,
            "assignment_type": "gcode_reconstructed_surface_face_selection",
            "gcode_path": str(self.gcode_path),
            "surface_face_count": len(self.selected_face_ids),
            "faces": face_payload,
            "voxel_ids": voxel_ids,
            "voxel_count": len(voxel_ids),
            "start_voxel": int(voxel_ids[0]),
            "end_voxel": int(voxel_ids[-1]),
            "selected_e": round(float(selected_e), 6),
            "cumulative_e_before": round(float(start_cum), 6),
            "cumulative_e_after": round(float(end_cum), 6),
            "layer_range": list(layer_range) if layer_range is not None else None,
            "gradient_steps": 1,
            "gradient_direction": "gcode_surface_face",
            "face_selection_tolerance_mm": round(float(self.face_selection_tolerance_mm), 6),
            "eta": 0.5,
        }
        self.assignments.append(assignment)
        print(
            f"Added assignment {assignment_index}: "
            f"{len(self.selected_face_ids)} surface triangles, {len(voxel_ids)} path voxels"
        )

    def save_all_assignments_to_json(self, output_path: Optional[str] = None) -> None:
        """Save all reconstructed-surface assignments."""
        output_path = Path(output_path) if output_path else (
            self.output_dir / "pyvista_gcode_surface_face_property_program.json"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "source_gcode": str(self.gcode_path),
            "voxel_threshold_e": self.voxel_threshold_e,
            "virtual_sample_spacing_mm": self.virtual_sample_spacing_mm,
            "rectangular_region_size_mm": [float(value) for value in self.rectangular_region_size_mm],
            "surface_grid_spacing_mm": round(float(self.surface_grid_spacing_mm), 6),
            "face_selection_tolerance_mm": round(float(self.face_selection_tolerance_mm), 6),
            "surface_triangle_count": int(self.surface_mesh.n_cells),
            "preheat_prime_e": round(float(self.preprint_e), 6),
            "assignment_count": len(self.assignments),
            "assignments": self.assignments,
        }
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        print(f"Saved {len(self.assignments)} assignments: {output_path}")

    def show(self) -> None:
        """Show the PyVista UI."""
        if self.plotter is None:
            self.setup_plotter()
        assert self.plotter is not None
        self.plotter.show()


if __name__ == "__main__":
    project_root = Path(__file__).resolve().parents[2]
    gcode_file = project_root / "input" / "gcode" / "vase.gcode"
    output_dir = project_root / "out" / "ui" / "pyvista_gcode_surface_face_selector"

    if not gcode_file.exists():
        print(f"G-code file not found: {gcode_file}")
        sys.exit(1)

    selector = PyVistaGcodeSurfaceFaceSelector(
        gcode_path=str(gcode_file),
        voxel_threshold_e=0.1,
        output_dir=str(output_dir),
        virtual_sample_spacing_mm=2,
        rectangular_region_size_mm=None,
        surface_grid_spacing_mm=None,
        face_selection_tolerance_mm=None,
    )
    selector.parse()
    selector.setup_plotter()
    selector.show()
