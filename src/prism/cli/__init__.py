"""Public PRISM command-line shell."""

from .main import (
    PrismAPIProtocol,
    build_parser,
    handle_add_material,
    handle_adjudication_history,
    handle_cases,
    handle_compare,
    handle_discover,
    handle_fetch,
    handle_fetch_all,
    handle_follow_up,
    handle_ingest,
    handle_material_journey,
    handle_material_journeys,
    handle_rebuild_report,
    handle_report,
    handle_report_version,
    handle_report_versions,
    handle_research,
    handle_search,
    handle_snapshot,
    handle_state,
    handle_timeline,
    main,
)

__all__ = [
    "PrismAPIProtocol",
    "build_parser",
    "handle_add_material",
    "handle_adjudication_history",
    "handle_cases",
    "handle_compare",
    "handle_discover",
    "handle_fetch",
    "handle_fetch_all",
    "handle_follow_up",
    "handle_ingest",
    "handle_material_journey",
    "handle_material_journeys",
    "handle_rebuild_report",
    "handle_report",
    "handle_report_version",
    "handle_report_versions",
    "handle_research",
    "handle_search",
    "handle_snapshot",
    "handle_state",
    "handle_timeline",
    "main",
    "main_entry",
]


def main_entry() -> int:
    """Console-script entry: run one command and return its exit status.

    The ``prism`` console script and ``python -m prism.cli`` are the same
    program under two names (requirements C-1): both go through the same
    :func:`main`, which owns parsing, the output contract and the 0/1/2
    exit codes.  The setuptools wrapper turns the returned status into the
    process exit code.
    """
    import asyncio

    return asyncio.run(main())
