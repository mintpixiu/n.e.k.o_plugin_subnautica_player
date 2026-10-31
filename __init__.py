"""N.E.K.O plugin: let the catgirl actually play Subnautica / Below Zero.

The facade owns only plumbing: lifecycle wiring, the LLM tool surface and the
Plugin Center entries.  Capture, discovery, the reactive pilot, the planner and
proactive narration live in :mod:`service`.

Safety defaults (all configurable in ``plugin.toml``):

* the plugin never launches a game -- the player opens it first;
* enabling the plugin does not start playing -- ``subnautica_play`` does;
* no key is injected unless the game window is in the foreground;
* F9 pauses/resumes; touching WASD/E/Tab/mouse yields immediately;
* the plugin never clicks "New Game" and never writes a save.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
    ui,
)

from . import catalog, discovery, prompts
from .service import SubnauticaService

# ---------------------------------------------------------------------------
# JSON schemas (module level so tests/metadata see exactly what the LLM sees)
# ---------------------------------------------------------------------------

PLAY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "game": {
            "type": "string",
            "description": (
                "主人说的游戏名，原话传进来。例如：深海迷航、Subnautica、零度之下、"
                "Below Zero。不传时：如果正好有一部支持的游戏在运行，就接管它。"
            ),
        },
        "objective": {
            "type": "string",
            "description": "可选：这一阶段的目标，例如「先做扫描仪」「找海蛾号碎片」「建第一个基地」。",
        },
    },
    "required": [],
}

PLAY_DESCRIPTION = (
    "让猫娘开始玩《深海迷航》或《深海迷航：零度之下》。**游戏必须由主人先手动打开并进入存档**；"
    "本工具只接管已经运行的游戏窗口，不会启动游戏，也不会点新游戏或覆盖存档。\n\n"
    "调用时机：主人说「去玩深海迷航」「玩零度之下」「你来玩」「帮我探索」「接手」这类开始/切换要求。\n\n"
    "返回会带 strategy 和主线阶段；如果游戏没开，会说明在等待，不要谎称已经在玩。"
)

COMMAND_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "instruction": {
            "type": "string",
            "description": (
                "主人/当前决策的一句话操作，例如：往前走 / 后退 / 上浮 / 下潜 / 左移 / 右移 / "
                "扫描 / 按E / 左键拾取 / 打开PDA / 转左 / 停手 / 继续。"
            ),
        },
        "seconds": {
            "type": "number",
            "description": "可选：持续秒数；0 表示用默认值。",
        },
    },
    "required": ["instruction"],
}

COMMAND_DESCRIPTION = (
    "让猫娘立刻做一个具体动作，不改变长期计划。\n\n"
    "调用时机：主人说「往前一点」「上浮」「扫描它」「按E进舱」「打开PDA」；"
    "或者你想在当前画面执行一个短动作。\n\n"
    "不要用它回答状态（用 subnautica_status）、不要用它开始游戏（用 subnautica_play）、"
    "不要在制造机/建造界面里用它乱按（用 subnautica_ui 先看再点）。"
)

STATUS_SCHEMA: Dict[str, Any] = {"type": "object", "properties": {}, "required": []}

STATUS_DESCRIPTION = (
    "查询当前游戏、窗口、前台、HUD、氧气/血量估计、当前策略、最近动作、"
    "长期计划、会话时长、是否被玩家接手、最近错误。\n\n"
    "调用时机：主人问「现在什么情况」「你怎么不动」「氧气还有多少」「计划到哪了」。"
)

LOOK_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "note": {"type": "string", "description": "可选：为什么要看，只写日志。"},
    },
    "required": [],
}

LOOK_DESCRIPTION = (
    "立刻截一张当前游戏画面，送进视觉上下文，返回画面特征和 HUD 判断。\n\n"
    "调用时机：主人问「你看到什么」；或者你要确认菜单、生物、资源、深度、氧气时。"
    "本工具不会 OCR 数字；氧气/血量具体数值看不到就说看不到。"
)

PLAN_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {
            "type": "string",
            "description": "这一阶段的目标，例如「先做扫描仪和 Seaglide」「找海蛾号碎片」「建第一个基地」。",
        },
        "steps": {
            "type": "string",
            "description": "可选：1-5 步，用中文分号/换行分隔。",
        },
        "mode": {
            "type": "string",
            "enum": ["explore", "surface", "gather", "scan", "fight", "evade",
                     "craft", "build", "vehicle", "story", "wait"],
            "description": "本地策略模式：探索/上浮/采集/扫描/战斗/规避/制造/建造/载具/主线/等待。",
        },
        "seconds": {
            "type": "number",
            "description": "这个模式保持多久；0/不传表示 45 秒。",
        },
    },
    "required": ["goal"],
}

PLAN_DESCRIPTION = (
    "设置猫娘这一阶段的**高层目标与打法**（不是单个按键），并写入长期计划。\n\n"
    "调用时机：你从截图判断出当前该做什么，例如「先回救生舱补水」「去残骸扫海蛾号碎片」"
    "「这个基地先放太阳能和储物柜」；主人说「你自己看着办」也可以。\n\n"
    "如果要制造/建造，把 mode 设为 craft/build，然后用 subnautica_ui 看截图点选；"
    "不要在没有看清界面时调用。"
)

UI_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["click", "key", "scroll"],
            "description": "click=点鼠标；key=按一个键；scroll=滚轮。",
        },
        "x": {"type": "number", "description": "click：画面横向位置 0..1（也接受像素坐标）。"},
        "y": {"type": "number", "description": "click：画面纵向位置 0..1（也接受像素坐标）。"},
        "key": {"type": "string", "description": "key：键名，例如 tab / e / esc / 1 / enter。"},
        "notches": {"type": "integer", "description": "scroll：滚轮格数，正数向上。"},
        "button": {"type": "string", "enum": ["left", "right"], "description": "click 的按键。"},
        "double": {"type": "boolean", "description": "click 是否双击。"},
    },
    "required": ["action"],
}

UI_DESCRIPTION = (
    "在 PDA / 制造机 / 建造 / 菜单界面里点鼠标、按键或滚动。\n\n"
    "调用时机：你刚用 subnautica_look 看清了界面，知道某个配方/标签/按钮的位置；"
    "或者需要按 Tab/Esc 关闭界面。**第一人称游戏画面没有可见指针，不要用这个工具乱点**；"
    "那种情况用 subnautica_command。\n\n"
    "每次点击后插件会重新截图并送进视觉上下文；如果点错了，先用 look 确认再修正。"
)

SEQUENCE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "maxItems": 12,
            "description": (
                "按顺序执行的动作。每项 action 可以是：forward/back/left/right/up/down/"
                "jump/interact/use/scan/attack/pda/slot1..5/hold/tap/wait/look/aim/click/scroll。"
                "hold/tap 需要 key；look 需要 dx/dy；aim 需要 x/y；click 可带 button。"
            ),
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "seconds": {"type": "number"},
                    "key": {"type": "string"},
                    "dx": {"type": "number"},
                    "dy": {"type": "number"},
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                    "notches": {"type": "integer"},
                    "button": {"type": "string", "enum": ["left", "right"]},
                },
                "required": ["action"],
            },
        }
    },
    "required": ["actions"],
}

SEQUENCE_DESCRIPTION = (
    "执行一小段连续动作（最多 12 步、约 15 秒），适合「前进→扫视→扫描→上浮」这种组合。\n\n"
    "调用时机：你已经想清楚一个短序列；或者主人说「你按这个顺序试一下」。"
    "玩家一碰键鼠，序列会很快停下并把控制权交还。"
)

AIM_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "x": {"type": "number", "description": "目标横向位置 0=最左，1=最右；也接受像素。"},
        "y": {"type": "number", "description": "目标纵向位置 0=最上，1=最下；也接受像素。"},
        "fire": {"type": "boolean", "description": "转过去后是否立刻左键，默认 false。"},
        "button": {"type": "string", "enum": ["left", "right"]},
    },
    "required": ["x", "y"],
}

AIM_DESCRIPTION = (
    "把视角转向画面中的某个位置（归一化坐标），可选立刻左键/右键。\n\n"
    "调用时机：你在截图里看到一条鱼、一个残骸、一个可扫描目标，想把准星转过去。"
    "照片里的像素坐标可以直接传；插件会换算。"
)

MILESTONE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "key": {
            "type": "string",
            "description": "阶段 key，例如 scanner / seaglide / base_start / seamoth。先用 subnautica_plan_status 查。",
        },
        "status": {"type": "string", "enum": ["done", "skipped"], "description": "默认 done。"},
        "note": {"type": "string", "description": "可选：看到的证据，例如「工作台已出现扫描仪选项」。"},
    },
    "required": ["key"],
}

MILESTONE_DESCRIPTION = (
    "把长期计划里的一个阶段标记为完成/跳过。\n\n"
    "调用时机：你**在画面里看到了明确证据**（新配方、载具、基地、剧情事件），"
    "不要仅凭猜测或时间流逝标记。写一句证据到 note，方便下次继续。"
)

NOTE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "description": "例如 blueprint / resource / hazard / plan / observation。"},
        "text": {"type": "string", "description": "要记住的事实，最多 1000 字。"},
    },
    "required": ["text"],
}

NOTE_DESCRIPTION = (
    "把一条观察写进长期日志，例如「在 120m 蛇菇洞穴看到磁铁矿」「海蛾号碎片还差 2 个」"
    "「这个方向有死神利维坦」。不要写没有看到的东西。"
)

BIOME_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "biome": {
            "type": "string",
            "description": "生态区 key，例如 safe_shallows / kelp_forest / jellyshroom / lost_river / crystal_caves。",
        },
    },
    "required": ["biome"],
}

BIOME_DESCRIPTION = (
    "记录第一次到达的生态区，返回该区域的深度、危险和资源提示。"
    "不确定时不要猜；可以先用 subnautica_look 看画面。"
)

SELFCHECK_SCHEMA: Dict[str, Any] = {"type": "object", "properties": {}, "required": []}

SELFCHECK_DESCRIPTION = (
    "只读环境自检：Steam 库、游戏窗口、前台、截图后端、真实截屏、HUD、输入结构体、配置。"
    "不会按键、不会启动游戏。"
)

GAMES_SCHEMA: Dict[str, Any] = {"type": "object", "properties": {}, "required": []}

GAMES_DESCRIPTION = (
    "列出支持的游戏、是否安装、是否在运行，以及本插件不支持的《Subnautica 2》。"
)


def _tool_error(code: str, message: str) -> Dict[str, Any]:
    return {"output": {"reason": message}, "is_error": True, "error": code}


def _ok_or_err(result: Any) -> Any:
    if isinstance(result, dict) and result.get("is_error"):
        output = result.get("output")
        reason = output.get("reason") if isinstance(output, dict) else None
        return Err(SdkError(str(reason or result.get("error") or "tool failed")))
    return Ok(result)


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@neko_plugin
class SubnauticaPlayerPlugin(NekoPluginBase):
    """Plugin facade: lifecycle + tools + Plugin Center entries."""

    def __init__(self, ctx):
        super().__init__(ctx)
        try:
            self.logger = self.enable_file_logging(log_level="INFO")
        except Exception:
            self.logger = getattr(ctx, "logger", None)
        self._service = SubnauticaService(
            logger=self.logger,
            push_message_fn=self.push_message,
            data_dir=self.data_path(),
        )
        self._started = False
        self._start_lock = asyncio.Lock()

    async def _ensure_service_started(self) -> bool:
        async with self._start_lock:
            if self._started:
                return True
            try:
                await self._service.start()
                self._started = True
                return True
            except Exception as exc:
                if self.logger is not None:
                    self.logger.warning(
                        "[startup] service.start failed; will retry — {}: {}",
                        type(exc).__name__, exc,
                    )
                return False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @lifecycle(id="startup")
    async def startup(self, **_):
        cfg = await self.config.dump(timeout=5.0)
        cfg = cfg if isinstance(cfg, dict) else {}
        raw = cfg.get("subnautica", {}) if isinstance(cfg.get("subnautica"), dict) else {}
        self._service.configure(raw)
        self._service.set_lang(prompts.user_lang())
        return Ok({
            "status": "ready",
            "installed": self._service.installed_ids(),
            "running": self._service.running_ids(),
            "autopilot": False,
            "note": "游戏需要主人手动打开；subnautica_play 才会接管。",
        })

    @lifecycle(id="shutdown")
    async def shutdown(self, **_):
        try:
            await self._service.stop()
        except Exception as exc:
            if self.logger is not None:
                self.logger.warning("[shutdown] {}", exc)
        self._started = False
        return Ok({"status": "shutdown"})

    @lifecycle(id="config_change")
    async def config_change(self, **_):
        cfg = await self.config.dump(timeout=5.0)
        cfg = cfg if isinstance(cfg, dict) else {}
        raw = cfg.get("subnautica", {}) if isinstance(cfg.get("subnautica"), dict) else {}
        self._service.configure(raw)
        return Ok({"status": "reconfigured", "dry_run": self._service.cfg.dry_run})

    @lifecycle(id="freeze")
    async def freeze(self, **_):
        try:
            self._service.stop_playing(reason="freeze")
        except Exception:
            pass
        return Ok({"status": "frozen"})

    @lifecycle(id="unfreeze")
    async def unfreeze(self, **_):
        return Ok({"status": "unfrozen"})

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    @llm_tool(name="subnautica_play", description=PLAY_DESCRIPTION,
              parameters=PLAY_SCHEMA, timeout=45.0)
    async def subnautica_play_tool(self, *, game: Any = "", objective: Any = "", **_):
        if not await self._ensure_service_started():
            return _tool_error("UNAVAILABLE", "插件服务起不来，看插件日志。")
        installed = self._service.installed_ids()
        self._service.get_status()
        running = self._service.running_ids()
        game_text = str(game or "").strip()
        if not game_text:
            if len(running) == 1:
                game_text = catalog.GAME_BY_ID[running[0]].name_zh
            elif len(running) > 1:
                names = "、".join(catalog.GAME_BY_ID[gid].name_zh for gid in running)
                return {
                    "summary": f"检测到多部游戏在运行：{names}。请主人说清玩哪一部。",
                    "running": running,
                }
            else:
                names = catalog.describe_installed(installed)
                return {
                    "summary": (
                        f"没说玩哪一部。现在已安装：{names}。请说「去玩深海迷航」或「玩零度之下」，"
                        "并先手动打开游戏、进入存档。"
                    ),
                    "installed": [catalog.GAME_BY_ID[gid].summary() for gid in installed],
                }
        resolution = catalog.resolve_game(game_text, installed=installed, running=running)
        if resolution.reason == "unsupported_subnautica_2":
            return {
                "summary": (
                    "《Subnautica 2》不在本插件范围内；本插件只操作《深海迷航》和"
                    "《深海迷航：零度之下》。我没有假装能玩它。"
                ),
                "supported": False,
            }
        if resolution.game is None:
            names = catalog.describe_installed(installed)
            return _tool_error(
                "UNKNOWN_GAME",
                f"没认出要玩哪部（用户说：{game_text[:50]!r}）。现在检测到：{names}。"
                "请说「深海迷航」或「零度之下」。",
            )
        profile = resolution.game
        if profile.id not in set(installed) and profile.id not in set(running):
            names = catalog.describe_installed(installed)
            return {
                "summary": (
                    f"没在 Steam 库里找到《{profile.name_zh}》。现在检测到：{names}。"
                    "请先在 Steam 安装；如果是非 Steam 版，先打开游戏再让我试一次。"
                ),
                "game": profile.summary(),
                "installed": [catalog.GAME_BY_ID[gid].summary() for gid in installed],
            }
        status = self._service.play(profile, objective=str(objective or "").strip())
        running_now = self._service.ensure_running_game(force=True)
        window = running_now.window if running_now is not None else None
        menu = bool(discovery.menu_active(window.hwnd)) if window is not None else False
        assumption = ""
        if resolution.assumed:
            assumption = f"（我把「{game_text[:30]}」理解成《{profile.name_zh}》。）"
        if running_now is None:
            summary = (
                f"已选定《{profile.name_zh}》，但还没检测到它的窗口。"
                "请主人**手动打开并进入存档**；窗口出现后我会自动开始。这不是故障。"
            )
        elif menu:
            summary = (
                f"已经看到《{profile.name_zh}》的窗口，但画面停在菜单/暂停。"
                "请主人先进入存档；进入水下后我就接手。我不会点「新游戏」。"
            )
        else:
            summary = (
                f"已接管《{profile.name_zh}》，目标：{status['objective']}。"
                "我开始按画面判断探索；主人随时可以碰键接手，F9 让我停手。"
            )
        if self._service.cfg.dry_run:
            summary += " 现在是 dry_run 演练：只观察、不按键。"
        return {
            "summary": summary + assumption,
            "game": profile.summary(),
            "strategy": profile.strategy_brief(max_milestones=10),
            "route": [item.as_dict() for item in profile.milestones[:12]],
            "waiting_for_game": running_now is None,
            "menu_active": menu,
            "assumed": resolution.assumed,
            "status": self._service.get_status(),
        }

    @llm_tool(name="subnautica_status", description=STATUS_DESCRIPTION,
              parameters=STATUS_SCHEMA, timeout=20.0)
    async def subnautica_status_tool(self, **_):
        try:
            return self._service.get_status()
        except Exception as exc:
            if self.logger is not None:
                self.logger.exception("[status] failed")
            return _tool_error("INTERNAL", type(exc).__name__)

    @llm_tool(name="subnautica_look", description=LOOK_DESCRIPTION,
              parameters=LOOK_SCHEMA, timeout=30.0)
    async def subnautica_look_tool(self, *, note: Any = "", **_):
        if not await self._ensure_service_started():
            return _tool_error("UNAVAILABLE", "插件服务起不来，看插件日志。")
        return await self._service.look(note=str(note or ""))

    @llm_tool(name="subnautica_command", description=COMMAND_DESCRIPTION,
              parameters=COMMAND_SCHEMA, timeout=20.0)
    async def subnautica_command_tool(self, *, instruction: Any = "", seconds: Any = 0, **_):
        if not isinstance(instruction, str) or not instruction.strip():
            return _tool_error("INVALID_ARGUMENT", "subnautica_command 需要一句操作，例如「上浮」「扫描」「按E」。")
        if not await self._ensure_service_started():
            return _tool_error("UNAVAILABLE", "插件服务起不来，看插件日志。")
        return self._service.apply_command(instruction.strip(), seconds=_float(seconds, 0.0))

    @llm_tool(name="subnautica_plan", description=PLAN_DESCRIPTION,
              parameters=PLAN_SCHEMA, timeout=20.0)
    async def subnautica_plan_tool(self, *, goal: Any = "", steps: Any = "",
                                    mode: Any = "explore", seconds: Any = 0, **_):
        if not isinstance(goal, str) or not goal.strip():
            return _tool_error("INVALID_ARGUMENT", "subnautica_plan 需要一句目标。")
        if not await self._ensure_service_started():
            return _tool_error("UNAVAILABLE", "插件服务起不来，看插件日志。")
        return self._service.set_directive(
            mode=str(mode or "explore"),
            goal=goal.strip(),
            steps=str(steps or ""),
            seconds=_float(seconds, 0.0) or 45.0,
        )

    @llm_tool(name="subnautica_ui", description=UI_DESCRIPTION,
              parameters=UI_SCHEMA, timeout=30.0)
    async def subnautica_ui_tool(self, *, action: Any = "click", x: Any = 0.5, y: Any = 0.5,
                                  key: Any = "", notches: Any = 1, button: Any = "left",
                                  double: Any = False, **_):
        if not await self._ensure_service_started():
            return _tool_error("UNAVAILABLE", "插件服务起不来，看插件日志。")
        action_text = str(action or "click").strip().lower()
        if action_text == "key":
            return await self._service.ui_key(str(key or ""))
        if action_text == "scroll":
            return await self._service.ui_scroll(_int(notches, 1))
        if action_text == "click":
            return await self._service.ui_click(
                _float(x, 0.5), _float(y, 0.5),
                button="right" if str(button).lower() == "right" else "left",
                double=bool(double),
            )
        return _tool_error("INVALID_ARGUMENT", "action 只能是 click / key / scroll。")

    @llm_tool(name="subnautica_sequence", description=SEQUENCE_DESCRIPTION,
              parameters=SEQUENCE_SCHEMA, timeout=35.0)
    async def subnautica_sequence_tool(self, *, actions: Any = None, **_):
        if not await self._ensure_service_started():
            return _tool_error("UNAVAILABLE", "插件服务起不来，看插件日志。")
        return await self._service.run_sequence(actions)

    @llm_tool(name="subnautica_aim", description=AIM_DESCRIPTION,
              parameters=AIM_SCHEMA, timeout=20.0)
    async def subnautica_aim_tool(self, *, x: Any = 0.5, y: Any = 0.5,
                                   fire: Any = False, button: Any = "left", **_):
        if not await self._ensure_service_started():
            return _tool_error("UNAVAILABLE", "插件服务起不来，看插件日志。")
        return self._service.aim(
            _float(x, 0.5), _float(y, 0.5),
            fire=bool(fire),
            button="right" if str(button).lower() == "right" else "left",
        )

    @llm_tool(name="subnautica_stop", description=(
        "让猫娘立刻停手：松开所有按键、停止自动操作，把键盘还给主人。"
        "不会关闭游戏，也不会丢存档。之后说「继续」或调 subnautica_resume。"
    ), parameters=STATUS_SCHEMA, timeout=15.0)
    async def subnautica_stop_tool(self, **_):
        if not await self._ensure_service_started():
            return _tool_error("UNAVAILABLE", "插件服务起不来，看插件日志。")
        if self._service.get_status().get("selected_game") is None:
            return {"summary": "现在还没在玩；不用停。"}
        status = self._service.stop_playing(reason="tool")
        return {"summary": "停手了，所有按键都松开了。想继续就说「继续」。", "status": status}

    @llm_tool(name="subnautica_resume", description=(
        "让猫娘重新接管已经选定的游戏（在 stop/玩家接手之后）。"
        "调用时机：主人说「继续」「你接着玩」「交给你」。"
    ), parameters=STATUS_SCHEMA, timeout=20.0)
    async def subnautica_resume_tool(self, **_):
        if not await self._ensure_service_started():
            return _tool_error("UNAVAILABLE", "插件服务起不来，看插件日志。")
        return self._service.resume_playing(reason="tool")

    @llm_tool(name="subnautica_plan_status", description=(
        "查看长期计划、当前阶段、已记录的生态区和最近日志。"
        "调用时机：主人问「计划到哪了」，或你想知道下一个目标。"
    ), parameters=STATUS_SCHEMA, timeout=20.0)
    async def subnautica_plan_status_tool(self, **_):
        if not await self._ensure_service_started():
            return _tool_error("UNAVAILABLE", "插件服务起不来，看插件日志。")
        return self._service.get_plan()

    @llm_tool(name="subnautica_milestone", description=MILESTONE_DESCRIPTION,
              parameters=MILESTONE_SCHEMA, timeout=20.0)
    async def subnautica_milestone_tool(self, *, key: Any = "", status: Any = "done",
                                         note: Any = "", **_):
        if not await self._ensure_service_started():
            return _tool_error("UNAVAILABLE", "插件服务起不来，看插件日志。")
        return self._service.mark_milestone(
            str(key or ""),
            status="skipped" if str(status).lower() == "skipped" else "done",
            note=str(note or ""),
        )

    @llm_tool(name="subnautica_note", description=NOTE_DESCRIPTION,
              parameters=NOTE_SCHEMA, timeout=20.0)
    async def subnautica_note_tool(self, *, kind: Any = "observation", text: Any = "", **_):
        if not await self._ensure_service_started():
            return _tool_error("UNAVAILABLE", "插件服务起不来，看插件日志。")
        return self._service.record_note(str(kind or "observation"), str(text or ""))

    @llm_tool(name="subnautica_biome", description=BIOME_DESCRIPTION,
              parameters=BIOME_SCHEMA, timeout=20.0)
    async def subnautica_biome_tool(self, *, biome: Any = "", **_):
        if not await self._ensure_service_started():
            return _tool_error("UNAVAILABLE", "插件服务起不来，看插件日志。")
        return self._service.observe_biome(str(biome or ""))

    @llm_tool(name="subnautica_selfcheck", description=SELFCHECK_DESCRIPTION,
              parameters=SELFCHECK_SCHEMA, timeout=60.0)
    async def subnautica_selfcheck_tool(self, **_):
        if not await self._ensure_service_started():
            return _tool_error("UNAVAILABLE", "插件服务起不来，看插件日志。")
        checks = await self._service.selfcheck()
        problems = checks.get("problems") or []
        summary = "自检通过。" if not problems else ("自检发现问题：" + "；".join(problems[:4]))
        return {"summary": summary, "checks": checks}

    @llm_tool(name="subnautica_games", description=GAMES_DESCRIPTION,
              parameters=GAMES_SCHEMA, timeout=20.0)
    async def subnautica_games_tool(self, **_):
        if not await self._ensure_service_started():
            return _tool_error("UNAVAILABLE", "插件服务起不来，看插件日志。")
        self._service.get_status()
        installed = set(self._service.installed_ids())
        running = set(self._service.running_ids())
        games = []
        for profile in catalog.GAMES:
            item = profile.summary()
            item["installed"] = profile.id in installed
            item["running"] = profile.id in running
            games.append(item)
        return {
            "summary": (
                "支持《深海迷航》与《深海迷航：零度之下》。"
                "《Subnautica 2》不在本插件范围内。"
                f" 已安装：{catalog.describe_installed(installed)}。"
            ),
            "games": games,
            "unsupported": discovery.unsupported_running(),
            "installed": sorted(installed),
            "running": sorted(running),
        }

    # ------------------------------------------------------------------
    # Plugin Center entries
    # ------------------------------------------------------------------

    @ui.action(id="subnautica_play", label="让猫娘玩深海迷航", icon="play",
               tone="primary", order=10)
    @plugin_entry(
        id="subnautica_play",
        name="开始玩深海迷航/零度之下",
        description=(
            "让猫娘接管已经打开的《深海迷航》或《零度之下》。"
            "不会自己启动游戏：主人先手动打开并进入存档。"
        ),
        input_schema=PLAY_SCHEMA,
        llm_result_fields=["summary"],
    )
    async def subnautica_play_entry(self, *, game: Any = "", objective: Any = "", **_):
        result = await self.subnautica_play_tool(game=game, objective=objective)
        return _ok_or_err(result)

    @ui.action(id="subnautica_stop", label="让猫娘停手", icon="stop",
               tone="danger", order=20)
    @plugin_entry(
        id="subnautica_stop",
        name="让猫娘停手",
        description="立刻停止自动操作并松开所有按键，把键盘还给主人。不会关闭游戏。",
        input_schema=STATUS_SCHEMA,
        llm_result_fields=["summary"],
    )
    async def subnautica_stop_entry(self, **_):
        result = await self.subnautica_stop_tool()
        return _ok_or_err(result)

    @plugin_entry(
        id="subnautica_resume",
        name="让猫娘继续玩",
        description="在停手/玩家接手之后，重新接管已经选定的游戏。",
        input_schema=STATUS_SCHEMA,
        llm_result_fields=["summary"],
    )
    async def subnautica_resume_entry(self, **_):
        result = await self.subnautica_resume_tool()
        return _ok_or_err(result)

    @ui.action(id="subnautica_status", label="刷新状态", icon="refresh", order=30)
    @plugin_entry(
        id="subnautica_status",
        name="查询深海迷航插件状态",
        description="查询游戏、窗口、前台、HUD、策略、计划、会话时长和最近错误。",
        input_schema=STATUS_SCHEMA,
        llm_result_fields=["summary"],
    )
    async def subnautica_status_entry(self, **_):
        result = await self.subnautica_status_tool()
        return _ok_or_err(result)

    @plugin_entry(
        id="subnautica_look",
        name="看一眼当前游戏画面",
        description="截一张当前游戏画面并送进视觉上下文，返回 HUD 与画面特征。",
        input_schema=LOOK_SCHEMA,
        llm_result_fields=["summary"],
    )
    async def subnautica_look_entry(self, *, note: Any = "", **_):
        result = await self.subnautica_look_tool(note=note)
        return _ok_or_err(result)

    @ui.action(id="subnautica_selfcheck", label="环境自检", icon="settings", order=40)
    @plugin_entry(
        id="subnautica_selfcheck",
        name="深海迷航环境自检",
        description="只读自检：Steam 库、窗口、前台、截图、真实画面、HUD、输入结构体。",
        input_schema=SELFCHECK_SCHEMA,
        llm_result_fields=["summary"],
    )
    async def subnautica_selfcheck_entry(self, **_):
        result = await self.subnautica_selfcheck_tool()
        return _ok_or_err(result)

    @plugin_entry(
        id="subnautica_plan_status",
        name="查看长期计划",
        description="查看当前阶段、已完成阶段、生态区记录和最近日志。",
        input_schema=STATUS_SCHEMA,
        llm_result_fields=["summary"],
    )
    async def subnautica_plan_status_entry(self, **_):
        result = await self.subnautica_plan_status_tool()
        return _ok_or_err(result)
