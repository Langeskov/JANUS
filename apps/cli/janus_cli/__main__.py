"""``python -m janus_cli`` entry point (equivalent to the ``janus`` script)."""

from __future__ import annotations

import sys

from janus_cli.main import main

if __name__ == "__main__":
    sys.exit(main())
