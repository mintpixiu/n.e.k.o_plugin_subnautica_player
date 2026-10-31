from __future__ import annotations

from dataclasses import replace

from plugin.plugins.subnautica_player import catalog
from plugin.plugins.subnautica_player.hud import HudState
from plugin.plugins.subnautica_player.policy import Op, PlayPolicy, PolicyState
from plugin.plugins.subnautica_player.vision import FrameFeatures


def new_policy() -> PlayPolicy:
    policy = PlayPolicy()
    policy.set_game(catalog.GAME_BY_ID["subnautica"])
    return policy


def features(**kwargs) -> FrameFeatures:
    base = FrameFeatures(
        width=1920,
        height=1080,
        mean_luma=0.42,
        motion=0.02,
        scene_hash=0x1234,
        underwater=True,
        dark_ui=0.05,
    )
    return replace(base, **kwargs)


def keys(ops: list[Op]) -> list[tuple[str, str, float]]:
    return [(op.kind, op.key, op.seconds) for op in ops]


class TestCommands:
    def test_forward_is_a_hold(self):
        command = new_policy().command_ops("往前走 2 秒")
        assert command.ops[0].kind == "hold"
        assert command.ops[0].key == "w"
        assert 1.9 <= command.ops[0].seconds <= 2.1

    def test_up_and_down_use_space_c(self):
        policy = new_policy()
        assert policy.command_ops("上浮").ops[0].key == "space"
        assert policy.command_ops("下潜").ops[0].key == "c"

    def test_scan_holds_left_mouse(self):
        command = new_policy().command_ops("扫描它")
        assert command.ops[0].kind == "hold"
        assert command.ops[0].key == "mouse1"
        assert command.ops[0].seconds >= 2.0

    def test_interact_and_pda(self):
        policy = new_policy()
        assert policy.command_ops("按E进舱").ops[0].key == "e"
        assert policy.command_ops("打开PDA").ops[0].key == "tab"

    def test_look_right_is_negative_dx(self):
        op = new_policy().command_ops("往右看").ops[0]
        assert op.kind == "look"
        assert op.dx < 0

    def test_slot_number(self):
        command = new_policy().command_ops("按3")
        assert command.ops[0].key == "3"

    def test_stop_and_resume_are_not_keys(self):
        policy = new_policy()
        assert policy.command_ops("停手").ops == []
        assert policy.command_ops("继续").ops == []

    def test_unknown_is_honest(self):
        command = new_policy().command_ops("去给我煮杯咖啡")
        assert command.ops == []
        assert "没听懂" in command.summary


class TestDecisions:
    def test_black_frame_refuses_input(self):
        decision = new_policy().decide(features(black=True), HudState())
        assert decision.state is PolicyState.BLIND
        assert decision.ops == []

    def test_ui_hands_off(self):
        decision = new_policy().decide(features(dark_ui=0.8), HudState(present=True), ui_kind="pda")
        assert decision.state in (PolicyState.CRAFT, PolicyState.MENU)
        assert decision.ops == []

    def test_damage_reflex(self):
        decision = new_policy().decide(
            features(red_border=0.05),
            HudState(present=True, damage=True),
        )
        assert decision.state is PolicyState.EVADE
        assert any(op.key == "s" for op in decision.ops)

    def test_oxygen_reflex_and_timer(self):
        policy = new_policy()
        assert policy.decide(features(), HudState(oxygen_low=True)).state is PolicyState.SURFACE
        assert policy.decide(features(), HudState(), underwater_seconds=40).state is PolicyState.SURFACE

    def test_emergency_above_command(self):
        policy = new_policy()
        policy.queue_command("往前走")
        decision = policy.decide(features(), HudState(oxygen_low=True))
        assert decision.state is PolicyState.SURFACE

    def test_command_runs_once(self):
        policy = new_policy()
        policy.queue_command("往前走")
        first = policy.decide(features(), HudState())
        assert any(op.key == "w" for op in first.ops)
        assert policy._pending is None
        policy.decide(features(), HudState())
        assert policy._pending is None

    def test_directive_craft_waits(self):
        policy = new_policy()
        policy.set_directive(mode="craft", goal="做扫描仪")
        decision = policy.decide(features(), HudState())
        assert decision.state in (PolicyState.CRAFT, PolicyState.MENU)
        assert decision.ops == []

    def test_stuck_ladder_advances(self):
        policy = new_policy()
        first = policy.decide(features(motion=0.0, scene_hash=7), HudState(), now=0.0)
        assert first.state is PolicyState.EXPLORE
        stuck = policy.decide(features(motion=0.0, scene_hash=7), HudState(), now=4.0)
        assert stuck.state is PolicyState.STUCK
        assert stuck.needs_llm

    def test_aim_converts_normalized_to_counts(self):
        policy = new_policy()
        ops = policy.aim_ops(0.75, 0.5, screen_w=1920, screen_h=1080)
        assert ops[0].kind == "look"
        assert ops[0].dx > 0
        assert abs(ops[0].dy) < 0.01


class TestOps:
    def test_op_summary_is_json_ready(self):
        op = Op(kind="hold", key="w", seconds=1.234)
        summary = op.summary()
        assert summary["kind"] == "hold"
        assert summary["key"] == "w"
        assert summary["seconds"] == 1.23
        assert "1.2s" in op.label()
