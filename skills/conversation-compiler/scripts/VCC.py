#!/usr/bin/env python3
"""VCC executable entry point."""

import io
import sys

from vcc.cli import main


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if len(sys.argv) > 1 and sys.argv[1] == "history-search":
        import history_search

        sys.exit(history_search.main(sys.argv[2:]))
    sys.exit(main())
