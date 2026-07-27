from __future__ import annotations

import os
import unittest
from pathlib import Path

from scripts.ui.all_in_one_pipeline_ui import to_windows_extended_path


class WindowsExtendedPathTests(unittest.TestCase):
    def test_extended_path_preserves_an_absolute_path(self) -> None:
        source = Path.cwd() / "output.txt"
        converted = to_windows_extended_path(source)

        if os.name == "nt":
            self.assertEqual(str(converted), "\\\\?\\" + str(source.resolve()))
        else:
            self.assertEqual(converted, source.resolve())

    @unittest.skipUnless(os.name == "nt", "Windows extended-path behavior")
    def test_extended_path_is_idempotent(self) -> None:
        source = to_windows_extended_path(Path.cwd() / "output.txt")

        self.assertEqual(to_windows_extended_path(source), source)
