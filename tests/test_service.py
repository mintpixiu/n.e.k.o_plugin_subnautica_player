from __future__ import annotations

import asyncio
import time

import numpy as np
from plugin.plugins.subnautica_player import capture, catalog, discovery
from plugin.plugins.subnautica_player.service import SubnauticaService


class FakeInjector:
    def __init__(self) -> None:
        self.held_keys = ()
        self.held_buttons = ()
        self.released = 0
        self.taps: list[tuple[str, float]] = []
        self.clicks: list[tuple[int, int, str]] = []
        self.looks: list[tuple[float, float]] = []

    def release_all(self) -> None:
        self.released += 1

    def key_down(self, name: str) -> None:
        self.taps.append((name, -1.0))

    def key_up(self, name: str) -> None:
        pass

    def tap(self, name: str, hold_seconds: float = 0.05) -> None:
        self.taps.append((name, hold_seconds))

    def look(self, dx: float, dy: float) -> None:
        self.looks.append((dx, dy))

    def click(self, button: str = "left") -> None:
        self.clicks.append((0, 0, button))

    def click_at(self, x, y, *, screen_w, screen_h, button="left", hold_seconds=0.06):
        self.clicks.append((x, y, button))

    def physical_keys_down(self, *, include_mouse: bool = True):
        return []

    def scroll(self, notches: int) -> None:
        pass

    def stats(self):
        return {"available": True, "held_keys": [], "held_buttons": [],
                "input_struct_ok": True, "events_sent": 0}


def fake_running() -> discovery.RunningGame:
    window = discovery.WindowInfo(
        hwnd=4242,
        pid=123,
        title="Subnautica",
        visible=True,
        rect=(0, 0, 1920, 1080),
        minimized=False,
        class_name="UnityWndClass",
    )
    return discovery.RunningGame(
        profile=catalog.GAME_BY_ID["subnautica"],
        pid=123,
        exe_path=r"D:\steam\steamapps\common\Subnautica\Subnautica.exe",
        window=window,
    )


def make_service(tmp_path, monkeypatch):
    running = fake_running()
    monkeypatch.setattr(discovery, "find_running_games", lambda: [running])
    monkeypatch.setattr(discovery, "installed_games", lambda refresh=False: {"subnautica": object()})
    monkeypatch.setattr(discovery, "is_foreground", lambda hwnd: True)
    service = SubnauticaService(data_dir=tmp_path)
    service.configure({"dry_run": True})
    service._running = True
    service._capture_frame = _async_none  # type: ignore[assignment]
    service.play(running.profile, objective="先做扫描仪")
    service._injector = FakeInjector()
    return service


async def _async_none(*args, **kwargs):
    return None


async def _fake_frame(*args, **kwargs):
    return capture.Frame(
        image=np.zeros((80, 120, 3), dtype=np.uint8),
        captured_at=time.time(),
        backend="fake",
        region=None,
    )


def test_play_starts_in_waiting_mode_and_does_not_autostart_game(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    status = service.get_status()
    assert status["autopilot_enabled"] is True
    assert status["mode"] == "waiting"
    assert status["objective"] == "先做扫描仪"
    assert status["running"]


def test_command_is_queued_for_one_tick(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    result = service.apply_command("上浮")
    assert result["ok"]
    command = service._policy.take_command()
    assert command is not None
    assert command.ops[0].key == "space"


def test_stop_releases_everything_and_blocks_commands(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    injector = service._injector
    result = service.stop_playing(reason="user")
    assert result["mode"] == "stopped"
    assert injector.released >= 1
    blocked = service.apply_command("往前")
    assert blocked["ok"] is False
    assert "停手" in blocked["summary"]


def test_resume_requires_game_and_foreground(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    service.stop_playing(reason="user")
    result = service.resume_playing(reason="user")
    assert result["ok"] is True
    assert service._autopilot_active is True


def test_ui_actions_are_refused_before_entering_gameplay(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    service._has_entered_gameplay = False
    service._ui_kind = "menu"
    service._latest_frame = asyncio.run(_fake_frame())
    click = asyncio.run(service.ui_click(0.5, 0.5))
    key = asyncio.run(service.ui_key("enter"))
    sequence = asyncio.run(service.run_sequence([{"action": "tap", "key": "enter"}]))
    for result in (click, key, sequence):
        assert result["ok"] is False
        assert "主菜单" in result["summary"]


def test_ui_click_is_refused_in_gameplay(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    service._has_entered_gameplay = True
    service._ui_kind = "gameplay"
    service._latest_frame = asyncio.run(_fake_frame())
    result = asyncio.run(service.ui_click(0.5, 0.5))
    assert result["ok"] is False
    assert "没有可见指针" in result["summary"]


def test_ui_click_uses_cursor_in_pda(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    service._has_entered_gameplay = True
    service._ui_kind = "pda"
    service._latest_frame = asyncio.run(_fake_frame())
    service._refresh_after_ui = _async_none  # type: ignore[assignment]
    result = asyncio.run(service.ui_click(0.25, 0.75))
    assert result["ok"] is True
    assert service._injector.clicks[-1][:2] == (30, 60)


def test_ui_click_adds_window_offset_for_windowed_capture(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    service._has_entered_gameplay = True
    service._ui_kind = "pda"
    service._full_frame_size = (1920, 1080)
    service._latest_frame = capture.Frame(
        image=np.zeros((80, 120, 3), dtype=np.uint8),
        captured_at=time.time(),
        backend="fake",
        region=(100, 200, 220, 280),
    )
    service._refresh_after_ui = _async_none  # type: ignore[assignment]
    result = asyncio.run(service.ui_click(0.5, 0.5))
    assert result["ok"] is True
    assert service._injector.clicks[-1][:2] == (160, 240)


def test_sequence_sprint_holds_both_keys(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    service._mode = "playing"
    service._has_entered_gameplay = True
    service._capture_frame = _fake_frame  # type: ignore[assignment]
    service._refresh_after_ui = _async_none  # type: ignore[assignment]
    result = asyncio.run(service.run_sequence([{"action": "sprint", "seconds": 0.1}]))
    assert result["ok"] is True
    pressed = [name for name, _ in service._injector.taps]
    assert "w" in pressed and "shift" in pressed


def test_sequence_runs_and_caps_length(tmp_path, monkeypatch):
    service = make_service(tmp_path, monkeypatch)
    service._mode = "playing"
    service._has_entered_gameplay = True
    service._capture_frame = _fake_frame  # type: ignore[assignment]
    service._refresh_after_ui = _async_none  # type: ignore[assignment]
    too_long = [{"action": "wait", "seconds": 1.0} for _ in range(20)]
    assert asyncio.run(service.run_sequence(too_long))["ok"] is False
    result = asyncio.run(service.run_sequence([{"action": "tap", "key": "e"}]))
    assert result["ok"] is True
    assert service._injector.taps[-1][0] == "e"
