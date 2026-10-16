import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from lifus import operations as ops


class WindowsNTFSTests(unittest.TestCase):
    @unittest.skipUnless(all(shutil.which(t) for t in ('xorriso','7z','wimlib-imagex','mkfs.ntfs','ntfscat','parted')), 'NTFS tools required')
    def test_real_ntfs_and_original_uefi_partition(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); tree=root/'tree'
            (tree/'efi/boot').mkdir(parents=True);(tree/'sources').mkdir()
            (tree/'efi/boot/bootx64.efi').write_bytes(b'fixture-not-bootable')
            (tree/'sources/boot.wim').write_bytes(b'fixture-not-a-wim')
            iso=root/'fixture.iso';disk=root/'disk.img'
            ops._run(['xorriso','-as','mkisofs','-J','-R','-o',str(iso),str(tree)])
            with disk.open('wb') as f:f.truncate(96*ops.MiB)
            answer=root/'answer.xml'
            answer.write_text('<unattend xmlns="urn:schemas-microsoft-com:unattend"/>')
            plan=ops.plan_operation('windows',str(iso),str(disk),allow_file=True,filesystem='ntfs',unattend=str(answer))
            with patch('os.geteuid',return_value=0):result=ops.execute_operation(plan)
            self.assertTrue(result['verified'])
            with disk.open('rb') as f:
                f.seek(plan['offset']); part=root/'ntfs.img';part.write_bytes(f.read(plan['size']))
                f.seek(plan['efi_offset']);efi=f.read(ops.MiB)
            self.assertEqual(ops._run(['ntfscat',str(part),'/autounattend.xml']),answer.read_bytes())
            self.assertEqual(ops._run(['ntfscat',str(part),'/efi/boot/bootx64.efi']),b'fixture-not-bootable')
            upstream=Path(__file__).resolve().parents[1]/'upstream/res/uefi/uefi-ntfs.img'
            self.assertEqual(efi,upstream.read_bytes())
            table=json.loads(ops._run(['sfdisk','--json',str(disk)]))['partitiontable']
            self.assertEqual(len(table['partitions']),2)
            self.assertEqual(table['partitions'][1]['type'].upper(),'C12A7328-F81F-11D2-BA4B-00A0C93EC93B')

    def test_unattend_validation_rejects_non_xml(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);disk=root/'disk'; bad=root/'bad.xml'
            with disk.open('wb') as f:f.truncate(96*ops.MiB)
            bad.write_text('not xml')
            with self.assertRaisesRegex(ValueError,'Invalid unattend XML'):
                ops.plan_operation('windows',None,str(disk),allow_file=True,unattend=str(bad))

if __name__=='__main__':unittest.main()
