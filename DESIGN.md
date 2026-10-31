# 设计说明 —— subnautica_player

> 目标：让猫娘**真的**玩《深海迷航》和《零度之下》，而不是“看起来在玩”。
> 本文记录架构、关键判断、实测依据和刻意不做的事。

---

## 1. 目标与非目标

### 目标

1. 用户手动打开游戏并读档后，插件能识别是哪一部、窗口是否可用、是否在菜单/HUD 界面。
2. 在没有 LLM 每 tick 指挥的情况下，也能持续上浮/下潜/探索/采集/扫描/脱困。
3. 每 30 秒主动把「HUD + 画面 + 判断 + 下一步」推给对话 LLM，由猫娘用自己的口吻说出来。
4. LLM 能通过工具改长期目标、做短序列、在看清 PDA/制造机/建造界面后点选。
5. 玩家随时能接手：碰键后约 0.1–0.4 秒松手；F9 硬停；说「停手/继续」也有效。
6. 长期计划诚实：只有看到证据才打勾，不把“时间过去了”当成“阶段完成了”。
7. 不启动游戏、不点新游戏、不写存档、不读内存、不作弊。
8. 能通过 N.E.K.O 插件管理导入，在冻结 Python 3.11 + numpy/Pillow 环境里跑。

### 非目标

* 不做读内存/读存档/自动瞄准/透视的外挂；
* 不保证 100% 通关；不声称一个脚本能解所有基地布局和剧情谜题；
* 不自动点主菜单的“新游戏”；
* 不代发 Steam 成就；
* 不操作《Subnautica 2》。

---

## 2. 模块

| 模块 | 职责 | 关键点 |
|------|------|--------|
| `__init__.py` | SDK facade：生命周期、16 个 LLM 工具、插件中心入口 | 不在 `startup` 里启动常驻任务 |
| `catalog.py` | 两部游戏、AppID、别名、键位、11/8 阶段路线、生态区知识 | 所有游戏差异集中在这里 |
| `discovery.py` | Steam 库、进程、窗口、前台、光标、DWM 物理边界、菜单 | 进程快照受限时走窗口枚举兜底 |
| `capture.py` | DXGI → GDI → Pillow 分层截图 | 失败后端隔离 + 冷却重试 |
| `dxgi_capture.py` | 纯 ctypes Desktop Duplication | 独占全屏；不依赖 dxcam/cv2 |
| `input.py` | SendInput 扫描码 + 相对鼠标 + 绝对点击 + 物理键轮询 | 前台守卫、`release_all`、F9 |
| `vision.py` | 亮度/运动/水下/暗色 UI/红边/文字带/准星 | 纯 numpy，不做目标识别 |
| `hud.py` | 血/食/水/氧四环颜色+形状检测 | **不 OCR 数字**；找不到就报 absent |
| `policy.py` | 指令解析 + 生存反射 + 探索/采集/扫描/卡住阶梯 | 低氧/受伤优先于玩家普通指令 |
| `planner.py` | 长期路线、生态区、日志、持久化 | 只由证据打勾；JSON 损坏自动重建 |
| `service.py` | 捕获/输入/策略/叙事/接管/UI 工具编排 | 所有竞态在这里处理 |
| `prompts.py` | 本地化 cue + 主动解说 prompt | 事实优先，禁止编造数字/进度 |

---

## 3. 数据流

```
用户语音
  └─ N.E.K.O LLM
       ├─ subnautica_play("深海迷航")
       │    └─ catalog.resolve_game → service.play → 发现窗口/等读档
       ├─ subnautica_plan / command / ui / sequence / aim
       │    └─ policy.Directive / Command / Op → service 执行
       └─ subnautica_milestone / note / biome → planner 持久日志

service._control_loop (约 3 fps)
  ├─ ensure_running_game()          # 进程/窗口兜底
  ├─ capture()                      # DXGI/GDI/PIL，DWM 物理边界裁窗口
  ├─ vision.analyze + hud.observe
  ├─ 守卫：黑屏 / 菜单 / 加载 / 失焦 / 玩家接手 / F9 / 会话超时
  ├─ policy.decide → _run_ops()     # 100ms 切片，随时可被打断
  └─ session/事件/状态

service._narration_loop (0.8s)
  ├─ 每 10s push image (ai_behavior="read")
  └─ 每 30s push situation+image (ai_behavior="respond")
       └─ 猫娘主动说话；可调用工具改计划或点 UI
```

