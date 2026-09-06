"""``python -m prism.webui``: serve the case home on the loopback interface.

The PrismAPI must live on the serving event loop (its event bus and pipeline
subscription bind to the loop that starts them), so the runtime is created in
NiceGUI's startup hook and closed in its shutdown hook; the page is built
against :class:`_LazyAPI`, which resolves the facade only after startup.  The
server refuses non-loopback hosts before importing NiceGUI — this slice has
no authentication, so it must stay local (REQUIREMENTS §13.7; remote access
is the user's own reverse-proxy decision, never ours) — and it never asks
NiceGUI to open a browser.
"""

from __future__ import annotations

import argparse
from collections.abc import Awaitable, Callable
import sys
from typing import Any

from .app import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_TITLE,
    WebUIUnavailableError,
    _nicegui,
    create_app,
)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


async def _start_runtime() -> Any:
    """Create the owned local runtime on the serving event loop."""
    from prism.runtime import create_runtime

    return await create_runtime()


class _LazyAPI:
    """Facade proxy that resolves the runtime's PrismAPI after startup."""

    def __init__(self, holder: dict[str, Any]) -> None:
        self._holder = holder

    def _api(self) -> Any:
        runtime = self._holder.get("runtime")
        if runtime is None:
            raise RuntimeError("PRISM runtime has not started yet")
        return runtime.api

    async def case_overviews(self, **filters: object) -> object:
        return await self._api().case_overviews(**filters)

    async def case_overview(self, case_id: str) -> object:
        return await self._api().case_overview(case_id)

    async def query_historical_snapshot(
        self,
        case_id: str,
        as_of: object,
        *,
        stage: str | None = None,
        kinds: object = None,
    ) -> object:
        return await self._api().query_historical_snapshot(
            case_id, as_of, stage=stage, kinds=kinds
        )

    async def debate_case(
        self, case_id: str, question: str, as_of: object,
        perspectives: object = None,
    ) -> object:
        return await self._api().debate_case(
            case_id, question, as_of, perspectives=perspectives
        )

    async def follow_up_debate(
        self, parent_run_id: str, question: str, perspective: str
    ) -> object:
        return await self._api().follow_up_debate(
            parent_run_id, question, perspective
        )

    async def search(self, query: object = None, **filters: object) -> object:
        return await self._api().search(query, **filters)

    async def add_material(
        self, source: object, target_case: object,
        metadata: object = None, **kwargs: object,
    ) -> object:
        return await self._api().add_material(source, target_case, metadata, **kwargs)

    async def process_material(
        self,
        source: object,
        metadata: object = None,
        *,
        target_case: object = None,
    ) -> object:
        return await self._api().process_material(
            source, metadata, target_case=target_case
        )

    async def material_journey(self, material_id: str) -> object:
        return await self._api().material_journey(material_id)

    async def material_journeys(
        self, *, case_id: object = None, status: object = None
    ) -> object:
        return await self._api().material_journeys(
            case_id=case_id, status=status
        )


def _upload_staging_root() -> Any:
    """Resolve the upload staging root inside ``PRISM_HOME`` (fixed path).

    The staging area is anchored directly on ``PRISM_HOME`` with a fixed
    directory name (``staging/uploads``) that is never derived from
    request data (security notes §2 of the workbench requirements).
    Resolving lazily keeps this importable and side-effect free; it stays
    consistent with the staging service's default controlled root, which
    is ``PRISM_HOME`` as well.
    """
    from prism.config import PathConfig

    return PathConfig.prism_home() / "staging" / "uploads"


def run(
    start_runtime: Callable[[], Awaitable[Any]] = _start_runtime,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    title: str = DEFAULT_TITLE,
    show: bool = False,
) -> None:
    """Serve the case home; loopback-only by default, no browser launch.

    ``start_runtime`` is awaited once inside NiceGUI's startup hook and its
    runtime closed on shutdown; ``host`` must be a loopback address (checked
    BEFORE the optional NiceGUI import so the refusal never needs the web
    framework) and ``show=False`` means no browser is opened automatically.
    This function blocks until the server stops and is never called by the
    offline test suite.
    """
    if host not in _LOOPBACK_HOSTS:
        raise ValueError(
            f"host {host!r} is not a loopback address; this WebUI has no "
            "authentication and only binds 127.0.0.1/localhost/::1 "
            "(expose it remotely through your own reverse proxy if you must)"
        )
    ui = _nicegui()
    from nicegui import app as nicegui_app

    holder: dict[str, Any] = {}

    create_app(
        _LazyAPI(holder), title=title, upload_staging_root=_upload_staging_root
    )

    async def _startup() -> None:
        holder["runtime"] = await start_runtime()

    async def _shutdown() -> None:
        runtime = holder.pop("runtime", None)
        if runtime is not None:
            await runtime.close()

    nicegui_app.on_startup(_startup)
    nicegui_app.on_shutdown(_shutdown)
    ui.run(host=host, port=port, title=title, show=show, reload=False)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m prism.webui",
        description=(
            "Serve the PRISM case-home WebUI (loopback only; the same "
            "PrismAPI facade the CLI uses)."
        ),
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="loopback address to bind (default: %(default)s)",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help="port to listen on (default: %(default)s)",
    )
    parser.add_argument(
        "--title", default=DEFAULT_TITLE, help="page title (default: %(default)s)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m prism.webui``; returns a process status."""
    args = build_arg_parser().parse_args(argv)
    try:
        run(host=args.host, port=args.port, title=args.title)
    except (WebUIUnavailableError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


__all__ = ["build_arg_parser", "main", "run"]
