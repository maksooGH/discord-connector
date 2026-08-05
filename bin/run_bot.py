#!/usr/bin/env python3
"""Start the capture daemon. Run this under a supervisor; it is meant to stay up."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from core.capture.main import main

if __name__ == "__main__":
    main()
