"""Long-horizon survival / tech / story plan with a small persistent journal.

The local policy is a reactive pilot; it cannot tell whether the player has
already built a Seamoth.  The planner therefore keeps a *honest* checklist:
nothing is marked complete by guessing.  The catgirl (or the player) marks a
milestone done through a tool after seeing the evidence, and the journal records
what was actually observed -- biome, depth band, a screenshot note, a blueprint,
a base.  That is enough to avoid repeating the same advice every session while
never claiming progress that did not happen.

If the JSON is corrupt or from an incompatible version, the planner starts a
fresh journal instead of crashing the plugin.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import catalog

__all__ = ["Planner", "PlanSnapshot"]

_VERSION = 1


@dataclass
class PlanSnapshot:
    game_id: str
    game_name: str
    mode: str
    focus: str
    milestone: dict[str, str] | None
    milestone_index: int
    completed: int
    total: int
    next_hint: str
    notes: list[dict[str, Any]] = field(default_factory=list)
    session_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "game_id": self.game_id,
            "game_name": self.game_name,
            "mode": self.mode,
            "focus": self.focus,
            "milestone": self.milestone,
            "milestone_index": self.milestone_index,
            "completed": self.completed,
            "total": self.total,
            "next_hint": self.next_hint,
            "notes": self.notes[-6:],
            "session_seconds": round(self.session_seconds, 1),
        }

    def one_line(self) -> str:
        if self.milestone is None:
            return f"当前计划：{self.focus or '自由探索'}"
        return (
            f"当前阶段 {self.milestone_index + 1}/{self.total}：{self.milestone['title']}；"
            f"已完成 {self.completed} 个"
        )


class Planner:
    """Checklist + notes persisted as one JSON file."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._data: dict[str, Any] = self._fresh()
        self._game: catalog.GameProfile | None = None
        self._session_started = 0.0
        self._session_seconds = 0.0
        self.load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _fresh() -> dict[str, Any]:
        return {
            "version": _VERSION,
            "game_id": "",
            "created_at": time.time(),
            "updated_at": time.time(),
            "sessions": 0,
            "total_seconds": 0.0,
            "focus": "",
            "steps": "",
            "milestones": {},   # key -> {"status": "done"/"skipped", "at": ...}
            "milestone_index": 0,
            "notes": [],
            "biomes": [],
            "last_seen": {},
        }

    def load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict) or raw.get("version") != _VERSION:
            return
        fresh = self._fresh()
        fresh.update({key: raw[key] for key in fresh if key in raw})
        if not isinstance(fresh.get("milestones"), dict):
            fresh["milestones"] = {}
        if not isinstance(fresh.get("notes"), list):
            fresh["notes"] = []
        if not isinstance(fresh.get("biomes"), list):
            fresh["biomes"] = []
        self._data = fresh

    def save(self) -> bool:
        if self.path is None:
            return False
        self._data["updated_at"] = time.time()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(self.path.suffix + ".tmp")
            temp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temp, self.path)
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def set_game(self, game: catalog.GameProfile) -> None:
        if self._data.get("game_id") != game.id:
            # A different game gets a fresh checklist and notes.  Keeping the
            # two games' progress in one indistinguishable bucket would be a lie.
            old_notes = self._data.get("notes", [])
            old_seconds = float(self._data.get("total_seconds", 0.0) or 0.0)
            old_sessions = int(self._data.get("sessions", 0) or 0)
            self._data = self._fresh()
            self._data["game_id"] = game.id
            self._data["notes"] = [
                note for note in old_notes
                if isinstance(note, dict) and note.get("game_id") == game.id
            ][-80:]
            self._data["total_seconds"] = old_seconds
            self._data["sessions"] = old_sessions
            self.save()
        self._game = game
        if not self._session_started:
            self._session_started = time.time()
            self._data["sessions"] = int(self._data.get("sessions", 0)) + 1
            self.save()

    def end_session(self) -> None:
        if self._session_started:
            self._session_seconds += time.time() - self._session_started
            self._data["total_seconds"] = float(self._data.get("total_seconds", 0.0)) + (
                time.time() - self._session_started
            )
            self._session_started = 0.0
            self.save()

    def tick_session(self, seconds: float) -> None:
        self._session_seconds = max(self._session_seconds, float(seconds))

    # ------------------------------------------------------------------
    # Plan
    # ------------------------------------------------------------------

    @property
    def game(self) -> catalog.GameProfile | None:
        return self._game

    def milestones(self) -> tuple[catalog.Milestone, ...]:
        return self._game.milestones if self._game is not None else ()

    def _status(self, key: str) -> str:
        item = self._data.get("milestones", {}).get(key)
        return str(item.get("status", "")) if isinstance(item, dict) else ""

    def current_milestone(self) -> tuple[int, catalog.Milestone | None]:
        milestones = self.milestones()
        for index, milestone in enumerate(milestones):
            if self._status(milestone.key) not in {"done", "skipped"}:
                return index, milestone
        return len(milestones), None

    def mark_milestone(self, key: str, *, status: str = "done", note: str = "") -> bool:
        milestones = {m.key: m for m in self.milestones()}
        if key not in milestones:
            return False
        status = "skipped" if str(status).lower() in {"skip", "skipped"} else "done"
        self._data.setdefault("milestones", {})[key] = {
            "status": status,
            "at": time.time(),
            "note": str(note or "")[:500],
        }
        self.record_note("milestone", f"{milestones[key].title} -> {status} {note}".strip())
        self.save()
        return True

    def set_focus(self, goal: str, steps: str = "") -> None:
        self._data["focus"] = str(goal or "").strip()[:500]
        self._data["steps"] = str(steps or "").strip()[:1500]
        self.record_note("focus", self._data["focus"])
        self.save()

    def record_note(self, kind: str, text: str) -> None:
        text = str(text or "").strip()
        if not text:
            return
        notes = self._data.setdefault("notes", [])
        notes.append({
            "kind": str(kind or "note")[:40],
            "text": text[:1000],
            "at": time.time(),
            "game_id": self._data.get("game_id", ""),
        })
        if len(notes) > 200:
            del notes[: len(notes) - 200]
        self.save()

    def observe_biome(self, biome_key: str) -> bool:
        key = str(biome_key or "").strip().lower()
        if key not in catalog.BIOMES:
            return False
        biomes = self._data.setdefault("biomes", [])
        if key in biomes:
            return False
        biomes.append(key)
        self.record_note("biome", catalog.BIOMES[key]["zh"])
        self.save()
        return True

    # ------------------------------------------------------------------
    # Snapshot / prompt material
    # ------------------------------------------------------------------

    def snapshot(self) -> PlanSnapshot:
        game = self._game
        index, milestone = self.current_milestone()
        milestones = self.milestones()
        completed = sum(
            1 for item in milestones
            if self._status(item.key) in {"done", "skipped"}
        )
        focus = str(self._data.get("focus", "") or "")
        if milestone is not None:
            hint = milestone.hint or milestone.detail
            mode = {
                "survival": "surface",
                "tech": "gather",
                "base": "build",
                "story": "story",
                "explore": "explore",
            }.get(milestone.phase, "explore")
        else:
            hint = "当前路线已全部标记完成；转入自由探索与基地经营。"
            mode = "explore"
        return PlanSnapshot(
            game_id=game.id if game else "",
            game_name=game.name_zh if game else "",
            mode=mode,
            focus=focus,
            milestone=milestone.as_dict() if milestone else None,
            milestone_index=index,
            completed=completed,
            total=len(milestones),
            next_hint=hint,
            notes=[n for n in self._data.get("notes", []) if isinstance(n, dict)][-6:],
            session_seconds=self._session_seconds,
        )

    def prompt_block(self) -> str:
        snap = self.snapshot()
        lines = [snap.one_line()]
        if snap.focus:
            lines.append(f"主人/当前目标：{snap.focus}")
        steps = str(self._data.get("steps", "") or "")
        if steps:
            lines.append(f"计划步骤：{steps}")
        if snap.milestone:
            lines.append(f"本阶段提示：{snap.milestone.get('hint') or snap.milestone.get('detail', '')}")
        if snap.notes:
            recent = "；".join(
                f"{item.get('kind', 'note')}:{str(item.get('text', ''))[:80]}"
                for item in snap.notes[-3:]
            )
            lines.append("最近记录：" + recent)
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "game_id": self._data.get("game_id", ""),
            "sessions": self._data.get("sessions", 0),
            "total_seconds": round(float(self._data.get("total_seconds", 0.0) or 0.0), 1),
            "focus": self._data.get("focus", ""),
            "steps": self._data.get("steps", ""),
            "milestones": dict(self._data.get("milestones", {})),
            "biomes": list(self._data.get("biomes", [])),
            "notes": list(self._data.get("notes", []))[-12:],
        }
