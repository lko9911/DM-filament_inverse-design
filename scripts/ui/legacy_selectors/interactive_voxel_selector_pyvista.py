"""
PyVista-based interactive selector for rectangular region cells and path voxels.

Left view:
    Spatially consistent rectangular region cells covering the printed shape.
Right view:
    Existing printing-sequence path voxels.
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
    from vtkmodules.vtkRenderingCore import vtkActor2D, vtkCoordinate, vtkPolyDataMapper2D
    from vtkmodules.vtkRenderingCore import vtkPointPicker
except ImportError as exc:
    raise SystemExit(
        "PyVista is not installed in this Python environment. "
        "Install pyvista/vtk or run the matplotlib selector instead."
    ) from exc

from interactive_voxel_selector import (
    annotate_voxels_with_layers,
    build_rectangular_region_grid,
    build_virtual_voxel_sample_cache,
    build_voxel_lookup,
    build_voxel_selection_cache,
    compute_selected_voxel_filament_e,
    estimate_rectangular_region_size,
    group_segments_into_voxels,
    layers_from_selected_voxels_cached,
    parse_gcode_extrusion_segments,
)


def make_path_polydata(flat_segments: np.ndarray, voxel_ids: Optional[Set[int]] = None) -> pv.PolyData:
    """Build one PolyData line mesh from flat segment rows."""
    if flat_segments.size == 0:
        return pv.PolyData()

    rows = flat_segments
    if voxel_ids is not None:
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


def make_region_center_polydata(regions: List[Dict]) -> pv.PolyData:
    """Build pickable region-center points."""
    if not regions:
        return pv.PolyData()

    centers = np.empty((len(regions), 3), dtype=float)
    region_ids = np.empty((len(regions),), dtype=int)
    rep_voxel_ids = np.empty((len(regions),), dtype=int)

    for index, region in enumerate(regions):
        bounds = region["box_bounds"]
        centers[index, :] = [
            (float(bounds["x_min"]) + float(bounds["x_max"])) * 0.5,
            (float(bounds["y_min"]) + float(bounds["y_max"])) * 0.5,
            (float(bounds["z_min"]) + float(bounds["z_max"])) * 0.5,
        ]
        voxel_ids = region.get("voxel_ids", [])
        region_ids[index] = int(region["region_id"])
        rep_voxel_ids[index] = int(np.median(voxel_ids)) if voxel_ids else 1

    mesh = pv.PolyData(centers)
    mesh.point_data["region_id"] = region_ids
    mesh.point_data["rep_voxel_id"] = rep_voxel_ids
    if mesh.n_cells == len(region_ids):
        mesh.cell_data["region_id"] = region_ids
        mesh.cell_data["rep_voxel_id"] = rep_voxel_ids
    return mesh


def make_region_box_mesh(
    regions: List[Dict],
    box_size_mm: Tuple[float, float, float],
) -> pv.PolyData:
    """Build filled rectangular cells as a glyph mesh."""
    centers = make_region_center_polydata(regions)
    if centers.n_points == 0:
        return pv.PolyData()

    cube = pv.Cube(
        center=(0.0, 0.0, 0.0),
        x_length=float(box_size_mm[0]),
        y_length=float(box_size_mm[1]),
        z_length=float(box_size_mm[2]),
    )
    glyph = centers.glyph(geom=cube, scale=False, orient=False)
    return glyph


def make_single_box(bounds: Dict[str, float]) -> pv.PolyData:
    """Build a single PyVista box from bounds dict."""
    return pv.Box(
        bounds=(
            float(bounds["x_min"]),
            float(bounds["x_max"]),
            float(bounds["y_min"]),
            float(bounds["y_max"]),
            float(bounds["z_min"]),
            float(bounds["z_max"]),
        )
    )


class PyVistaVoxelRegionSelector:
    """Interactive two-view selector using PyVista/VTK."""

    def __init__(
        self,
        gcode_path: str,
        voxel_threshold_e: float = 0.1,
        output_dir: Optional[str] = None,
        virtual_sample_spacing_mm: float = 0.2,
        rectangular_region_size_mm: Optional[Tuple[float, float, float]] = None,
    ):
        self.gcode_path = Path(gcode_path)
        self.output_dir = Path(output_dir) if output_dir else self.gcode_path.parent
        self.voxel_threshold_e = voxel_threshold_e
        self.virtual_sample_spacing_mm = virtual_sample_spacing_mm
        self.rectangular_region_size_mm = rectangular_region_size_mm

        self.segments: List[Dict] = []
        self.voxels: List[Dict] = []
        self.flat_segments: np.ndarray = np.empty((0, 10), dtype=float)
        self.preprint_e = 0.0

        self.voxel_lookup: Dict[int, Dict] = {}
        self.selection_cache: Dict[str, np.ndarray] = {}
        self.virtual_sample_cache: Dict[str, np.ndarray] = {}
        self.region_grid: List[Dict] = []
        self.region_lookup: Dict[int, Dict] = {}
        self.voxel_to_region: Dict[int, int] = {}

        self.path_mesh = pv.PolyData()
        self.region_mesh = pv.PolyData()
        self.region_centers = pv.PolyData()

        self.plotter: Optional[pv.Plotter] = None
        self.left_renderer = None
        self.right_renderer = None
        self.region_cells_actor = None
        self.region_points_actor = None
        self.path_actor = None
        self.drag_box_actor = None
        self.drag_box_renderer = None
        self.current_region: Optional[Dict] = None
        self.current_regions: List[Dict] = []
        self.selected_voxel_ids: Set[int] = set()
        self.assignments: List[Dict] = []
        self.drag_selection_active = False
        self.drag_selection_mode = "region"
        self.drag_start_pos: Optional[Tuple[int, int]] = None
        self.drag_current_pos: Optional[Tuple[int, int]] = None
        self.drag_camera_state: Dict[str, Dict[str, object]] = {}
        self.drag_observers_registered = False
        self.drag_observer_ids: List[int] = []
        self.visible_pick_bin_px = 24
        self.visible_pick_depth_tolerance = 0.001
        self.visible_pick_zbuffer_tolerance = 0.035
        self.drag_selection_padding_px = 1
        self.voxel_drag_mode = "add"
        self.brush_voxel_ids: Set[int] = set()
        self.brush_last_report_count = 0

    def parse(self) -> None:
        """Parse G-code and build path voxels plus rectangular region cells."""
        print("=" * 60)
        print("PyVista Voxel Region Selector")
        print("=" * 60)

        start = time.time()
        self.segments, self.preprint_e = parse_gcode_extrusion_segments(str(self.gcode_path))
        self.voxels, self.flat_segments = group_segments_into_voxels(
            self.segments,
            self.voxel_threshold_e,
        )
        annotate_voxels_with_layers(self.voxels)

        self.voxel_lookup = build_voxel_lookup(self.voxels)
        self.selection_cache = build_voxel_selection_cache(self.voxels)
        self.virtual_sample_cache = build_virtual_voxel_sample_cache(
            self.voxels,
            self.virtual_sample_spacing_mm,
        )

        if self.rectangular_region_size_mm is None:
            self.rectangular_region_size_mm = estimate_rectangular_region_size(self.voxels)

        self.region_grid, self.region_lookup, self.voxel_to_region = build_rectangular_region_grid(
            self.virtual_sample_cache,
            self.rectangular_region_size_mm,
        )

        self.path_mesh = make_path_polydata(self.flat_segments)
        self.region_centers = make_region_center_polydata(self.region_grid)
        self.region_mesh = make_region_box_mesh(self.region_grid, self.rectangular_region_size_mm)

        print(f"Parsed in {time.time() - start:.2f}s")
        print(f"  Segments: {len(self.segments):,}")
        print(f"  Path voxels: {len(self.voxels):,} @ E {self.voxel_threshold_e:.3f}")
        print(
            f"  Region cells: {len(self.region_grid):,} @ "
            f"{self.rectangular_region_size_mm[0]:.3f} x "
            f"{self.rectangular_region_size_mm[1]:.3f} x "
            f"{self.rectangular_region_size_mm[2]:.3f} mm"
        )
        print(f"  Virtual samples: {len(self.virtual_sample_cache['voxel_ids']):,}")

    def setup_plotter(self) -> None:
        """Create the two-panel PyVista interface."""
        self.plotter = pv.Plotter(shape=(1, 2), window_size=(1800, 900))

        self.plotter.subplot(0, 0)
        self.left_renderer = self.plotter.renderer
        self.plotter.add_text("Box-region view", font_size=12, name="left_title")
        self.region_cells_actor = self.plotter.add_mesh(
            self.region_mesh,
            scalars="rep_voxel_id" if "rep_voxel_id" in self.region_mesh.point_data else None,
            cmap="viridis",
            opacity=0.78,
            show_edges=False,
            pickable=False,
            name="region_cells",
        )
        self.region_points_actor = self.plotter.add_mesh(
            self.region_centers,
            scalars="rep_voxel_id",
            cmap="viridis",
            point_size=7,
            render_points_as_spheres=True,
            opacity=0.55,
            pickable=True,
            name="region_pick_points",
        )
        self.plotter.add_axes()
        self.plotter.reset_camera()

        self.plotter.subplot(0, 1)
        self.right_renderer = self.plotter.renderer
        self.plotter.add_text("Path voxel view", font_size=12, name="right_title")
        self.path_actor = self.plotter.add_mesh(
            self.path_mesh,
            scalars="voxel_id",
            cmap="viridis",
            line_width=1.0,
            opacity=0.45,
            pickable=True,
            name="path_voxels",
        )
        self.plotter.add_axes()
        self.plotter.reset_camera()

        self.plotter.link_views()
        self.plotter.add_text(
            "Click regions to toggle multi-select. B: box drag, V: add voxels, X: remove voxels, A: add, W: save, C: clear",
            position="lower_left",
            font_size=9,
            name="status_text",
        )

        self.enable_click_region_selection()
        self.plotter.add_key_event("a", self.add_current_region_assignment)
        self.plotter.add_key_event("w", self.save_all_assignments_to_json)
        self.plotter.add_key_event("c", self.clear_selection)
        self.plotter.add_key_event("b", self.enable_drag_region_selection)
        self.plotter.add_key_event("B", self.enable_drag_region_selection)
        self.plotter.add_key_event("v", self.enable_drag_voxel_selection)
        self.plotter.add_key_event("V", self.enable_drag_voxel_selection)
        self.plotter.add_key_event("x", self.enable_remove_voxel_selection)
        self.plotter.add_key_event("X", self.enable_remove_voxel_selection)
        self._register_drag_observers()

    def enable_click_region_selection(self, restore_state: Optional[Dict[str, Dict[str, object]]] = None) -> None:
        """Enable normal left-click point picking."""
        if self.plotter is None:
            return
        camera_state = restore_state if restore_state is not None else self._snapshot_camera_state()
        self.drag_selection_active = False
        self.drag_selection_mode = "region"
        self.drag_start_pos = None
        self.drag_current_pos = None
        self.brush_voxel_ids.clear()
        self.brush_last_report_count = 0
        self._clear_drag_box_actor(render=False)
        self._set_drag_pickability(False)
        try:
            self.plotter.disable_picking()
        except Exception:
            pass
        try:
            self.plotter.enable_trackball_style()
        except Exception:
            try:
                self.plotter.iren.enable_trackball_style()
            except Exception:
                pass
        self.plotter.enable_point_picking(
            callback=self._on_pick,
            left_clicking=True,
            use_picker=True,
            show_point=False,
            show_message=False,
            picker="point",
            tolerance=0.02,
        )
        self._restore_camera_state(camera_state)
        if restore_state is not None:
            self.drag_camera_state = {}

    def _set_drag_pickability(self, enabled: bool) -> None:
        """Restrict rectangle dragging to the region-center actor."""
        if self.region_points_actor is not None:
            self.region_points_actor.SetPickable(True)
        if self.path_actor is not None:
            self.path_actor.SetPickable(True)
        if self.region_cells_actor is not None:
            self.region_cells_actor.SetPickable(False)

    def _snapshot_camera_state(self) -> Dict[str, Dict[str, object]]:
        """Capture camera state for both views before changing interaction modes."""
        state: Dict[str, Dict[str, object]] = {}
        renderers = {
            "left": self.left_renderer,
            "right": self.right_renderer,
        }
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
        """Restore camera state after PyVista/VTK interaction style changes."""
        if not state:
            return
        renderers = {
            "left": self.left_renderer,
            "right": self.right_renderer,
        }
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
        camera_state = self._snapshot_camera_state()
        self.drag_camera_state = camera_state
        try:
            self.plotter.disable_picking()
        except Exception:
            pass
        try:
            self.plotter.iren.interactor.SetInteractorStyle(vtkInteractorStyleUser())
        except Exception:
            pass
        self._restore_camera_state(camera_state)

    def _return_to_click_mode_after_drag(self) -> None:
        """Return to click mode while preserving the camera from drag start."""
        camera_state = self.drag_camera_state or self._snapshot_camera_state()
        self.enable_click_region_selection(restore_state=camera_state)

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

    def _drag_renderer(self):
        """Return the renderer where the active drag rectangle should be drawn."""
        return self.right_renderer if self.drag_selection_mode == "voxel" else self.left_renderer

    def _draw_drag_box_actor(self, start_pos: Tuple[int, int], end_pos: Tuple[int, int]) -> None:
        """Draw a 2D rectangle whose display coordinates match selection logic."""
        renderer = self._drag_renderer()
        if renderer is None or self.plotter is None:
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

        renderer.AddActor2D(actor)
        self.drag_box_actor = actor
        self.drag_box_renderer = renderer
        self.plotter.render()

    def _register_drag_observers(self) -> None:
        """Register lightweight mouse observers for custom rectangle selection."""
        if self.plotter is None or self.drag_observers_registered:
            return
        interactor = getattr(self.plotter.iren, "interactor", None)
        if interactor is not None and hasattr(interactor, "AddObserver"):
            press_id = interactor.AddObserver("LeftButtonPressEvent", self._on_left_button_press, 1.0)
            release_id = interactor.AddObserver("LeftButtonReleaseEvent", self._on_left_button_release, 1.0)
            move_id = interactor.AddObserver("MouseMoveEvent", self._on_mouse_move, 1.0)
            self.drag_observer_ids = [int(press_id), int(release_id), int(move_id)]
        else:
            press_id = self.plotter.iren.add_observer("LeftButtonPressEvent", self._on_left_button_press)
            release_id = self.plotter.iren.add_observer("LeftButtonReleaseEvent", self._on_left_button_release)
            move_id = self.plotter.iren.add_observer("MouseMoveEvent", self._on_mouse_move)
            self.drag_observer_ids = [int(press_id), int(release_id), int(move_id)]
        self.drag_observers_registered = True

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

    def _position_in_left_view(self, pos: Tuple[int, int]) -> bool:
        """Check whether a display coordinate is inside the left renderer viewport."""
        return self._position_in_renderer_view(pos, self.left_renderer)

    def _position_in_right_view(self, pos: Tuple[int, int]) -> bool:
        """Check whether a display coordinate is inside the right renderer viewport."""
        return self._position_in_renderer_view(pos, self.right_renderer)

    def _position_in_renderer_view(self, pos: Tuple[int, int], renderer) -> bool:
        """Check whether a display coordinate is inside a renderer viewport."""
        if self.plotter is None or renderer is None:
            return False
        width, height = self.plotter.window_size
        vx0, vy0, vx1, vy1 = renderer.GetViewport()
        x, y = pos
        x_inside = (vx0 * width) <= x <= (vx1 * width)
        y_inside = any((vy0 * height) <= candidate <= (vy1 * height) for candidate in self._display_y_candidates(y))
        return x_inside and y_inside

    def _project_region_centers_to_display(self) -> np.ndarray:
        """Project region centers into the left renderer display coordinate system."""
        points = np.asarray(self.region_centers.points)
        region_ids = np.asarray(self.region_centers.point_data["region_id"], dtype=int)
        projected = np.empty((points.shape[0], 4), dtype=float)

        for index, point in enumerate(points):
            self.left_renderer.SetWorldPoint(float(point[0]), float(point[1]), float(point[2]), 1.0)
            self.left_renderer.WorldToDisplay()
            display_x, display_y, display_z = self.left_renderer.GetDisplayPoint()
            projected[index, :] = [display_x, display_y, display_z, float(region_ids[index])]

        return projected

    def _project_region_footprints_to_display(self) -> np.ndarray:
        """Project each region's screen-space footprint plus center depth."""
        if self.left_renderer is None or not self.region_grid:
            return np.empty((0, 7), dtype=float)

        projected = np.empty((len(self.region_grid), 8), dtype=float)

        for index, region in enumerate(self.region_grid):
            bounds = region["box_bounds"]
            xs = (float(bounds["x_min"]), float(bounds["x_max"]))
            ys = (float(bounds["y_min"]), float(bounds["y_max"]))
            zs = (float(bounds["z_min"]), float(bounds["z_max"]))
            display_points = []

            for world_x in xs:
                for world_y in ys:
                    for world_z in zs:
                        self.left_renderer.SetWorldPoint(world_x, world_y, world_z, 1.0)
                        self.left_renderer.WorldToDisplay()
                        display_points.append(self.left_renderer.GetDisplayPoint())

            display_array = np.array(display_points, dtype=float)
            center_x = (float(bounds["x_min"]) + float(bounds["x_max"])) * 0.5
            center_y = (float(bounds["y_min"]) + float(bounds["y_max"])) * 0.5
            center_z = (float(bounds["z_min"]) + float(bounds["z_max"])) * 0.5
            self.left_renderer.SetWorldPoint(center_x, center_y, center_z, 1.0)
            self.left_renderer.WorldToDisplay()
            display_center_x, display_center_y, display_center_z = self.left_renderer.GetDisplayPoint()

            projected[index, :] = [
                float(np.min(display_array[:, 0])),
                float(np.max(display_array[:, 0])),
                float(np.min(display_array[:, 1])),
                float(np.max(display_array[:, 1])),
                float(display_center_x),
                float(display_center_y),
                float(display_center_z),
                float(np.min(display_array[:, 2])),
            ]

        return projected

    def _project_path_segment_samples_to_display(self) -> np.ndarray:
        """Project path segment endpoints and midpoints into the right renderer display system."""
        if self.right_renderer is None or self.flat_segments.size == 0:
            return np.empty((0, 6), dtype=float)

        rows = self.flat_segments
        starts = rows[:, 3:6]
        ends = rows[:, 6:9]
        midpoints = (starts + ends) * 0.5
        voxel_ids = rows[:, 0].astype(int)
        projected = np.empty((rows.shape[0] * 3, 6), dtype=float)
        sample_sets = ((starts, 0), (midpoints, 1), (ends, 2))

        out_index = 0
        for samples, sample_index in sample_sets:
            for segment_index, point in enumerate(samples):
                self.right_renderer.SetWorldPoint(float(point[0]), float(point[1]), float(point[2]), 1.0)
                self.right_renderer.WorldToDisplay()
                display_x, display_y, display_z = self.right_renderer.GetDisplayPoint()
                projected[out_index, :] = [
                    display_x,
                    display_y,
                    display_z,
                    float(voxel_ids[segment_index]),
                    float(segment_index),
                    float(sample_index),
                ]
                out_index += 1

        return projected

    def _path_segments_from_projected_samples(self, projected: np.ndarray) -> Dict[int, Dict[str, object]]:
        """Group projected endpoint and midpoint samples by source path segment."""
        segments: Dict[int, Dict[str, object]] = {}
        for display_x, display_y, display_z, voxel_id, segment_index, sample_index in projected:
            segment_key = int(segment_index)
            segment = segments.setdefault(
                segment_key,
                {
                    "voxel_id": int(voxel_id),
                    "samples": [],
                },
            )
            segment["samples"].append((float(display_x), float(display_y), float(display_z), int(sample_index)))
        return segments

    def _display_segment_hits_rectangle(
        self,
        samples: List[Tuple[float, float, float, int]],
        x0: int,
        x1: int,
        y0: int,
        y1: int,
    ) -> bool:
        """Return True when projected segment samples or their screen-space span hit the rectangle."""
        if not samples:
            return False

        for display_x, display_y, _display_z, _sample_index in samples:
            if x0 <= display_x <= x1 and y0 <= display_y <= y1:
                return True

        ordered = sorted(samples, key=lambda sample: sample[3])
        points = [(sample[0], sample[1]) for sample in ordered]
        for first, second in zip(points, points[1:]):
            if self._display_line_intersects_rectangle(first, second, x0, x1, y0, y1):
                return True

        return False

    def _display_line_intersects_rectangle(
        self,
        first: Tuple[float, float],
        second: Tuple[float, float],
        x0: int,
        x1: int,
        y0: int,
        y1: int,
    ) -> bool:
        """Return True when a 2D display line crosses the drag rectangle."""
        rect_edges = (
            ((x0, y0), (x1, y0)),
            ((x1, y0), (x1, y1)),
            ((x1, y1), (x0, y1)),
            ((x0, y1), (x0, y0)),
        )
        return any(self._display_lines_intersect(first, second, edge_start, edge_end) for edge_start, edge_end in rect_edges)

    def _display_lines_intersect(
        self,
        a: Tuple[float, float],
        b: Tuple[float, float],
        c: Tuple[float, float],
        d: Tuple[float, float],
    ) -> bool:
        """Return True when two 2D display line segments intersect."""
        def orientation(p, q, r) -> float:
            return (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1])

        def on_segment(p, q, r) -> bool:
            return (
                min(p[0], r[0]) <= q[0] <= max(p[0], r[0])
                and min(p[1], r[1]) <= q[1] <= max(p[1], r[1])
            )

        o1 = orientation(a, b, c)
        o2 = orientation(a, b, d)
        o3 = orientation(c, d, a)
        o4 = orientation(c, d, b)
        eps = 1e-9

        if o1 * o2 < 0 and o3 * o4 < 0:
            return True
        if abs(o1) <= eps and on_segment(a, c, b):
            return True
        if abs(o2) <= eps and on_segment(a, d, b):
            return True
        if abs(o3) <= eps and on_segment(c, a, d):
            return True
        if abs(o4) <= eps and on_segment(c, b, d):
            return True
        return False

    def _nearest_visible_depth_by_bin(self, projected: np.ndarray) -> Dict[Tuple[int, int], float]:
        """Build a coarse screen-space depth map for visible-surface drag selection."""
        nearest_depth: Dict[Tuple[int, int], float] = {}
        bin_size = max(1, int(self.visible_pick_bin_px))

        for display_x, display_y, display_z, *_rest in projected:
            key = (int(display_x // bin_size), int(display_y // bin_size))
            old_depth = nearest_depth.get(key)
            if old_depth is None or display_z < old_depth:
                nearest_depth[key] = float(display_z)

        return nearest_depth

    def _is_visible_projected_region(
        self,
        display_x: float,
        display_y: float,
        display_z: float,
        nearest_depth: Dict[Tuple[int, int], float],
    ) -> bool:
        """Return True when the projected region is on the camera-facing surface."""
        if not self._is_near_visible_zbuffer(display_x, display_y, display_z, self.left_renderer):
            return False

        bin_size = max(1, int(self.visible_pick_bin_px))
        bx = int(display_x // bin_size)
        by = int(display_y // bin_size)
        best_depth = float("inf")

        for offset_x in (-1, 0, 1):
            for offset_y in (-1, 0, 1):
                depth = nearest_depth.get((bx + offset_x, by + offset_y))
                if depth is not None and depth < best_depth:
                    best_depth = depth

        return display_z <= best_depth + float(self.visible_pick_depth_tolerance)

    def _is_near_visible_zbuffer(
        self,
        display_x: float,
        display_y: float,
        display_z: float,
        renderer,
    ) -> bool:
        """Use the rendered z-buffer to reject regions hidden behind the front surface."""
        if renderer is None or self.plotter is None or not hasattr(renderer, "GetZ"):
            return True

        x = int(round(display_x))
        y = int(round(display_y))
        candidates = [(x, y)]
        flipped_y = self._display_y_candidates(y)[1]
        if flipped_y != y:
            candidates.append((x, flipped_y))

        best_z = float("inf")
        for candidate_x, candidate_y in candidates:
            try:
                z_value = float(renderer.GetZ(candidate_x, candidate_y))
            except Exception:
                continue
            if 0.0 <= z_value < 1.0:
                best_z = min(best_z, z_value)

        if best_z == float("inf"):
            return True

        return float(display_z) <= best_z + float(self.visible_pick_zbuffer_tolerance)

    def _regions_from_display_rectangle(
        self,
        start_pos: Tuple[int, int],
        end_pos: Tuple[int, int],
    ) -> List[Dict]:
        """Select visible region centers whose display coordinates fall inside a drag box."""
        if self.left_renderer is None or self.region_centers.n_points == 0:
            return []

        x0, x1 = sorted((int(start_pos[0]), int(end_pos[0])))
        y0, y1 = sorted((int(start_pos[1]), int(end_pos[1])))
        if abs(x1 - x0) < 3 or abs(y1 - y0) < 3:
            return []

        center_projected = self._project_region_centers_to_display()
        footprint_projected = self._project_region_footprints_to_display()
        nearest_depth = self._nearest_visible_depth_by_bin(center_projected)
        selected_region_ids = self._region_ids_from_projected_rectangle(
            footprint_projected,
            nearest_depth,
            x0,
            x1,
            y0,
            y1,
        )

        if not selected_region_ids and self.plotter is not None:
            height = int(self.plotter.window_size[1])
            flipped_y0, flipped_y1 = sorted((height - y0, height - y1))
            selected_region_ids = self._region_ids_from_projected_rectangle(
                footprint_projected,
                nearest_depth,
                x0,
                x1,
                flipped_y0,
                flipped_y1,
            )

        print(
            f"Region drag box x={x0}-{x1}, y={y0}-{y1}: "
            f"{len(selected_region_ids)} visible regions"
        )
        return [
            self.region_lookup[region_id]
            for region_id in selected_region_ids
            if region_id in self.region_lookup
        ]

    def _region_ids_from_projected_rectangle(
        self,
        projected: np.ndarray,
        nearest_depth: Dict[Tuple[int, int], float],
        x0: int,
        x1: int,
        y0: int,
        y1: int,
    ) -> List[int]:
        """Return visible region ids whose projected footprint intersects a drag rectangle."""
        selected_region_ids: List[int] = []
        overlap_count = 0
        padding = int(self.drag_selection_padding_px)
        padded_x0 = x0 - padding
        padded_x1 = x1 + padding
        padded_y0 = y0 - padding
        padded_y1 = y1 + padding

        for index, (
            footprint_x0,
            footprint_x1,
            footprint_y0,
            footprint_y1,
            display_x,
            display_y,
            _display_center_z,
            display_front_z,
        ) in enumerate(projected, start=1):
            overlaps_x = footprint_x1 >= padded_x0 and footprint_x0 <= padded_x1
            overlaps_y = footprint_y1 >= padded_y0 and footprint_y0 <= padded_y1
            if not (overlaps_x and overlaps_y):
                continue
            overlap_count += 1
            if self._is_visible_projected_region(display_x, display_y, display_front_z, nearest_depth):
                selected_region_ids.append(int(index))
        print(f"Region drag overlap={overlap_count}, visible={len(selected_region_ids)}")
        return selected_region_ids

    def _voxel_ids_from_display_rectangle(
        self,
        start_pos: Tuple[int, int],
        end_pos: Tuple[int, int],
    ) -> Set[int]:
        """Select path voxels whose projected segment span falls inside a drag box."""
        if self.right_renderer is None or self.flat_segments.size == 0:
            return set()

        x0, x1 = sorted((int(start_pos[0]), int(end_pos[0])))
        y0, y1 = sorted((int(start_pos[1]), int(end_pos[1])))
        if abs(x1 - x0) < 3 or abs(y1 - y0) < 3:
            return set()

        projected = self._project_path_segment_samples_to_display()
        voxel_ids: Set[int] = set()

        for segment in self._path_segments_from_projected_samples(projected).values():
            samples = segment["samples"]
            if not self._display_segment_hits_rectangle(samples, x0, x1, y0, y1):
                continue
            voxel_ids.add(int(segment["voxel_id"]))

        return voxel_ids

    def _voxel_id_at_display_position(self, pos: Tuple[int, int]) -> Optional[int]:
        """Pick the nearest path voxel at a display position in the right view."""
        if self.right_renderer is None:
            return None

        picker = vtkPointPicker()
        picker.SetTolerance(0.06)

        candidates = [pos]
        flipped_y = self._display_y_candidates(pos[1])[1]
        if flipped_y != pos[1]:
            candidates.append((pos[0], flipped_y))

        for x, y in candidates:
            picked = picker.Pick(float(x), float(y), 0.0, self.right_renderer)
            if not picked:
                continue
            dataset = picker.GetDataSet()
            point_id = picker.GetPointId()
            if dataset is None or point_id < 0:
                continue
            mesh = pv.wrap(dataset)
            if "voxel_id" in mesh.point_data:
                return int(mesh.point_data["voxel_id"][point_id])

        return None

    def _brush_voxel_at_position(self, pos: Tuple[int, int]) -> None:
        """Collect one voxel under the brush cursor."""
        if not self._position_in_right_view(pos):
            return
        voxel_id = self._voxel_id_at_display_position(pos)
        if voxel_id is not None:
            self.brush_voxel_ids.add(int(voxel_id))
            count = len(self.brush_voxel_ids)
            if count != self.brush_last_report_count and (count == 1 or count % 25 == 0):
                self.brush_last_report_count = count
                print(f"Voxel brush collected {count} path voxels. Press V/X again or release to apply.")

    def _on_left_button_press(self, interactor, event_name) -> None:
        """Remember drag start position when rectangle mode is active."""
        if not self.drag_selection_active:
            return
        pos = self._event_position(interactor)
        if self.drag_selection_mode == "voxel":
            in_target_view = self._position_in_right_view(pos)
        else:
            in_target_view = self._position_in_left_view(pos)
        if not in_target_view:
            self.drag_start_pos = None
            self.drag_current_pos = None
            self._clear_drag_box_actor(render=True)
            return
        self.drag_start_pos = pos
        self.drag_current_pos = pos
        self._draw_drag_box_actor(pos, pos)
        if self.drag_selection_mode == "voxel":
            self.brush_voxel_ids.clear()
            self._brush_voxel_at_position(pos)

    def _on_mouse_move(self, interactor, event_name) -> None:
        """Collect path voxels while brushing in voxel selection mode."""
        if not self.drag_selection_active:
            return
        if self.drag_start_pos is None:
            return
        pos = self._event_position(interactor)
        self.drag_current_pos = pos
        self._draw_drag_box_actor(self.drag_start_pos, pos)
        if self.drag_selection_mode == "voxel":
            self._brush_voxel_at_position(pos)

    def _on_left_button_release(self, interactor, event_name) -> None:
        """Complete custom rectangle selection on mouse release."""
        if not self.drag_selection_active:
            return
        end_pos = self._event_position(interactor)
        start_pos = self.drag_start_pos
        self.drag_current_pos = end_pos
        self.drag_start_pos = None
        self._clear_drag_box_actor(render=False)

        if self.drag_selection_mode == "voxel":
            end_in_target_view = self._position_in_right_view(end_pos) or bool(self.brush_voxel_ids)
        else:
            end_in_target_view = self._position_in_left_view(end_pos)

        if start_pos is None or not end_in_target_view:
            print("Drag selection canceled. Start and end the drag in the active view.")
            self._return_to_click_mode_after_drag()
            return

        if self.drag_selection_mode == "voxel":
            self._brush_voxel_at_position(end_pos)
            voxel_ids = set(self.brush_voxel_ids)
            if not voxel_ids:
                voxel_ids = self._voxel_ids_from_display_rectangle(start_pos, end_pos)
            if not voxel_ids:
                print("Drag selection found no path voxels.")
                self._return_to_click_mode_after_drag()
                return
            self.select_voxels(voxel_ids, operation=self.voxel_drag_mode)
            self.brush_voxel_ids.clear()
            self.brush_last_report_count = 0
        else:
            regions = self._regions_from_display_rectangle(start_pos, end_pos)
            if not regions:
                print("Drag selection found no regions.")
                self._return_to_click_mode_after_drag()
                return
            self.select_regions(regions)

        self._return_to_click_mode_after_drag()

    def _cancel_drag_selection(self) -> None:
        """Cancel the current drag mode and return to normal picking."""
        if self.plotter is not None:
            print("Drag selection canceled/reset.")
        self._clear_drag_box_actor(render=False)
        self._return_to_click_mode_after_drag()

    def _finish_region_drag_selection(self) -> bool:
        """Apply a pending region drag without relying on mouse release."""
        if self.drag_selection_mode != "region":
            return False
        if self.drag_start_pos is None or self.drag_current_pos is None:
            self._cancel_drag_selection()
            return False

        regions = self._regions_from_display_rectangle(self.drag_start_pos, self.drag_current_pos)
        if not regions:
            print("Region drag found no regions.")
            self._cancel_drag_selection()
            return False

        self.select_regions(regions)
        self._return_to_click_mode_after_drag()
        return True

    def _finish_voxel_brush_selection(self) -> bool:
        """Apply collected voxel brush ids without relying on mouse release."""
        if self.drag_selection_mode != "voxel":
            return False
        voxel_ids = set(self.brush_voxel_ids)
        if not voxel_ids:
            self._cancel_drag_selection()
            return False
        self.select_voxels(voxel_ids, operation=self.voxel_drag_mode)
        self._return_to_click_mode_after_drag()
        return True

    def _voxel_id_from_picker(self, picker) -> Optional[int]:
        """Resolve a picked path point/cell to a voxel id."""
        dataset = picker.GetDataSet() if picker is not None and hasattr(picker, "GetDataSet") else None
        point_id = picker.GetPointId() if picker is not None and hasattr(picker, "GetPointId") else -1
        cell_id = picker.GetCellId() if picker is not None and hasattr(picker, "GetCellId") else -1

        if dataset is not None and point_id >= 0:
            mesh = pv.wrap(dataset)
            if "voxel_id" in mesh.point_data:
                return int(mesh.point_data["voxel_id"][point_id])

        if dataset is not None and cell_id >= 0:
            mesh = pv.wrap(dataset)
            if "voxel_id" in mesh.cell_data:
                return int(mesh.cell_data["voxel_id"][cell_id])

        return None

    def _region_from_picker(self, picked_point, picker) -> Optional[Dict]:
        """Resolve a picked point to a rectangular region."""
        dataset = picker.GetDataSet() if picker is not None and hasattr(picker, "GetDataSet") else None
        point_id = picker.GetPointId() if picker is not None and hasattr(picker, "GetPointId") else -1

        if dataset is not None and point_id >= 0:
            mesh = pv.wrap(dataset)
            if "region_id" in mesh.point_data:
                region_id = int(mesh.point_data["region_id"][point_id])
                return self.region_lookup.get(region_id)

        if picked_point is None or self.region_centers.n_points == 0:
            return None

        point = np.array(picked_point, dtype=float)
        centers = np.asarray(self.region_centers.points)
        nearest_index = int(np.argmin(np.linalg.norm(centers - point, axis=1)))
        region_id = int(self.region_centers.point_data["region_id"][nearest_index])
        return self.region_lookup.get(region_id)

    def _on_pick(self, picked_point, picker=None) -> None:
        """Pick callback for both panels."""
        voxel_id = self._voxel_id_from_picker(picker)
        if voxel_id is not None:
            operation = "add" if self.selected_voxel_ids else "replace"
            self.select_voxels({voxel_id}, operation=operation)
            return

        region = self._region_from_picker(picked_point, picker)
        if region is None:
            return
        self.toggle_region_selection(region)

    def _combined_region(self, regions: List[Dict], voxel_ids: Set[int]) -> Dict:
        """Build a combined region payload from one or more grid regions."""
        bounds_list = [region["box_bounds"] for region in regions]
        return {
            "region_id": int(regions[0]["region_id"]) if len(regions) == 1 else None,
            "region_ids": [int(region["region_id"]) for region in regions],
            "region_type": "grid_box" if len(regions) == 1 else "grid_box_group",
            "grid_index": list(regions[0].get("grid_index", [])) if len(regions) == 1 else None,
            "box_bounds": {
                "x_min": float(min(bounds["x_min"] for bounds in bounds_list)),
                "x_max": float(max(bounds["x_max"] for bounds in bounds_list)),
                "y_min": float(min(bounds["y_min"] for bounds in bounds_list)),
                "y_max": float(max(bounds["y_max"] for bounds in bounds_list)),
                "z_min": float(min(bounds["z_min"] for bounds in bounds_list)),
                "z_max": float(max(bounds["z_max"] for bounds in bounds_list)),
            },
            "voxel_ids": sorted(int(voxel_id) for voxel_id in voxel_ids),
            "voxel_count": len(voxel_ids),
        }

    def _bounds_from_voxel_ids(self, voxel_ids: Set[int]) -> Dict[str, float]:
        """Compute a bounding box around selected path voxels."""
        if not voxel_ids or self.flat_segments.size == 0:
            return {
                "x_min": 0.0,
                "x_max": 0.0,
                "y_min": 0.0,
                "y_max": 0.0,
                "z_min": 0.0,
                "z_max": 0.0,
            }

        rows = self.flat_segments[np.isin(self.flat_segments[:, 0].astype(int), np.array(sorted(voxel_ids), dtype=int))]
        if rows.size == 0:
            return {
                "x_min": 0.0,
                "x_max": 0.0,
                "y_min": 0.0,
                "y_max": 0.0,
                "z_min": 0.0,
                "z_max": 0.0,
            }

        points = np.vstack((rows[:, 3:6], rows[:, 6:9]))
        return {
            "x_min": float(np.min(points[:, 0])),
            "x_max": float(np.max(points[:, 0])),
            "y_min": float(np.min(points[:, 1])),
            "y_max": float(np.max(points[:, 1])),
            "z_min": float(np.min(points[:, 2])),
            "z_max": float(np.max(points[:, 2])),
        }

    def toggle_region_selection(self, region: Dict) -> None:
        """Add or remove one clicked region from the current region selection."""
        region_id = int(region["region_id"])
        selected_by_id = {
            int(existing_region["region_id"]): existing_region
            for existing_region in self.current_regions
        }

        if region_id in selected_by_id:
            selected_by_id.pop(region_id)
            action = "Removed"
        else:
            selected_by_id[region_id] = region
            action = "Added"

        if not selected_by_id:
            self.clear_selection()
            print(f"Removed R{region_id}; no regions selected.")
            return

        regions = [
            selected_by_id[key]
            for key in sorted(selected_by_id)
        ]
        self.select_regions(regions, action_label=f"{action} R{region_id}")

    def select_regions(self, regions: List[Dict], action_label: Optional[str] = None) -> None:
        """Select one or more rectangular regions and highlight their path voxels."""
        if not regions:
            return

        self.current_regions = regions
        voxel_ids: Set[int] = set()
        for region in regions:
            voxel_ids.update(int(voxel_id) for voxel_id in region.get("voxel_ids", []))

        self.selected_voxel_ids = voxel_ids
        self.current_region = self._combined_region(regions, voxel_ids)
        selected_path = make_path_polydata(self.flat_segments, self.selected_voxel_ids)

        assert self.plotter is not None

        self.plotter.subplot(0, 0)
        self.plotter.remove_actor("selected_region", render=False)
        selected_boxes = make_region_box_mesh(regions, self.rectangular_region_size_mm)
        if selected_boxes.n_cells > 0:
            self.plotter.add_mesh(
                selected_boxes,
                color="#ef4444",
                opacity=0.95,
                show_edges=False,
                pickable=False,
                name="selected_region",
                reset_camera=False,
            )

        self.plotter.subplot(0, 1)
        self.plotter.remove_actor("selected_path_voxels", render=False)
        if selected_path.n_cells > 0:
            self.plotter.add_mesh(
                selected_path,
                color="#ef4444",
                line_width=5,
                opacity=1.0,
                pickable=False,
                name="selected_path_voxels",
                reset_camera=False,
            )

        low = min(self.selected_voxel_ids) if self.selected_voxel_ids else 0
        high = max(self.selected_voxel_ids) if self.selected_voxel_ids else 0
        region_label = (
            f"R{regions[0]['region_id']}"
            if len(regions) == 1
            else f"{len(regions)} regions"
        )
        prefix = action_label if action_label is not None else f"Selected {region_label}"
        message = (
            f"{prefix}: {len(self.selected_voxel_ids)} path voxels "
            f"across {region_label} "
            f"(V{low}-{high}). Press A to add."
        )
        self.plotter.add_text(message, position="lower_left", font_size=9, name="status_text")
        self.plotter.render()
        print(message)

    def select_voxels(self, voxel_ids: Set[int], operation: str = "replace") -> None:
        """Select, add, or remove path voxels directly and highlight them in the path view."""
        if not voxel_ids:
            return

        incoming_voxel_ids = set(int(voxel_id) for voxel_id in voxel_ids)
        if operation == "add" and self.selected_voxel_ids:
            self.selected_voxel_ids.update(incoming_voxel_ids)
        elif operation == "remove":
            self.selected_voxel_ids.difference_update(incoming_voxel_ids)
        else:
            self.current_regions = []
            self.selected_voxel_ids = incoming_voxel_ids

        if not self.selected_voxel_ids:
            self.current_region = None
            self.clear_selection()
            return

        if self.current_regions:
            self.current_region = self._combined_region(self.current_regions, self.selected_voxel_ids)
            self.current_region["region_type"] = "hybrid_region_voxel_group"
        else:
            self.current_region = {
                "region_id": None,
                "region_ids": [],
                "region_type": "path_voxel_group",
                "grid_index": None,
                "box_bounds": self._bounds_from_voxel_ids(self.selected_voxel_ids),
                "voxel_ids": sorted(self.selected_voxel_ids),
                "voxel_count": len(self.selected_voxel_ids),
            }
        selected_path = make_path_polydata(self.flat_segments, self.selected_voxel_ids)

        assert self.plotter is not None

        if not self.current_regions:
            self.plotter.subplot(0, 0)
            self.plotter.remove_actor("selected_region", render=False)

        self.plotter.subplot(0, 1)
        self.plotter.remove_actor("selected_path_voxels", render=False)
        if selected_path.n_cells > 0:
            self.plotter.add_mesh(
                selected_path,
                color="#ef4444",
                line_width=5,
                opacity=1.0,
                pickable=False,
                name="selected_path_voxels",
                reset_camera=False,
            )

        low = min(self.selected_voxel_ids)
        high = max(self.selected_voxel_ids)
        if operation == "add":
            action = f"Added {len(incoming_voxel_ids)} path voxels"
        elif operation == "remove":
            action = f"Removed {len(incoming_voxel_ids)} path voxels"
        else:
            action = f"Selected {len(self.selected_voxel_ids)} path voxels directly"
        message = (
            f"{action}; current selection has {len(self.selected_voxel_ids)} path voxels "
            f"(V{low}-{high}). Press A to add."
        )
        self.plotter.add_text(message, position="lower_left", font_size=9, name="status_text")
        self.plotter.render()
        print(message)

    def enable_drag_region_selection(self) -> None:
        """Enable rectangle drag selection mode."""
        if self.plotter is None:
            return
        if self.drag_selection_active:
            if self.drag_selection_mode == "region":
                self._finish_region_drag_selection()
                return
            self._cancel_drag_selection()
        self.drag_selection_active = True
        self.drag_selection_mode = "region"
        self.drag_start_pos = None
        self.drag_current_pos = None
        self._set_drag_pickability(True)
        self._enable_drag_interactor_style()
        self.plotter.add_text(
            "Hold left mouse and drag over the left box-region view. Release or press B again to select.",
            position="lower_left",
            font_size=9,
            name="status_text",
        )
        print("Region drag selection enabled. Hold left mouse and drag over the left view.")

    def enable_drag_voxel_selection(self) -> None:
        """Enable path voxel drag selection mode."""
        if self.plotter is None:
            return
        if self.drag_selection_active:
            if self.drag_selection_mode == "voxel" and self.voxel_drag_mode == "add":
                self._finish_voxel_brush_selection()
                return
            self._cancel_drag_selection()
        self.drag_selection_active = True
        self.drag_selection_mode = "voxel"
        self.voxel_drag_mode = "add"
        self.drag_start_pos = None
        self.brush_voxel_ids.clear()
        self.brush_last_report_count = 0
        self._set_drag_pickability(True)
        self._enable_drag_interactor_style()
        self.plotter.add_text(
            "Hold left mouse and drag over path voxels in the right view. Release or press V again to add.",
            position="lower_left",
            font_size=9,
            name="status_text",
        )
        print("Path voxel brush-add enabled. Hold left mouse and drag over the right view.")

    def enable_remove_voxel_selection(self) -> None:
        """Enable path voxel removal drag mode."""
        if self.plotter is None:
            return
        if self.drag_selection_active:
            if self.drag_selection_mode == "voxel" and self.voxel_drag_mode == "remove":
                self._finish_voxel_brush_selection()
                return
            self._cancel_drag_selection()
        self.drag_selection_active = True
        self.drag_selection_mode = "voxel"
        self.voxel_drag_mode = "remove"
        self.drag_start_pos = None
        self.brush_voxel_ids.clear()
        self.brush_last_report_count = 0
        self._set_drag_pickability(True)
        self._enable_drag_interactor_style()
        self.plotter.add_text(
            "Hold left mouse and drag over path voxels in the right view. Release or press X again to remove.",
            position="lower_left",
            font_size=9,
            name="status_text",
        )
        print("Path voxel brush-remove enabled. Hold left mouse and drag over the right view.")

    def clear_selection(self) -> None:
        """Clear current highlighted region."""
        self.current_region = None
        self.current_regions = []
        self.selected_voxel_ids.clear()
        if self.plotter is not None:
            self.plotter.remove_actor("selected_region", render=False)
            self.plotter.remove_actor("selected_path_voxels", render=False)
            self.plotter.add_text(
                "Selection cleared. Click regions to multi-select, press B for box drag, or V for path voxels.",
                position="lower_left",
                font_size=9,
                name="status_text",
            )
            self.plotter.render()

    def add_current_region_assignment(self) -> None:
        """Add current region as a material assignment unit."""
        if self.current_region is None or not self.selected_voxel_ids:
            print("No region selected.")
            return

        voxel_ids = sorted(self.selected_voxel_ids)
        selected_e, start_cum, end_cum = compute_selected_voxel_filament_e(
            self.selection_cache,
            set(voxel_ids),
        )
        layer_range = layers_from_selected_voxels_cached(self.selection_cache, set(voxel_ids))
        assignment_index = len(self.assignments) + 1
        assignment = {
            "assignment_index": assignment_index,
            "assignment_type": (
                "voxel_selection"
                if self.current_region.get("region_type") in {"path_voxel_group", "hybrid_region_voxel_group"}
                else "rectangular_region"
            ),
            "region_id": self.current_region.get("region_id"),
            "region_ids": list(self.current_region.get("region_ids", [])),
            "region_type": str(self.current_region.get("region_type", "grid_box")),
            "grid_index": self.current_region.get("grid_index"),
            "box_bounds": dict(self.current_region["box_bounds"]),
            "start_voxel": int(voxel_ids[0]),
            "end_voxel": int(voxel_ids[-1]),
            "voxel_ids": voxel_ids,
            "voxel_count": len(voxel_ids),
            "selected_e": round(float(selected_e), 6),
            "cumulative_e_before": round(float(start_cum), 6),
            "cumulative_e_after": round(float(end_cum), 6),
            "layer_range": list(layer_range) if layer_range is not None else None,
            "gradient_steps": 1,
            "gradient_direction": (
                "voxel"
                if self.current_region.get("region_type") in {"path_voxel_group", "hybrid_region_voxel_group"}
                else "region"
            ),
            "eta": 0.5,
        }
        self.assignments.append(assignment)
        if assignment["assignment_type"] == "voxel_selection":
            label = f"{assignment['voxel_count']} path voxels"
        elif assignment.get("region_id") is not None:
            label = f"R{assignment['region_id']}"
        else:
            label = f"{len(assignment['region_ids'])} regions"
        print(f"Added {label} as assignment {assignment_index}")

    def save_all_assignments_to_json(self, output_path: Optional[str] = None) -> None:
        """Save all selected rectangular regions."""
        output_path = Path(output_path) if output_path else (
            self.output_dir / "pyvista_rectangular_region_property_program.json"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "source_gcode": str(self.gcode_path),
            "voxel_threshold_e": float(self.voxel_threshold_e),
            "virtual_sample_spacing_mm": float(self.virtual_sample_spacing_mm),
            "rectangular_region_size_mm": [float(value) for value in self.rectangular_region_size_mm],
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
    output_dir = project_root / "out" / "ui" / "pyvista_voxel_selector"

    if not gcode_file.exists():
        print(f"G-code file not found: {gcode_file}")
        sys.exit(1)

    selector = PyVistaVoxelRegionSelector(
        gcode_path=str(gcode_file),
        voxel_threshold_e=0.1,
        output_dir=str(output_dir),
    )
    selector.parse()
    selector.setup_plotter()
    selector.show()
