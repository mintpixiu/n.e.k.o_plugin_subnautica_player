from __future__ import annotations

import json

from plugin.plugins.subnautica_player import catalog
from plugin.plugins.subnautica_player.planner import Planner


def test_initial_snapshot_and_milestone(tmp_path):
    planner = Planner(tmp_path / "plan.json")
    planner.set_game(catalog.GAME_BY_ID["subnautica"])
    snap = planner.snapshot()
    assert snap.game_id == "subnautica"
    assert snap.total == len(catalog.GAME_BY_ID["subnautica"].milestones)
    assert snap.milestone is not None
    assert snap.completed == 0
    assert snap.milestone["key"] == "extinguish_repair"


def test_mark_done_and_persist(tmp_path):
    path = tmp_path / "plan.json"
    planner = Planner(path)
    planner.set_game(catalog.GAME_BY_ID["subnautica"])
    assert planner.mark_milestone("extinguish_repair", note="修好了无线电")
    snap = planner.snapshot()
    assert snap.completed == 1
    assert snap.milestone is not None and snap.milestone["key"] == "scanner"
    assert path.is_file()

    reloaded = Planner(path)
    reloaded.set_game(catalog.GAME_BY_ID["subnautica"])
    assert reloaded.snapshot().completed == 1
    assert any(item["kind"] == "milestone" for item in reloaded.as_dict()["notes"])


def test_invalid_milestone_is_rejected(tmp_path):
    planner = Planner(tmp_path / "plan.json")
    planner.set_game(catalog.GAME_BY_ID["subnautica"])
    assert planner.mark_milestone("does_not_exist") is False


def test_focus_and_notes(tmp_path):
    planner = Planner(tmp_path / "plan.json")
    planner.set_game(catalog.GAME_BY_ID["below_zero"])
    planner.set_focus("找到海卡车碎片", "先扫残骸；再回救生舱")
    planner.record_note("blueprint", "海卡车驾驶舱还差 1 个碎片")
    snap = planner.snapshot()
    assert snap.focus == "找到海卡车碎片"
    assert "海卡车" in planner.prompt_block()
    assert any(item["kind"] == "blueprint" for item in snap.notes)


def test_biome_record_is_deduplicated(tmp_path):
    planner = Planner(tmp_path / "plan.json")
    planner.set_game(catalog.GAME_BY_ID["subnautica"])
    assert planner.observe_biome("kelp_forest")
    assert not planner.observe_biome("kelp_forest")
    assert "kelp_forest" in planner.as_dict()["biomes"]


def test_corrupt_journal_does_not_crash(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text("{not json", encoding="utf-8")
    planner = Planner(path)
    planner.set_game(catalog.GAME_BY_ID["subnautica"])
    assert planner.snapshot().total > 0
    # The next save replaces the corrupt file with valid JSON.
    planner.record_note("observation", "test")
    json.loads(path.read_text(encoding="utf-8"))


def test_switching_game_restarts_checklist(tmp_path):
    planner = Planner(tmp_path / "plan.json")
    planner.set_game(catalog.GAME_BY_ID["subnautica"])
    planner.mark_milestone("extinguish_repair")
    planner.set_game(catalog.GAME_BY_ID["below_zero"])
    snap = planner.snapshot()
    assert snap.game_id == "below_zero"
    assert snap.completed == 0
    assert snap.milestone is not None
