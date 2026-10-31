# 深海迷航 & 零度之下 陪玩 —— N.E.K.O 插件包

一个可被 **N.E.K.O → 插件管理 → 导入** 正常导入的 Python 插件。

装好以后，**你自己打开游戏并读档进入水下**，对猫娘说：

> 「去玩深海迷航」
>
> 「玩零度之下」
>
> 「你来探索，先做扫描仪」

她会先判断窗口、前台、菜单和 HUD，主动说明「我看到了什么 / 我判断什么 / 下一步做什么」，然后开始截屏、用硬件扫描码操作键鼠、按生存反射和长期计划推进。你随时可以：

- 直接碰 WASD、鼠标、E、Tab：她在下一次检查（约 0.1–0.4 秒）松手；
- 按 **F9**：硬暂停/继续，松开所有按键；
- 说「停手」「我来」：立刻停手；
- 说「继续」「交给你」：重新接管；
- 说「往前」「上浮」「扫描」「按E」：和她一起操作。

> **重要边界（不骗人）**：这是一个「截屏视觉 + 本地生存反射 + 对话 LLM 规划 + UI 点击」的陪玩插件，
> 不是读内存/读存档的外挂，也不是保证 100% 通关的速通脚本。她能真正接管操作、持续探索、主动解说，
> 并在看清 PDA/制造机/建造界面后用鼠标帮你点选；但复杂基地布局、后期剧情解谜、
> 以及「到底该不该现在下潜」仍需要你或 LLM 一起判断。详见文末「已知限制」。

---

## 交付物

| 文件 | 说明 |
|------|------|
| **`dist/subnautica_player.neko-plugin`** | **要导入的插件包**（官方 `neko-plugin build` 产出，v1.0.0） |
| [`N.E.K.O/plugin/plugins/subnautica_player/`](.) | 插件源码与独立 Git 仓库 |
| [`docs/quickstart.md`](docs/quickstart.md) | 插件内使用指南 |
| [`DESIGN.md`](DESIGN.md) | 架构、判断依据、安全边界、测试矩阵 |
| [`PUBLISHING.md`](PUBLISHING.md) | 仓库/市场发布手册（当前只上传仓库，**未发布市场**） |

---

## 支持的游戏

| 游戏 | Steam AppID | 可执行文件 | 说明 |
|------|-------------|------------|------|
| 深海迷航（Subnautica） | 264710 | `Subnautica.exe` | 支持 |
| 深海迷航：零度之下（Below Zero） | 848450 | `SubnauticaZero.exe` | 支持；额外提示低温 |
| Subnautica 2 | 1962700 | `Subnautica2.exe` | **明确不支持**，不会假装能玩 |

---

## 安装

1. 打开 **N.E.K.O → 插件管理**
2. 点 **导入**，选择 `dist/subnautica_player.neko-plugin`
3. 启用「深海迷航 & 零度之下 陪玩」
4. （建议）先做一次 **深海迷航环境自检**
5. 手动打开游戏，**读档进入水下**
6. 对猫娘说「去玩深海迷航」或「玩零度之下」

> 插件默认 `auto_start = false` + `autopilot_enabled = false`：
> **仅仅启用插件不会抢键盘、也不会自动开游戏**。必须显式 `subnautica_play` 才会接管。

---

## 她怎么“自己玩”

每个控制 tick（默认约 3 次/秒）：

```
截屏（DXGI 独占全屏 → GDI → Pillow）
  → 画面特征（亮度/运动/水下/暗色 UI/受伤红边/文字带）
  → HUD 四环（血/食/水/氧）颜色+形状检测
  → 守卫：黑屏 / 菜单 / 加载 / 失焦 / 玩家接手 / F9 / 会话超时
  → 生存反射：低氧上浮、受伤后撤、饥饿找食物
  → 本地策略：探索 / 采集 / 扫描 / 卡住阶梯
  → 按键宏（100ms 切片，随时可中断）
```

同时，后台叙事循环会：

- 每 10 秒把一张 JPEG 画面推进 LLM 视觉上下文（`ai_behavior="read"`）；
- 每 30 秒推一条结构化「HUD + 画面 + 判断 + 计划」；猫娘主动开口；
- 低氧/受伤/卡住/进入 PDA 时额外提示（有冷却）。

猫娘看到画面后可以调用这些工具：

