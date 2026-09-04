"""Runnable loopback-only entry point for the optional PRISM WebUI."""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from .app import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_TITLE, CaseHomeController, _nicegui, create_app


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the PRISM NiceGUI case home")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--title", default=DEFAULT_TITLE)
    return parser


def run(api: Any, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, title: str = DEFAULT_TITLE) -> None:
    if host != DEFAULT_HOST:
        raise ValueError("PRISM WebUI only binds loopback by default")
    create_app(api, title=title)
    _nicegui().run(host=host, port=port, show=False, reload=False)


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    from prism.runtime import create_runtime
    runtime = asyncio.run(create_runtime())
    try:
        run(runtime.api, host=args.host, port=args.port, title=args.title)
    finally:
        close = getattr(runtime, "close", None)
        if callable(close):
            result = close()
            if hasattr(result, "__await__"):
                asyncio.run(result)


if __name__ == "__main__":
    main()
