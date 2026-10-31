"""The long-lived service behind the Subnautica plugin facade.

Responsibilities are deliberately narrow and testable:

* discover the game the player opened and keep its window / foreground guard
  honest;
* capture the frame, analyse it and run the reactive :class:`policy.PlayPolicy`;
* stream screenshots and a structured situation report into the dialogue model
  so the catgirl can narrate, change the plan, or click a PDA/fabricator UI;
* yield to the player the instant they touch WASD/E/Tab/mouse buttons (or press
  F9), and release every held key when doing so;
* persist a journal and an honest milestone checklist across sessions.

No task is started from ``@lifecycle(startup)``: that hook runs on a transient
event loop, and tasks created there are cancelled when it returns.  The facade
starts the service lazily from the first tool/entry call on the host's loop.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from . import capture as capture_mod
from . import catalog, discovery, prompts
from . import hud as hud_mod
from . import input as input_mod
from . import planner as planner_mod
from . import policy as policy_mod
from . import vision as vision_mod

__all__ = ["ServiceConfig", "SubnauticaService"]


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_float(value: Any, default: float, *, low: float | None = None,
              high: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = float(default)
    if low is not None:
        result = max(low, result)
    if high is not None:
        result = min(high, result)
    return result


def _as_int(value: Any, default: int, *, low: int | None = None,
            high: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = int(default)
    if low is not None:
        result = max(low, result)
    if high is not None:
        result = min(high, result)
    return result


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


@dataclass
class ServiceConfig:
    capture_backend: str = "auto"
    capture_monitor: int = 0
    min_capture_edge: int = 160
    target_fps: float = 3.0
    frame_max_edge_px: int = 1152
    frame_jpeg_quality: int = 72
    frame_max_bytes: int = 120000
    stream_frames_to_llm: bool = True
    frame_stream_interval_seconds: float = 10.0
    commentary_interval_seconds: float = 30.0
    problem_cooldown_seconds: float = 60.0
    urgent_cooldown_seconds: float = 24.0
    require_foreground: bool = True
    autopilot_enabled: bool = False
    autopilot_tick_seconds: float = 0.35
    dry_run: bool = False
    max_session_minutes: float = 90.0
    blind_frame_limit: int = 8
    look_units_per_degree: float = 13.0
    horizontal_fov: float = 70.0
    vertical_fov: float = 45.0
    invert_y: bool = False
    yield_on_human_input: bool = True
    human_input_grace_seconds: float = 12.0
    pause_hotkey_enabled: bool = True
    auto_resume_from_menu: bool = False
    menu_stable_ticks: int = 3
    start_in_game_required: bool = True
    oxygen_safe_seconds: float = 28.0
    objective: str = ""
    plan_steps: tuple[str, ...] = ()
    keys: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "ServiceConfig":
        if not isinstance(raw, Mapping):
            raw = {}
        cfg = cls()
        cfg.capture_backend = _as_str(raw.get("capture_backend"), cfg.capture_backend).lower()
        if cfg.capture_backend not in ("auto", "dxgi", "gdi", "pil"):
            cfg.capture_backend = "auto"
        cfg.capture_monitor = _as_int(raw.get("capture_monitor"), cfg.capture_monitor, low=0, high=8)
        cfg.min_capture_edge = _as_int(raw.get("min_capture_edge"), cfg.min_capture_edge, low=32, high=2048)
        cfg.target_fps = _as_float(raw.get("target_fps"), cfg.target_fps, low=0.5, high=15.0)
        cfg.frame_max_edge_px = _as_int(raw.get("frame_max_edge_px"), cfg.frame_max_edge_px, low=320, high=2048)
        cfg.frame_jpeg_quality = _as_int(raw.get("frame_jpeg_quality"), cfg.frame_jpeg_quality, low=10, high=95)
        cfg.frame_max_bytes = _as_int(raw.get("frame_max_bytes"), cfg.frame_max_bytes, low=0, high=360000)
        cfg.stream_frames_to_llm = _as_bool(raw.get("stream_frames_to_llm"), cfg.stream_frames_to_llm)
        cfg.frame_stream_interval_seconds = _as_float(
            raw.get("frame_stream_interval_seconds"), cfg.frame_stream_interval_seconds, low=2.0, high=120.0)
        cfg.commentary_interval_seconds = _as_float(
            raw.get("commentary_interval_seconds"), cfg.commentary_interval_seconds, low=8.0, high=300.0)
        cfg.problem_cooldown_seconds = _as_float(
            raw.get("problem_cooldown_seconds"), cfg.problem_cooldown_seconds, low=5.0, high=600.0)
        cfg.urgent_cooldown_seconds = _as_float(
            raw.get("urgent_cooldown_seconds"), cfg.urgent_cooldown_seconds, low=5.0, high=300.0)
        cfg.require_foreground = _as_bool(raw.get("require_foreground"), cfg.require_foreground)
        cfg.autopilot_enabled = _as_bool(raw.get("autopilot_enabled"), cfg.autopilot_enabled)
        cfg.autopilot_tick_seconds = _as_float(
            raw.get("autopilot_tick_seconds"), cfg.autopilot_tick_seconds, low=0.1, high=3.0)
        cfg.dry_run = _as_bool(raw.get("dry_run"), cfg.dry_run)
        cfg.max_session_minutes = _as_float(
            raw.get("max_session_minutes"), cfg.max_session_minutes, low=0.0, high=600.0)
        cfg.blind_frame_limit = _as_int(raw.get("blind_frame_limit"), cfg.blind_frame_limit, low=2, high=120)
        cfg.look_units_per_degree = _as_float(
            raw.get("look_units_per_degree"), cfg.look_units_per_degree, low=0.2, high=200.0)
        cfg.horizontal_fov = _as_float(raw.get("horizontal_fov"), cfg.horizontal_fov, low=50.0, high=130.0)
        cfg.vertical_fov = _as_float(raw.get("vertical_fov"), cfg.vertical_fov, low=25.0, high=100.0)
        cfg.invert_y = _as_bool(raw.get("invert_y"), cfg.invert_y)
        cfg.yield_on_human_input = _as_bool(raw.get("yield_on_human_input"), cfg.yield_on_human_input)
        cfg.human_input_grace_seconds = _as_float(
            raw.get("human_input_grace_seconds"), cfg.human_input_grace_seconds, low=2.0, high=120.0)
        cfg.pause_hotkey_enabled = _as_bool(raw.get("pause_hotkey_enabled"), cfg.pause_hotkey_enabled)
        cfg.auto_resume_from_menu = _as_bool(raw.get("auto_resume_from_menu"), cfg.auto_resume_from_menu)
        cfg.menu_stable_ticks = _as_int(raw.get("menu_stable_ticks"), cfg.menu_stable_ticks, low=2, high=30)
        cfg.start_in_game_required = _as_bool(
            raw.get("start_in_game_required"), cfg.start_in_game_required)
        cfg.oxygen_safe_seconds = _as_float(
            raw.get("oxygen_safe_seconds"), cfg.oxygen_safe_seconds, low=8.0, high=120.0)
        cfg.objective = _as_str(raw.get("objective"), "")
        raw_steps = raw.get("plan_steps")
        if isinstance(raw_steps, (list, tuple)):
            cfg.plan_steps = tuple(str(item).strip() for item in raw_steps if str(item).strip())
        raw_keys = raw.get("keys")
        if isinstance(raw_keys, Mapping):
            cfg.keys = {
                str(key): str(value).strip()
                for key, value in raw_keys.items()
                if isinstance(value, str) and value.strip()
            }
        return cfg

    def as_dict(self) -> dict[str, Any]:
        return {
            "capture_backend": self.capture_backend,
            "capture_monitor": self.capture_monitor,
            "min_capture_edge": self.min_capture_edge,
            "target_fps": self.target_fps,
            "frame_max_edge_px": self.frame_max_edge_px,
            "frame_jpeg_quality": self.frame_jpeg_quality,
            "frame_max_bytes": self.frame_max_bytes,
            "stream_frames_to_llm": self.stream_frames_to_llm,
            "frame_stream_interval_seconds": self.frame_stream_interval_seconds,
            "commentary_interval_seconds": self.commentary_interval_seconds,
            "problem_cooldown_seconds": self.problem_cooldown_seconds,
            "urgent_cooldown_seconds": self.urgent_cooldown_seconds,
            "require_foreground": self.require_foreground,
            "autopilot_enabled": self.autopilot_enabled,
            "autopilot_tick_seconds": self.autopilot_tick_seconds,
            "dry_run": self.dry_run,
            "max_session_minutes": self.max_session_minutes,
            "blind_frame_limit": self.blind_frame_limit,
            "look_units_per_degree": self.look_units_per_degree,
            "horizontal_fov": self.horizontal_fov,
            "vertical_fov": self.vertical_fov,
            "invert_y": self.invert_y,
            "yield_on_human_input": self.yield_on_human_input,
            "human_input_grace_seconds": self.human_input_grace_seconds,
            "pause_hotkey_enabled": self.pause_hotkey_enabled,
            "auto_resume_from_menu": self.auto_resume_from_menu,
            "menu_stable_ticks": self.menu_stable_ticks,
            "start_in_game_required": self.start_in_game_required,
            "oxygen_safe_seconds": self.oxygen_safe_seconds,
            "objective": self.objective,
            "plan_steps": list(self.plan_steps),
            "keys": dict(self.keys),
        }


class SubnauticaService:
    """Owns capture, input, policy, planner and the background loops."""

    def __init__(
        self,
        *,
        logger: Any = None,
        push_message_fn: Callable[..., Any] | None = None,
        data_dir: Any = None,
    ) -> None:
        self._logger = logger
        self._push = push_message_fn
        self._data_dir = data_dir
        self.cfg = ServiceConfig()
        self._lang = prompts.DEFAULT_LANG

        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._op_lock = asyncio.Lock()
        self._capturer: capture_mod.ScreenCapturer | None = None
        self._injector: input_mod.KeyInjector | None = None
        self._analyzer = vision_mod.FrameAnalyzer()
        self._hud = hud_mod.HudTracker()
        self._policy: policy_mod.PlayPolicy | None = None
        plan_path = None
        if data_dir is not None:
            try:
                plan_path = data_dir / "plan.json"
            except Exception:
                plan_path = None
        self._planner = planner_mod.Planner(plan_path)

        self._game: catalog.GameProfile | None = None
        self._running_game: discovery.RunningGame | None = None
        self._running_games: list[discovery.RunningGame] = []
        self._last_scan_at = 0.0

        self._mode = "idle"  # idle | waiting | playing | soft_yield | paused | stopped
        self._autopilot_active = False
        self._objective = ""
        self._plan_steps: list[str] = []

        self._latest_frame: capture_mod.Frame | None = None
        self._latest_features: vision_mod.FrameFeatures | None = None
        self._latest_hud = hud_mod.HudState()
        self._latest_decision: policy_mod.Decision | None = None
        self._last_ops: list[policy_mod.Op] = []
        self._last_ops_at = 0.0
        self._ui_kind = "unknown"

        self._tick_count = 0
        self._frames_seen = 0
        self.frames_streamed = 0
        self.commentaries = 0
        self._last_tick_at = 0.0
        self._started_at = 0.0
        self._session_started_at = 0.0
        self._played_seconds = 0.0
        self._underwater_since = 0.0
        self._underwater_seconds = 0.0

        self._blind_ticks = 0
        self._menu_ticks = 0
        self._gameplay_ticks = 0
        self._has_entered_gameplay = False
        self._away = False
        self._human_seen_at = 0.0
        self._pause_latched = False
        self._last_hotkey_down = False
        self._takeover_announced_at = 0.0
        self._waiting_announced_at = 0.0
        self._menu_announced_at = 0.0
        self._last_menu_press_at = 0.0

        self._last_stream_at = 0.0
        self._last_commentary_at = 0.0
        self._last_urgent_at = 0.0
        self._last_problem_at = 0.0
        self._last_problem_key = ""

        self._events: list[dict[str, Any]] = []
        self._errors: list[str] = []
        self._last_objective_announced = ""

    # ------------------------------------------------------------------
    # Logging / events
    # ------------------------------------------------------------------

    def _log(self, level: str, message: str, *args: Any) -> None:
        logger = self._logger
        if logger is None:
            return
        try:
            getattr(logger, level)(message, *args)
        except Exception:
            pass

    def _record_error(self, message: str) -> None:
        self._errors.append(str(message)[:500])
        if len(self._errors) > 30:
            del self._errors[:-30]
        self._log("warning", "{}", message)

    def _event(self, kind: str, detail: str = "") -> None:
        self._events.append({"at": round(time.time(), 3), "kind": kind, "detail": detail[:200]})
        if len(self._events) > 60:
            del self._events[:-60]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        discovery.enable_dpi_awareness()
        self._running = True
        self._started_at = time.time()
        self._spawn(self._control_loop(), "control")
        self._spawn(self._hotkey_loop(), "hotkey")
        self._spawn(self._narration_loop(), "narration")
        self._log("info", "Subnautica service started")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._autopilot_active = False
        self._release_all()
        tasks = list(self._tasks)
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self._capturer is not None:
            try:
                self._capturer.close()
            except Exception:
                pass
            self._capturer = None
        try:
            self._planner.end_session()
        except Exception:
            pass
        self._mode = "idle"
        self._log("info", "Subnautica service stopped")

    def _spawn(self, coro, name: str) -> None:
        task = asyncio.create_task(coro, name=f"subnautica-{name}")
        self._tasks.append(task)

        def _done(completed: asyncio.Task) -> None:
            if completed.cancelled():
                return
            exc = completed.exception()
            if exc is not None:
                self._record_error(f"{name} loop died: {type(exc).__name__}: {exc}")

        task.add_done_callback(_done)

    # ------------------------------------------------------------------
    # Configuration / language
    # ------------------------------------------------------------------

    def configure(self, raw: Mapping[str, Any] | None) -> ServiceConfig:
        self.cfg = ServiceConfig.from_dict(raw)
        self._objective = self.cfg.objective
        self._plan_steps = list(self.cfg.plan_steps)
        if self._policy is not None and self._game is not None:
            self._policy = policy_mod.PlayPolicy(
                look_units_per_degree=self.cfg.look_units_per_degree,
                horizontal_fov=self.cfg.horizontal_fov,
                vertical_fov=self.cfg.vertical_fov,
                invert_y=self.cfg.invert_y,
                oxygen_safe_seconds=self.cfg.oxygen_safe_seconds,
            )
            self._policy.set_game(self._game, key_overrides=self.cfg.keys)
        if self._injector is not None:
            self._injector.require_foreground = self.cfg.require_foreground
            self._injector.dry_run = self.cfg.dry_run
        return self.cfg

    def set_lang(self, lang: str | None) -> None:
        self._lang = prompts.normalise_lang(lang)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def installed_ids(self) -> list[str]:
        return sorted(discovery.installed_games().keys())

    def running_ids(self) -> list[str]:
        self._scan_running(force=True)
        return sorted({game.profile.id for game in self._running_games})

    def _scan_running(self, *, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self._last_scan_at) < 2.0:
            return
        self._last_scan_at = now
        try:
            self._running_games = discovery.find_running_games()
        except Exception as exc:
            self._record_error(f"running-game scan failed: {type(exc).__name__}: {exc}")
            self._running_games = []
        selected = None
        if self._game is not None:
            selected = next(
                (game for game in self._running_games if game.profile.id == self._game.id),
                None,
            )
        self._running_game = selected

    def _select_running(self) -> discovery.RunningGame | None:
        if self._game is not None:
            for game in self._running_games:
                if game.profile.id == self._game.id:
                    return game
        return self._running_games[0] if self._running_games else None

    def ensure_running_game(self, *, force: bool = False) -> discovery.RunningGame | None:
        self._scan_running(force=force)
        if self._game is None:
            running = self._select_running()
            if running is not None:
                self._apply_running(running)
            return running
        selected = next(
            (item for item in self._running_games if item.profile.id == self._game.id),
            None,
        )
        if selected is not None and selected.window is None:
            selected = None
        if selected is not None:
            self._running_game = selected
            return selected
        self._running_game = None
        return None

    def _apply_running(self, running: discovery.RunningGame) -> None:
        if self._game is not None and self._game.id == running.profile.id:
            self._running_game = running
            return
        self._game = running.profile
        self._running_game = running
        self._policy = policy_mod.PlayPolicy(
            look_units_per_degree=self.cfg.look_units_per_degree,
            horizontal_fov=self.cfg.horizontal_fov,
            vertical_fov=self.cfg.vertical_fov,
            invert_y=self.cfg.invert_y,
            oxygen_safe_seconds=self.cfg.oxygen_safe_seconds,
        )
        self._policy.set_game(running.profile, key_overrides=self.cfg.keys)
        self._hud = hud_mod.HudTracker()
        self._planner.set_game(running.profile)
        self._event("adopted", running.profile.id)

    def _is_game_foreground(self) -> bool:
        running = self._running_game
        return bool(running and running.window and discovery.is_foreground(running.window.hwnd))

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def _ensure_capturer(self) -> capture_mod.ScreenCapturer:
        if self._capturer is None:
            self._capturer = capture_mod.ScreenCapturer(
                logger=self._logger,
                monitor=self.cfg.capture_monitor,
                preferred=self.cfg.capture_backend,
            )
        return self._capturer

    def _crop_to_window(self, frame: capture_mod.Frame) -> capture_mod.Frame:
        """Crop a full-desktop frame to the game window in normalized space.

        Capture reports physical pixels (DXGI) while ``GetWindowRect`` is
        DPI-virtualized on this host (2560 physical vs 1707 logical).  Cropping
        by *normalized* geometry is correct in both cases; cropping by raw pixel
        values would slice off the right/bottom of the game.
        """
        running = self._running_game
        if running is None or running.window is None:
            return frame
        left, top, right, bottom = discovery.physical_window_rect(running.window.hwnd)
        height, width = frame.image.shape[:2]
        if right <= left or bottom <= top:
            return frame
        # Fullscreen: DWM bounds equal the physical desktop (e.g. 0,0,2560,1600)
        # while GetWindowRect reported 1707x1067.  No crop, no DPI guesswork.
        if left <= 1 and top <= 1 and right >= width - 1 and bottom >= height - 1:
            return frame
        fx0 = max(0.0, min(1.0, left / float(max(1, width))))
        fy0 = max(0.0, min(1.0, top / float(max(1, height))))
        fx1 = max(0.0, min(1.0, right / float(max(1, width))))
        fy1 = max(0.0, min(1.0, bottom / float(max(1, height))))
        if fx1 - fx0 < 0.05 or fy1 - fy0 < 0.05:
            return frame
        try:
            x0, x1 = int(fx0 * width), int(fx1 * width)
            y0, y1 = int(fy0 * height), int(fy1 * height)
            if x1 - x0 < 64 or y1 - y0 < 64:
                return frame
            image = frame.image[y0:y1, x0:x1]
            return capture_mod.Frame(
                image=image,
                captured_at=frame.captured_at,
                backend=frame.backend,
                region=(x0, y0, x1, y1),
            )
        except Exception:
            return frame

    async def _capture_frame(self, *, force: bool = False) -> capture_mod.Frame | None:
        running = self._running_game
        if running is None or running.window is None:
            return None
        if not running.window.is_usable(min_edge=self.cfg.min_capture_edge):
            return None
        if not self._is_game_foreground() and not force:
            return None
        try:
            capturer = self._ensure_capturer()
        except Exception as exc:
            self._record_error(f"capture init failed: {type(exc).__name__}: {exc}")
            return None
        region = None  # full desktop; _crop_to_window handles windowed mode
        frame = capturer.capture(region, retries=1)
        if frame is None:
            previous = self._latest_frame
            if previous is not None and (time.time() - previous.captured_at) < 1.2:
                return previous
            await asyncio.sleep(0.03)
            frame = capturer.capture(region, retries=2)
        if frame is None:
            return None
        frame = self._crop_to_window(frame)
        self._latest_frame = frame
        self._frames_seen += 1
        return frame

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------

    def _ensure_injector(self) -> input_mod.KeyInjector:
        if self._injector is None:
            self._injector = input_mod.KeyInjector(
                logger=self._logger,
                require_foreground=self.cfg.require_foreground,
                foreground_check=self._is_game_foreground,
                dry_run=self.cfg.dry_run,
            )
        return self._injector

    def _release_all(self) -> None:
        if self._injector is None:
            return
        try:
            self._injector.release_all()
        except Exception as exc:
            self._log("debug", "release_all failed: {}", exc)

    async def _sleep_watched(self, seconds: float) -> bool:
        """Sleep in 100 ms slices; true means control was interrupted."""
        remaining = max(0.0, float(seconds))
        while remaining > 0:
            slice_seconds = min(0.1, remaining)
            await asyncio.sleep(slice_seconds)
            remaining -= slice_seconds
            if self._mode not in ("playing", "soft_yield"):
                return True
            if self.cfg.yield_on_human_input and self._mode == "playing":
                keys = self._human_keys()
                if keys:
                    self._human_seen_at = time.time()
                    self._yield_to_human(keys)
                    return True
        return False

    async def _run_ops(self, ops: list[policy_mod.Op]) -> None:
        injector = self._ensure_injector()
        if not ops:
            return
        self._last_ops = list(ops)
        self._last_ops_at = time.time()
        for op in ops:
            if not self._running or self._mode not in ("playing", "soft_yield"):
                return
            try:
                if op.kind == "wait":
                    if await self._sleep_watched(max(0.0, op.seconds)):
                        return
                elif op.kind == "look":
                    injector.look(op.dx, op.dy)
                    await asyncio.sleep(0.06)
                elif op.kind == "tap":
                    if not op.key:
                        continue
                    injector.key_down(op.key)
                    try:
                        if await self._sleep_watched(max(0.01, op.seconds)):
                            return
                    finally:
                        injector.key_up(op.key)
                elif op.kind == "hold":
                    if not op.key:
                        continue
                    injector.key_down(op.key)
                    try:
                        if await self._sleep_watched(max(0.01, op.seconds)):
                            return
                    finally:
                        injector.key_up(op.key)
                elif op.kind == "click":
                    injector.click(button=op.button)
                    await asyncio.sleep(0.05)
                elif op.kind == "scroll":
                    injector.scroll(op.notches)
                    await asyncio.sleep(0.05)
            except (input_mod.InputGuardError, input_mod.InputUnavailable, ValueError) as exc:
                self._note_input_blocked(exc)
                return
        await asyncio.sleep(0.02)

    def _note_input_blocked(self, exc: Exception) -> None:
        if isinstance(exc, input_mod.InputGuardError) and "dry_run" in str(exc):
            self._push_problem("DRY_RUN_NOTE")
        elif isinstance(exc, input_mod.InputGuardError):
            self._push_problem("NOT_FOCUSED")
        else:
            self._push_problem("INPUT_FAILED", str(exc))
        self._log("debug", "input blocked: {}", exc)

    # ------------------------------------------------------------------
    # Loops
    # ------------------------------------------------------------------

    async def _control_loop(self) -> None:
        self._log("info", "control loop started")
        while self._running:
            started = time.monotonic()
            try:
                await self._control_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error(f"control tick failed: {type(exc).__name__}: {exc}")
                await asyncio.sleep(0.5)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.02, (1.0 / self.cfg.target_fps) - elapsed))

    async def _control_tick(self) -> None:
        if not self._running:
            return
        if not self._autopilot_active and self._injector is not None and (
                self._injector.held_keys or self._injector.held_buttons):
            self._release_all()
            return
        if not self._autopilot_active:
            return
        if self._op_lock.locked():
            return
        now = time.monotonic()
        dt = min(2.0, max(0.0, now - self._last_tick_at)) if self._last_tick_at else 0.0
        self._last_tick_at = now
        self._tick_count += 1

        if self._session_expired():
            self._push_cue("CUE_PREFIX_URGENT", prompts.t("SESSION_LIMIT", lang=self._lang))
            self.stop_playing(reason="session-limit")
            return

        running = self.ensure_running_game()
        if running is None:
            self._announce_waiting()
            return
        if self._mode == "stopped":
            return
        if running.window is None or not running.window.is_usable(min_edge=self.cfg.min_capture_edge):
            self._push_problem(
                "WINDOW_MINIMIZED",
                f"window={running.window.width}x{running.window.height}" if running.window else "no-window",
            )
            return
        if not self._is_game_foreground():
            if not self._away:
                self._away = True
                self._release_all()
                self._push_problem("NOT_FOCUSED")
            return
        if self._away:
            self._away = False
            self._event("foreground", "back")

        if self._mode in ("paused", "stopped"):
            self._release_all()
            return

        frame = await self._capture_frame()
        if frame is None:
            self._blind_ticks += 1
            if self._blind_ticks == max(2, self.cfg.blind_frame_limit // 2):
                self._push_problem("NO_CAPTURE", self._capture_error_detail())
            return

        features = self._analyzer.analyze(frame.image)
        hud = self._hud.observe(frame.image, features)
        self._latest_features = features
        self._latest_hud = hud

        if features.black:
            self._blind_ticks += 1
            if self._blind_ticks >= self.cfg.blind_frame_limit:
                self._release_all()
                if self._blind_ticks == self.cfg.blind_frame_limit:
                    self._push_problem("BLIND_CAPTURE", self._capture_error_detail())
                return
        else:
            self._blind_ticks = 0

        if self.cfg.yield_on_human_input and self._mode in ("playing", "soft_yield"):
            keys = self._human_keys()
            if keys:
                self._human_seen_at = time.time()
                if self._mode == "playing":
                    self._yield_to_human(keys)
                return
            if self._mode == "soft_yield":
                if (time.time() - self._human_seen_at) >= self.cfg.human_input_grace_seconds and \
                        not self._pause_latched and self._autopilot_active:
                    self._mode = "playing"
                    self._push_cue("CUE_PREFIX", prompts.t("RESUMED", lang=self._lang))
                return

        cursor = discovery.cursor_visible()
        self._ui_kind = self._classify_ui(features, hud, cursor)
        if self._ui_kind in ("menu", "loading") and not self._has_entered_gameplay:
            # Main-menu art contains enough blue/orange to fool a colour-ring
            # detector.  Do not report a fake oxygen/water reading from it.
            hud = hud_mod.HudState(damage=hud.damage)
            self._latest_hud = hud
        if self._ui_kind in ("menu", "pda", "loading"):
            self._menu_ticks += 1
            state = {
                "menu": policy_mod.PolicyState.MENU,
                "pda": policy_mod.PolicyState.CRAFT,
                "loading": policy_mod.PolicyState.WAITING,
            }[self._ui_kind]
            self._latest_decision = policy_mod.Decision(
                state,
                reason=f"画面是 {self._ui_kind} 界面，不盲按；等主人或 LLM 操作",
                needs_llm=self._has_entered_gameplay and self._ui_kind == "pda",
                mode="craft" if self._ui_kind == "pda" else "wait",
            )
            if self._ui_kind == "menu":
                self._handle_menu()
            return
        self._menu_ticks = 0

        if not self._has_entered_gameplay:
            if cursor:
                self._handle_menu()
                return
            self._gameplay_ticks += 1
            if self._gameplay_ticks >= 2:
                self._has_entered_gameplay = True
                self._event("gameplay", "started")
                self._push_cue(
                    prompts.t("PLAYING", lang=self._lang, game=running.profile.name_zh),
                )
        if self.cfg.start_in_game_required and not self._has_entered_gameplay:
            return
        if self._mode == "soft_yield":
            return

        if self._mode == "waiting":
            self._mode = "playing"
        self._played_seconds += dt
        self._planner.tick_session(self._played_seconds)
        if features.underwater:
            if not self._underwater_since:
                self._underwater_since = time.time()
            self._underwater_seconds = time.time() - self._underwater_since
        else:
            self._underwater_since = 0.0
            self._underwater_seconds = 0.0

        snapshot = self._planner.snapshot()
        decision = self._policy.decide(
            features,
            hud,
            now=time.time(),
            underwater_seconds=self._underwater_seconds,
            ui_kind=self._ui_kind,
            planner_mode=snapshot.mode,
            planner_goal=snapshot.focus or (snapshot.milestone or {}).get("title", ""),
            session_seconds=self._played_seconds,
        ) if self._policy is not None else policy_mod.Decision(
            policy_mod.PolicyState.WAITING, reason="策略尚未初始化")
        self._latest_decision = decision
        await self._run_ops(decision.ops)
        self._maybe_push_event(decision)

    async def _hotkey_loop(self) -> None:
        """F9 works even while the autopilot is paused/stopped."""
        while self._running:
            try:
                if self.cfg.pause_hotkey_enabled:
                    down = input_mod.KeyInjector.pause_hotkey_down()
                    if down and not self._last_hotkey_down:
                        if self._mode in ("paused", "stopped") and self._game is not None:
                            self.resume_playing(reason="hotkey")
                        elif self._autopilot_active:
                            self.stop_playing(reason="hotkey")
                    self._last_hotkey_down = down
                await asyncio.sleep(0.12)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error(f"hotkey loop failed: {type(exc).__name__}: {exc}")
                await asyncio.sleep(0.5)

    async def _narration_loop(self) -> None:
        self._log("info", "narration loop started")
        while self._running:
            try:
                await asyncio.sleep(0.8)
                self._narration_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error(f"narration failed: {type(exc).__name__}: {exc}")
                await asyncio.sleep(1.0)

    def _narration_tick(self) -> None:
        frame = self._latest_frame
        if frame is None:
            return
        if self._mode not in ("playing", "soft_yield") or self._game is None:
            return
        if (time.time() - frame.captured_at) > 3.0:
            return
        now = time.time()
        if self.cfg.stream_frames_to_llm and \
                (now - self._last_stream_at) >= self.cfg.frame_stream_interval_seconds:
            self._last_stream_at = now
            if self._stream_frame(frame):
                self.frames_streamed += 1
        if (now - self._last_commentary_at) >= self.cfg.commentary_interval_seconds:
            self._last_commentary_at = now
            self._push_commentary(frame)

    # ------------------------------------------------------------------
    # UI classification / menu
    # ------------------------------------------------------------------

    def _classify_ui(self, features, hud, cursor_visible: bool) -> str:
        if features.black:
            return "loading"
        if cursor_visible:
            if not self._has_entered_gameplay:
                return "menu"
            if features.dark_ui > 0.58 or not hud.present:
                return "pda"
            return "menu"
        if features.dark_ui > 0.93 and features.mean_luma < 0.06:
            return "loading"
        return "gameplay"

    def _human_keys(self) -> list[str]:
        try:
            return self._ensure_injector().physical_keys_down()
        except Exception:
            return []

    def _yield_to_human(self, keys: list[str]) -> None:
        self._mode = "soft_yield"
        self._human_seen_at = time.time()
        self._release_all()
        if (time.time() - self._takeover_announced_at) > 20.0:
            self._takeover_announced_at = time.time()
            detail = "、".join(keys[:3])
            self._push_cue(prompts.t("TAKEOVER", lang=self._lang, keys=detail))
        self._event("takeover", ",".join(keys[:4]))

    def _handle_menu(self) -> None:
        running = self._running_game
        if not running:
            return
        if not self._has_entered_gameplay:
            now = time.time()
            if (now - self._menu_announced_at) > 45.0:
                self._menu_announced_at = now
                self._push_cue(
                    prompts.t("MENU_WAITING", lang=self._lang, game=running.profile.name_zh),
                )
            return
        if not self.cfg.auto_resume_from_menu:
            return
        if self._menu_ticks < self.cfg.menu_stable_ticks:
            return
        if (time.time() - self._human_seen_at) < self.cfg.human_input_grace_seconds:
            return
        if (time.time() - self._last_menu_press_at) < 4.0:
            return
        injector = self._ensure_injector()
        try:
            injector.tap(self._policy._key("exit") if self._policy else "e", hold_seconds=0.06)
            self._last_menu_press_at = time.time()
            self._menu_ticks = 0
            self._event("menu-resume", "escape/e")
        except Exception as exc:
            self._note_input_blocked(exc)

    # ------------------------------------------------------------------
    # Public control API
    # ------------------------------------------------------------------

    def play(self, game: catalog.GameProfile, *, objective: str = "") -> dict[str, Any]:
        self._game = game
        self._policy = policy_mod.PlayPolicy(
            look_units_per_degree=self.cfg.look_units_per_degree,
            horizontal_fov=self.cfg.horizontal_fov,
            vertical_fov=self.cfg.vertical_fov,
            invert_y=self.cfg.invert_y,
            oxygen_safe_seconds=self.cfg.oxygen_safe_seconds,
        )
        self._policy.set_game(game, key_overrides=self.cfg.keys)
        self._hud = hud_mod.HudTracker()
        self._analyzer.reset()
        self._injector = input_mod.KeyInjector(
            logger=self._logger,
            require_foreground=self.cfg.require_foreground,
            foreground_check=self._is_game_foreground,
            dry_run=self.cfg.dry_run,
        )
        self._planner.set_game(game)
        self._objective = str(objective or "").strip() or self._planner.snapshot().focus or \
            self._default_objective(game)
        self._plan_steps = list(self.cfg.plan_steps)
        self._mode = "waiting"
        self._autopilot_active = True
        self._pause_latched = False
        self._has_entered_gameplay = False
        self._gameplay_ticks = 0
        self._blind_ticks = 0
        self._menu_ticks = 0
        self._away = False
        self._played_seconds = 0.0
        self._session_started_at = time.time()
        self._underwater_seconds = 0.0
        self._underwater_since = 0.0
        self._last_tick_at = time.monotonic()
        self._last_objective_announced = ""
        self._scan_running(force=True)
        self._running_game = next(
            (item for item in self._running_games if item.profile.id == game.id),
            None,
        )
        self._event("play", game.id)
        self._log("info", "play requested: {} objective={!r}", game.id, self._objective)
        return self.get_status()

    def stop_playing(self, *, reason: str = "user") -> dict[str, Any]:
        self._autopilot_active = False
        self._pause_latched = True
        self._release_all()
        self._mode = "stopped"
        try:
            self._planner.end_session()
        except Exception:
            pass
        self._event("stop", reason)
        if reason in ("user", "tool", "session-limit"):
            self._push_cue(prompts.t("STOPPED", lang=self._lang))
        return self.get_status()

    def resume_playing(self, *, reason: str = "user") -> dict[str, Any]:
        if self._game is None:
            return {"ok": False, "summary": "还没有选游戏；先让我玩《深海迷航》。", "status": self.get_status()}
        if self._running_game is None:
            self.ensure_running_game(force=True)
        if self._running_game is None:
            return {
                "ok": False,
                "summary": f"《{self._game.name_zh}》没有在运行。主人先手动打开并进入存档；我不主动启动游戏。",
                "status": self.get_status(),
            }
        if not self._is_game_foreground():
            return {
                "ok": False,
                "summary": "游戏不在前台；先切回游戏，我再接手。",
                "status": self.get_status(),
            }
        self._autopilot_active = True
        self._pause_latched = False
        self._mode = "playing" if self._has_entered_gameplay else "waiting"
        self._last_tick_at = time.monotonic()
        if not self._session_started_at:
            self._session_started_at = time.time()
        self._event("resume", reason)
        self._push_cue(prompts.t("RESUMED", lang=self._lang))
        return {"ok": True, "summary": "好，我接着来。", "status": self.get_status()}

    def apply_command(self, text: str, *, seconds: float = 0.0) -> dict[str, Any]:
        if self._policy is None:
            return {"ok": False, "summary": "还没开始游戏；先让我玩《深海迷航》。"}
        if not self._autopilot_active or self._mode == "stopped":
            return {"ok": False, "summary": "我现在停手了；先说「继续」再给动作。"}
        command = self._policy.command_ops(text, seconds=seconds)
        if not command.ops:
            # Stop/resume are service-level actions, not key presses.
            if any(word in text for word in ("停手", "别玩", "停止", "stop")):
                self.stop_playing(reason="user")
                return {"ok": True, "summary": "停手了，所有按键都松开了。"}
            if any(word in text for word in ("继续", "接着玩", "resume", "continue")):
                result = self.resume_playing(reason="user")
                return {"ok": bool(result.get("ok")), "summary": result.get("summary", "")}
            return {"ok": False, "summary": command.summary or "没听懂这个动作。"}
        if self._mode == "paused":
            self._mode = "playing"
        self._policy.queue_command(text)
        self._event("command", command.summary)
        return {
            "ok": True,
            "summary": command.summary or f"收到：{text}",
            "command": command.as_dict(),
            "note": "动作会在下一次控制 tick（约 0.35 秒）执行。",
        }

    def set_directive(self, *, mode: str, goal: str, steps: str = "",
                      seconds: float = 45.0) -> dict[str, Any]:
        if self._policy is None:
            return {"ok": False, "summary": "还没开始游戏；先让我玩《深海迷航》。"}
        directive = self._policy.set_directive(mode=mode, goal=goal, steps=steps, seconds=seconds)
        if goal:
            self._objective = goal
            self._planner.set_focus(goal, steps)
        self._plan_steps = [part.strip() for part in str(steps or "").replace("；", ";").split(";") if part.strip()]
        self._event("directive", f"{directive.mode}:{directive.goal}")
        return {
            "ok": True,
            "summary": f"现在按「{directive.goal or directive.mode}」来，模式 {directive.mode}，约 {directive.seconds:.0f} 秒。",
            "directive": directive.summary(),
        }

    def aim(self, x: float, y: float, *, fire: bool = False,
            button: str = "left") -> dict[str, Any]:
        if self._policy is None:
            return {"ok": False, "summary": "还没开始游戏。"}
        frame = self._latest_frame
        ops = self._policy.aim_ops(
            x, y,
            screen_w=frame.width if frame else 0,
            screen_h=frame.height if frame else 0,
        )
        if fire:
            ops.append(policy_mod.Op(kind="click", button=button, note="fire"))
        self._policy.queue_ops(ops, text="aim", summary="转向并开火" if fire else "转向")
        self._event("aim", f"{x:.2f},{y:.2f},fire={fire}")
        return {
            "ok": True,
            "summary": f"视角转向画面 ({float(x):.2f}, {float(y):.2f})" + (" 并开火。" if fire else "。"),
            "ops": [op.summary() for op in ops],
        }

    async def run_sequence(self, actions: Any) -> dict[str, Any]:
        if not isinstance(actions, list) or not actions:
            return {"ok": False, "summary": "sequence 需要一个动作列表。"}
        if len(actions) > 16:
            return {"ok": False, "summary": "一次最多 16 个动作，避免失控。"}
        if self._policy is None or not self._autopilot_active:
            return {"ok": False, "summary": "现在没有在玩；先让我开始。"}
        if not self._has_entered_gameplay:
            return {"ok": False, "summary": "还在主菜单/加载画面；先由主人进入存档，我不在菜单里按键。"}
        if not self._is_game_foreground():
            return {"ok": False, "summary": "游戏不在前台，拒绝执行序列。"}
        ops: list[policy_mod.Op] = []
        total_seconds = 0.0
        for index, item in enumerate(actions):
            if not isinstance(item, Mapping):
                return {"ok": False, "summary": f"第 {index + 1} 个动作不是对象。"}
            action = str(item.get("action") or item.get("kind") or "").strip().lower()
            seconds = _as_float(item.get("seconds"), 0.0, low=0.0, high=8.0)
            key = str(item.get("key") or "").strip()
            if action in ("forward", "back", "left", "right", "up", "down", "sprint"):
                key = self._policy._key(action)
                seconds = seconds or 1.0
            elif action in ("hold", "tap"):
                seconds = seconds or (0.8 if action == "hold" else 0.08)
            elif action == "wait":
                seconds = seconds or 0.5
                ops.append(policy_mod.Op(kind="wait", seconds=seconds))
                total_seconds += seconds
                continue
            elif action in ("look", "turn"):
                dx = _as_float(item.get("dx"), 0.0, low=-360.0, high=360.0)
                dy = _as_float(item.get("dy"), 0.0, low=-180.0, high=180.0)
                ops.append(policy_mod.Op(kind="look", dx=dx, dy=dy))
                continue
            elif action in ("aim", "look_at"):
                x = _as_float(item.get("x"), 0.5, low=0.0, high=1.0)
                y = _as_float(item.get("y"), 0.5, low=0.0, high=1.0)
                frame = self._latest_frame
                ops.extend(self._policy.aim_ops(
                    x, y,
                    screen_w=frame.width if frame else 0,
                    screen_h=frame.height if frame else 0,
                ))
                continue
            elif action in ("click", "shoot", "attack"):
                ops.append(policy_mod.Op(kind="click", button="left"))
                continue
            elif action == "scan":
                ops.append(policy_mod.Op(
                    kind="hold",
                    key=self._policy._key("scan"),
                    seconds=2.2,
                    note="scan",
                ))
                total_seconds += 2.2
                continue
            elif action in (
                "interact", "enter", "exit", "pda", "jump", "use",
                "reload", "deconstruct", "alt_tool",
                "slot1", "slot2", "slot3", "slot4", "slot5",
            ):
                key = self._policy._key(action)
                if not key:
                    return {"ok": False, "summary": f"动作 {action} 没有可用的键位。"}
                seconds = seconds or 0.1
            elif action == "scroll":
                ops.append(policy_mod.Op(kind="scroll", notches=_as_int(item.get("notches"), 1, low=-10, high=10)))
                continue
            elif not key:
                return {"ok": False, "summary": f"第 {index + 1} 个动作缺少 key/action。"}
            ops.append(policy_mod.Op(kind="hold" if action == "hold" else "tap", key=key, seconds=seconds))
            total_seconds += seconds
        if total_seconds > 15.0:
            return {"ok": False, "summary": "序列总时长超过 15 秒；拆成几段。"}
        async with self._op_lock:
            frame = await self._capture_frame(force=True)
            if frame is not None and not self._is_game_foreground():
                return {"ok": False, "summary": "游戏刚刚切到后台，序列取消。"}
            await self._run_ops(ops)
            await self._refresh_after_ui()
        self._event("sequence", f"{len(ops)} ops")
        return {
            "ok": True,
            "summary": f"执行了 {len(ops)} 个动作，共约 {total_seconds:.1f} 秒。",
            "ops": [op.summary() for op in ops],
            "status": self._quick_status(),
        }

    async def ui_click(self, x: float, y: float, *, button: str = "left",
                       double: bool = False) -> dict[str, Any]:
        """Click at a normalized coordinate in a menu/PDA/fabricator UI."""
        if not self._autopilot_active:
            return {"ok": False, "summary": "现在没有在玩。"}
        if not self._has_entered_gameplay:
            return {
                "ok": False,
                "summary": "还在主菜单/加载画面；先由主人进入存档，我不在菜单里点选或开新游戏。",
            }
        if not self._is_game_foreground():
            return {"ok": False, "summary": "游戏不在前台，拒绝点击。"}
        frame = self._latest_frame
        if frame is None:
            frame = await self._capture_frame(force=True)
        if frame is None:
            return {"ok": False, "summary": "没有可用画面，先不点击。"}
        if self._ui_kind not in ("pda", "menu"):
            return {
                "ok": False,
                "summary": "现在是第一人称游戏画面，没有可见指针；用 subnautica_command / subnautica_sequence。",
            }
        fx = max(0.0, min(1.0, _as_float(x, 0.5)))
        fy = max(0.0, min(1.0, _as_float(y, 0.5)))
        px, py = int(fx * frame.width), int(fy * frame.height)
        async with self._op_lock:
            injector = self._ensure_injector()
            try:
                injector.click_at(px, py, screen_w=frame.width, screen_h=frame.height,
                                  button=button, hold_seconds=0.06)
                if double:
                    await asyncio.sleep(0.12)
                    injector.click(button=button)
            except (input_mod.InputGuardError, input_mod.InputUnavailable, ValueError) as exc:
                self._note_input_blocked(exc)
                return {"ok": False, "summary": f"点击失败：{exc}"}
            await asyncio.sleep(0.18)
            await self._refresh_after_ui()
        self._event("ui-click", f"{fx:.2f},{fy:.2f},{button}")
        return {
            "ok": True,
            "summary": f"在画面 ({fx:.2f}, {fy:.2f}) 点了一下{('右键' if button == 'right' else '左键')}。",
            "ui": self._ui_kind,
            "frame": self._latest_features.summary() if self._latest_features else None,
            "hud": self._latest_hud.summary(),
        }

    async def ui_key(self, key: str) -> dict[str, Any]:
        if not self._autopilot_active:
            return {"ok": False, "summary": "现在没有在玩。"}
        if not self._has_entered_gameplay:
            return {"ok": False, "summary": "还在主菜单/加载画面；我不在菜单里按键。"}
        if not self._is_game_foreground():
            return {"ok": False, "summary": "游戏不在前台，拒绝按键。"}
        key = str(key or "").strip()
        if not key:
            return {"ok": False, "summary": "缺少 key。"}
        async with self._op_lock:
            injector = self._ensure_injector()
            try:
                injector.tap(key, hold_seconds=0.08)
            except (input_mod.InputGuardError, input_mod.InputUnavailable, ValueError) as exc:
                self._note_input_blocked(exc)
                return {"ok": False, "summary": f"按键失败：{exc}"}
            await asyncio.sleep(0.15)
            await self._refresh_after_ui()
        self._event("ui-key", key)
        return {"ok": True, "summary": f"按了 {key.upper()}。", "ui": self._ui_kind}

    async def ui_scroll(self, notches: int) -> dict[str, Any]:
        if not self._autopilot_active:
            return {"ok": False, "summary": "现在没有在玩。"}
        if not self._has_entered_gameplay:
            return {"ok": False, "summary": "还在主菜单/加载画面；我不在菜单里滚动。"}
        if not self._is_game_foreground():
            return {"ok": False, "summary": "游戏不在前台，拒绝滚动。"}
        count = _as_int(notches, 1, low=-10, high=10)
        async with self._op_lock:
            injector = self._ensure_injector()
            try:
                injector.scroll(count)
            except (input_mod.InputGuardError, input_mod.InputUnavailable, ValueError) as exc:
                self._note_input_blocked(exc)
                return {"ok": False, "summary": f"滚动失败：{exc}"}
            await asyncio.sleep(0.20)
            await self._refresh_after_ui()
        return {"ok": True, "summary": f"滚轮 {count:+d} 格。", "ui": self._ui_kind}

    async def _refresh_after_ui(self) -> None:
        frame = await self._capture_frame(force=True)
        if frame is None:
            return
        features = self._analyzer.analyze(frame.image)
        hud = self._hud.observe(frame.image, features)
        self._latest_features = features
        self._latest_hud = hud
        self._ui_kind = self._classify_ui(features, hud, discovery.cursor_visible())
        if self.cfg.stream_frames_to_llm:
            self._stream_frame(frame)

    # ------------------------------------------------------------------
    # Status / diagnostics
    # ------------------------------------------------------------------

    def _capture_error_detail(self) -> str:
        if self._capturer is None:
            return "capturer not created yet"
        return str(getattr(self._capturer, "last_error", "") or "capture returned no frame")

    def _session_expired(self) -> bool:
        if self.cfg.max_session_minutes <= 0 or not self._session_started_at:
            return False
        return self._played_seconds > self.cfg.max_session_minutes * 60.0

    def _default_objective(self, game: catalog.GameProfile | None = None) -> str:
        if game is None:
            game = self._game
        if game is None:
            return "先活下来，再探索"
        return (
            "先活下来并建立循环：补氧、找食物和水，扫描残骸，做出扫描仪和 Seaglide；"
            "然后向 100 米外探索，逐步做出海蛾号/海卡车与第一个基地。"
        )

    def _current_objective(self) -> str:
        if self._objective:
            return self._objective
        snap = self._planner.snapshot()
        if snap.focus:
            return snap.focus
        return self._default_objective()

    def _quick_status(self) -> dict[str, Any]:
        return {
            "mode": self._mode,
            "autopilot": self._autopilot_active,
            "ui": self._ui_kind,
            "game": self._game.name_zh if self._game else None,
            "running": self._running_game is not None,
            "focused": self._is_game_foreground(),
            "policy": self._latest_decision.state.value if self._latest_decision else "waiting",
            "underwater_seconds": round(self._underwater_seconds, 1),
        }

    def get_status(self) -> dict[str, Any]:
        if (time.time() - self._last_scan_at) > 2.0:
            self._scan_running(force=True)
        running = self._running_game
        window = running.window if running else None
        snapshot = self._planner.snapshot()
        status = {
            "ok": True,
            "selected_game": self._game.summary() if self._game else None,
            "objective": self._current_objective(),
            "plan": snapshot.as_dict(),
            "plan_steps": list(self._plan_steps),
            "installed": [
                catalog.GAME_BY_ID[gid].summary()
                for gid in self.installed_ids() if gid in catalog.GAME_BY_ID
            ],
            "running": [game.summary() for game in self._running_games],
            "window": window.summary() if window else None,
            "focused": self._is_game_foreground(),
            "cursor_visible": discovery.cursor_visible(),
            "menu_active": discovery.menu_active(window.hwnd) if window else None,
            "ui_kind": self._ui_kind,
            "capture": self._capturer.status() if self._capturer is not None else None,
            "autopilot_enabled": self._autopilot_active,
            "mode": self._mode,
            "dry_run": self.cfg.dry_run,
            "policy": {
                "state": self._latest_decision.state.value if self._latest_decision else "waiting",
                "reason": self._latest_decision.reason if self._latest_decision else "",
                "last_ops": [op.summary() for op in self._last_ops],
            },
            "features": self._latest_features.summary() if self._latest_features else None,
            "hud": self._latest_hud.summary(),
            "session_seconds": round(self._played_seconds, 1),
            "underwater_seconds": round(self._underwater_seconds, 1),
            "ticks": self._tick_count,
            "frames_seen": self._frames_seen,
            "frames_streamed": self.frames_streamed,
            "commentaries": self.commentaries,
            "takeover": {
                "mode": self._mode,
                "human_grace_seconds": self.cfg.human_input_grace_seconds,
                "pause_hotkey": "F9" if self.cfg.pause_hotkey_enabled else None,
            },
            "events": list(self._events[-12:]),
            "last_error": self._errors[-1] if self._errors else "",
            "errors": list(self._errors[-5:]),
        }
        game_name = self._game.name_zh if self._game else "未选游戏"
        status["summary"] = (
            f"{game_name}：mode={self._mode} autopilot={self._autopilot_active} "
            f"running={len(self._running_games)} focused={status['focused']} "
            f"ui={self._ui_kind} state={status['policy']['state']} "
            f"played={status['session_seconds']}s水下={status['underwater_seconds']}s "
            f"last={self._latest_decision.reason if self._latest_decision else 'idle'}"
        )
        return status

    async def selfcheck(self) -> dict[str, Any]:
        self._scan_running(force=True)
        problems: list[str] = []
        checks: dict[str, Any] = {}
        installed = self.installed_ids()
        checks["installed"] = [
            {"id": gid, "name": catalog.GAME_BY_ID[gid].name_zh}
            for gid in installed if gid in catalog.GAME_BY_ID
        ]
        checks["running"] = [game.summary() for game in self._running_games]
        unsupported = discovery.unsupported_running()
        if unsupported:
            problems.append("检测到不支持的《Subnautica 2》；本插件只操作《深海迷航》与《零度之下》。")
        if not installed:
            problems.append("没有在 Steam 库里找到 Subnautica / Below Zero；确认 Steam 库路径可读。")
        running = self._running_game or self.ensure_running_game(force=True)
        checks["selected"] = self._game.id if self._game else None
        if self._game is not None and running is None:
            problems.append(f"《{self._game.name_zh}》没有运行：需要主人先手动打开并进入存档。")
        if running is not None:
            checks["window"] = running.summary()
            if running.window is None:
                problems.append("进程在跑，但没找到可用的游戏窗口。")
            elif not running.window.is_usable(min_edge=self.cfg.min_capture_edge):
                problems.append(
                    f"窗口不可用（可能最小化/太小）：{running.window.rect}；插件不会对着 1x1 的画面按键。"
                )
            checks["foreground"] = self._is_game_foreground()
            if not checks["foreground"]:
                problems.append("游戏窗口不在最前面；插件只会等待，不会往别的窗口按键。")
            checks["cursor_visible"] = discovery.cursor_visible()
            checks["menu_active"] = discovery.menu_active(running.window.hwnd) if running.window else None
            checks["save_root"] = str(discovery.save_root(running.profile) or "")

        try:
            capturer = self._ensure_capturer()
            checks["capture"] = capturer.probe()
            if running is not None:
                frame = await self._capture_frame(force=True)
                if frame is None:
                    problems.append("截图失败：" + self._capture_error_detail())
                else:
                    features = self._analyzer.analyze(frame.image)
                    hud = self._hud.observe(frame.image, features)
                    checks["frame"] = frame.summary()
                    checks["features"] = features.summary()
                    checks["hud"] = hud.summary()
                    self._latest_frame = frame
                    self._latest_features = features
                    self._latest_hud = hud
                    if features.black:
                        problems.append(
                            "截到的画面是全黑的：独占全屏下优先使用 dxgi 后端；窗口/无边框模式 GDI 也可用。"
                        )
        except Exception as exc:
            checks["capture"] = {"error": f"{type(exc).__name__}: {exc}"}
            problems.append(f"截图后端初始化失败：{type(exc).__name__}: {exc}")

        injector = self._ensure_injector()
        checks["input"] = injector.stats()
        if not injector.available:
            problems.append("SendInput 不可用（非 Windows 或 user32 加载失败）。")
        if not injector.layout_is_valid():
            problems.append("INPUT 结构体大小不符合 ABI，注入会失败。")
        checks["config"] = self.cfg.as_dict()
        checks["dry_run"] = self.cfg.dry_run
        checks["ok"] = not problems
        checks["problems"] = problems
        if self.cfg.dry_run:
            checks["dry_run_note"] = "dry_run 开着：只观察、不按键；关掉后才会真正操作。"
        return checks

    async def look(self, note: str = "") -> dict[str, Any]:
        if not self._running:
            return {"ok": False, "summary": "服务还没启动。"}
        running = self.ensure_running_game(force=True)
        if running is None or running.window is None:
            return {"ok": False, "summary": "没看到游戏窗口。主人要先手动打开游戏并进入存档。"}
        if not self._is_game_foreground():
            return {"ok": False, "summary": "游戏窗口不在最前面，现在截图会截到别的东西；先切回游戏。"}
        frame = self._latest_frame
        if frame is None or (time.time() - frame.captured_at) > 0.8:
            frame = await self._capture_frame(force=True)
        if frame is None:
            return {"ok": False, "summary": "截图失败：" + self._capture_error_detail()}
        features = self._latest_features or self._analyzer.analyze(frame.image)
        hud = self._latest_hud
        self._ui_kind = self._classify_ui(features, hud, discovery.cursor_visible())
        if self._ui_kind in ("menu", "loading") and not self._has_entered_gameplay:
            hud = hud_mod.HudState(damage=hud.damage)
        if self.cfg.stream_frames_to_llm:
            self._stream_frame(frame)
        detail = vision_mod.frame_features_summary(features)
        return {
            "ok": True,
            "note": str(note or ""),
            "game": self._game.name_zh if self._game else None,
            "ui": self._ui_kind,
            "features": features.summary(),
            "hud": hud.summary(),
            "hud_line": hud.one_line(),
            "summary": f"看了一眼：{detail}。{hud.one_line()}",
        }

    # ------------------------------------------------------------------
    # Planner-facing API
    # ------------------------------------------------------------------

    def get_plan(self) -> dict[str, Any]:
        snapshot = self._planner.snapshot()
        return {
            "ok": True,
            "summary": snapshot.one_line(),
            "plan": snapshot.as_dict(),
            "prompt": self._planner.prompt_block(),
            "milestones": [m.as_dict() for m in self._planner.milestones()],
        }

    def mark_milestone(self, key: str, *, status: str = "done", note: str = "") -> dict[str, Any]:
        key = str(key or "").strip()
        if not self._planner.mark_milestone(key, status=status, note=note):
            valid = [m.key for m in self._planner.milestones()]
            return {
                "ok": False,
                "summary": f"没有这个阶段：{key}。可选：{', '.join(valid[:12])}",
                "valid": valid,
            }
        snapshot = self._planner.snapshot()
        return {"ok": True, "summary": snapshot.one_line(), "plan": snapshot.as_dict()}

    def record_note(self, kind: str, text: str) -> dict[str, Any]:
        if not str(text or "").strip():
            return {"ok": False, "summary": "记录内容为空。"}
        self._planner.record_note(kind, text)
        return {"ok": True, "summary": "记下了。", "plan": self._planner.snapshot().as_dict()}

    def observe_biome(self, biome_key: str) -> dict[str, Any]:
        if self._planner.observe_biome(biome_key):
            info = catalog.BIOMES[str(biome_key).strip().lower()]
            return {"ok": True, "summary": f"第一次记录生态区：{info['zh']}（{info['depth']}）。", "biome": info}
        valid = list(catalog.BIOMES)
        return {"ok": False, "summary": "没记录（未知或已记录）。", "valid": valid}

    # ------------------------------------------------------------------
    # LLM-facing prompts / pushes
    # ------------------------------------------------------------------

    def _situation_text(self) -> str:
        game_name = self._game.name_zh if self._game else "未选游戏"
        features = self._latest_features
        hud = self._latest_hud
        decision = self._latest_decision
        lines = [
            f"游戏：{game_name}",
            f"状态：{self._mode} / {decision.state.value if decision else 'waiting'} / UI={self._ui_kind}",
            f"目标：{self._current_objective()}",
            f"画面：{vision_mod.frame_features_summary(features)}",
            f"HUD：{hud.one_line()}",
            f"水下：{self._underwater_seconds:.1f}s，本次游玩 {self._played_seconds / 60.0:.1f} 分钟",
            f"我的判断：{decision.reason if decision else '先观察一下'}",
            f"计划：{self._planner.prompt_block()}",
        ]
        if decision is not None and decision.ops:
            lines.append("下一步操作：" + "；".join(op.label() for op in decision.ops[:4]))
        running = self._running_game
        if running is not None and running.window is not None:
            lines.append(
                f"窗口：{running.window.width}x{running.window.height}，"
                f"{'在前台' if self._is_game_foreground() else '不在前台'}"
            )
        return "\n".join(lines)

    def _push_parts(self, parts: list[dict[str, Any]], *, ai_behavior: str,
                    priority: int, coalesce_key: str, visibility=None) -> bool:
        if self._push is None:
            return False
        try:
            self._push(
                source="subnautica_player",
                visibility=list(visibility if visibility is not None else []),
                ai_behavior=ai_behavior,
                parts=parts,
                priority=priority,
                coalesce_key=coalesce_key,
            )
            return True
        except Exception as exc:
            self._log("debug", "push failed: {}", exc)
            return False

    def _push_cue(self, text: str, *, urgent: bool = False) -> bool:
        prefix = prompts.t("CUE_PREFIX_URGENT" if urgent else "CUE_PREFIX", lang=self._lang)
        return self._push_parts(
            [{"type": "text", "text": prefix + "\n" + text}],
            ai_behavior="respond",
            priority=7 if urgent else 6,
            coalesce_key="subnautica_cue",
        )

    def _frame_jpeg(self, frame: capture_mod.Frame | None = None) -> bytes | None:
        frame = frame or self._latest_frame
        if frame is None:
            return None
        return capture_mod.encode_jpeg(
            frame.image,
            max_edge=self.cfg.frame_max_edge_px,
            quality=self.cfg.frame_jpeg_quality,
            max_bytes=self.cfg.frame_max_bytes,
        )

    def _stream_frame(self, frame: capture_mod.Frame) -> bool:
        payload = self._frame_jpeg(frame)
        if not payload:
            return False
        return self._push_parts(
            [{"type": "image", "data": payload, "mime": "image/jpeg"}],
            ai_behavior="read",
            priority=2,
            coalesce_key="subnautica_frame",
        )

    def _push_commentary(self, frame: capture_mod.Frame) -> None:
        if self._game is None:
            return
        text = prompts.commentary(
            lang=self._lang,
            situation=self._situation_text(),
            game=self._game.name_zh,
            objective=self._current_objective(),
            plan=self._planner.prompt_block(),
        )
        parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
        payload = self._frame_jpeg(frame)
        if payload:
            parts.append({"type": "image", "data": payload, "mime": "image/jpeg"})
        if self._push_parts(
            parts,
            ai_behavior="respond",
            priority=4,
            coalesce_key="subnautica_commentary",
        ):
            self.commentaries += 1
            self._event("commentary", self._latest_decision.reason if self._latest_decision else "")

    def _maybe_push_event(self, decision: policy_mod.Decision) -> None:
        now = time.time()
        if decision.state in (
            policy_mod.PolicyState.SURFACE,
            policy_mod.PolicyState.EVADE,
            policy_mod.PolicyState.RECOVER,
            policy_mod.PolicyState.STUCK,
            policy_mod.PolicyState.CRAFT,
            policy_mod.PolicyState.BUILD,
        ) and (now - self._last_urgent_at) > self.cfg.urgent_cooldown_seconds:
            self._last_urgent_at = now
            self._push_cue(decision.speak or decision.reason, urgent=decision.urgent)
            self._last_commentary_at = now

    def _announce_waiting(self) -> None:
        now = time.time()
        if (now - self._waiting_announced_at) < self.cfg.problem_cooldown_seconds:
            return
        self._waiting_announced_at = now
        game_name = self._game.name_zh if self._game else "《深海迷航》"
        self._push_cue(prompts.t("WAITING", lang=self._lang, game=game_name))

    def _push_problem(self, key: str, detail: str = "") -> None:
        now = time.time()
        if (now - self._last_problem_at) < self.cfg.problem_cooldown_seconds:
            return
        self._last_problem_at = now
        self._last_problem_key = key
        self._push_cue(prompts.t(key, lang=self._lang, detail=detail or "unknown"))
