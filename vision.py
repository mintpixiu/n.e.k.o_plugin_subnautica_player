"""Cheap, dependency-light frame understanding for Subnautica.

The host interpreter ships ``numpy`` and ``Pillow`` but not OpenCV, so this
module deliberately uses only array arithmetic.  It extracts the signals the
autopilot and the catgirl's narration actually need:

* is the frame black / frozen / loading?
* how much is moving, and where? (a crude motion centroid)
* are we above or below water?
* is the image dominated by a dark translucent UI (PDA / fabricator / builder)?
* is the screen edge flashing red? (damage)
* is there a subtitle / notification band?
* where is the centre reticle and what colour is it?

Nothing here claims to identify a fish, a wreck or a recipe.  Those inferences
belong to the policy (motion + HUD + damage) and, above all, to the LLM looking
at the pushed frame.  The module's job is to be *honest* about what arithmetic
can and cannot see.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

try:  # pragma: no cover - import environment dependent
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore[assignment]

__all__ = [
    "FrameAnalyzer",
    "FrameFeatures",
    "frame_features_summary",
    "perceptual_hash",
]


def _require_numpy() -> None:
    if np is None:
        raise RuntimeError("numpy is required for frame analysis")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class FrameFeatures:
    width: int = 0
    height: int = 0
    mean_luma: float = 0.0
    contrast: float = 0.0
    motion: float = 0.0
    scene_hash: int = 0
    black: bool = False
    frozen: bool = False
    red_border: float = 0.0
    white_surface: float = 0.0
    center_luma: float = 0.0
    center_edge: float = 0.0
    crosshair: bool = False
    crosshair_rgb: tuple[int, int, int] = (0, 0, 0)
    crosshair_red: float = 0.0
    crosshair_blue: float = 0.0
    crosshair_orange: float = 0.0
    text_band: float = 0.0
    motion_x: float = 0.5
    motion_y: float = 0.5
    motion_strength: float = 0.0
    edge_density: float = 0.0
    aspect: float = 0.0
    avg_rgb: tuple[float, float, float] = (0.0, 0.0, 0.0)
    blue_dominance: float = 0.0
    green_dominance: float = 0.0
    dark_ratio: float = 0.0
    bright_ratio: float = 0.0
    underwater: bool = False
    dark_ui: float = 0.0
    vignette: float = 0.0
    detail: dict[str, float] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "size": [self.width, self.height],
            "mean_luma": round(self.mean_luma, 3),
            "contrast": round(self.contrast, 3),
            "motion": round(self.motion, 4),
            "scene_hash": f"{self.scene_hash:016x}",
            "black": self.black,
            "frozen": self.frozen,
            "red_border": round(self.red_border, 4),
            "white_surface": round(self.white_surface, 3),
            "center_luma": round(self.center_luma, 3),
            "crosshair": self.crosshair,
            "crosshair_rgb": list(self.crosshair_rgb),
            "crosshair_red": round(self.crosshair_red, 3),
            "crosshair_blue": round(self.crosshair_blue, 3),
            "crosshair_orange": round(self.crosshair_orange, 3),
            "text_band": round(self.text_band, 3),
            "motion_centroid": [round(self.motion_x, 3), round(self.motion_y, 3)],
            "motion_strength": round(self.motion_strength, 4),
            "edge_density": round(self.edge_density, 3),
            "avg_rgb": [round(channel, 1) for channel in self.avg_rgb],
            "blue_dominance": round(self.blue_dominance, 3),
            "green_dominance": round(self.green_dominance, 3),
            "dark_ratio": round(self.dark_ratio, 3),
            "bright_ratio": round(self.bright_ratio, 3),
            "underwater": self.underwater,
            "dark_ui": round(self.dark_ui, 3),
            "vignette": round(self.vignette, 3),
            "detail": {key: round(value, 3) for key, value in self.detail.items()},
        }


def perceptual_hash(image, *, width: int = 9, height: int = 8) -> int:
    """64-bit dHash; resolution independent and cheap to compare."""
    _require_numpy()
    array = np.asarray(image)
    if array.ndim == 3:
        luma = array[..., 0] * 0.299 + array[..., 1] * 0.587 + array[..., 2] * 0.114
    else:
        luma = array.astype("float32")
    if luma.size == 0:
        return 0
    rows = np.linspace(0, luma.shape[0] - 1, height).astype(int)
    cols = np.linspace(0, luma.shape[1] - 1, width).astype(int)
    small = luma[np.ix_(rows, cols)]
    bits = small[:, 1:] > small[:, :-1]
    value = 0
    for bit in bits.reshape(-1):
        value = (value << 1) | int(bool(bit))
    return value


class FrameAnalyzer:
    """Stateful analyser: remembers the previous frame for motion features."""

    def __init__(self, *, analysis_width: int = 192) -> None:
        _require_numpy()
        self.analysis_width = max(64, int(analysis_width))
        self._prev_small = None
        self._prev_hash = 0
        self._prev_luma = 0.0
        self._frozen_ticks = 0
        self.frames = 0

    # -- helpers ---------------------------------------------------------

    def _small(self, image):
        array = np.asarray(image)
        if array.ndim == 2:
            array = np.stack([array] * 3, axis=-1)
        height, width = array.shape[:2]
        if width <= self.analysis_width:
            return array
        step = max(1, int(round(width / self.analysis_width)))
        return array[::step, ::step]

    @staticmethod
    def _luma(array):
        return (array[..., 0].astype("float32") * 0.299
                + array[..., 1].astype("float32") * 0.587
                + array[..., 2].astype("float32") * 0.114)

    def _red_border(self, array) -> float:
        height, width = array.shape[:2]
        band_y = max(2, int(height * 0.08))
        band_x = max(2, int(width * 0.08))
        border = np.concatenate([
            array[:band_y, :, :].reshape(-1, 3),
            array[-band_y:, :, :].reshape(-1, 3),
            array[band_y:-band_y, :band_x, :].reshape(-1, 3),
            array[band_y:-band_y, -band_x:, :].reshape(-1, 3),
        ])
        if border.size == 0:
            return 0.0
        r = border[:, 0].astype("float32")
        g = border[:, 1].astype("float32")
        b = border[:, 2].astype("float32")
        mask = (r > 80) & (r > g * 1.45 + 10) & (r > b * 1.35 + 10)
        return float(mask.mean())

    def _white_surface(self, array) -> float:
        height, width = array.shape[:2]
        y0, y1 = int(height * 0.18), int(height * 0.88)
        x0, x1 = int(width * 0.12), int(width * 0.88)
        patch = array[y0:y1, x0:x1].reshape(-1, 3).astype("float32")
        if patch.size == 0:
            return 0.0
        high = patch.max(axis=1)
        low = patch.min(axis=1)
        mask = (high > 135) & ((high - low) < 55)
        return float(mask.mean())

    def _text_band(self, array) -> float:
        height, width = array.shape[:2]
        y0, y1 = int(height * 0.66), int(height * 0.95)
        x0, x1 = int(width * 0.10), int(width * 0.90)
        patch = array[y0:y1, x0:x1]
        if patch.size == 0:
            return 0.0
        luma = self._luma(patch)
        bright = (luma > 165).astype("float32")
        if bright.shape[1] < 3:
            return 0.0
        transitions = np.abs(np.diff(bright, axis=1)).mean()
        density = bright.mean()
        return float(min(1.0, transitions * 6.0) * min(1.0, density * 4.0))

    def _crosshair(self, array):
        height, width = array.shape[:2]
        cx, cy = width // 2, height // 2
        half = max(3, min(width, height) // 90)
        patch = array[max(0, cy - half):cy + half + 1, max(0, cx - half):cx + half + 1]
        if patch.size == 0:
            return False, (0, 0, 0), 0.0, 0.0, 0.0
        luma = self._luma(patch)
        inner = float(luma.max())
        ring_y0, ring_y1 = max(0, cy - half * 4), min(height, cy + half * 4)
        ring_x0, ring_x1 = max(0, cx - half * 4), min(width, cx + half * 4)
        ring = self._luma(array[ring_y0:ring_y1, ring_x0:ring_x1])
        ambient = float(np.median(ring)) if ring.size else 0.0
        score = (inner - ambient) / 255.0
        present = score > 0.12

        flat = patch.reshape(-1, 3)
        bright_idx = np.argsort(luma.reshape(-1))[-max(1, flat.shape[0] // 8):]
        rgb = flat[bright_idx].mean(axis=0) if flat.size else np.zeros(3)
        r, g, b = float(rgb[0]), float(rgb[1]), float(rgb[2])
        total = max(1.0, r + g + b)
        red = max(0.0, (r - max(g, b)) / total)
        blue = max(0.0, (b - max(r, g)) / total)
        orange = max(0.0, (r - b) / total) * max(0.0, (g - b) / total) * 2.0
        return present, (int(r), int(g), int(b)), red, blue, orange

    def _motion(self, small):
        luma = self._luma(small)
        if luma.shape[0] >= 8 and luma.shape[1] >= 8:
            height, width = luma.shape
            grid_y, grid_x = 6, 8
            cell_h, cell_w = max(1, height // grid_y), max(1, width // grid_x)
            previous = self._prev_small
            diff = np.abs(luma - previous) if previous is not None and previous.shape == luma.shape \
                else np.zeros_like(luma)
            motion = float(diff.mean() / 255.0)
            cells = []
            for gy in range(grid_y):
                for gx in range(grid_x):
                    block = diff[gy * cell_h:(gy + 1) * cell_h, gx * cell_w:(gx + 1) * cell_w]
                    if block.size:
                        cells.append((float(block.mean()), gx, gy))
            strength = 0.0
            mx = 0.5
            my = 0.5
            if cells:
                best = max(cells, key=lambda item: item[0])
                strength = best[0] / 255.0
                average = sum(c[0] for c in cells) / len(cells)
                if best[0] > average * 1.8 + 1.0:
                    mx = (best[1] + 0.5) / grid_x
                    my = (best[2] + 0.5) / grid_y
            return motion, mx, my, strength, luma
        return 0.0, 0.5, 0.5, 0.0, luma

    def _colour_stats(self, array) -> dict[str, float]:
        flat = array.reshape(-1, 3).astype("float32")
        if flat.size == 0:
            return {}
        r, g, b = flat[:, 0], flat[:, 1], flat[:, 2]
        total = np.maximum(1.0, r + g + b)
        mean = flat.mean(axis=0)
        # Underwater scenes are blue/teal-dominant.  Surfacing flips the upper
        # half to a bright sky, which is exactly why this is only a feature.
        blue_dom = float(((b - r) / total).mean())
        green_dom = float(((g - r) / total).mean())
        luma = self._luma(flat)
        dark = float((luma < 62).mean())
        bright = float((luma > 178).mean())
        return {
            "avg_r": float(mean[0]),
            "avg_g": float(mean[1]),
            "avg_b": float(mean[2]),
            "blue_dominance": blue_dom,
            "green_dominance": green_dom,
            "dark_ratio": dark,
            "bright_ratio": bright,
        }

    def _dark_ui(self, array) -> float:
        """Fraction of the central 80% that is very dark and low-saturation.

        Subnautica's PDA, fabricator and builder overlays all dim the world and
        draw a dark panel.  This is not a classifier; it is a conservative cue
        that the catgirl should not keep swimming while she is in a menu.
        """
        height, width = array.shape[:2]
        patch = array[int(height * 0.15):int(height * 0.88),
                      int(width * 0.12):int(width * 0.88)].astype("float32")
        if patch.size == 0:
            return 0.0
        luma = self._luma(patch)
        low = float((luma < 58).mean())
        # A dark panel that covers most of the centre is much stronger evidence
        # than a dark cave wall, so require a high threshold in the policy.
        return low

    def _vignette(self, array) -> float:
        """Centre-to-edge luma ratio (low = tunnel vision / damage vignette)."""
        height, width = array.shape[:2]
        centre = self._luma(array[int(height * 0.35):int(height * 0.65),
                                 int(width * 0.35):int(width * 0.65)])
        edges = np.concatenate([
            self._luma(array[:int(height * 0.08), :]).reshape(-1),
            self._luma(array[-int(height * 0.08):, :]).reshape(-1),
        ])
        if centre.size == 0 or edges.size == 0:
            return 0.0
        return float(max(0.0, 1.0 - float(edges.mean()) / max(1.0, float(centre.mean()))))

    # -- public ----------------------------------------------------------

    def analyze(self, image) -> FrameFeatures:
        _require_numpy()
        array = np.asarray(image)
        if array.ndim != 3 or array.shape[0] < 8 or array.shape[1] < 8:
            return FrameFeatures(width=int(array.shape[1]) if array.ndim >= 2 else 0,
                                 height=int(array.shape[0]) if array.ndim >= 2 else 0,
                                 black=True)
        if array.shape[2] > 3:
            array = array[..., :3]
        small = self._small(array)
        luma = self._luma(small)
        mean = float(luma.mean())
        contrast = float(luma.std())
        black = mean < 8.0 and float(luma.max()) < 28.0
        motion, mx, my, strength, _ = self._motion(small)
        if motion < 0.0004:
            self._frozen_ticks += 1
        else:
            self._frozen_ticks = 0
        scene_hash = perceptual_hash(small)
        crosshair, rgb, red, blue, orange = self._crosshair(array)
        height, width = array.shape[:2]
        cx0, cx1 = int(width * 0.30), int(width * 0.70)
        cy0, cy1 = int(height * 0.25), int(height * 0.75)
        centre = luma[int(height * 0.25):int(height * 0.75), cx0:cx1]
        edge = 0.0
        if centre.shape[0] > 2 and centre.shape[1] > 2:
            gx = np.abs(np.diff(centre, axis=1)).mean()
            gy = np.abs(np.diff(centre, axis=0)).mean()
            edge = float((gx + gy) / 255.0)
        whole_edge = 0.0
        if luma.shape[0] > 2 and luma.shape[1] > 2:
            whole_edge = float((np.abs(np.diff(luma, axis=1)).mean()
                                + np.abs(np.diff(luma, axis=0)).mean()) / 255.0 * 2.0)

        stats = self._colour_stats(small)
        blue_dom = float(stats.get("blue_dominance", 0.0))
        # The threshold is intentionally generous: dark caves and aurora skies
        # both produce false negatives, and the policy treats "not underwater"
        # only as a hint, never as permission to stop surfacing.
        underwater = blue_dom > 0.055 and mean > 35.0
        features = FrameFeatures(
            width=width,
            height=height,
            mean_luma=mean / 255.0,
            contrast=contrast / 255.0,
            motion=motion,
            scene_hash=scene_hash,
            black=black,
            frozen=self._frozen_ticks >= 3,
            red_border=self._red_border(small),
            white_surface=self._white_surface(small),
            center_luma=float(self._luma(array[cy0:cy1, cx0:cx1]).mean()) / 255.0,
            center_edge=edge,
            crosshair=crosshair,
            crosshair_rgb=rgb,
            crosshair_red=red,
            crosshair_blue=blue,
            crosshair_orange=orange,
            text_band=self._text_band(array),
            motion_x=mx,
            motion_y=my,
            motion_strength=strength,
            edge_density=whole_edge,
            aspect=width / float(height) if height else 0.0,
            avg_rgb=(float(stats.get("avg_r", 0.0)),
                     float(stats.get("avg_g", 0.0)),
                     float(stats.get("avg_b", 0.0))),
            blue_dominance=blue_dom,
            green_dominance=float(stats.get("green_dominance", 0.0)),
            dark_ratio=float(stats.get("dark_ratio", 0.0)),
            bright_ratio=float(stats.get("bright_ratio", 0.0)),
            underwater=underwater,
            dark_ui=self._dark_ui(array),
            vignette=self._vignette(array),
        )
        self._prev_small = luma
        self._prev_hash = scene_hash
        self._prev_luma = mean
        self.frames += 1
        return features

    def reset(self) -> None:
        self._prev_small = None
        self._prev_hash = 0
        self._prev_luma = 0.0
        self._frozen_ticks = 0


def frame_features_summary(features: FrameFeatures | None) -> str:
    """One-line Chinese description used in prompts and status."""
    if features is None:
        return "还没有画面"
    parts = [
        f"亮度{features.mean_luma:.2f}",
        f"运动{features.motion:.3f}",
        f"对比{features.contrast:.2f}",
    ]
    parts.append("水下" if features.underwater else "可能在水面/陆地/室内")
    if features.black:
        parts.append("全黑")
    if features.frozen:
        parts.append("画面几乎静止")
    if features.red_border > 0.012:
        parts.append(f"画面边缘泛红{features.red_border:.2f}（可能受伤）")
    if features.dark_ui > 0.55:
        parts.append("中心有大块暗色 UI（PDA/制造机/建造界面）")
    if features.vignette > 0.45:
        parts.append("四周明显变暗（可能是低氧/受伤）")
    if features.crosshair:
        parts.append(f"准星RGB{features.crosshair_rgb}")
    if features.text_band > 0.25:
        parts.append("底部有字幕/提示")
    return "，".join(parts)
