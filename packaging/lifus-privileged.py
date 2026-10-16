#!/usr/bin/python3 -I
"""Isolated sudo entry; never import from the caller's working directory."""
from pathlib import Path
import sys

here = Path(__file__).resolve().parent
base = here.parent if here.name == 'packaging' else here
sys.path.insert(0, str(base))
from lifus.privileges import worker_main

if __name__ == '__main__':
    raise SystemExit(worker_main())