| 工具 | 用途 |
|------|------|
| `subnautica_play` | 开始/切换游戏（必须已经手动打开并读档） |
| `subnautica_status` | 查窗口、前台、HUD、策略、计划、错误 |
| `subnautica_look` | 立刻截图并送进视觉上下文 |
| `subnautica_command` | 立刻做一个动作：「上浮」「扫描」「按E」 |
| `subnautica_plan` | 设这一阶段的目标与模式（探索/采集/制造/建造/主线…） |
| `subnautica_ui` | 在 PDA/制造机/建造界面点击、按键、滚动 |
| `subnautica_sequence` | 一次执行最多 12 步短序列 |
| `subnautica_aim` | 把视角转向画面中的归一化坐标 |
| `subnautica_milestone` | 看到证据后标记阶段完成/跳过 |
| `subnautica_note` / `subnautica_biome` | 记录长期日志与生态区 |
| `subnautica_stop` / `subnautica_resume` | 停手/继续 |
| `subnautica_selfcheck` | 只读环境自检 |
| `subnautica_games` | 列出支持的游戏与安装/运行状态 |

### 长期计划

两套游戏各有一条 11 个阶段（Below Zero 为 8 个）的早期路线：灭火修复 → 扫描仪 → Seaglide → 第一个基地 →
Sunbeam/Aurora → 海蛾号/海卡车 → Degasi/大礁/Al-An → Lost River/水晶洞穴 → 熔岩区/制造者洞穴 → 火箭/结局。

计划**不会自动打勾**。猫娘必须在画面里看到明确证据（新配方、载具、基地、剧情事件），
才调用 `subnautica_milestone` 记录；否则她会继续当前阶段并写日志。这样不会出现“假装通关”。

### 制造 / 建造

本地反射层**不会**在没有看清界面前盲点配方。流程是：

1. 猫娘用 `subnautica_look` 看清制造机/建造界面；
2. 用 `subnautica_ui` 按截图坐标点击分类/配方/放置；
3. 插件每次点击后重新截图，送回视觉上下文；
4. 点错就用 `look` 修正，或直接说「停手」自己接管。

这比“固定坐标宏”慢，但不会因为分辨率/解锁顺序不同而把材料点错。

---

## 你能对她说什么

| 你说 | 她的行为 |
|------|----------|
| 去玩深海迷航 / 玩零度之下 | 选游戏并接管（没开就等窗口） |
| 往前走 / 后退 / 左移 / 右移 | 按住对应方向 |
| 上浮 / 下潜 | 空格 / C |
| 扫描它 / 按E / 打开PDA | 对应按键；扫描会按住左键约 2 秒 |
| 停手 / 别玩了 / 我来 | 松开所有键，硬停 |
| 继续 / 你接着玩 / 交给你 | 重新接管 |
| 现在什么情况 / 计划到哪了 | 状态 / 长期计划 |
| 先做扫描仪 / 找海蛾号碎片 | `subnautica_plan` 改阶段目标 |
| 你自己看着办 | 保留当前计划，继续探索 |

---

## 安全设计

| 风险 | 处理 |
|------|------|
| 打字到别的窗口 | 只有游戏窗口在前台才注入；`require_foreground=true` |
| 游戏最小化 / 1×1 窗口 | 明确拒绝，不按键；提示切回前台 |
| 全黑画面 | 连续 8 帧全黑 → 停止按键，报告真实截图错误 |
| 主菜单里乱点 | 没进入过水下时只等待；`subnautica_ui`/`sequence` 也被拒绝 |
| 玩家突然接手 | 轮询 WASD/E/Tab/鼠标键（排除自己按住的键）→ 约 0.1–0.4 秒松手 |
| 硬暂停 | **F9**（Subnautica 的 F8 是反馈菜单、F11 是截图，不能占用） |
| 会话失控 | `max_session_minutes=90` 硬停 |
| 作弊/改存档 | 不读内存、不写存档、不执行控制台命令、不代发成就 |
| 独占全屏截不到 | 自带纯 ctypes DXGI Desktop Duplication，不依赖 cv2/dxcam |
| DPI 缩放裁错画面 | 用 DWM 物理边界裁窗口；全屏不裁 |

---

## 配置（`plugin.toml` 的 `[subnautica]`）

