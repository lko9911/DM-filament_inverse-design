from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from .component_property_designer import component_voxel_count
    from .full_gcode_object_property_designer import parse_full_gcode_objects
except ImportError:
    from component_property_designer import component_voxel_count
    from full_gcode_object_property_designer import parse_full_gcode_objects


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = PROJECT_ROOT / "main.py"
QT_PROPERTY_DESIGNER_PY = PROJECT_ROOT / "scripts" / "ui" / "qt_full_gcode_object_property_designer.py"
RUN_FROM_RESULT_PY = PROJECT_ROOT / "scripts" / "source_dm_filament" / "run_from_result.py"
PRUSA_XL_CONVERTER_PY = PROJECT_ROOT / "scripts" / "prusa_xl" / "convert_to_prusa_xl.py"
DEFAULT_GCODE_DIR = PROJECT_ROOT / "input" / "gcode"


def resolve_default_gcode_path() -> Path:
    gcode_files = sorted(DEFAULT_GCODE_DIR.glob("*.gcode"))
    if gcode_files:
        return gcode_files[0]
    return DEFAULT_GCODE_DIR


DEFAULT_GCODE_PATH = resolve_default_gcode_path()
DESIGNER_DEPENDENCY_IMPORTS = "import PyQt5, vtkmodules"
PIPELINE_DEPENDENCY_IMPORTS = "import tqdm, numpy, matplotlib, openpyxl"

PROPERTY_PATH_ENV_KEY = "B_FDM_PROPERTY_PATH"
SAMPLE_INFO_PATH_ENV_KEY = "B_FDM_SAMPLE_INFO_PATH"
MATERIAL_DICTIONARY_ENV_KEY = "B_FDM_MATERIAL_DICTIONARY_PATH"
BEAM_BEST_PER_STEP_ENV_KEY = "B_FDM_BEAM_BEST_PER_STEP"
RESULT_COUNT_ENV_KEY = "B_FDM_RESULT_COUNT"
ADJACENCY_SEARCH_ALGORITHM_ENV_KEY = "B_FDM_ADJACENCY_SEARCH_ALGORITHM"
RUN_SOURCE_DM_FILAMENT_ENV_KEY = "B_FDM_RUN_SOURCE_DM_FILAMENT"
SOURCE_DM_MATLAB_COMMAND_ENV_KEY = "B_FDM_MATLAB_COMMAND"
GA_POPULATION_SIZE_ENV_KEY = "B_FDM_GA_POPULATION_SIZE"
GA_GENERATIONS_ENV_KEY = "B_FDM_GA_GENERATIONS"
GA_ELITE_COUNT_ENV_KEY = "B_FDM_GA_ELITE_COUNT"
GA_MUTATION_RATE_ENV_KEY = "B_FDM_GA_MUTATION_RATE"
GA_TOURNAMENT_SIZE_ENV_KEY = "B_FDM_GA_TOURNAMENT_SIZE"
GA_RANDOM_SEED_ENV_KEY = "B_FDM_GA_RANDOM_SEED"
GA_MAX_BEST_CANDIDATES_ENV_KEY = "B_FDM_GA_MAX_BEST_CANDIDATES"
ETA_SUM_FITNESS_WEIGHT_ENV_KEY = "B_FDM_ETA_SUM_FITNESS_WEIGHT"
BRIGHTER_MODE_ENV_KEY = "B_FDM_BRIGHTER_MODE"
SPIRAL_FEED_START_ENV_KEY = "B_FDM_SPIRAL_FEED_START_MM"
SPIRAL_FEED_END_ENV_KEY = "B_FDM_SPIRAL_FEED_END_MM"
REORDER_GCODE_STRATEGY_ENV_KEY = "B_FDM_REORDERED_GCODE_STRATEGY"
WITHIN_LAYER_REORDER_GCODE_STRATEGY = "reorder_mesh_occurrences_within_each_layer_keep_xyz"
FEED_MATERIAL_OPTIONS = [
    "AUTO",
    "BLACK",
    "WHITE",
    "CYAN",
    "MAGENTA",
    "YELLOW",
    "PLA",
    "CPLA",
    "TPU",
    "PETG",
    "SMP",
]
MATERIAL_NAME_TO_CODE = {
    "PLA": 1,
    "CPLA": 2,
    "TPU": 3,
    "PETG": 4,
    "SMP": 5,
    "CYAN": 100,
    "MAGENTA": 200,
    "YELLOW": 300,
    "WHITE": 400,
    "BLACK": 500,
}


@dataclass
class CandidateEntry:
    label: str
    result_dir: Path


class PipelineUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("b-FDM All-in-One Pipeline with Property-Guided Designer")
        self.geometry("1280x980")

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.candidates: list[CandidateEntry] = []
        self.current_worker: threading.Thread | None = None

        self.gcode_path_var = tk.StringVar(
            value=str(DEFAULT_GCODE_PATH)
        )
        self.property_path_var = tk.StringVar()
        self.sample_info_path_var = tk.StringVar()
        self.material_dict_path_var = tk.StringVar(value=str(PROJECT_ROOT / "input" / "config" / "material_dictionary.json"))
        self.voxel_threshold_var = tk.StringVar(value="2.0")
        self.result_count_var = tk.StringVar(value="3")
        self.beam_limit_var = tk.StringVar(value="50")
        self.algorithm_var = tk.StringVar(value="ga")
        self.ga_population_size_var = tk.StringVar(value="240")
        self.ga_generations_var = tk.StringVar(value="350")
        self.ga_elite_count_var = tk.StringVar(value="12")
        self.ga_mutation_rate_var = tk.StringVar(value="0.06")
        self.ga_tournament_size_var = tk.StringVar(value="4")
        self.ga_random_seed_var = tk.StringVar(value="42")
        self.ga_max_best_candidates_var = tk.StringVar(value="200")
        self.eta_sum_fitness_weight_var = tk.StringVar(value="10.0")
        self.matlab_command_var = tk.StringVar(value="matlab")
        self.feed_length_start_var = tk.StringVar(value="10")
        self.feed_length_end_var = tk.StringVar(value="130")
        self.feed_material_start_var = tk.StringVar(value="AUTO")
        self.feed_material_end_var = tk.StringVar(value="AUTO")
        self.prepare_only_var = tk.BooleanVar(value=False)
        self.run_all_after_opt_var = tk.BooleanVar(value=False)
        self.brighter_mode_var = tk.BooleanVar(value=False)
        self.prusa_xl_enabled_var = tk.BooleanVar(value=True)
        self.prusa_xl_purge_enabled_var = tk.BooleanVar(value=True)
        self.prusa_xl_center_model_var = tk.BooleanVar(value=True)
        self.prusa_xl_tool_map_var = tk.StringVar(
            value="400=T0, 500=T1, 100=T2, 200=T3, 300=T4"
        )
        self.prusa_xl_purge_x_var = tk.StringVar(value="220")
        self.prusa_xl_purge_y_var = tk.StringVar(value="20")
        self.prusa_xl_purge_step_y_var = tk.StringVar(value="10")
        self.status_var = tk.StringVar(value="Ready.")

        self._build_layout()
        self.gcode_path_var.trace_add("write", lambda *_args: self._sync_generated_property_path())
        self.gcode_path_var.trace_add("write", lambda *_args: self._sync_generated_sample_info_path())
        self._sync_generated_property_path()
        self._sync_generated_sample_info_path()
        self.after(120, self._drain_log_queue)
        self._clear_candidates("Optimization candidates will appear after you run optimization.")

    def _build_layout(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)

        config = ttk.LabelFrame(root, text="Inputs", padding=10)
        config.grid(row=0, column=0, sticky="ew")
        config.columnconfigure(1, weight=1)

        self._add_path_row(config, 0, "G-code", self.gcode_path_var, [("G-code", "*.gcode"), ("All files", "*.*")])
        ttk.Label(config, text="Generated Property JSON").grid(row=1, column=0, sticky="w", pady=(8, 0))
        property_entry = ttk.Entry(config, textvariable=self.property_path_var, state="readonly")
        property_entry.grid(row=1, column=1, sticky="ew", padx=(8, 8), pady=(8, 0))
        ttk.Button(config, text="Refresh", command=self._sync_generated_property_path).grid(row=1, column=2, sticky="ew", pady=(8, 0))
        ttk.Label(config, text="Generated Sample Info").grid(row=2, column=0, sticky="w", pady=(8, 0))
        sample_entry = ttk.Entry(config, textvariable=self.sample_info_path_var, state="readonly")
        sample_entry.grid(row=2, column=1, sticky="ew", padx=(8, 8), pady=(8, 0))
        ttk.Button(config, text="Refresh", command=self._sync_generated_sample_info_path).grid(row=2, column=2, sticky="ew", pady=(8, 0))
        self._add_path_row(config, 3, "Material Dict", self.material_dict_path_var, [("JSON", "*.json"), ("All files", "*.*")])

        options = ttk.LabelFrame(root, text="Options", padding=10)
        options.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        for index in range(8):
            options.columnconfigure(index, weight=1)

        ttk.Label(options, text="Voxel threshold").grid(row=0, column=0, sticky="w")
        ttk.Entry(options, textvariable=self.voxel_threshold_var, width=10).grid(row=0, column=1, sticky="ew", padx=(6, 12))
        ttk.Label(options, text="Result count").grid(row=0, column=2, sticky="w")
        ttk.Entry(options, textvariable=self.result_count_var, width=10).grid(row=0, column=3, sticky="ew", padx=(6, 12))
        ttk.Label(options, text="Beam keep per step").grid(row=0, column=4, sticky="w")
        ttk.Entry(options, textvariable=self.beam_limit_var, width=10).grid(row=0, column=5, sticky="ew", padx=(6, 12))
        ttk.Label(options, text="Algorithm").grid(row=0, column=6, sticky="w")
        ttk.Combobox(
            options,
            textvariable=self.algorithm_var,
            values=["ga", "beam", "astar", "bfs", "dfs", "dijkstra"],
            state="readonly",
            width=12,
        ).grid(row=0, column=7, sticky="ew")

        ttk.Label(options, text="MATLAB command").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(options, textvariable=self.matlab_command_var).grid(row=1, column=1, columnspan=3, sticky="ew", padx=(6, 12), pady=(10, 0))
        ttk.Checkbutton(options, text="Prepare only", variable=self.prepare_only_var).grid(row=1, column=4, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Checkbutton(options, text="Run DM after optimization", variable=self.run_all_after_opt_var).grid(row=1, column=6, columnspan=2, sticky="w", pady=(10, 0))

        ttk.Label(options, text="Print start feed").grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(options, textvariable=self.feed_length_start_var, width=10).grid(row=2, column=1, sticky="ew", padx=(6, 12), pady=(10, 0))
        ttk.Label(options, text="Print end feed").grid(row=2, column=2, sticky="w", pady=(10, 0))
        ttk.Entry(options, textvariable=self.feed_length_end_var, width=10).grid(row=2, column=3, sticky="ew", padx=(6, 12), pady=(10, 0))
        ttk.Checkbutton(options, text="Brighter", variable=self.brighter_mode_var).grid(row=2, column=4, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Label(options, text="Start margin material").grid(row=3, column=0, sticky="w", pady=(10, 0))
        ttk.Combobox(
            options,
            textvariable=self.feed_material_start_var,
            values=FEED_MATERIAL_OPTIONS,
            state="readonly",
            width=12,
        ).grid(row=3, column=1, sticky="ew", padx=(6, 12), pady=(10, 0))
        ttk.Label(options, text="End margin material").grid(row=3, column=2, sticky="w", pady=(10, 0))
        ttk.Combobox(
            options,
            textvariable=self.feed_material_end_var,
            values=FEED_MATERIAL_OPTIONS,
            state="readonly",
            width=12,
        ).grid(row=3, column=3, sticky="ew", padx=(6, 12), pady=(10, 0))

        ttk.Checkbutton(
            options,
            text="Generate Prusa XL G-code",
            variable=self.prusa_xl_enabled_var,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Checkbutton(
            options,
            text="XL purge/prime",
            variable=self.prusa_xl_purge_enabled_var,
        ).grid(row=4, column=2, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Checkbutton(
            options,
            text="Center on 360x360 bed",
            variable=self.prusa_xl_center_model_var,
        ).grid(row=4, column=4, columnspan=2, sticky="w", pady=(10, 0))

        ttk.Label(options, text="XL tool map").grid(row=5, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(options, textvariable=self.prusa_xl_tool_map_var).grid(
            row=5,
            column=1,
            columnspan=7,
            sticky="ew",
            padx=(6, 0),
            pady=(10, 0),
        )

        ttk.Label(options, text="XL purge X").grid(row=6, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(options, textvariable=self.prusa_xl_purge_x_var, width=10).grid(
            row=6, column=1, sticky="ew", padx=(6, 12), pady=(10, 0)
        )
        ttk.Label(options, text="XL purge Y").grid(row=6, column=2, sticky="w", pady=(10, 0))
        ttk.Entry(options, textvariable=self.prusa_xl_purge_y_var, width=10).grid(
            row=6, column=3, sticky="ew", padx=(6, 12), pady=(10, 0)
        )
        ttk.Label(options, text="XL purge step Y").grid(row=6, column=4, sticky="w", pady=(10, 0))
        ttk.Entry(options, textvariable=self.prusa_xl_purge_step_y_var, width=10).grid(
            row=6, column=5, sticky="ew", padx=(6, 12), pady=(10, 0)
        )

        content = ttk.Panedwindow(root, orient=tk.HORIZONTAL)
        content.grid(row=2, column=0, sticky="nsew", pady=(10, 0))

        left = ttk.Frame(content, padding=4)
        right = ttk.Frame(content, padding=4)
        content.add(left, weight=3)
        content.add(right, weight=2)
        left.rowconfigure(2, weight=1)
        left.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        actions = ttk.LabelFrame(left, text="Actions", padding=10)
        actions.grid(row=0, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)

        ttk.Button(actions, text="Open Property Designer", command=self.prepare_input_files).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(actions, text="Run Optimization", command=self.run_optimization).grid(row=0, column=1, sticky="ew")
        ttk.Button(actions, text="Refresh Candidates", command=self.refresh_candidates).grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=(8, 0))
        ttk.Button(actions, text="Make DM Filament", command=self.run_dm_for_selected_candidate).grid(row=1, column=1, sticky="ew", pady=(8, 0))

        note = ttk.Label(
            left,
            text=(
                "Workflow:\n"
                "1. Use 'Open Property Designer' to generate sample_info.json and launch the property designer.\n"
                "2. In the designer, prefer property-guided mode when you want the system to choose material pair, ratio, and eta from experimental data.\n"
                "3. Save the Property_*.json from the designer.\n"
                "4. Run optimization to generate pattern + length + result folders.\n"
                "5. Choose one candidate and generate DM filament output.\n"
                "6. When enabled, the generated DM G-code is converted to Prusa XL tool-change G-code."
            ),
            justify=tk.LEFT,
        )
        note.grid(row=1, column=0, sticky="ew", pady=(10, 0))

        log_frame = ttk.LabelFrame(left, text="Log", padding=8)
        log_frame.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, wrap="word", height=18)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        candidate_frame = ttk.LabelFrame(right, text="Optimization Candidates", padding=10)
        candidate_frame.grid(row=0, column=0, sticky="nsew")
        candidate_frame.rowconfigure(0, weight=1)
        candidate_frame.columnconfigure(0, weight=1)
        self.candidate_list = tk.Listbox(candidate_frame, exportselection=False)
        self.candidate_list.grid(row=0, column=0, sticky="nsew")
        candidate_scroll = ttk.Scrollbar(candidate_frame, orient=tk.VERTICAL, command=self.candidate_list.yview)
        candidate_scroll.grid(row=0, column=1, sticky="ns")
        self.candidate_list.configure(yscrollcommand=candidate_scroll.set)
        self.candidate_list.bind("<<ListboxSelect>>", lambda _event: self._update_candidate_details())

        details = ttk.LabelFrame(right, text="Selected Candidate", padding=10)
        details.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        details.rowconfigure(0, weight=1)
        details.columnconfigure(0, weight=1)
        self.details_text = tk.Text(details, wrap="word", height=18)
        self.details_text.grid(row=0, column=0, sticky="nsew")
        details_scroll = ttk.Scrollbar(details, orient=tk.VERTICAL, command=self.details_text.yview)
        details_scroll.grid(row=0, column=1, sticky="ns")
        self.details_text.configure(yscrollcommand=details_scroll.set)

        status = ttk.Label(root, textvariable=self.status_var, anchor="w")
        status.grid(row=3, column=0, sticky="ew", pady=(8, 0))

    def _add_path_row(
        self,
        parent: ttk.LabelFrame,
        row: int,
        label: str,
        variable: tk.StringVar,
        filetypes: list[tuple[str, str]],
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=(0 if row == 0 else 8, 0))
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", padx=(8, 8), pady=(0 if row == 0 else 8, 0))
        ttk.Button(
            parent,
            text="Browse",
            command=lambda: self._browse_file(variable, filetypes),
        ).grid(row=row, column=2, sticky="ew", pady=(0 if row == 0 else 8, 0))

    def _browse_file(self, variable: tk.StringVar, filetypes: list[tuple[str, str]]) -> None:
        initial = Path(variable.get()).parent if variable.get().strip() else PROJECT_ROOT
        selected = filedialog.askopenfilename(initialdir=initial, filetypes=filetypes)
        if selected:
            variable.set(selected)
            if variable is self.gcode_path_var:
                self._sync_generated_property_path()
                self._sync_generated_sample_info_path()
            self.refresh_candidates()

    def _sync_generated_property_path(self) -> None:
        gcode_text = self.gcode_path_var.get().strip()
        if not gcode_text:
            self.property_path_var.set(str(PROJECT_ROOT / "input" / "config" / "Property_generated.json"))
            return
        gcode_path = Path(gcode_text)
        stem = gcode_path.stem if gcode_path.stem else "generated"
        self.property_path_var.set(str((PROJECT_ROOT / "input" / "config" / f"Property_{stem}.json").resolve()))

    def _sync_generated_sample_info_path(self) -> None:
        gcode_text = self.gcode_path_var.get().strip()
        if not gcode_text:
            self.sample_info_path_var.set(str(PROJECT_ROOT / "input" / "config" / "sample_info_generated.json"))
            return
        gcode_path = Path(gcode_text)
        stem = gcode_path.stem if gcode_path.stem else "generated"
        self.sample_info_path_var.set(str((PROJECT_ROOT / "input" / "config" / f"sample_info_{stem}.json").resolve()))

    def _python_can_launch_qt_designer(self, python_path: Path) -> bool:
        if not python_path.exists():
            return False
        try:
            result = subprocess.run(
                [str(python_path), "-c", DESIGNER_DEPENDENCY_IMPORTS],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=PROJECT_ROOT,
                timeout=8,
                check=False,
            )
        except Exception:
            return False
        return result.returncode == 0

    def _python_can_run_pipeline(self, python_path: Path) -> bool:
        if not python_path.exists():
            return False
        try:
            result = subprocess.run(
                [str(python_path), "-c", PIPELINE_DEPENDENCY_IMPORTS],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=PROJECT_ROOT,
                timeout=8,
                check=False,
            )
        except Exception:
            return False
        return result.returncode == 0

    def _candidate_designer_pythons(self) -> list[Path]:
        candidates: list[Path] = []

        def add(path: object) -> None:
            if not path:
                return
            candidate = Path(path)
            if candidate.is_dir():
                candidate = candidate / ("python.exe" if os.name == "nt" else "python")
            try:
                candidate = candidate.resolve()
            except OSError:
                return
            if candidate not in candidates:
                candidates.append(candidate)

        add(sys.executable)
        add(os.environ.get("B_FDM_QT_PYTHON"))
        add(os.environ.get("CONDA_PREFIX"))
        add(sys.prefix)
        add(sys.base_prefix)

        executable = Path(sys.executable)
        for parent in executable.parents:
            add(parent / ("python.exe" if os.name == "nt" else "python"))
            if parent.name.lower() == "envs":
                add(parent.parent / ("python.exe" if os.name == "nt" else "python"))

        for command in ["python", "python3"]:
            found = shutil.which(command)
            if found:
                add(found)
        return candidates

    def _resolve_qt_designer_python(self) -> Path:
        for candidate in self._candidate_designer_pythons():
            if self._python_can_launch_qt_designer(candidate):
                if candidate != Path(sys.executable).resolve():
                    self.log_queue.put(
                        "[INFO] Current Python cannot launch the Qt designer dependencies; "
                        f"using: {candidate}\n"
                    )
                return candidate
        raise RuntimeError(
            "PyQt5/vtkmodules are not installed in any detected Python environment.\n"
            "Install them in the current environment, or set B_FDM_QT_PYTHON to a Python executable that can import PyQt5 and vtkmodules."
        )

    def _resolve_pipeline_python(self) -> Path:
        configured = os.environ.get("B_FDM_PIPELINE_PYTHON", "").strip()
        candidates = []
        if configured:
            candidates.append(Path(configured))
        candidates.extend(self._candidate_designer_pythons())
        seen: set[Path] = set()
        for candidate in candidates:
            try:
                candidate = candidate.resolve()
            except OSError:
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            if self._python_can_run_pipeline(candidate):
                if candidate != Path(sys.executable).resolve():
                    self.log_queue.put(
                        "[INFO] Current Python cannot run the pipeline dependencies; "
                        f"using: {candidate}\n"
                    )
                return candidate
        raise RuntimeError(
            "Pipeline dependencies are not installed in any detected Python environment.\n"
            "Missing at least one of: tqdm, numpy, matplotlib, openpyxl.\n"
            "Install the missing package in the current environment, or set B_FDM_PIPELINE_PYTHON to a Python executable with the pipeline dependencies."
        )

    def append_log(self, text: str) -> None:
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)

    def _drain_log_queue(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.append_log(line)
        self.after(120, self._drain_log_queue)

    def _validate_common_paths(self) -> bool:
        for label, value in [
            ("Material Dictionary", self.material_dict_path_var.get()),
        ]:
            if not Path(value).exists():
                messagebox.showerror("Missing file", f"{label} was not found:\n{value}")
                return False
        sample_info_path = Path(self.sample_info_path_var.get())
        if not sample_info_path.exists():
            messagebox.showerror(
                "Missing generated sample info",
                "The generated sample info JSON was not found.\n"
                "Use 'Open Property Designer' first.\n\n"
                f"Expected path:\n{sample_info_path}",
            )
            return False
        property_path = Path(self.property_path_var.get())
        if not property_path.exists():
            messagebox.showerror(
                "Missing generated property JSON",
                "The generated property JSON was not found.\n"
                "Use 'Open Property Designer' first, then save the Property JSON in the designer.\n\n"
                f"Expected path:\n{property_path}",
            )
            return False
        return True

    def _generate_sample_info_json(self) -> None:
        gcode_path = Path(self.gcode_path_var.get()).resolve()
        if not gcode_path.exists():
            raise FileNotFoundError(f"G-code was not found: {gcode_path}")
        components = parse_full_gcode_objects(gcode_path)
        if not components:
            raise ValueError(
                "No printable extrusion found in the G-code. "
                "Object comments are optional; custom G-code without mesh metadata is treated as one component."
            )

        threshold_e = float(self.voxel_threshold_var.get().strip())
        if threshold_e <= 0.0:
            raise ValueError("Voxel threshold must be greater than 0.")

        total_filament_e_mm = 0.0
        voxels: list[dict[str, object]] = []
        next_layer_start = 1
        for component in components:
            component_total_e = float(component.total_e)
            total_filament_e_mm += component_total_e
            voxel_count = component_voxel_count(component_total_e, threshold_e)
            layer_count = max(1, int(component.layer_count))
            per_voxel_e = component_total_e / voxel_count if voxel_count > 0 else 0.0

            for local_index in range(voxel_count):
                layer_num = next_layer_start + min((local_index * layer_count) // voxel_count, layer_count - 1)
                voxels.append(
                    {
                        "voxel_id": len(voxels) + 1,
                        "voxel_filament_e_mm": round(per_voxel_e, 6),
                        "layer_num": int(layer_num),
                    }
                )
            next_layer_start += layer_count

        sample_info_payload = {
            "source_gcode": str(gcode_path),
            "voxel_threshold_e": threshold_e,
            "total_filament_e_mm": round(total_filament_e_mm, 6),
            "voxel_count": len(voxels),
            "voxels": voxels,
        }
        sample_info_path = Path(self.sample_info_path_var.get()).resolve()
        sample_info_path.parent.mkdir(parents=True, exist_ok=True)
        sample_info_path.write_text(json.dumps(sample_info_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log_queue.put(f"[DONE] Generated sample info: {sample_info_path}\n")

    def prepare_input_files(self) -> None:
        gcode_path = Path(self.gcode_path_var.get()).resolve()
        if not gcode_path.exists():
            messagebox.showerror("Missing file", f"G-code was not found:\n{gcode_path}")
            return
        if not QT_PROPERTY_DESIGNER_PY.exists():
            messagebox.showerror("Missing script", f"Qt property designer was not found:\n{QT_PROPERTY_DESIGNER_PY}")
            return
        try:
            components = parse_full_gcode_objects(gcode_path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("G-code parse error", f"Could not parse G-code:\n{gcode_path}\n\n{exc}")
            return
        if not components:
            messagebox.showerror(
                "Unsupported G-code for Qt designer",
                "No printable extrusion found in the selected G-code.\n\n"
                "The Qt designer can use object-labeled comments like:\n"
                "'; printing object ... id:N copy M'\n\n"
                "mesh-labeled comments like:\n"
                "';MESH:part_name.STL'\n\n"
                "or Prusa/Orca object selection commands like:\n"
                "'M486 S0' and 'M486 AObjectName'.\n\n"
                "If none exist, it falls back to one full-G-code component when positive-E extrusion moves exist.\n\n"
                f"Selected file:\n{gcode_path}",
            )
            return

        def task() -> None:
            self._generate_sample_info_json()

        def on_success() -> None:
            try:
                designer_python = self._resolve_qt_designer_python()
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Missing Qt designer dependency", str(exc))
                self.status_var.set("Failed: Qt designer dependencies are missing.")
                return
            command = [
                str(designer_python),
                str(QT_PROPERTY_DESIGNER_PY),
                str(gcode_path),
                "--output",
                self.property_path_var.get(),
                "--voxel-threshold-e",
                self.voxel_threshold_var.get(),
            ]
            launch_env = os.environ.copy()
            launch_env[REORDER_GCODE_STRATEGY_ENV_KEY] = WITHIN_LAYER_REORDER_GCODE_STRATEGY
            launch_env[BRIGHTER_MODE_ENV_KEY] = "1" if self.brighter_mode_var.get() else "0"
            # Source_DM_filament consumes the prepared filament in the reverse
            # direction, matching the swap used by run_dm_for_selected_candidate.
            launch_env[SPIRAL_FEED_START_ENV_KEY] = self.feed_length_end_var.get().strip()
            launch_env[SPIRAL_FEED_END_ENV_KEY] = self.feed_length_start_var.get().strip()
            self.log_queue.put(
                "[OPEN] Property designer: "
                f"{QT_PROPERTY_DESIGNER_PY}\n"
                f"       Python: {designer_python}\n"
                f"       G-code: {gcode_path}\n"
                f"       Output: {self.property_path_var.get()}\n"
            )
            subprocess.Popen(command, cwd=PROJECT_ROOT, env=launch_env)
            self.status_var.set("Sample info generated and property-guided designer launched.")

        self.status_var.set("Generating sample info...")
        self._start_worker(task, on_success=on_success)

    def _property_output_root(self) -> Path:
        property_path = Path(self.property_path_var.get())
        return PROJECT_ROOT / "out" / property_path.stem

    def _clear_candidates(self, status_text: str) -> None:
        self.candidates = []
        self.candidate_list.delete(0, tk.END)
        self.details_text.delete("1.0", tk.END)
        self.status_var.set(status_text)

    def refresh_candidates(self) -> None:
        output_root = self._property_output_root()
        result_root = output_root / "result"
        self._clear_candidates(f"No candidate result folders found under {result_root}.")

        if result_root.exists():
            for result_json in sorted(result_root.glob("candidate_rank_*/result.json")):
                payload = self._safe_load_json(result_json)
                candidate_name = result_json.parent.name
                label = candidate_name
                if payload is not None:
                    label = (
                        f"{candidate_name} | score={payload.get('candidate_score')} | "
                        f"eta_sum={payload.get('candidate_eta_sum')} | "
                        f"switch={payload.get('material_switch_count')}"
                    )
                self.candidates.append(CandidateEntry(label=label, result_dir=result_json.parent))
                self.candidate_list.insert(tk.END, label)
        if self.candidates:
            self.candidate_list.selection_set(0)
            self._update_candidate_details()
            self.status_var.set(f"Loaded {len(self.candidates)} candidate result folder(s).")

    def _safe_load_json(self, path: Path) -> dict[str, object] | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _format_length_details(self, payload: dict[str, object]) -> list[str]:
        lines: list[str] = []
        length_values = payload.get("length")
        step_spatial_metadata = payload.get("step_spatial_metadata")

        if isinstance(length_values, list) and length_values:
            numeric_lengths = [float(value) for value in length_values]
            lines.append("Length info:")
            lines.append(f"  Step count: {len(numeric_lengths)}")
            lines.append(f"  Total length: {sum(numeric_lengths):.6f}")
            for index, value in enumerate(numeric_lengths, start=1):
                lines.append(f"  Step {index}: {value:.6f}")
        else:
            lines.append("Length info:")
            lines.append("  No length values found.")

        if isinstance(step_spatial_metadata, list) and step_spatial_metadata:
            lines.append("")
            lines.append("Step spatial metadata:")
            for index, item in enumerate(step_spatial_metadata, start=1):
                if not isinstance(item, dict):
                    continue
                lines.append(
                    "  Step {index}: voxel {start_voxel}-{end_voxel}, layer {start_layer}-{end_layer}, e={step_e}".format(
                        index=index,
                        start_voxel=item.get("start_voxel_index"),
                        end_voxel=item.get("end_voxel_index"),
                        start_layer=item.get("start_layer"),
                        end_layer=item.get("end_layer"),
                        step_e=item.get("step_filament_e_mm"),
                    )
                )

        return lines

    def _update_candidate_details(self) -> None:
        selection = self.candidate_list.curselection()
        self.details_text.delete("1.0", tk.END)
        if not selection:
            return
        entry = self.candidates[selection[0]]
        result_json = entry.result_dir / "result.json"
        payload = self._safe_load_json(result_json)
        if payload is None:
            self.details_text.insert(tk.END, f"Could not read:\n{result_json}")
            return
        summary = [
            f"Result dir: {entry.result_dir}",
            f"Candidate rank: {payload.get('candidate_rank')}",
            f"Original rank: {payload.get('original_candidate_rank')}",
            f"Score: {payload.get('candidate_score')}",
            f"Eta sum: {payload.get('candidate_eta_sum')}",
            f"Material switches: {payload.get('material_switch_count')}",
            "",
        ]
        summary.extend(self._format_length_details(payload))
        summary.extend(
            [
                "",
            "Files:",
            f"  {entry.result_dir / 'length.txt'}",
            f"  {entry.result_dir / 'matrix.txt'}",
            f"  {entry.result_dir / 'po.txt'}",
            f"  {entry.result_dir / 'result.json'}",
            ]
        )
        self.details_text.insert(tk.END, "\n".join(summary))

    def _selected_candidate_dir(self) -> Path | None:
        selection = self.candidate_list.curselection()
        if not selection:
            return None
        return self.candidates[selection[0]].result_dir

    def _start_worker(self, target, *, on_success=None) -> None:
        if self.current_worker is not None and self.current_worker.is_alive():
            messagebox.showinfo("Busy", "A task is already running. Please wait for it to finish.")
            return

        def runner() -> None:
            try:
                target()
            except Exception as exc:  # noqa: BLE001
                error_text = str(exc)
                self.log_queue.put(f"\n[ERROR] {error_text}\n")
                self.after(0, lambda message=error_text: self.status_var.set(f"Failed: {message}"))
            else:
                if on_success is not None:
                    self.after(0, on_success)

        self.current_worker = threading.Thread(target=runner, daemon=True)
        self.current_worker.start()

    def _stream_subprocess(
        self,
        command: list[str],
        env: dict[str, str] | None = None,
        display_command: str | None = None,
    ) -> None:
        shown_command = display_command if display_command is not None else " ".join(command)
        self.log_queue.put(f"\n[START] {shown_command}\n")
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        assert process.stdout is not None
        for line in process.stdout:
            self.log_queue.put(line)
        exit_code = process.wait()
        if exit_code != 0:
            raise RuntimeError(f"Command failed with exit code {exit_code}: {shown_command}")
        self.log_queue.put(f"[DONE] {shown_command}\n")

    def run_optimization(self) -> None:
        if not self._validate_common_paths():
            return

        property_payload = self._safe_load_json(Path(self.property_path_var.get()))
        sample_payload = self._safe_load_json(Path(self.sample_info_path_var.get()))
        if property_payload is None or sample_payload is None:
            messagebox.showerror("Invalid input JSON", "Could not read generated property/sample info JSON.")
            return
        property_voxel_count = int(property_payload.get("voxel_count", 0))
        sample_voxel_count = int(sample_payload.get("voxel_count", 0))
        if property_voxel_count != sample_voxel_count:
            self.log_queue.put(
                "[WARN] Property/sample resolution count differs. Continuing because the current workflow "
                "uses component extrusion lengths as the source of truth.\n"
                f"       Property count: {property_voxel_count}, sample count: {sample_voxel_count}\n"
                "       If length generation fails later, reopen the Property Designer and save again.\n"
            )

        def task() -> None:
            pipeline_python = self._resolve_pipeline_python()
            env = os.environ.copy()
            env[PROPERTY_PATH_ENV_KEY] = str(Path(self.property_path_var.get()).resolve())
            env[SAMPLE_INFO_PATH_ENV_KEY] = str(Path(self.sample_info_path_var.get()).resolve())
            env[MATERIAL_DICTIONARY_ENV_KEY] = str(Path(self.material_dict_path_var.get()).resolve())
            env[RESULT_COUNT_ENV_KEY] = self.result_count_var.get().strip()
            env[BEAM_BEST_PER_STEP_ENV_KEY] = self.beam_limit_var.get().strip()
            env[ADJACENCY_SEARCH_ALGORITHM_ENV_KEY] = self.algorithm_var.get().strip()
            env[GA_POPULATION_SIZE_ENV_KEY] = self.ga_population_size_var.get().strip()
            env[GA_GENERATIONS_ENV_KEY] = self.ga_generations_var.get().strip()
            env[GA_ELITE_COUNT_ENV_KEY] = self.ga_elite_count_var.get().strip()
            env[GA_MUTATION_RATE_ENV_KEY] = self.ga_mutation_rate_var.get().strip()
            env[GA_TOURNAMENT_SIZE_ENV_KEY] = self.ga_tournament_size_var.get().strip()
            env[GA_RANDOM_SEED_ENV_KEY] = self.ga_random_seed_var.get().strip()
            env[GA_MAX_BEST_CANDIDATES_ENV_KEY] = self.ga_max_best_candidates_var.get().strip()
            env[ETA_SUM_FITNESS_WEIGHT_ENV_KEY] = self.eta_sum_fitness_weight_var.get().strip()
            env[BRIGHTER_MODE_ENV_KEY] = "1" if self.brighter_mode_var.get() else "0"
            env[RUN_SOURCE_DM_FILAMENT_ENV_KEY] = "0"
            self._stream_subprocess([str(pipeline_python), str(MAIN_PY)], env=env)

        def on_success() -> None:
            self.refresh_candidates()
            self.status_var.set("Optimization finished.")
            if self.run_all_after_opt_var.get() and self.candidates:
                self.run_dm_for_selected_candidate()

        self.status_var.set("Running optimization...")
        self._start_worker(task, on_success=on_success)

    def run_dm_for_selected_candidate(self) -> None:
        candidate_dir = self._selected_candidate_dir()
        if candidate_dir is None:
            messagebox.showinfo("No candidate", "Select a candidate result folder first.")
            return

        try:
            print_start_feed = float(self.feed_length_start_var.get().strip())
            print_end_feed = float(self.feed_length_end_var.get().strip())
        except ValueError:
            messagebox.showerror("Invalid feed lengths", "Print start/end feed must be numeric values.")
            return

        gcode_stem = Path(self.gcode_path_var.get().strip()).stem or "gcode"

        def _format_feed_value(value: float) -> str:
            return f"{value:g}".replace("-", "neg")

        def _format_material_value(value: str) -> str:
            return str(value or "AUTO").strip().replace(" ", "_")

        start_material = self.feed_material_start_var.get().strip() or "AUTO"
        end_material = self.feed_material_end_var.get().strip() or "AUTO"
        prusa_xl_enabled = self.prusa_xl_enabled_var.get()
        if prusa_xl_enabled and self.prepare_only_var.get():
            messagebox.showerror(
                "Prusa XL conversion unavailable",
                "Prusa XL conversion needs the generated DM G-code.\n"
                "Turn off 'Prepare only' or disable 'Generate Prusa XL G-code'.",
            )
            return
        if prusa_xl_enabled and not PRUSA_XL_CONVERTER_PY.exists():
            messagebox.showerror(
                "Missing Prusa XL converter",
                f"Prusa XL converter was not found:\n{PRUSA_XL_CONVERTER_PY}",
            )
            return

        prusa_xl_purge_x = 220.0
        prusa_xl_purge_y = 20.0
        prusa_xl_purge_step_y = 10.0
        prusa_xl_tool_mappings: list[str] = []
        if prusa_xl_enabled:
            try:
                prusa_xl_purge_x = float(self.prusa_xl_purge_x_var.get().strip())
                prusa_xl_purge_y = float(self.prusa_xl_purge_y_var.get().strip())
                prusa_xl_purge_step_y = float(self.prusa_xl_purge_step_y_var.get().strip())
                prusa_xl_tool_mappings = self._parse_prusa_xl_tool_mappings(
                    self.prusa_xl_tool_map_var.get()
                )
                candidate_payload = self._safe_load_json(candidate_dir / "result.json")
                po_rows = candidate_payload.get("po", []) if candidate_payload else []
                required_material_codes = {
                    int(row[0])
                    for row in po_rows
                    if isinstance(row, list) and row
                }
                for margin_material in (start_material, end_material):
                    normalized_margin_material = margin_material.upper()
                    if normalized_margin_material != "AUTO":
                        required_material_codes.add(
                            MATERIAL_NAME_TO_CODE[normalized_margin_material]
                        )
                mapped_material_codes = {
                    int(mapping.split("=", 1)[0])
                    for mapping in prusa_xl_tool_mappings
                }
                missing_material_codes = sorted(
                    required_material_codes - mapped_material_codes
                )
                if missing_material_codes:
                    missing_text = ", ".join(str(code) for code in missing_material_codes)
                    raise ValueError(
                        "XL tool map does not cover the selected candidate's materials: "
                        f"{missing_text}"
                    )
            except ValueError as exc:
                messagebox.showerror("Invalid Prusa XL settings", str(exc))
                return

        prusa_xl_purge_enabled = self.prusa_xl_purge_enabled_var.get()
        prusa_xl_center_model = self.prusa_xl_center_model_var.get()
        dm_output_name = (
            f"{gcode_stem}_FeedStart_{_format_feed_value(print_start_feed)}"
            f"_end_{_format_feed_value(print_end_feed)}"
        )
        if start_material.upper() != "AUTO" or end_material.upper() != "AUTO":
            dm_output_name += (
                f"_StartMat_{_format_material_value(start_material)}"
                f"_EndMat_{_format_material_value(end_material)}"
            )
        dm_output_dir = candidate_dir / dm_output_name

        def task() -> None:
            pipeline_python = self._resolve_pipeline_python()
            command = [
                str(pipeline_python),
                str(RUN_FROM_RESULT_PY),
                str(candidate_dir),
                "--output-dir",
                str(dm_output_dir),
                "--matlab-command",
                self.matlab_command_var.get().strip(),
                "--feed-length-start",
                str(print_end_feed),
                "--feed-length-end",
                str(print_start_feed),
                "--feed-start-material",
                end_material,
                "--feed-end-material",
                start_material,
            ]
            if self.prepare_only_var.get():
                command.append("--prepare-only")
            display_command = (
                f"{candidate_dir} --output-dir {dm_output_dir} --matlab-command {self.matlab_command_var.get().strip()} "
                f"--feed-length-start {print_start_feed} --feed-length-end {print_end_feed} "
                f"--feed-start-material {start_material} --feed-end-material {end_material}"
            )
            if self.prepare_only_var.get():
                display_command += " --prepare-only"
            self._stream_subprocess(command, display_command=display_command)

            if prusa_xl_enabled:
                dm_gcode_path = dm_output_dir / f"{dm_output_name}_mod.txt"
                prusa_xl_gcode_path = dm_output_dir / f"{dm_output_name}_mod_PrusaXL.gcode"
                effective_po_path = dm_output_dir / "po_material_switches.txt"
                if not dm_gcode_path.exists():
                    raise FileNotFoundError(
                        "DM G-code was not created at the expected path:\n"
                        f"{dm_gcode_path}"
                    )
                if not effective_po_path.exists():
                    raise FileNotFoundError(
                        "DM material sequence report was not created at the expected path:\n"
                        f"{effective_po_path}"
                    )

                prusa_command = [
                    str(pipeline_python),
                    str(PRUSA_XL_CONVERTER_PY),
                    "--input",
                    str(dm_gcode_path),
                    "--po",
                    str(effective_po_path),
                    "--output",
                    str(prusa_xl_gcode_path),
                    "--purge-x",
                    str(prusa_xl_purge_x),
                    "--purge-y",
                    str(prusa_xl_purge_y),
                    "--purge-step-y",
                    str(prusa_xl_purge_step_y),
                    "--tool-command-style",
                    "xl",
                ]
                for mapping in prusa_xl_tool_mappings:
                    prusa_command.extend(["--map", mapping])
                if not prusa_xl_purge_enabled:
                    prusa_command.append("--no-purge")
                if not prusa_xl_center_model:
                    prusa_command.append("--no-center-model-on-bed")

                self._stream_subprocess(prusa_command)
                if not prusa_xl_gcode_path.exists():
                    raise FileNotFoundError(
                        "Prusa XL G-code was not created at the expected path:\n"
                        f"{prusa_xl_gcode_path}"
                    )
                self.log_queue.put(
                    f"[RESULT] Prusa XL G-code: {prusa_xl_gcode_path}\n"
                )

        def on_success() -> None:
            if prusa_xl_enabled:
                self.status_var.set(
                    f"DM filament + Prusa XL G-code created: {dm_output_dir.name}"
                )
            else:
                self.status_var.set(f"DM filament output created: {dm_output_dir.name}")

        self.status_var.set(f"Running DM filament for {candidate_dir.name}...")
        self._start_worker(task, on_success=on_success)

    @staticmethod
    def _parse_prusa_xl_tool_mappings(raw_value: str) -> list[str]:
        mappings = [item.strip() for item in raw_value.split(",") if item.strip()]
        if not mappings:
            raise ValueError(
                "XL tool map is empty. Use entries such as 400=T0, 500=T1."
            )

        normalized: list[str] = []
        seen_materials: set[int] = set()
        for mapping in mappings:
            if "=" not in mapping:
                raise ValueError(
                    f"Invalid XL tool mapping '{mapping}'. Use MATERIAL=TOOL, for example 400=T0."
                )
            material_text, tool_text = mapping.split("=", 1)
            try:
                material_code = int(material_text.strip())
            except ValueError as exc:
                raise ValueError(
                    f"Invalid material code in XL tool mapping '{mapping}'."
                ) from exc
            tool = tool_text.strip().upper()
            if tool not in {"T0", "T1", "T2", "T3", "T4"}:
                raise ValueError(
                    f"Invalid Prusa XL tool in mapping '{mapping}'. Use T0 through T4."
                )
            if material_code in seen_materials:
                raise ValueError(
                    f"Material code {material_code} appears more than once in the XL tool map."
                )
            seen_materials.add(material_code)
            normalized.append(f"{material_code}={tool}")
        return normalized


def main() -> None:
    app = PipelineUI()
    app.mainloop()


if __name__ == "__main__":
    main()
