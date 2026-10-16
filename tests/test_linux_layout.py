"""Linux-only checkout and packaged resource contracts."""
from pathlib import Path
import unittest
from lifus.freedos import _assets


class LinuxLayoutTests(unittest.TestCase):
    def test_assets_live_inside_linux_tree(self):
        from lifus import freedos
        base = Path(freedos.__file__).resolve().parents[1]
        self.assertEqual(_assets(), base / 'upstream')
        for name in ('res/freedos/KERNEL.SYS', 'res/freedos/COMMAND.COM',
                     'res/uefi/uefi-ntfs.img', 'src/ms-sys/inc/mbr_win7.h',
                     'src/ms-sys/inc/br_fat16_0x0.h', 'src/ms-sys/inc/br_fat16fd_0x3e.h'):
            self.assertTrue((_assets() / name).is_file(), name)
