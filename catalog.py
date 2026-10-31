"""Game catalogue and survival knowledge for the Subnautica player plugin.

Only two games are supported: *Subnautica* (2018) and *Subnautica: Below Zero*.
Both are first-person underwater survival games built on Unity, so one control
layer serves both.  Everything that actually differs -- Steam ids, executable
names, title patterns, story beats, biome hazards and the HUD flavour -- lives
in this module.  The rest of the plugin reads :class:`GameProfile` objects.

The defaults below come from three independent sources:

* the installed games' ``options.bin`` (key bindings, Chinese locale),
* Steam ``appmanifest_*.acf`` on the reference machine (app ids / install dirs),
* the public Subnautica wiki key-binding table.

A player with a custom layout can override every key from ``plugin.toml``
without editing code.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

__all__ = [
    "BIOMES",
    "GAMES",
    "GAME_BY_ID",
    "GameProfile",
    "Milestone",
    "SUPPORTED_IDS",
    "aliases_for",
    "describe_installed",
    "normalise_text",
    "resolve_game",
]

# ---------------------------------------------------------------------------
# Default keyboard / mouse map
# ---------------------------------------------------------------------------

#: Canonical action -> default key.  ``mouse1``/``mouse2`` are understood by
#: :mod:`input` as mouse buttons, not keyboard scan codes.
#:
#: Verified against Below Zero's ``options.bin`` on the reference machine:
#: MoveForward W, MoveBackward S, MoveLeft A, MoveRight D, MoveUp Space,
#: MoveDown C, PDA Tab, Deconstruct Q, Exit/Holster E, LeftHand MouseButtonLeft,
#: RightHand MouseButtonRight, AltTool F, Reload R, Sprint LeftShift,
#: AutoMove X, TakePicture F11, Slot1..5 = 1..5.
SUBNAUTICA_KEYS: dict[str, str] = {
    "forward": "w",
    "back": "s",
    "left": "a",
    "right": "d",
    "up": "space",
    "down": "c",
    "sprint": "shift",
    "jump": "space",
    "interact": "e",
    "enter": "e",
    "exit": "e",
    "use": "mouse1",
    "attack": "mouse1",
    "alt_use": "mouse2",
    "alt_attack": "mouse2",
    "scan": "mouse1",
    "reload": "r",
    "pda": "tab",
    "deconstruct": "q",
    "alt_tool": "f",
    "auto_move": "x",
    "photo": "f11",
    "slot1": "1",
    "slot2": "2",
    "slot3": "3",
    "slot4": "4",
    "slot5": "5",
}

#: Pause key.  F8 is Subnautica's feedback menu and F11 is its screenshot key,
#: so neither may be used.  F9 is unbound in both games.
PAUSE_KEY = "f9"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Milestone:
    """One early/mid-game checkpoint used by the long-horizon planner."""

    key: str
    title: str
    detail: str
    phase: str  # survival | tech | base | story | explore
    hint: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "title": self.title,
            "detail": self.detail,
            "phase": self.phase,
            "hint": self.hint,
        }


@dataclass(frozen=True)
class GameProfile:
    id: str
    name_zh: str
    name_en: str
    app_id: str
    exe: str
    install_dirs: tuple[str, ...]
    title_patterns: tuple[str, ...]
    aliases: tuple[str, ...]
    keys: Mapping[str, str]
    strategy: str
    milestones: tuple[Milestone, ...] = ()
    hud_style: str = "subnautica"
    notes: str = ""
    release: str = ""
    depth_note: str = ""

    @property
    def display(self) -> str:
        return self.name_zh

    @property
    def playable(self) -> bool:
        return True

    def key(self, action: str, overrides: Mapping[str, str] | None = None) -> str:
        if overrides and action in overrides and overrides[action]:
            return str(overrides[action])
        return str(self.keys.get(action, "") or "")

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name_zh,
            "name_en": self.name_en,
            "app_id": self.app_id,
            "exe": self.exe,
            "playable": True,
            "hud_style": self.hud_style,
            "milestones": len(self.milestones),
            "release": self.release,
            "notes": self.notes,
        }

    def strategy_brief(self, *, max_milestones: int = 8) -> str:
        milestones = list(self.milestones[:max_milestones])
        tail = ""
        if len(self.milestones) > len(milestones):
            tail = f"…共 {len(self.milestones)} 个阶段"
        route = "；".join(f"{m.title}：{m.detail}" for m in milestones) + tail
        parts = [f"{self.name_zh}（{self.name_en}）", self.strategy]
        if route:
            parts.append("主线/发展路线：" + route)
        if self.depth_note:
            parts.append(self.depth_note)
        if self.notes:
            parts.append(self.notes)
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Early route for Subnautica
# ---------------------------------------------------------------------------

SUBNAUTICA_MILESTONES: tuple[Milestone, ...] = (
    Milestone(
        "extinguish_repair",
        "灭火与修复救生舱",
        "用灭火器扑灭舱内火，拿急救包；用维修工具修好舱体与无线电。",
        "survival",
        "没有维修工具时先找洞穴硫磺和硅橡胶。",
    ),
    Milestone(
        "scanner",
        "做出扫描仪",
        "钛×2 + 电池。扫描附近的碎片，解锁海蛾号、移动载具舱和建造枪。",
        "tech",
        "金属残骸用左键拾取；先扫大块残骸，不要只捡散落钛。",
    ),
    Milestone(
        "seaglide",
        "做出 Seaglide",
        "电池+润滑剂+钛+铜线；它决定前 200 米的探索半径。",
        "tech",
        "碎片在 Safe Shallows / Kelp Forest 的残骸附近。",
    ),
    Milestone(
        "base_start",
        "建立第一个海底基地",
        "建造枪 + 2 个管道/房间 + 太阳能 + 储物柜；把它当补给点。",
        "base",
        "基地选址优先 20-60 米、靠近 Kelp Forest 与 Safe Shallows 交界。",
    ),
    Milestone(
        "sunbeam",
        "回应 Sunbeam 信号",
        "修好无线电后收听信号，去浮岛；不要错过倒计时。",
        "story",
        "浮岛上有面包树、紫罗兰种子和 Degasi 基地。",
    ),
    Milestone(
        "aurora",
        "探索 Aurora",
        "防辐射服 + 维修工具 + 推进炮 + 激光切割器；拿海蛾号深度模块与舰长室代码。",
        "story",
        "船头有死神利维坦；贴着船体上层走，别在开阔水面直线游。",
    ),
    Milestone(
        "seamoth",
        "海蛾号与 200m 深度",
        "移动载具舱造海蛾号，装深度模块；开始 200-500 米资源循环。",
        "tech",
        "需要移动载具舱蓝图、钛、玻璃、润滑剂和动力电池。",
    ),
    Milestone(
        "degasi",
        "追踪 Degasi 基地",
        "跟着 PDA 信号去 250m 蛇菇洞穴与 500m 大礁；拿独眼巨人号碎片。",
        "story",
        "蛇菇洞穴有磁铁矿和页岩；注意蟹蛇。",
    ),
    Milestone(
        "lost_river",
        "进入 Lost River",
        "500-900 米；带深度模块、手电、食物水、建造枪；找疾病研究中心。",
        "story",
        "幽灵利维坦会守通道；靠岩壁慢慢走，别开载具灯挑衅。",
    ),
    Milestone(
        "lava",
        "熔岩区与海皇",
        "1300 米以下；海虾号/独眼巨人号抗压升级，找海皇囚笼。",
        "story",
        "海龙利维坦在熔岩湖；热泉会持续伤害。",
    ),
    Milestone(
        "rocket",
        "海王星火箭",
        "收集火箭蓝图与材料，按阶段建造发射平台、助推器、燃料舱。",
        "story",
        "发射前确认已拿到孵化酶并解除感染。",
    ),
)

# ---------------------------------------------------------------------------
# Early route for Below Zero
# ---------------------------------------------------------------------------

BELOW_ZERO_MILESTONES: tuple[Milestone, ...] = (
    Milestone(
        "drop_pod",
        "修复救生舱与定位",
        "灭火、拿补给、修无线电；先确保氧气与温度。",
        "survival",
        "Below Zero 的寒冷会掉血；上岸/进舱会回暖。",
    ),
    Milestone(
        "seatruck",
        "拼出海卡车",
        "扫描海卡车碎片，做移动载具舱与海卡车；它是移动基地。",
        "tech",
        "海卡车模块可在月池制造；优先 Storage 与 Fabricator 模块。",
    ),
    Milestone(
        "alan",
        "找到 Al-An",
        "跟随信号到 Twisty Bridges / Deep Twisty Bridges，接入外星设施。",
        "story",
        "Al-An 需要身体部件，后面会指向 Crystal Caves 与 Fabricator Caverns。",
    ),
    Milestone(
        "delta",
        "Delta 站与温室",
        "扫描 Delta 站，拿温室与雪狐碎片；准备抗寒装备。",
        "story",
        "陆上区域有冰虫；雪狐号比步行安全。",
    ),
    Milestone(
        "crystal",
        "水晶洞穴",
        "海卡车抗压升级后下潜；找 Al-An 的身体部件。",
        "story",
        "暗影利维坦在水晶洞穴；关灯、贴地、别直冲。",
    ),
    Milestone(
        "fabricator_caverns",
        "制造者洞穴",
        "带海虾号/海卡车与补给，取回最后的身体部件。",
        "story",
        "洞穴高温高压，先做抗压与隔热升级。",
    ),
    Milestone(
        "alan_body",
        "为 Al-An 组装身体",
        "把部件带回基地，完成组装并听取下一步。",
        "story",
        "组装在基地的外星科技台完成。",
    ),
    Milestone(
        "endgame",
        "结局与撤离",
        "按 Al-An 的指引完成最终设施，准备返回。",
        "story",
        "最终区域有守卫；先清场再交互。",
    ),
)


# ---------------------------------------------------------------------------
# Biome knowledge (shared by prompts and the local planner)
# ---------------------------------------------------------------------------

BIOMES: dict[str, dict[str, Any]] = {
    "safe_shallows": {
        "zh": "安全浅滩",
        "depth": "0-30m",
        "hazards": "爆炸鱼（Crashfish）从巢里冲出来；毒气鱼会放毒气。",
        "resources": "钛、石英、金属残骸、酸蘑菇。",
        "advice": "出生点。先扫残骸，别在爆炸鱼巢口停留。",
    },
    "kelp_forest": {
        "zh": "海藻森林",
        "depth": "0-50m",
        "hazards": "潜行者会咬金属残骸，也会撞人。",
        "resources": "爬行藤种子、硅橡胶、金属残骸。",
        "advice": "潜行者咬残骸时不要贴脸；拿到种子就上浮。",
    },
    "grassy_plateaus": {
        "zh": "草状高原",
        "depth": "50-150m",
        "hazards": "沙鲨、咬人鱼。",
        "resources": "盐块、铜矿、钛、石英。",
        "advice": "开阔地，贴着礁石走，别在沙地中央停。",
    },
    "jellyshroom": {
        "zh": "蛇菇洞穴",
        "depth": "100-250m",
        "hazards": "蟹蛇；洞顶低，容易迷路。",
        "resources": "磁铁矿、页岩、锂。",
        "advice": "带手电和额外氧气；沿着发光蘑菇走。",
    },
    "mushroom_forest": {
        "zh": "蘑菇森林",
        "depth": "100-250m",
        "hazards": "骨鲨。",
        "resources": "独眼巨人号碎片、锂、铜。",
        "advice": "残骸多，适合扫蓝图；听到骨鲨就贴底。",
    },
    "aurora": {
        "zh": "极光号",
        "depth": "0-150m",
        "hazards": "辐射、死神利维坦、火灾。",
        "resources": "海蛾号深度模块、舰长室代码、推进炮碎片。",
        "advice": "先做防辐射服；从船头侧面靠近，避开船头正前方。",
    },
    "grand_reef": {
        "zh": "大礁",
        "depth": "200-500m",
        "hazards": "幽灵利维坦、强流。",
        "resources": "蓝晶石、钻石、残骸。",
        "advice": "沿岩壁走，别在开阔蓝水直线游。",
    },
    "lost_river": {
        "zh": "失落之河",
        "depth": "500-900m",
        "hazards": "幽灵利维坦、毒气、低温。",
        "resources": "镍、结晶硫、化石。",
        "advice": "带深度模块和补给；先找疾病研究中心。",
    },
    "lava_zones": {
        "zh": "熔岩区",
        "depth": "1000-1500m",
        "hazards": "海龙利维坦、热泉、高温。",
        "resources": "蓝晶石、离子晶体。",
        "advice": "海虾号要有抗压和隔热；不要停在热泉上。",
    },
    "twisty_bridges": {
        "zh": "扭曲桥",
        "depth": "0-100m",
        "hazards": "潜行者、寒冷。",
        "resources": "钛、石英、海卡车碎片。",
        "advice": "Below Zero 的出生区，先找海卡车碎片。",
    },
    "crystal_caves": {
        "zh": "水晶洞穴",
        "depth": "200-500m",
        "hazards": "暗影利维坦、低温。",
        "resources": "蓝晶石、晶体。",
        "advice": "关掉载具灯，贴地慢行；暗影利维坦靠声音和光定位。",
    },
    "glacial_basin": {
        "zh": "冰川盆地",
        "depth": "陆地",
        "hazards": "冰虫、雪行者、暴风雪。",
        "resources": "雪狐号碎片、钛、石英。",
        "advice": "陆上优先雪狐号；步行时听冰面震动。",
    },
}


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

SUBNAUTICA = GameProfile(
    id="subnautica",
    name_zh="深海迷航",
    name_en="Subnautica",
    app_id="264710",
    exe="Subnautica.exe",
    install_dirs=("Subnautica",),
    title_patterns=(r"subnautica", r"深海迷航"),
    aliases=(
        "subnautica", "深海迷航", "深海", "美丽水世界", "subnautica 1",
        "一代", "1代", "生存", "subnautica survival",
    ),
    keys=SUBNAUTICA_KEYS,
    strategy=(
        "先活下来，再向外扩：氧气优先于贪资源；看到利维坦先退到岩壁/载具；"
        "每个新深度先确认抗压和返程氧气。所有操作都从画面判断，不读内存。"
    ),
    milestones=SUBNAUTICA_MILESTONES,
    hud_style="subnautica",
    release="1.22.83031",
    depth_note="关键深度：0-200m 海蛾号；200-500m 大礁；500-900m 失落之河；1000m+ 熔岩区。",
    notes=(
        "按左键使用/拾取/扫描；E 进出载具/收放工具；Tab 开 PDA；"
        "Q 拆除；R 装填；1-5 快捷栏；空格上浮；C 下潜。"
    ),
)

BELOW_ZERO = GameProfile(
    id="below_zero",
    name_zh="深海迷航：零度之下",
    name_en="Subnautica: Below Zero",
    app_id="848450",
    exe="SubnauticaZero.exe",
    install_dirs=("SubnauticaZero",),
    title_patterns=(r"below\s*zero", r"零度之下", r"subnauticazero"),
    aliases=(
        "below zero", "belowzero", "零度之下", "深海迷航零度之下",
        "深海迷航：零度之下", "冰河", "寒零", "subnautica zero",
        "subnautica below zero", "subnauticabelowzero", "subnauticazero",
    ),
    keys=SUBNAUTICA_KEYS,
    strategy=(
        "Below Zero 的核心是温度与载具：冷会持续掉血，海卡车是移动基地；"
        "陆上优先雪狐号。先做保暖装备，再下潜；遇到暗影利维坦关灯贴地。"
    ),
    milestones=BELOW_ZERO_MILESTONES,
    hud_style="subnautica",
    release="1.22.83031",
    depth_note="关键深度：0-100m 扭曲桥；200m+ 水晶洞穴；海卡车需抗压升级。",
    notes=(
        "操作与 Subnautica 基本一致；额外注意体温。"
        "左键使用/拾取/扫描；E 进出载具；Tab 开 PDA。"
    ),
)

GAMES: tuple[GameProfile, ...] = (SUBNAUTICA, BELOW_ZERO)
GAME_BY_ID: dict[str, GameProfile] = {game.id: game for game in GAMES}
SUPPORTED_IDS: tuple[str, ...] = tuple(game.id for game in GAMES)


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------


def normalise_text(text: str) -> str:
    """NFKC + lowercase + strip punctuation, for forgiving alias matching."""
    folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return re.sub(r"[\s_\-:：·・,，.。!！?？'\"“”‘’（）()\[\]【】]+", "", folded)


def aliases_for(game: GameProfile) -> tuple[str, ...]:
    return (game.name_zh, game.name_en, game.id, *game.aliases)


@dataclass
class Resolution:
    game: GameProfile | None = None
    assumed: bool = False
    requested: str = ""
    reason: str = ""


def resolve_game(text: str, *, installed: Iterable[str] | None = None,
                 running: Iterable[str] | None = None) -> Resolution:
    """Resolve a user phrase to one profile without silently swapping games.

    Exact aliases win.  A fuzzy match is accepted only when exactly one game
    matches; ambiguity returns ``game=None`` and a reason instead of guessing.
    """
    raw = str(text or "").strip()
    if not raw:
        return Resolution(requested=raw, reason="empty")
    needle = normalise_text(raw)
    if "subnautica2" in needle or "深海迷航2" in needle:
        return Resolution(requested=raw, reason="unsupported_subnautica_2")
    best: list[tuple[int, GameProfile]] = []
    for game in GAMES:
        for alias in aliases_for(game):
            candidate = normalise_text(alias)
            if not candidate:
                continue
            if needle == candidate:
                return Resolution(game=game, requested=raw, reason="exact")
            if len(candidate) >= 3 and (candidate in needle or needle in candidate):
                best.append((len(candidate), game))
    if not best:
        return Resolution(requested=raw, reason="unknown")
    best.sort(key=lambda item: item[0], reverse=True)
    top_length, top_game = best[0]
    rivals = {game.id for length, game in best if length == top_length}
    if len(rivals) > 1:
        return Resolution(requested=raw, reason="ambiguous")
    return Resolution(game=top_game, requested=raw, reason="fuzzy")


def describe_installed(installed: Iterable[str]) -> str:
    ids = set(installed)
    names = [game.name_zh for game in GAMES if game.id in ids]
    return "、".join(names) if names else "没有检测到"


def walk_route(game: GameProfile) -> list[dict[str, str]]:
    """Public helper for status/prompt callers (kept out of the dataclass)."""
    return [milestone.as_dict() for milestone in game.milestones]
