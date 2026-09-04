"""Optional NiceGUI WebUI shell over the PRISM application facade.

Importing this package never imports NiceGUI: the shell resolves the optional
dependency lazily (:func:`create_app`) so PRISM's default runtime stays
dependency-free without the ``webui`` extra, and every read goes through the
same :class:`~prism.api.PrismAPI` facade the CLI uses.
"""

from .app import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_TITLE,
    NICEGUI_MISSING_MESSAGE,
    PLOTLY_MISSING_MESSAGE,
    CaseHomeController,
    WebUIUnavailableError,
    build_timeline_figure,
    create_app,
)
from .controller import parse_as_of, snapshot_view
from .server import build_arg_parser, run
from .debate import DebateTheaterController, build_debate_theater_page
from .evidence import EvidenceBrowserController, build_evidence_page
from .materials import MaterialEntryController, build_material_entry_page

__all__ = [
    "CaseHomeController",
    "DebateTheaterController",
    "EvidenceBrowserController",
    "MaterialEntryController",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_TITLE",
    "NICEGUI_MISSING_MESSAGE",
    "PLOTLY_MISSING_MESSAGE",
    "WebUIUnavailableError",
    "build_arg_parser",
    "build_timeline_figure",
    "create_app",
    "build_debate_theater_page",
    "build_evidence_page",
    "build_material_entry_page",
    "parse_as_of",
    "run",
    "snapshot_view",
]
