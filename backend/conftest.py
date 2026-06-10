"""Make the package and tools importable when running under pytest."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (HERE, os.path.join(HERE, "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)
