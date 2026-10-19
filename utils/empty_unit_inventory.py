#!/usr/bin/env python3
"""Empty Rust inventory for the Python-only Git Viewspace test suite."""

import json
import sys

if sys.argv[1:] == ["--list-tests"]:
    print(json.dumps({"protocol": 2, "tests": []}))
else:
    raise SystemExit(2)