---

## 4. 菜单 vs 游戏：实测出来的判据

Subnautica 是 Unity 游戏，第一人称游戏时**隐藏/锁定光标**；主菜单、暂停菜单、PDA、制造机、建造界面**显示光标**。
所以 `menu_active()` 的首要判据是 `GetCursorInfo` 的 `CURSOR_SHOWING`，而不是分辨率或像素。

`_classify_ui()` 再结合：

| 信号 | 含义 |
|------|------|
| `cursor_visible` + 未进入过水下 | 主菜单：只等待，不按键 |
| `cursor_visible` + 已进入 + `dark_ui > 0.58` | PDA/制造机/建造界面：交给 LLM 看截图点选 |
| `cursor_visible` + 已进入 + HUD 不在 | 暂停/设置菜单：等待 |
| `dark_ui > 0.93` 且亮度极低 | 加载画面：等待 |
| 光标隐藏、HUD/水下画面 | 游戏内：允许操作 |

这比“看画面亮不亮”稳，但仍有误判：如果玩家把 HUD 用 F6 关掉，暂停菜单可能被识别成 pda。
两种情况下插件都**不会注入移动键**，所以误判只会让她等，不会乱按。

### DPI / 全屏

本机显示缩放 150%：

```
GetWindowRect        = (0, 0, 1707, 1067)   # DPI 虚拟化
DwmGetWindowAttribute= (0, 0, 2560, 1600)   # 物理像素
DXGI frame           = 2560x1600
```

用 `GetWindowRect` 裁 DXGI 画面会只截到左上 2/3，这是实测抓到的真 bug。
修复：`discovery.physical_window_rect()` 用 `DWMWA_EXTENDED_FRAME_BOUNDS`，全屏时直接不裁。

### 进程发现

本机的 Python 进程用 `CreateToolhelp32Snapshot` 只能看到 33 个进程，连 Steam/Subnautica 都不在里面，
但 `EnumWindows` + `OpenProcess` 能看到 Unity 窗口。因此 `find_running_games()` 有窗口枚举兜底：
只接受 `UnityWndClass` + 标题匹配，并且明确排除 `Subnautica 2` 和 Steam 浏览器窗口。
没有这个兜底，插件会在玩家明明开着游戏时说“没检测到”。

---

## 5. 输入

* 键盘：`SendInput` + `KEYEVENTF_SCANCODE`，与物理键盘一致；不 `PostMessage`。
* 鼠标视角：相对 `MOUSEEVENTF_MOVE`，按 120 counts 切片，避免单事件被 clamp。
* 鼠标点击：`MOUSEEVENTF_ABSOLUTE` 移动 + 左/右键；只用于 UI。
* 前台守卫：`foreground_check` 绑定当前游戏窗口；`release_all` 永远允许 key-up。
* 物理键轮询：`GetAsyncKeyState` 读 WASD/空格/C/Shift/E/R/Q/F/X/T/Tab/1-5/鼠标键，
  排除注入器自己按住的键；命中即 `soft_yield`。
* F9：独立 `_hotkey_loop` 每 120ms 轮询，**autopilot 停止时也能恢复**。

### 竞态处理

`_run_ops` 的每个 hold 用 `_sleep_watched` 按 100ms 切片：

* 切片结束检查 `_mode`，变成 stopped/soft_yield 就立即 `return`；
* 检查物理键，命中就 `_yield_to_human`（release_all + 推 cue + 返回）；
* `finally` 仍然 key_up，不会因为 `return` 留下按键；
* 控制循环即使 `_autopilot_active=false` 也兜底检查 `held_keys/held_buttons` 并 `release_all()`。

`_op_lock` 防止控制循环和 `subnautica_sequence`/`ui_click` 同时注入。

---

## 6. 策略状态机

`PolicyState`: `waiting / menu / blind / surface / explore / gather / scan / evade / recover / craft / build / stuck / paused`。

