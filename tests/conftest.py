"""Offline SDK stub for the plugin-local test suite.

The Market CI checks the real SDK separately via ``neko-plugin check``.  These
tests must also run in a bare checkout (and in the package build) where the
N.E.K.O source tree is not importable, so they inject the smallest faithful
facade: decorators record their metadata, ``NekoPluginBase`` provides the few
attributes the plugin actually touches, and nothing here performs I/O.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
NEKO_ROOT = PLUGIN_DIR.parents[2]


def _install_stub() -> None:
    for name in [key for key in sys.modules if key == "plugin" or key.startswith("plugin.")]:
        sys.modules.pop(name, None)

    plugin_pkg = types.ModuleType("plugin")
    plugin_pkg.__path__ = [str(NEKO_ROOT / "plugin")]  # type: ignore[attr-defined]
    sdk_pkg = types.ModuleType("plugin.sdk")
    sdk_pkg.__path__ = []  # type: ignore[attr-defined]
    sdk_mod = types.ModuleType("plugin.sdk.plugin")

    class SdkError(Exception):
        def __init__(self, message: str = "", *, code: str | None = None) -> None:
            super().__init__(message)
            self.message = message
            self.code = code

    class Result:
        def __init__(self, *, ok: bool, value=None, error=None) -> None:
            self.ok = ok
            self.value = value
            self.error = error

    def Ok(value=None):
        return Result(ok=True, value=value)

    def Err(error=None):
        return Result(ok=False, error=error)

    def _decorator(**metadata):
        def apply(func):
            func.__neko_metadata__ = dict(metadata)
            return func
        return apply

    def plugin_entry(**metadata):
        return _decorator(**metadata)

    def llm_tool(**metadata):
        return _decorator(**metadata)

    def lifecycle(**metadata):
        return _decorator(**metadata)

    class _Ui:
        action = staticmethod(lambda **metadata: _decorator(**metadata))

    class PluginConfig:
        def __init__(self, ctx) -> None:
            self._ctx = ctx

        async def dump(self, timeout: float = 5.0):
            return dict(self._ctx.config)

        async def get_int(self, key: str, default: int = 0):
            return default

        async def update(self, **kwargs):
            return Ok({})

    class NekoPluginBase:
        def __init__(self, ctx) -> None:
            self.ctx = ctx
            self.config = PluginConfig(ctx)
            self.logger = logging.getLogger("subnautica_player.tests")

        @property
        def plugin_id(self) -> str:
            return str(getattr(self.ctx, "plugin_id", "subnautica_player"))

        def data_path(self, *parts):
            base = Path(self.ctx.data_dir)
            return base.joinpath(*parts) if parts else base

        def cache_path(self, *parts):
            base = Path(self.ctx.data_dir) / "cache"
            return base.joinpath(*parts) if parts else base

        def enable_file_logging(self, log_level: str = "INFO"):
            logger = logging.getLogger("subnautica_player.tests.file")
            logger.setLevel(getattr(logging, log_level, logging.INFO))
            return logger

        def push_message(self, **kwargs):
            return self.ctx.push_message(**kwargs)

    class StubContext:
        def __init__(self, *, plugin_id: str = "subnautica_player", config=None, data_dir=None):
            self.plugin_id = plugin_id
            self.config = config or {}
            self.data_dir = Path(data_dir or ".")
            self.messages: list[dict] = []
            self.metadata = {}

        def push_message(self, **kwargs):
            self.messages.append(kwargs)
            return {"submitted": True}

    for name, value in {
        "SdkError": SdkError,
        "Result": Result,
        "Ok": Ok,
        "Err": Err,
        "NekoPluginBase": NekoPluginBase,
        "StubContext": StubContext,
        "neko_plugin": lambda cls: cls,
        "plugin_entry": plugin_entry,
        "llm_tool": llm_tool,
        "lifecycle": lifecycle,
        "ui": _Ui,
        "PluginMeta": object,
        "Plugins": object,
        "PluginRouter": object,
        "PluginConfig": PluginConfig,
        "PluginStore": object,
        "SystemInfo": object,
        "tr": lambda key, **kwargs: key,
    }.items():
        setattr(sdk_mod, name, value)

    plugin_pkg.sdk = sdk_pkg
    sdk_pkg.plugin = sdk_mod
    sys.modules["plugin"] = plugin_pkg
    sys.modules["plugin.sdk"] = sdk_pkg
    sys.modules["plugin.sdk.plugin"] = sdk_mod


_install_stub()

# Import through the exact package path the host uses.  The synthetic
# ``plugin`` package points at the real ``N.E.K.O/plugin`` directory so pytest
# can also import these test modules as package children.
import importlib  # noqa: E402

_SUBMODULE = "plugin.plugins.subnautica_player"
if _SUBMODULE not in sys.modules:
    importlib.import_module(_SUBMODULE)
