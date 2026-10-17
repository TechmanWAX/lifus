"""Opt-in real-ISO direct-write verification. Creates only a private FILE target.

Run: python tests/real_windows_direct.py /path/to/windows.iso
Reports peak staging file allocation and independently hashes every ISO file
against mtools readback. Never accepts a target device argument.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import resource
import sys
import tempfile
import threading
import time
from unittest.mock import patch

from lifus import operations as op
from lifus.direct_fat import stream_command


def main():
    source = os.path.abspath(sys.argv[1])
    original = op._source_identity(source)
    records = op._members(source)
    files = [r for r in records if not r['directory']]
    assert all(r['size'] <= op.FAT_LIMIT for r in files), 'Ordinary FAT32 ISO required'
    payload = sum(r['size'] for r in files)
    size = max(8*1024*op.MiB, ((payload + 256*op.MiB)//op.MiB+1)*op.MiB)
    directory = tempfile.mkdtemp(prefix='lifus-real-direct-')
    print('Private file-target workspace (retained for inspection):', directory, flush=True)
    with contextlib.nullcontext(directory) as work:
        root = Path(work)
        disk = root/'target.img'
        stage = root/'stage'; stage.mkdir()
        # Dirty all target bytes so zero-file data and metadata cannot rely on holes.
        with disk.open('xb') as f:
            block = b'X' * op.MiB
            for _ in range(size//op.MiB):
                f.write(block)
            f.flush(); os.fsync(f.fileno())
        plan = op.plan_operation('windows', source, str(disk), allow_file=True,
                                 staging_dir=str(stage), partition_table='gpt')
        assert plan['staging_bytes'] == 0, plan
        peak = 0
        created_files = set()
        stop = threading.Event()
        phases = set()
        def monitor():
            nonlocal peak
            while not stop.is_set():
                allocated = 0
                for p in stage.rglob('*'):
                    try:
                        if p.is_file():
                            allocated += p.stat().st_blocks * 512
                            created_files.add(str(p.relative_to(stage)))
                    except FileNotFoundError:
                        pass
                peak = max(peak, allocated)
                stop.wait(0.005)
        thread = threading.Thread(target=monitor)
        thread.start()
        start = time.monotonic()
        try:
            with patch('os.geteuid', return_value=0):
                result = op.execute_operation(plan, progress=lambda e: phases.add(e['phase']))
        finally:
            stop.set(); thread.join()
        elapsed = time.monotonic()-start
        assert result['verified'] and peak == 0 and not created_files
        checked = 0
        hash_manifest = hashlib.sha256()
        for record in files:
            hashes = []
            for args in (['7z', 'x', '-so', '-spd', '--', source, record['path']],
                         ['mtype', '-i', str(disk)+'@@'+str(plan['offset']), '::/'+record['path']]):
                digest = hashlib.sha256(); count = 0
                for chunk in stream_command(args, lambda: None):
                    digest.update(chunk); count += len(chunk)
                assert count == record['size'], (record, count)
                hashes.append(digest.digest())
            assert hashes[0] == hashes[1], record['path']
            hash_manifest.update(record['path'].encode()+b'\0'+hashes[0])
            checked += 1
        assert checked == len(files)
        assert op._source_identity(source) == original
        assert disk.stat().st_size == size
        assert not list(stage.iterdir())
        print(json.dumps(dict(source=source, source_bytes=original[2], files_verified=checked,
                              payload_bytes=payload, target_bytes=size, staging_estimate=plan['staging_bytes'],
                              peak_staging_allocated=peak, staging_files=list(created_files),
                              write_and_verify_seconds=round(elapsed, 3), total_seconds=round(time.monotonic()-start, 3),
                              max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                              file_hash_manifest_sha256=hash_manifest.hexdigest(),
                              phases=sorted(phases), result=result), indent=2))


if __name__ == '__main__':
    main()