### 决策优先级

1. 没有画面 / 全黑 → 拒绝按键；
2. UI（菜单/PDA/制造机/建造/加载）→ 交给 LLM/玩家，不盲按；
3. 受伤/血量低 → 后退 + 上浮；
4. 低氧或水下时间 > 28s → 强制上浮；
5. 饥饿/口渴 → 边找食物边请猫娘看 PDA；
6. 玩家/LLM 的一次性命令 → 执行一次；
7. `subnautica_plan` 的模式（surface/gather/scan/vehicle/evade/craft/build/story）；
8. 默认探索；场景长时间不变 → 卡住阶梯。

> 低氧/受伤**优先于**玩家的“往前走”。这不是覆盖主人，而是防止主人一句“往前”把猫娘送进溺水。

### 探索

6 相位循环：前进+扫视 → 前进+环顾+左键 → 前进+反向扫视 → 受控下潜+前进+低头 → 前进+扫视 → 上浮+环顾+左键。
每个相位约 4.2 秒。下潜只占 1/6，且低氧反射会打断。

### 卡住阶梯

`scene_hash` 不变且 `motion < 0.006` 超过 2.4 秒：

0. 左右看 55°；
1. 后退 + 上浮；
2. 侧移 + 前进；
3. 按 E + 上浮。

每级只做一个决策，随后重新观察。它不寻路，只避免“对着墙一直游”。

### 指令解析

支持：前进/后退/左移/右移/上浮/下潜/冲刺、跳、扫描（按住左键 2.2s）、按E/互动/进出载具、
左键使用/拾取/攻击、PDA、拆除、换弹、建造、旋转、快捷栏 1-5、左右看/抬头/低头、停手/继续。
“往前走 2 秒”里的 `2` 不会再被误判成快捷栏 2（回归测试盯住）。

---

## 7. HUD 检测

参考图与实机菜单截图都验证了四环结构。实现是纯 numpy：

1. 裁左下 42% × 48% 区域，缩到 ≤280px 宽；
2. RGB→HSV；
3. 四个窄 hue 窗口 + 饱和度/亮度阈值；
4. 连通域；按 bbox 长宽比、填充率、面积挑“圆环”；
5. 用历史最大面积做基线，当前面积/基线 = 相对比例；
6. 低于阈值给 `oxygen_low/health_low/food_low/water_low`。

刻意不做：
* 不 OCR 中间的数字（宿主没有 OCR，硬编模板分辨率一变就废）；
* 找不到环就 `present=False`，不把“看不见”当作“0%”；
* 血/食/水/氧的百分比是**相对基线**，不是游戏真实数值；prompt 里明确禁止把它当精确数字。

参考图测试：四环全部识别，缩到 25% 时四项都报 low；纯蓝背景报 `present=False`。

---

## 8. PDA / 制造机 / 建造

本地策略只负责“不要乱按”。真正的点选交给 LLM：

1. `subnautica_look` 或每 10s 的截图流把界面送进视觉上下文；
2. 猫娘用 `subnautica_ui(action="click", x=..., y=...)` 点击；
3. 插件重截图并再送进上下文，形成闭环；
4. 点错就 `look` 修正，或 `subnautica_stop` 让玩家接手。

`ui_click` 的三道门：

* 必须已经进入过水下（主菜单里拒绝，防开新游戏）；
* 必须是 `menu/pda` 界面（第一人称没有指针，拒绝盲点）；
* 必须前台 + 有新鲜画面。

这比固定坐标宏慢，但不会因为 UI 缩放/解锁顺序不同把材料点错。

---

## 9. 长期计划

`planner.py` 为每部游戏保存一个 JSON：

```json
{
  "version": 1,
  "game_id": "subnautica",
  "milestones": {"scanner": {"status": "done", "at": 0, "note": "..."}},
  "biomes": ["safe_shallows"],
  "notes": [{"kind": "blueprint", "text": "...", "at": 0}],
  "focus": "先做扫描仪"
}
```

