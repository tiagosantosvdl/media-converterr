import os
import tempfile
import unittest

import media_converterr as mc


class GpuLockTests(unittest.TestCase):
    def test_second_holder_is_refused_until_release(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "gpu.lock")
            a, b = mc.GpuLock(path), mc.GpuLock(path)
            self.assertTrue(a.try_acquire())
            self.assertFalse(b.try_acquire())
            a.release()
            self.assertTrue(b.try_acquire())
            b.release()

    def test_unset_path_never_blocks(self):
        lock = mc.GpuLock(None)
        self.assertTrue(lock.try_acquire())
        self.assertTrue(lock.try_acquire())


if __name__ == "__main__":
    unittest.main()
