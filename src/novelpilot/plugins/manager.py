"""Plugin manager.

Mirrors the pattern used in BookOfShadows: a plugin is a module exposing a
``manifest`` dict and a ``run`` entry point, registered by name.  Keeping
plugins optional means the core engine has no dependency on cover generation,
illustration, or rights auditing.
"""

from __future__ import annotations

import importlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("novelpilot.plugins")


class Plugin(Protocol):
    manifest: dict[str, Any]

    async def run(self, **kwargs: Any) -> Any: ...


@dataclass(slots=True)
class PluginInfo:
    name: str
    version: str
    description: str
    module: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "module": self.module,
        }


class PluginManager:
    def __init__(self, search_paths: list[str] | None = None) -> None:
        self._plugins: dict[str, Plugin] = {}
        self._infos: dict[str, PluginInfo] = {}
        self.search_paths = search_paths or ["novelpilot.plugins"]

    def register(self, name: str, plugin: Plugin) -> None:
        self._plugins[name] = plugin
        manifest = getattr(plugin, "manifest", {}) or {}
        self._infos[name] = PluginInfo(
            name=name,
            version=str(manifest.get("version", "0.0.0")),
            description=str(manifest.get("description", "")),
            module=type(plugin).__module__,
        )

    def discover(self) -> list[PluginInfo]:
        """Import every module in the plugin package and register what it exposes."""
        for path in self.search_paths:
            try:
                package = importlib.import_module(path)
            except ImportError:
                continue
            pkg_dir = Path(getattr(package, "__file__", "") or "").parent
            if not pkg_dir.exists():
                continue
            for f in sorted(pkg_dir.glob("*.py")):
                if f.stem.startswith("_") or f.stem == "manager":
                    continue
                mod_name = f"{path}.{f.stem}"
                try:
                    mod = importlib.import_module(mod_name)
                except Exception as exc:  # noqa: BLE001
                    log.warning("failed to import plugin %s: %s", mod_name, exc)
                    continue
                plugin = getattr(mod, "plugin", None)
                if plugin is not None:
                    self.register(f.stem, plugin)
        return list(self._infos.values())

    def get(self, name: str) -> Plugin:
        if name not in self._plugins:
            self.discover()
        if name not in self._plugins:
            raise KeyError(f"no plugin named {name!r}")
        return self._plugins[name]

    def list(self) -> list[PluginInfo]:
        if not self._infos:
            self.discover()
        return list(self._infos.values())

    async def run(self, name: str, **kwargs: Any) -> Any:
        return await self.get(name).run(**kwargs)

    def write_manifests(self, outdir: str | Path) -> list[Path]:
        """Emit a manifest.json per plugin, for inspection or packaging."""
        outdir = Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for name, info in self._infos.items():
            path = outdir / f"{name}.manifest.json"
            path.write_text(json.dumps(info.to_dict(), indent=2), encoding="utf-8")
            written.append(path)
        return written