* 切换游戏会重置对应路线，不会把 Subnautica 的进度套到 Below Zero；
* JSON 损坏/版本不符 → 新日志，不崩溃；
* 文件写入用临时文件 + `os.replace`，避免半截文件；
* 打勾只通过 `subnautica_milestone`，且 prompt 要求先看到证据；
* 笔记/生态区用于下次继续，不重复建议。

---

## 10. 失败模式

| 失败 | 检测 | 行为 |
|------|------|------|
| 游戏没开 | 进程+窗口都无 | 等待，60s 一次提示“不是故障” |
| 窗口最小化 | `IsIconic` + rect < `min_capture_edge` | 不截图、不按键，提示切回 |
| 窗口不在前台 | `GetForegroundWindow`/`GetAncestor` | 松手，等待 |
| 全黑 | `mean<8 and max<28` | 连续 8 帧后停手，报告真实错误 |
| DXGI 失败 | 捕获抛异常 | 隔离该后端 30s，降级 GDI/PIL |
| 截图无新帧 | DXGI 返回 None | 复用 1.2s 内上帧，不误判黑屏 |
| 输入失败 | SendInput 返回 < n | 记录 winerror，松手，限频提示 |
| 会话超时 | 实际游玩秒数 > 90min | 停手 + 提示 |
| 玩家接手 | 物理键轮询 | 松手，12s 空闲后恢复（F9 硬停除外） |

---

## 11. 测试矩阵

| 文件 | 覆盖 | 数量 |
|------|------|------|
| `test_contract.py` | 清单/工具/入口/安全默认/无依赖/无启动副作用 | 15 |
| `test_policy.py` | 指令解析、优先级、低氧、受伤、卡住、aim | 18 |
| `test_hud.py` | 四环识别、消耗比例、低警告、无 HUD 不猜 | 4 |
| `test_planner.py` | 初始阶段、打勾持久化、焦点、生态区、损坏日志、切游戏 | 7 |
| `test_discovery.py` | 别名、进程/窗口映射、排除 Subnautica 2、最小化 | 7 |
| `test_service.py` | 等待模式、命令队列、停手松键、恢复、UI 门禁、序列上限 | 9 |
| `test_smoke.py` | 清单存在 | 1 |
| 合计 |  | **61** |

全部离线：不启动游戏、不调用真实 `SendInput`、不依赖 N.E.K.O 源码（conftest 提供严格 SDK 桩）。

---

## 12. 刻意不做的事

* 不自动启动游戏 —— 用户必须自己打开；
* 不点“新游戏”/“继续”以外的菜单 —— 主菜单 `ui_*` 全拒绝；
* 不写存档/不改存档/不读存档；
* 不读游戏内存；
* 不执行控制台命令；
* 不代发成就；
* 不用固定坐标宏做制造/建造；
* 不把“HUD 相对比例”说成精确氧气/血量；
* 不在没有证据时打勾长期阶段。

---

## 13. 实测记录（本机）

| 项目 | 结果 |
|------|------|
| `neko-plugin check --release` | passed；payload hash 校验通过 |
| 插件测试 | 61 passed |
| 真实 SDK 实例化 | 成功；16 个 LLM 工具注册 |
| 真实窗口发现 | 进程快照只看到 33 个进程时，窗口兜底找到 `Subnautica`（UnityWndClass） |
| 真实截屏 | DXGI/GDI/Pillow 均 2560×1600 |
| DWM 物理边界 | 2560×1600（`GetWindowRect` 1707×1067） |
| 主菜单守卫 | 光标可见 → 不按键；`subnautica_ui` 在主菜单被拒绝 |
| 真实 `look` | 推送 JPEG + HUD 文本成功 |
| 游戏内完整游玩 | **未验证**：本机测试时游戏停在主菜单，未读档；不声称已验证 |

---

## 14. 后续可做

1. 用真实存档校准 HUD 环位置/阈值（不同 UI 缩放、4K、超宽屏）；
2. 增加保存文件**只读**解析（gameinfo.json）来获得精确深度/时间，但绝不写；
3. 为制造机增加“已知配方页”截图模板，减少 LLM 点击次数；
4. 给基地建造加一个“先放地板/房间”的本地宏，仍由 LLM 确认位置；
5. 采集真实游玩 30 分钟的日志，统计卡住/误判率。
