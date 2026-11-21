"""Library walks skip excluded and hidden folders (recycle bins, staging folders, trash)."""
import os
import tempfile
import unittest

import media_converterr as mc


class LibraryWalkTests(unittest.TestCase):
    def test_hidden_and_excluded_folders_are_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            for folder in ("Show/Season 1", ".recycle-bin/Show", "#recycle/Show", ".Trash-1000", "_staging"):
                os.makedirs(os.path.join(root, folder))
                open(os.path.join(root, folder, "a.mkv"), "w").close()
            seen = sorted(os.path.relpath(r, root) for r, _, files in mc.walk_library(root, ["#recycle"]) if files)
            self.assertEqual(seen, ["Show/Season 1", "_staging"])


if __name__ == "__main__":
    unittest.main()
