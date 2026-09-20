"""The app is a small platform. Each tool (the schedule now; receipts, storage and so on later) is a Module: it lists
its pages, may add tabs to Settings and cards to Home, and gets its own folder for data. See docs/ADDING_A_MODULE.md."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import dominos_schedule as core


@dataclass
class PageSpec:
    key: str        # unique across the whole app; also the name used with app.show_page(key)
    title: str      # sidebar text
    icon: str       # a glyph from the Windows icon font (see NAV_ICONS in schedule_gui.py)
    cls: object     # the page class, or its name in schedule_gui


@dataclass
class Module:
    key: str                                   # short and permanent; names the module's data folder
    name: str                                  # shown on Home and as the sidebar heading
    description: str = ""
    section: str = ""                          # sidebar heading; empty for none
    pages: list = field(default_factory=list)  # [PageSpec]
    settings_tabs: list = field(default_factory=list)   # [(tab title, build(settings_page, body_frame))]
    home_card: Optional[Callable] = None       # build(parent, app) -> a widget for the Home page; None for a plain card


# Tools that are planned but not built yet; Home lists them so it is clear where they will appear.
PLANNED = [("Receipts", "Log receipts and keep them searchable."),
           ("Storage", "Browse and back up files on your TrueNAS server.")]

_registry: list = []


def register(module: Module) -> Module:
    if any(m.key == module.key for m in _registry):
        raise ValueError(f"module {module.key!r} is already registered")
    taken = {p.key for m in _registry for p in m.pages}
    clash = taken & {p.key for p in module.pages}
    if clash:
        raise ValueError(f"page keys already used: {sorted(clash)}")
    _registry.append(module)
    return module


def modules() -> list:
    return list(_registry)


def all_pages() -> list:
    return [page for module in _registry for page in module.pages]


def nav() -> list:
    """[(key, title)] in sidebar order."""
    return [(p.key, p.title) for p in all_pages()]


def sections() -> dict:
    """{first page key of a module: heading} for modules that have one."""
    return {m.pages[0].key: m.section for m in _registry if m.pages and m.section}


def module_dir(key: str) -> Path:
    """A folder a module can keep its own files in (created on first use). It lives next to the other app data."""
    path = core.BASE_DIR / "modules" / key
    path.mkdir(parents=True, exist_ok=True)
    return path


def module_settings(config: dict, key: str) -> dict:
    """The module's own section of config.json (config['modules'][key]), created empty if it isn't there yet."""
    return config.setdefault("modules", {}).setdefault(key, {})
