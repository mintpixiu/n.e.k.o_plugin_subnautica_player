"""Subnautica HUD reading with pure numpy.

The survival HUD is a cluster of four coloured rings in the lower-left corner:

* red   -> health
* orange -> food
* blue  -> water
* teal  -> oxygen (with a large number in the middle)

This module finds the *rings* by colour and shape, then tracks their relative
size.  It deliberately does **not** OCR the number: the host has no OCR engine,
and pretending to read "45" from a blurry screenshot would be worse than saying
"看不到具体数值".  The ring size is enough to answer the only question the reflex
layer needs: "is this resource falling dangerously low?"

The detector is conservative.  If it cannot find a believable ring it reports
``present=False`` and every ratio as ``None``; the policy then refuses to use
HUD-derived emergency logic and falls back to the damage vignette and the LLM's
eyes.  A false "low oxygen" would make the catgirl surface constantly; a false
"safe" could drown her, so absence of evidence is reported as absence.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

try:  # pragma: no cover - import environment dependent
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore[assignment]

from .vision import FrameFeatures

__all__ = ["HudState", "HudTracker"]


@dataclass
class HudState:
    present: bool = False
    rings: int = 0
    health_ratio: float | None = None
    food_ratio: float | None = None
    water_ratio: float | None = None
    oxygen_ratio: float | None = None
    health_low: bool = False
    food_low: bool = False
    water_low: bool = False
    oxygen_low: bool = False
    damage: bool = False
    stale_frames: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "present": self.present,
            "rings": self.rings,
            "health_ratio": None if self.health_ratio is None else round(self.health_ratio, 2),
            "food_ratio": None if self.food_ratio is None else round(self.food_ratio, 2),
            "water_ratio": None if self.water_ratio is None else round(self.water_ratio, 2),
            "oxygen_ratio": None if self.oxygen_ratio is None else round(self.oxygen_ratio, 2),
            "health_low": self.health_low,
            "food_low": self.food_low,
            "water_low": self.water_low,
            "oxygen_low": self.oxygen_low,
            "damage": self.damage,
            "stale_frames": self.stale_frames,
        }

    def one_line(self) -> str:
        if not self.present:
            return "没有稳定识别到左下角生存 HUD（不猜数值）"
        bits = []
        for label, ratio in (
            ("血", self.health_ratio),
            ("食", self.food_ratio),
            ("水", self.water_ratio),
            ("氧", self.oxygen_ratio),
        ):
            if ratio is not None:
                bits.append(f"{label}{int(round(ratio * 100))}%")
        if self.damage:
            bits.append("正在受伤")
        if self.oxygen_low:
            bits.append("氧气偏低")
        if self.health_low:
            bits.append("血量偏低")
        return "HUD：" + "，".join(bits or ["只看到部分圆环"])


# Hue windows in degrees.  Subnautica's rings are saturated and bright; the
# windows are narrow enough that seawater, kelp and coral rarely land in all
# three of saturation/value/shape tests at once.
_RING_WINDOWS: dict[str, tuple[float, float]] = {
    "health": (-10.0, 16.0),
    "food": (18.0, 52.0),
    "water": (196.0, 232.0),
    # The oxygen ring is a green->teal gradient and is visibly larger than the
    # other three; its whole gradient has to be included or the component is
    # split into an arc that fails the roundness check.
    "oxygen": (112.0, 235.0),
}


def _hsv(array):
    """Vectorised RGB->HSV (H in degrees, S/V in 0..1) using numpy only."""
    rgb = array.astype("float32") / 255.0
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    maximum = np.max(rgb, axis=-1)
    minimum = np.min(rgb, axis=-1)
    delta = maximum - minimum
    saturation = np.where(maximum > 1e-6, delta / np.maximum(maximum, 1e-6), 0.0)
    hue = np.zeros_like(maximum)
    non_zero = delta > 1e-6
    red = non_zero & (maximum == r)
    green = non_zero & (maximum == g)
    blue = non_zero & (maximum == b)
    hue[red] = ((g[red] - b[red]) / delta[red]) % 6.0
    hue[green] = (b[green] - r[green]) / delta[green] + 2.0
    hue[blue] = (r[blue] - g[blue]) / delta[blue] + 4.0
    hue *= 60.0
    return hue, saturation, maximum


def _components(mask, *, max_components: int = 240):
    """Connected components on a small boolean mask; returns bbox/area/fill."""
    height, width = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    found: list[tuple[int, int, int, int, int, float]] = []
    for start_y, start_x in zip(*np.nonzero(mask)):
        if seen[start_y, start_x]:
            continue
        queue = deque([(int(start_y), int(start_x))])
        seen[start_y, start_x] = True
        area = 0
        min_y = max_y = int(start_y)
        min_x = max_x = int(start_x)
        while queue:
            y, x = queue.popleft()
            area += 1
            min_y, max_y = min(min_y, y), max(max_y, y)
            min_x, max_x = min(min_x, x), max(max_x, x)
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    queue.append((ny, nx))
        bbox_w = max_x - min_x + 1
        bbox_h = max_y - min_y + 1
        fill = area / float(max(1, bbox_w * bbox_h))
        found.append((area, min_x, min_y, bbox_w, bbox_h, fill))
        if len(found) >= max_components:
            break
    return found


def _pick_ring(mask) -> tuple[int, tuple[int, int, int, int]] | None:
    """Pick the most ring-like component (compact, hollow, round-ish)."""
    height, width = mask.shape
    best: tuple[float, int, tuple[int, int, int, int]] | None = None
    for area, x, y, box_w, box_h, fill in _components(mask):
        if area < 12 or area > max(80, int(height * width * 0.12)):
            continue
        aspect = box_w / float(max(1, box_h))
        if not (0.42 <= aspect <= 2.4):
            continue
        if not (0.12 <= fill <= 0.95):
            continue
        # Rings are hollow: fill is normally 0.25-0.65.  A solid coral blob is
        # less likely to score as high, but is not rejected outright.
        ring_score = (1.0 - abs(aspect - 1.0)) * (1.0 - abs(fill - 0.42))
        if best is None or ring_score > best[0]:
            best = (ring_score, area, (x, y, box_w, box_h))
    if best is None:
        return None
    return best[1], best[2]


class HudTracker:
    """Track the four survival rings across frames."""

    def __init__(self, *, window: int = 6) -> None:
        self.window = max(3, int(window))
        self._baseline: dict[str, float] = {}
        self._last: dict[str, float] = {}
        self._shape: tuple[int, int] | None = None
        self._misses = 0
        self._history: dict[str, deque[float]] = {
            name: deque(maxlen=self.window) for name in _RING_WINDOWS
        }

    def reset(self) -> None:
        self._baseline.clear()
        self._last.clear()
        self._shape = None
        self._misses = 0
        for history in self._history.values():
            history.clear()

    def _roi(self, image):
        height, width = image.shape[:2]
        y0, y1 = int(height * 0.52), int(height * 0.995)
        x0, x1 = 0, int(width * 0.42)
        patch = image[y0:y1, x0:x1]
        if patch.size == 0:
            return None
        # Work at <= 280 px wide.  The HUD ring remains 10-30 px across on a
        # 1440p/4K screen, enough for connected-component shape checks but cheap.
        if patch.shape[1] > 280:
            step = max(1, int(round(patch.shape[1] / 280.0)))
            patch = patch[::step, ::step]
        return patch

    def observe(self, image, features: FrameFeatures | None = None) -> HudState:
        if np is None:
            return HudState(damage=bool(features and features.red_border > 0.018))
        array = np.asarray(image)
        if array.ndim != 3 or array.shape[0] < 16 or array.shape[1] < 16:
            return HudState(stale_frames=self._misses + 1)
        patch = self._roi(array)
        if patch is None:
            return HudState(stale_frames=self._misses + 1)
        if self._shape is not None and self._shape != patch.shape[:2]:
            # Resolution / window mode changed; old baselines are meaningless.
            self.reset()
        self._shape = patch.shape[:2]

        hue, saturation, value = _hsv(patch)
        colors = {
            "health": (((hue < 16.0) | (hue > 350.0)) & (saturation > 0.62) & (value > 0.52)),
            "food": ((hue >= 18.0) & (hue <= 52.0) & (saturation > 0.62) & (value > 0.58)),
            "water": ((hue >= 196.0) & (hue <= 232.0) & (saturation > 0.58) & (value > 0.58)),
            "oxygen": ((hue >= 112.0) & (hue <= 235.0) & (saturation > 0.50) & (value > 0.52)),
        }

        rings = 0
        counts: dict[str, float] = {}
        for name, mask in colors.items():
            picked = _pick_ring(mask)
            if picked is None:
                continue
            area, _bbox = picked
            rings += 1
            counts[name] = float(area)
            self._history[name].append(float(area))
            self._last[name] = float(area)
            self._baseline[name] = max(self._baseline.get(name, 0.0), float(area))

        if rings == 0:
            self._misses += 1
            return HudState(
                present=False,
                stale_frames=self._misses,
                damage=bool(features and features.red_border > 0.018),
            )
        self._misses = 0

        ratios: dict[str, float | None] = {}
        for name in _RING_WINDOWS:
            baseline = self._baseline.get(name, 0.0)
            current = self._last.get(name)
            if not baseline or current is None:
                ratios[name] = None
            else:
                ratios[name] = max(0.0, min(1.0, current / baseline))

        damage = bool(features and (features.red_border > 0.018 or features.vignette > 0.62))
        health_ratio = ratios["health"]
        oxygen_ratio = ratios["oxygen"]
        return HudState(
            present=rings >= 2,
            rings=rings,
            health_ratio=health_ratio,
            food_ratio=ratios["food"],
            water_ratio=ratios["water"],
            oxygen_ratio=oxygen_ratio,
            health_low=bool(health_ratio is not None and health_ratio < 0.42),
            food_low=bool(ratios["food"] is not None and ratios["food"] < 0.30),
            water_low=bool(ratios["water"] is not None and ratios["water"] < 0.30),
            oxygen_low=bool(oxygen_ratio is not None and oxygen_ratio < 0.32),
            damage=damage,
            stale_frames=0,
        )