| 键 | 默认 | 含义 |
|----|------|------|
| `capture_backend` | `auto` | `auto/dxgi/gdi/pil`；独占全屏需要 DXGI |
| `target_fps` | `3.0` | 截屏/决策频率 |
| `frame_max_edge_px` | `1152` | 送模型前缩放的最长边 |
| `stream_frames_to_llm` | `true` | 关闭后猫娘看不到画面 |
| `commentary_interval_seconds` | `30` | 主动解说间隔 |
| `dry_run` | `false` | 只观察、不按键；第一次建议开 |
| `max_session_minutes` | `90` | 单次最长游玩 |
| `oxygen_safe_seconds` | `28` | HUD 读不到氧时的上浮兜底 |
| `look_units_per_degree` | `13` | 鼠标灵敏度换算；转多了调大 |
| `yield_on_human_input` | `true` | 玩家碰键就松手 |
| `human_input_grace_seconds` | `12` | 玩家停手后自动恢复等待 |
| `pause_hotkey_enabled` | `true` | F9 暂停/继续 |
| `auto_resume_from_menu` | `false` | 默认不替主人按 Esc 关菜单 |
| `start_in_game_required` | `true` | 没看到游戏画面绝不按键 |
| `keys` | 空 | 每个动作的键位覆盖，例如 `forward="w"` |

配置面板见 [`config.schema.json`](config.schema.json)；示例见 [`config.example.toml`](config.example.toml)。

---

## 已知限制

- **不是读内存/读存档**：她只能看到屏幕，不能直接知道精确坐标、深度、背包格数或蓝图列表；
  氧气/血量数字不会 OCR，HUD 只给“充足/偏低”的相对估计。
- **不保证通关**：复杂基地、后期剧情解谜、利维坦规避仍需要你或 LLM 判断。
- **制造/建造依赖截图点击**：如果界面字太小、UI 缩放太大或被桌面宠物遮挡，她可能看不清；
  这时请说「停手」并自己点，或把游戏切无边框/提高 UI 缩放。
- **独占全屏依赖 DXGI**：如果机器上 Desktop Duplication 不可用，请把游戏改成无边框窗口。
- **玩家按住与猫娘相同的键**时，`GetAsyncKeyState` 无法区分“人”和“注入”，这是 Win32 的固有限制；
  她会在其他游戏键/鼠标键上让位。
- **会话时长**按实际游玩秒数计算，不按插件开启时间。

---

## 验证结果（本机）

```
官方 CLI check --release              passed
插件契约/策略/HUD/服务测试            64 passed
打包 payload hash 校验                passed
真实宿主 SDK 实例化 + 16 个工具注册   passed
真实窗口发现（进程快照受限时走窗口）  passed
真实截屏 2560×1600（DXGI/GDI/Pillow） passed
真实 HUD 截图（菜单）                 能识别圆环；菜单被守卫拒绝按键
DWM 物理窗口边界                      2560×1600（GetWindowRect 只有 1707×1067）
INPUT 结构体                          40 字节，ABI 正确
```

> 说明：本机 Subnautica 当时停在主菜单，未读档，所以“进入水下后的完整游玩”没有被声称验证过。
> 主菜单守卫、截图、HUD、窗口发现、宿主导入和工具注册均已实测。真实存档游玩请在主人读档后验证。

---

## 目录

```text
subnautica_player/
├── plugin.toml              # 清单与默认配置
├── config.schema.json       # 插件中心配置表单
├── __init__.py              # SDK facade：生命周期 + 16 个 LLM 工具 + 插件中心入口
├── catalog.py               # 两部游戏、键位、路线、生态区知识
├── discovery.py             # Steam/进程/窗口/前台/菜单/DWM 物理边界
├── capture.py               # DXGI → GDI → Pillow
├── dxgi_capture.py          # 纯 ctypes Desktop Duplication
├── input.py                 # SendInput 扫描码 + 鼠标 + 物理键轮询 + F9
├── vision.py                # 纯 numpy 画面特征
├── hud.py                   # 四环 HUD 检测（不 OCR）
├── policy.py                # 指令解析 + 生存反射 + 探索/卡住阶梯
├── planner.py               # 长期路线 + 持久日志（plan.json）
├── prompts.py               # 本地化 cue + 主动解说 prompt
├── service.py               # 捕获/输入/循环/接管/叙事/工具编排
├── docs/quickstart.md       # 插件内指南
├── tests/                   # 64 项离线测试
├── DESIGN.md
└── PUBLISHING.md
```

---

## 开发

从插件仓库根目录：

```bash
uvx ruff==0.12.4 check --ignore-noqa --config ruff.toml .
```

从 N.E.K.O 仓库根目录：

```bash
uv run neko-plugin check subnautica_player
uv run neko-plugin check -r subnautica_player
```

---

## 市场状态

- 源码仓库：准备上传到 `https://github.com/mintpixiu/n.e.k.o_plugin_subnautica_player`
- 市场：**尚未发布**。没有调用 `neko-plugin publish`，没有创建 Release，也没有提交市场审核。
- 首次市场发布前还需要：作者账号登录网页、提交仓库、首次审核通过、打 `v1.0.0` tag 触发 Release。
