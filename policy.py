"""Reactive Subnautica pilot plus a natural-language command parser.

The policy is intentionally *not* a speedrun script.  It keeps the catgirl
alive and moving while the dialogue model does the high-level thinking:

* emergency reflexes: surface on low oxygen, retreat on damage, yield in menus;
* exploration: swim in bursts, sweep the camera, descend gradually, click to
  collect, and climb a "stuck ladder" when the scene stops changing;
* commands: turn short Chinese/English phrases from the player or the LLM into
  concrete key/mouse operations;
* UI hand-off: while the PDA, fabricator or builder is open it does not inject
  movement; the LLM (or the player) drives the cursor through explicit tools.

Everything returns :class:`Op` primitives so :mod:`service` can slice long holds
into interruptible 100 ms chunks (the player must be able to take over at any
moment).
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from . import catalog

__all__ = [
    "Command",
    "Decision",
    "Directive",
    "LookCalibration",
    "Op",
    "PlayPolicy",
    "PolicyState",
]


class PolicyState(str, Enum):
    WAITING = "waiting"
    MENU = "menu"
    BLIND = "blind"
    SURFACE = "surface"
    EXPLORE = "explore"
    GATHER = "gather"
    SCAN = "scan"
    EVADE = "evade"
    RECOVER = "recover"
    CRAFT = "craft"
    BUILD = "build"
    STUCK = "stuck"
    PAUSED = "paused"


@dataclass
class Op:
    """One interruptible action primitive."""

    kind: str  # hold | tap | look | wait | click | scroll
    key: str = ""
    seconds: float = 0.0
    dx: float = 0.0
    dy: float = 0.0
    button: str = "left"
    notches: int = 0
    note: str = ""

    def summary(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind}
        if self.key:
            payload["key"] = self.key
        if self.seconds:
            payload["seconds"] = round(self.seconds, 2)
        if self.kind == "look":
            payload["dx"] = round(self.dx, 1)
            payload["dy"] = round(self.dy, 1)
        if self.kind == "click":
            payload["button"] = self.button
        if self.kind == "scroll":
            payload["notches"] = self.notches
        if self.note:
            payload["note"] = self.note
        return payload

    def label(self) -> str:
        if self.kind == "hold":
            return f"按住 {self.key} {self.seconds:.1f}s"
        if self.kind == "chord":
            return f"同时按住 {self.key} {self.seconds:.1f}s"
        if self.kind == "tap":
            return f"按 {self.key}"
        if self.kind == "look":
            return f"转视角 dx={self.dx:.0f} dy={self.dy:.0f}"
        if self.kind == "wait":
            return f"等 {self.seconds:.1f}s"
        if self.kind == "click":
            return f"{self.button} 键"
        if self.kind == "scroll":
            return f"滚轮 {self.notches:+d}"
        return self.kind


@dataclass
class LookCalibration:
    """Counts per degree for relative mouse-look."""

    units_per_degree: float = 13.0
    horizontal_fov: float = 70.0
    vertical_fov: float = 45.0
    invert_y: bool = False

    def turn_counts(self, degrees: float) -> float:
        return float(degrees) * max(1.0, float(self.units_per_degree))

    def counts_for_ndc(self, ndc_x: float, ndc_y: float) -> tuple[float, float]:
        """Convert a normalized screen target (0..1) to mouse counts."""
        yaw = (float(ndc_x) - 0.5) * self.horizontal_fov
        pitch = (float(ndc_y) - 0.5) * self.vertical_fov
        if self.invert_y:
            pitch = -pitch
        return self.turn_counts(yaw), self.turn_counts(pitch)


@dataclass
class Directive:
    """A temporary high-level mode set by the LLM or the player."""

    mode: str = "explore"
    goal: str = ""
    steps: str = ""
    seconds: float = 45.0
    created_at: float = field(default_factory=time.time)

    @property
    def until(self) -> float:
        return self.created_at + max(3.0, float(self.seconds))

    def expired(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) >= self.until

    def summary(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "goal": self.goal,
            "steps": self.steps,
            "seconds": round(self.seconds, 1),
            "expires_in": round(max(0.0, self.until - time.time()), 1),
        }


@dataclass
class Command:
    text: str
    ops: list[Op]
    summary: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "summary": self.summary,
            "ops": [op.summary() for op in self.ops],
        }


@dataclass
class Decision:
    state: PolicyState
    ops: list[Op] = field(default_factory=list)
    reason: str = ""
    speak: str = ""
    needs_llm: bool = False
    urgent: bool = False
    mode: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "reason": self.reason,
            "ops": [op.summary() for op in self.ops],
            "needs_llm": self.needs_llm,
            "urgent": self.urgent,
        }


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return re.sub(r"[\s_\-:：·・,，.。!！?？'\"“”‘’（）()\[\]【】]+", "", folded)


def _seconds(text: str, default: float) -> float:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(秒|s|sec|seconds)?", text)
    if not match:
        return default
    try:
        value = float(match.group(1))
    except ValueError:
        return default
    return max(0.08, min(12.0, value))


_DIRECTION_WORDS = {
    "forward": ("w", ("往前走", "向前", "前进", "往前", "向前游", "forward", "w")),
    "back": ("s", ("后退", "往后", "向后", "退后", "back", "s")),
    "left": ("a", ("左移", "往左", "向左", "左边", "left", "a")),
    "right": ("d", ("右移", "往右", "向右", "右边", "right", "d")),
    "up": ("space", ("上浮", "上升", "向上", "往上游", "浮上去", "swimup", "ascend", "up")),
    "down": ("c", ("下潜", "下降", "向下", "往下", "潜下去", "swimdown", "descend", "down")),
}


class PlayPolicy:
    """Tiny reactive state machine, independent of the SDK."""

    def __init__(
        self,
        *,
        look_units_per_degree: float = 13.0,
        horizontal_fov: float = 70.0,
        vertical_fov: float = 45.0,
        invert_y: bool = False,
        oxygen_safe_seconds: float = 28.0,
        stuck_seconds: float = 2.4,
    ) -> None:
        self.calibration = LookCalibration(
            units_per_degree=look_units_per_degree,
            horizontal_fov=horizontal_fov,
            vertical_fov=vertical_fov,
            invert_y=invert_y,
        )
        self.oxygen_safe_seconds = max(8.0, float(oxygen_safe_seconds))
        self.stuck_seconds = max(1.0, float(stuck_seconds))
        self._game: catalog.GameProfile | None = None
        self._keys: dict[str, str] = {}
        self._directive: Directive | None = None
        self._pending: Command | None = None
        self._reset_motion()

    # ------------------------------------------------------------------
    # Setup / state
    # ------------------------------------------------------------------

    def set_game(self, game: catalog.GameProfile, *, key_overrides: dict[str, str] | None = None) -> None:
        self._game = game
        self._keys = dict(key_overrides or {})
        self._reset_motion()
        self._directive = None
        self._pending = None

    def reset(self) -> None:
        self._reset_motion()
        self._directive = None
        self._pending = None

    def _reset_motion(self) -> None:
        self._last_hash = 0
        self._same_since: float | None = None
        self._stuck_level = 0
        self._phase_started = 0.0
        self._phase_index = 0
        self._sweep_sign = 1
        self._last_collect = 0.0
        self._started_at = time.time()

    def _key(self, action: str) -> str:
        if action in self._keys and self._keys[action]:
            return str(self._keys[action])
        if self._game is not None:
            return self._game.key(action, self._keys)
        return catalog.SUBNAUTICA_KEYS.get(action, "")

    def _tap(self, action: str, *, hold: float = 0.07) -> list[Op]:
        key = self._key(action)
        if not key:
            return []
        return [Op(kind="tap", key=key, seconds=hold, note=action)]

    def _hold(self, action: str, seconds: float) -> list[Op]:
        key = self._key(action)
        if not key:
            return []
        return [Op(kind="hold", key=key, seconds=float(seconds), note=action)]

    def _look(self, dx_degrees: float, dy_degrees: float = 0.0) -> list[Op]:
        return [Op(
            kind="look",
            dx=self.calibration.turn_counts(dx_degrees),
            dy=self.calibration.turn_counts(dy_degrees),
            note=f"turn {dx_degrees:.0f}/{dy_degrees:.0f}",
        )]

    def directive(self) -> Directive | None:
        if self._directive is not None and self._directive.expired():
            self._directive = None
        return self._directive

    def set_directive(self, *, mode: str, goal: str, steps: str = "",
                      seconds: float = 45.0) -> Directive:
        mode = str(mode or "explore").strip().lower()
        if mode not in {
            "explore", "surface", "gather", "scan", "fight", "evade",
            "craft", "build", "vehicle", "story", "wait",
        }:
            mode = "explore"
        self._directive = Directive(
            mode=mode,
            goal=str(goal or "").strip(),
            steps=str(steps or "").strip(),
            seconds=float(seconds or 45.0),
        )
        self._phase_started = 0.0
        return self._directive

    def clear_directive(self) -> None:
        self._directive = None

    def queue_command(self, text: str) -> Command:
        command = self.command_ops(text)
        self._pending = command
        return command

    def queue_ops(self, ops: list[Op], *, text: str = "", summary: str = "") -> Command:
        """Queue an already-built action list (used by the UI/aim tools)."""
        command = Command(text=text, ops=list(ops), summary=summary or "序列动作")
        self._pending = command
        return command

    def take_command(self) -> Command | None:
        command = self._pending
        self._pending = None
        return command

    # ------------------------------------------------------------------
    # Command parsing
    # ------------------------------------------------------------------

    def command_ops(self, text: str, *, seconds: float = 0.0) -> Command:
        raw = str(text or "").strip()
        normalized = _norm(raw)
        if not normalized:
            return Command(raw, [], "没有听懂要做什么")
        default_seconds = _seconds(raw, 1.2) if seconds <= 0 else min(12.0, float(seconds))

        # Stop / resume are handled by the service, but recognising them here
        # gives a precise result instead of a random key.
        if any(word in normalized for word in ("停手", "别玩", "停止", "暂停", "stop", "pause")):
            return Command(raw, [], "停手（由服务层松开按键）")
        if any(word in normalized for word in ("继续", "接着玩", "resume", "continue")):
            return Command(raw, [], "继续（由服务层恢复）")

        # Modifiers first.
        sprint = any(word in normalized for word in ("冲刺", "加速", "sprint", "快游"))
        if sprint:
            seconds_value = min(6.0, default_seconds + 1.0)
            forward = self._key("forward") or "w"
            sprint_key = self._key("sprint") or "shift"
            return Command(
                raw,
                [Op(kind="chord", key=f"{forward}+{sprint_key}", seconds=seconds_value, note="sprint")],
                "向前冲刺",
            )

        # Look commands.  Positive mouse dx turns right; positive dy turns down.
        if any(word in normalized for word in ("左看", "左转", "看左边", "lookleft", "turnleft")):
            return Command(raw, self._look(-45), "向左转 45°")
        if any(word in normalized for word in ("右看", "右转", "看右边", "lookright", "turnright")):
            return Command(raw, self._look(45), "向右转 45°")
        if any(word in normalized for word in ("抬头", "向上看", "看上面", "lookup")):
            return Command(raw, self._look(0, -20), "抬头")
        if any(word in normalized for word in ("低头", "向下看", "看下面", "lookdown")):
            return Command(raw, self._look(0, 20), "低头")

        # Scan is a hold, not a tap: the scanner needs ~2 s on target.
        if any(word in normalized for word in ("扫描", "scan")):
            return Command(
                raw,
                [Op(kind="hold", key=self._key("use") or "mouse1", seconds=2.2, note="scan")],
                "按住左键扫描 2.2 秒",
            )

        # Interact / vehicle / holster.
        if any(word in normalized for word in (
            "互动", "交互", "按e", "按E", "进入", "上船", "下船", "上载具", "下载具",
            "进舱", "出舱", "use", "interact", "enter", "exit",
        )):
            return Command(raw, self._tap("interact", hold=0.12), "按 E 互动/进出载具")

        if any(word in normalized for word in ("打开pda", "开pda", "pda", "物品栏", "背包", "tab")):
            return Command(raw, self._tap("pda", hold=0.12), "按 Tab 打开 PDA")

        if any(word in normalized for word in ("收起", "收起工具", "holster")):
            return Command(raw, self._tap("exit", hold=0.12), "按 E 收起工具")

        if any(word in normalized for word in ("拆除", "拆掉", "deconstruct")):
            return Command(raw, self._tap("deconstruct", hold=0.12), "按 Q 拆除")

        if any(word in normalized for word in ("换弹", "装填", "reload")):
            return Command(raw, self._tap("reload", hold=0.12), "按 R 换弹/装电池")

        if any(word in normalized for word in ("建造", "放置", "build", "place")):
            return Command(raw, self._tap("use", hold=0.12), "左键放置/建造")

        if any(word in normalized for word in ("旋转", "rotate")):
            key = "q" if any(word in normalized for word in ("左", "left")) else "e"
            return Command(raw, [Op(kind="tap", key=key, seconds=0.1)], f"按 {key.upper()} 旋转")

        # Use / pickup / attack.
        if any(word in normalized for word in ("拾取", "捡起", "捡", "pickup", "pick")):
            return Command(raw, self._tap("use", hold=0.12), "左键拾取")
        if any(word in normalized for word in ("攻击", "打", "咬", "attack")):
            return Command(raw, self._tap("attack", hold=0.16), "左键攻击")
        if any(word in normalized for word in ("使用", "用", "use", "物品")):
            return Command(raw, self._tap("use", hold=0.12), "左键使用")

        # Quick slots.
        slot_match = re.fullmatch(
            r"(?:按|快捷栏|槽位|slot)?([1-5])(?:号|槽|slot)?",
            normalized,
        )
        if slot_match:
            number = slot_match.group(1)
            return Command(raw, self._tap(f"slot{number}", hold=0.08), f"选择快捷栏 {number}")

        # Movement holds.  Longest phrase wins, so "上浮" cannot become "上".
        matches: list[tuple[int, str, str]] = []
        for action, (key, words) in _DIRECTION_WORDS.items():
            for word in words:
                if word in normalized:
                    matches.append((len(word), action, key))
        if matches:
            matches.sort(reverse=True)
            _, action, _key = matches[0]
            seconds_value = default_seconds if action not in ("up", "down") else max(default_seconds, 1.5)
            return Command(
                raw,
                self._hold(action, seconds_value),
                f"{'上浮' if action == 'up' else '下潜' if action == 'down' else action} {seconds_value:.1f}s",
            )

        # A literal key / game action fallback: "按 f", "e", "tab".
        match = re.search(r"按(?:一下)?\s*([a-z0-9]+)", normalized)
        candidate = match.group(1) if match else (normalized if len(normalized) <= 4 else "")
        if candidate:
            key = candidate.upper()
            if key in {"SPACE", "TAB", "ENTER", "ESC", "ESC"}:
                return Command(raw, [Op(kind="tap", key=key, seconds=0.08)], f"按 {key}")
            if len(key) == 1 and key.isalnum():
                return Command(raw, [Op(kind="tap", key=key, seconds=0.08)], f"按 {key}")
        return Command(raw, [], "没听懂这个动作；可以用「前进/后退/上浮/下潜/扫描/按E/停手」")

    # ------------------------------------------------------------------
    # Main decision
    # ------------------------------------------------------------------

    def decide(
        self,
        features,
        hud,
        *,
        now: float | None = None,
        underwater_seconds: float = 0.0,
        ui_kind: str = "gameplay",
        planner_mode: str = "",
        planner_goal: str = "",
        session_seconds: float = 0.0,
    ) -> Decision:
        now = time.time() if now is None else float(now)
        if features is None:
            return Decision(PolicyState.WAITING, reason="还没有画面")
        if getattr(features, "black", False):
            return Decision(PolicyState.BLIND, reason="画面全黑，拒绝按键")
        if ui_kind in {"pda", "fabricator", "builder", "menu", "loading"}:
            mode = "craft" if ui_kind == "fabricator" else "build" if ui_kind == "builder" else "wait"
            return Decision(
                PolicyState.CRAFT if mode == "craft" else PolicyState.BUILD if mode == "build" else PolicyState.MENU,
                reason=f"画面是 {ui_kind} 界面，交给 LLM/玩家点击，不盲按",
                needs_llm=True,
                mode=mode,
            )

        # Emergency reflexes.
        damage = bool(getattr(hud, "damage", False) or getattr(features, "red_border", 0.0) > 0.018)
        health_low = bool(getattr(hud, "health_low", False))
        oxygen_low = bool(getattr(hud, "oxygen_low", False))
        if damage or health_low:
            return Decision(
                PolicyState.EVADE,
                self._hold("back", 1.1) + self._look(24, -8) + self._hold("up", 1.4),
                reason="受伤/血量低：后退、上浮、找掩体",
                speak="我在掉血，先退开再上浮。",
                urgent=True,
                needs_llm=True,
            )
        if oxygen_low or (underwater_seconds > self.oxygen_safe_seconds):
            return Decision(
                PolicyState.SURFACE,
                self._hold("up", 2.8) + self._look(0, -12),
                reason="氧气偏低/水下时间过长：强制上浮",
                speak="氧气不够了，我先上浮换气。",
                urgent=oxygen_low,
            )

        # Low food/water cannot be fixed blindly: there is no guaranteed slot
        # for food and clicking the wrong item could drop a tool.  Ask the LLM
        # to look at the PDA, while continuing a gentle search for edible fauna.
        if bool(getattr(hud, "food_low", False) or getattr(hud, "water_low", False)):
            return Decision(
                PolicyState.RECOVER,
                self._hold("forward", 1.2) + self._tap("use", hold=0.1),
                reason="饥饿/口渴：边找食物边请猫娘打开 PDA 确认补给",
                speak="我饿了/渴了，得找点吃的；我先看看附近有没有鱼。",
                urgent=True,
                needs_llm=True,
            )

        # A direct player/LLM command wins for exactly one decision, but only
        # after the safety reflexes above: "swim forward" must not override
        # drowning or a damage spike.
        command = self.take_command()
        if command is not None:
            if not command.ops:
                return Decision(
                    PolicyState.EXPLORE,
                    reason=command.summary or "命令没有可执行动作",
                    speak=command.summary,
                )
            return Decision(PolicyState.EXPLORE, command.ops, reason=f"执行指令：{command.summary}")

        directive = self.directive()
        mode = (directive.mode if directive is not None else "") or (planner_mode or "explore")
        goal = (directive.goal if directive is not None and directive.goal else planner_goal)

        if mode == "surface":
            return Decision(PolicyState.SURFACE, self._hold("up", 3.0), reason="按指令上浮")
        if mode == "vehicle":
            return Decision(
                PolicyState.EXPLORE,
                self._hold("forward", 2.4) + self._look(8, 0) + self._tap("interact", hold=0.1),
                reason="载具模式：前进、观察、必要时上下载具",
            )
        if mode == "craft":
            return Decision(PolicyState.CRAFT, reason="等待制造界面操作", needs_llm=True, mode="craft")
        if mode == "build":
            return Decision(PolicyState.BUILD, reason="等待建造界面操作", needs_llm=True, mode="build")
        if mode == "wait":
            return Decision(PolicyState.RECOVER, [Op(kind="wait", seconds=0.5)], reason="按指令等待")
        if mode == "evade" or mode == "fight":
            return Decision(
                PolicyState.EVADE,
                self._hold("back", 0.9) + self._look(20, 0) + self._hold("up", 0.7),
                reason="规避/战斗模式：保持距离",
            )
        if mode == "scan":
            return Decision(
                PolicyState.SCAN,
                self._hold("use", 2.0) + self._look(25, -6) + self._hold("use", 1.4),
                reason="扫描模式：对准中心按住左键，再扫一圈",
            )
        if mode == "gather":
            return self._gather(features, now, goal)
        if mode == "story":
            return self._explore(features, now, goal, descend_bias=0.25)
        return self._explore(features, now, goal)

    # ------------------------------------------------------------------
    # Behaviours
    # ------------------------------------------------------------------

    def _gather(self, features, now: float, goal: str) -> Decision:
        ops = self._hold("forward", 0.8) + self._look(0, 10)
        if now - self._last_collect > 2.8:
            self._last_collect = now
            ops += self._tap("use", hold=0.12)
        return Decision(
            PolicyState.GATHER,
            ops,
            reason="采集：低头靠近，左键拾取/扫描",
            speak=(f"我在找{goal}。" if goal else ""),
        )

    def _explore(self, features, now: float, goal: str, *, descend_bias: float = 0.18) -> Decision:
        scene_hash = int(getattr(features, "scene_hash", 0) or 0)
        motion = float(getattr(features, "motion", 0.0) or 0.0)
        if scene_hash != self._last_hash or motion > 0.006:
            self._last_hash = scene_hash
            self._same_since = now
            self._stuck_level = 0

        if self._same_since is not None and now - self._same_since > self.stuck_seconds:
            return self._stuck(now)

        if not self._phase_started or now - self._phase_started > 4.2:
            self._phase_started = now
            self._phase_index += 1
            self._sweep_sign *= -1

        phase = self._phase_index % 6
        ops: list[Op] = []
        if phase == 0:
            ops += self._hold("forward", 2.6)
            ops += self._look(10 * self._sweep_sign, -3)
        elif phase == 1:
            ops += self._hold("forward", 1.6)
            ops += self._look(28 * self._sweep_sign, 0)
            ops += self._tap("use", hold=0.1)
        elif phase == 2:
            ops += self._hold("forward", 2.0)
            ops += self._look(-20 * self._sweep_sign, 5)
        elif phase == 3:
            # Descend in a controlled step, never while the Oxygen reflex is
            # close to firing.  This is what turns a surface swimmer into an
            # explorer.
            if descend_bias > 0:
                ops += self._hold("down", 1.3)
            ops += self._hold("forward", 1.4)
            ops += self._look(14, 12)
        elif phase == 4:
            ops += self._hold("forward", 2.2)
            ops += self._look(-12 * self._sweep_sign, -8)
        else:
            ops += self._hold("up", 0.8)
            ops += self._look(35 * self._sweep_sign, 0)
            ops += self._tap("use", hold=0.12)
        if goal:
            ops.append(Op(kind="wait", seconds=0.02, note=f"goal:{goal}"))
        return Decision(PolicyState.EXPLORE, ops, reason="探索：前进+扫视+采集")

    def _stuck(self, now: float) -> Decision:
        level = self._stuck_level % 4
        self._stuck_level += 1
        self._same_since = now  # give the ladder one full stuck window
        if level == 0:
            ops = self._look(55, -10) + self._hold("forward", 0.9) + self._look(-55, 10)
            reason = "卡住：先看两边"
        elif level == 1:
            ops = self._hold("back", 1.0) + self._hold("up", 0.7)
            reason = "卡住：后退上浮脱困"
        elif level == 2:
            ops = self._hold("left", 0.9) + self._hold("forward", 1.1)
            reason = "卡住：侧移绕开"
        else:
            ops = self._tap("interact", hold=0.12) + self._hold("up", 1.1)
            reason = "卡住：尝试互动并上浮"
        return Decision(PolicyState.STUCK, ops, reason=reason, needs_llm=True)

    def aim_ops(self, x: float, y: float, *, screen_w: int = 0, screen_h: int = 0) -> list[Op]:
        """Turn the camera toward a normalized screen coordinate.

        ``x``/``y`` may be 0..1 or pixel coordinates.  Pixel input is normalised
        against the current frame, which makes the LLM's numbers robust across
        resolutions.
        """
        fx, fy = float(x), float(y)
        if screen_w and screen_h and (fx > 1.0 or fy > 1.0):
            fx = fx / float(screen_w)
            fy = fy / float(screen_h)
        fx = max(0.0, min(1.0, fx))
        fy = max(0.0, min(1.0, fy))
        dx, dy = self.calibration.counts_for_ndc(fx, fy)
        return [Op(kind="look", dx=dx, dy=dy, note=f"aim {fx:.2f},{fy:.2f}")]
