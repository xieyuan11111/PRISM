"""``python -m prism.cli`` entry point."""

from __future__ import annotations

import asyncio

from .main import main


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
