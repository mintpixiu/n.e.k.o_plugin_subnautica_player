from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw
from plugin.plugins.subnautica_player.hud import HudTracker
from plugin.plugins.subnautica_player.vision import FrameAnalyzer


def hud_image(scales=(1.0, 1.0, 1.0, 1.0), size=(1280, 720)) -> np.ndarray:
    width, height = size
    image = Image.new("RGB", (width, height), (20, 50, 70))
    draw = ImageDraw.Draw(image)

    def ring(cx, cy, radius, colour, thickness=8):
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                     outline=colour, width=thickness)

    hs, fs, ws, os_ = scales
    ring(120, 560, max(8, int(46 * hs)), (220, 50, 50), 9)
    ring(190, 620, max(8, int(40 * fs)), (240, 150, 30), 8)
    ring(285, 620, max(8, int(40 * ws)), (40, 160, 220), 8)
    ring(270, 540, max(12, int(58 * os_)), (40, 190, 160), 12)
    return np.array(image)


def test_full_hud_is_detected():
    tracker = HudTracker()
    array = hud_image()
    state = tracker.observe(array, FrameAnalyzer().analyze(array))
    assert state.present
    assert state.rings >= 3
    assert state.oxygen_ratio is not None
    assert not state.oxygen_low
    assert "HUD" in state.one_line()


def test_depleting_rings_lower_ratio_and_trigger_warnings():
    tracker = HudTracker()
    analyzer = FrameAnalyzer()
    first = hud_image()
    tracker.observe(first, analyzer.analyze(first))
    low = hud_image((0.25, 0.25, 0.25, 0.2))
    state = tracker.observe(low, analyzer.analyze(low))
    assert state.present
    assert state.health_low
    assert state.food_low
    assert state.water_low
    assert state.oxygen_low
    assert state.health_ratio is not None and state.health_ratio < 0.4


def test_no_coloured_rings_reports_absence_not_zero():
    tracker = HudTracker()
    array = np.zeros((720, 1280, 3), dtype=np.uint8)
    array[..., 0] = 15
    array[..., 2] = 40
    state = tracker.observe(array, FrameAnalyzer().analyze(array))
    assert not state.present
    assert state.health_ratio is None
    assert state.oxygen_ratio is None
    assert "不猜" in state.one_line()


def test_damage_vignette_sets_damage_flag():
    tracker = HudTracker()
    array = hud_image()
    features = FrameAnalyzer().analyze(array)
    features.red_border = 0.05
    state = tracker.observe(array, features)
    assert state.damage
