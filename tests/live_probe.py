"""Read-only live probe.  Never presses a key, never launches a game.

Run from the N.E.K.O repo root (or any cwd with the N.E.K.O source available):

    N.E.K.O/.venv/Scripts/python.exe plugin/plugins/subnautica_player/tests/live_probe.py

It prints installed games, running windows, foreground/cursor state, capture
backend, frame features and HUD.  Use it to calibrate a machine before blaming
the plugin.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def _find_neko_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "plugin" / "sdk" / "plugin").is_dir():
            return parent
    raise SystemExit("Cannot find the N.E.K.O source root (plugin/sdk/plugin).")


def _load_plugin():
    sys.path.insert(0, str(_find_neko_root()))
    from plugin.plugins.subnautica_player import discovery
    from plugin.plugins.subnautica_player.service import SubnauticaService

    return discovery, SubnauticaService


async def main(save_frame: str | None) -> int:
    discovery, service_cls = _load_plugin()
    service = service_cls()
    service.configure({})
    service._running = True
    service._scan_running(force=True)
    installed = service.installed_ids()
    running = service.ensure_running_game(force=True)
    report = {
        "installed": installed,
        "running": [game.summary() for game in service._running_games],
        "unsupported": discovery.unsupported_running(),
        "selected": running.profile.id if running else None,
        "window": running.window.summary() if running and running.window else None,
        "foreground": service._is_game_foreground(),
        "cursor_visible": discovery.cursor_visible(),
        "menu_active": discovery.menu_active(running.window.hwnd) if running and running.window else None,
    }

    if running is None:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 2

    service._game = running.profile
    if not service._is_game_foreground():
        report["capture"] = "skipped: game is not in the foreground"
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 4
    frame = await service._capture_frame(force=True)
    if frame is None:
        report["capture_error"] = service._capture_error_detail()
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 3

    features = service._analyzer.analyze(frame.image)
    hud = service._hud.observe(frame.image, features)
    report.update({
        "frame": frame.summary(),
        "features": features.summary(),
        "hud": hud.summary(),
        "hud_line": hud.one_line(),
        "input": service._ensure_injector().stats(),
        "config": service.cfg.as_dict(),
    })
    if save_frame:
        from PIL import Image

        Image.fromarray(frame.image).save(save_frame)
        report["saved_frame"] = save_frame
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--save-frame", default="")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.save_frame or None)))
