#!/usr/bin/env python3
"""RAT - Repo Analysis Tool: start the dashboard.

Usage:
    python3 run.py                    # http://127.0.0.1:8000
    python3 run.py --port 9000
    python3 run.py --host 0.0.0.0     # expose on the network (careful)
    python3 run.py --data ./data      # where repositories are stored

Requires: Python 3.9+ and git (any modern version). No pip packages needed.
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from rat.server import serve  # noqa: E402  (path bootstrap above)


def main():
    parser = argparse.ArgumentParser(description="RAT - Repo Analysis Tool (COMS3011A)")
    parser.add_argument("--host", default=os.environ.get("RAT_HOST", "127.0.0.1"),
                        help="interface to bind (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("RAT_PORT", "8000")),
                        help="port to listen on (default 8000)")
    parser.add_argument("--data", default=os.environ.get("RAT_DATA", os.path.join(ROOT, "data")),
                        help="data directory (default ./data)")
    args = parser.parse_args()
    serve(args.host, args.port, args.data)


if __name__ == "__main__":
    main()
