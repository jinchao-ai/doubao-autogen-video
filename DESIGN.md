# 豆包自动出片引擎 · 整体设计方案 v2

> 目标：把「豆包网页生成图片/视频」包装成本地 HTTP API，题材输入 → 分镜 → 参考图 → 出图/出片 → 校验，全自动流水线。图片与视频均全自动，额度检测 + 多账号轮换 + 次日重置等待。

## 0. 结论摘要

- **对外**：本地 REST API（`http://127.0.0.1:8000`），任何脚本/agent 框架可用 curl 调用。
- **对内**：Playwright 驱动真实 Chrome（persistent profile 保存登录态），复刻已验证的网页操作（CDN 挂附件、base64 注 prompt、点发送、轮询、抓图/抓视频），不逆向接口，风控面最小。
- **LLM 边界（核心原则）**：凡程序化能稳定执行的一律不走大模型。LLM 只出现在两处离线环节：①题材→分镜→prompt 创意扩写；②校验失败后的 prompt 重写。引擎运行时零 LLM。
- **全自动**：t2i / i2v 均自动发送；额度信号自动分类 → 切下一可用账号 → 全部耗尽则休眠到次日重置。

## 0.5 M2 实测结论（2026-10-19，E03_f1 洞室建立 I2V 全链路通过）

**实测样本**：E03_f1 洞室建立（首帧=E03_kf1 + 204 字 I2V prompt），发送→自动确认→生成→收割全程无人工干预，产出 h264 720×1280 10.04s，与历史交付规格一致。全流程实测耗时 4 分 50 秒。

关键发现与修正（已固化进代码）：

1. **双布局现实**：豆包前端灰度漂移，同一 profile 会随机落入「经典对话布局」或「豆包工作工作台布局」。工作台无「图像/视频生成」模式按钮、无 upload-file-input、workspace-switch-chip 禁用点不动、AB 开关翻转无效（服务端下发）。→ `detect_layout()` 轮询识别，工作台下跳过 switch_mode，由 agent 按附件+prompt 自行路由，实测路由正确。
2. **附件唯一通道**：工作台下 DataTransfer 写 input + change 事件 app 不感知（预览卡片不渲染）；**dragenter/dragover/drop 完整事件序列派发到 `file_drop_area` 才生效**（预览卡片 `[data-testid=attachment-image-card]` 出现，挂载后有校验）。经典布局仍走 input + change。
3. **headless session 被服务端作废**：headless 发送生成请求后，整个登录态被豆包主动作废（页面变登出态欢迎页）→ 必须**有头模式跑在真实显示（:0）**+ `ignore_default_args=["--enable-automation"]` 去自动化指纹。有头模式下 isTrusted 校验通过、session 存活。
4. **工作台 agent 的确认交互**：agent 先输出完整分镜方案表（B01/B02/B03），停在「正在询问用户」卡片等确认——「按要求生成」= 启动高消耗生成，「暂不生成」= 中止。→ 轮询循环内 `_auto_confirm()` 精确全等匹配点击「按要求生成/确认生成/开始生成」，绝不误触「暂不生成」。
5. **视频懒挂载**：生成完成只渲染 block-video 容器（xgplayer 不挂 `<video>`），**点击块后播放器才挂载**，`currentSrc` 直出 douyinvod.com 真实直链。→ `wait_video` 轮询中自动点块挂载；另加 `harvest_video_from_chat()` 超时恢复通道（会话服务端持久化，生成完但没抓到的可回源收割，不重复消耗额度）。
6. **CDN 签名 URL 会过期**（实测 P02 褡裢 403）→ driver.arm 支持**本地文件 base64 直传**（优先），CDN URL 仅作跨集复用通道，过期走 harvest_upload_urls 回源重采。
7. **额度实测**：该次 I2V 显示「消耗 0」（活动/新用户计费口径待 M4 长期观察），真实额度信号文案待触发时抓取原文更新 config 正则。

## 1. 为什么不用 TRAE 内置浏览器 MCP

TRAE 内置浏览器是客户端内嵌 Chromium（Blink 内核，页面兼容 Chrome），通过 MCP 协议暴露 navigate/evaluate/click 等工具——但该 MCP server 由 TRAE 客户端托管，**仅 TRAE 环境内的 agent 可调用**，外部框架无法连接。因此本引擎用 Playwright 自建等价驱动层，能力对等、可被任意进程复用。

## 2. 总体架构

```
任意客户端（脚本 / 其他 agent 框架 / 人）
        ↓ HTTP (REST)
┌─────────────────────────────────────────┐
│ FastAPI 引擎服务 (server.py)              │
│  · 任务队列 + 状态机 (registry.py)        │
│  · 账号池与额度治理 (accounts.py)         │
│  · 校验 (verifier.py)                    │
│  · 后台 worker：备料→发送→轮询→下载→校验   │
└───────────────┬─────────────────────────┘
                ↓ Playwright
┌─────────────────────────────────────────┐
│ DoubaoDriver (driver.py)                 │
│  每账号一个 persistent Chrome profile     │
│  · switch_mode(图像生成/视频生成)          │
│  · attach_refs(CDN 签名 URL 挂附件)        │
│  · inject_prompt(base64 注入编辑器)        │
│  · auto_send / poll / harvest            │
└─────────────────────────────────────────┘
```

与现有资产体系衔接：`自动出片/queue.json`（镜头队列）、`vidref/`（参考图）、`资产图/`（资产库）、`资产注册表 assets.json`（CDN URL 跨集复用）。

## 3. LLM 边界（稳定环节零大模型）

| 环节 | 实现方式 | LLM? |
|------|---------|------|
| 题材→分镜→prompt 扩写 | agent 离线产出分镜 md + 每镜 t2i/i2v prompt 文件（风格块从 config 逐字拼接，人工过审可选） | 是（离线） |
| 违禁词检查 | 词表扫描，命中即拒绝入队 | 否 |
| 附件挂载 / prompt 注入 / 发送 / 轮询 / 下载 | 纯代码（已验证的 JS 注入模式） | 否 |
| 结果校验 | 尺寸/比例/分区色板/diff | 否 |
| 校验失败重试 | 同 prompt 重试 1 次（生成有随机性） | 否 |
| 仍失败 | 标记 `needs_rewrite` → 由 agent 重写后 `POST /api/tasks/{id}/rewrite` 续跑 | 是（离线） |
| 账号轮换 / 额度检测 / 重置等待 | 纯代码 | 否 |

## 4. 账号池与额度治理（全自动核心）

**额度信号分类**（扫描页面尾部文案 + 生成按钮状态，正则匹配）：

| kind | 信号示例 | 处置 |
|------|---------|------|
| `rate_limited` | 「操作过于频繁」「请稍后再试」 | 账号冷却 10~30 分钟，任务换号重试 |
| `quota_exhausted` | 「今日额度」「上限」「已用完」 | 标记账号 exhausted，reset_at = 次日 00:05 |
| `login_expired` | 跳转登录页 / 无头像 | 标记账号，任务挂起，通知人工重登 |
| `dom_changed` | 关键选择器连续找不到 | 引擎报警（health 端点），人工更新 config 选择器 |
| `generation_timeout` | 超时无结果（图 5min / 视频 10min） | 同 prompt 重试 1 次，再失败计一次 rate_limited |

**调度策略**：
- `pick()`：在 active 且不在冷却期的账号中选当日用量最少者（均衡消耗）。
- 全部 exhausted → worker `sleep` 到最早 `reset_at`，醒来继续。
- 每账号持久化状态（`state/accounts_state.json`）：`used_today / reset_at / cooldown_until / status`。
- 单账号串行执行；节流：每镜间随机 15~40s（config 可调）。

## 5. API 设计

```
POST /api/t2i            # 文生图
  { "prompt": "...", "refs": ["S03","C01"], "aspect": "9:16" }
  → { "task_id": "...", "status": "pending" }

POST /api/i2v            # 图生视频（prompt + 首帧参考）
  { "prompt": "...", "refs": ["E03_kf2"], "duration": 10 }
  → { "task_id": "...", "status": "pending" }

POST /api/episodes       # 整集批处理：镜头列表（含每镜 prompt+refs）全量入队
POST /api/tasks/{id}/rewrite   # needs_rewrite 后回填新 prompt 续跑
GET  /api/tasks/{id}      # 状态与校验报告
GET  /api/queue          # 队列总览
GET  /api/health         # 自检：登录态/编辑器/上传口/账号池状态
```

## 6. 任务状态机

```
pending → armed(备料完) → sent → generating
       → downloading → verifying → done
                              ├→ retry(同prompt, ≤1次) → sent
                              └→ needs_rewrite(等人工/agent 回填) 
整集: all done → (可选)拼图人工过审(config.human_review)
额度异常: 任意态 → requeued + 账号标记
```

## 7. 校验框架（verifier）

- **程序化**（默认必做）：文件落盘、尺寸、9:16 比例带、分区色板统计（上/中/下 warm/dark/blue/green 占比）。
- **镜头期望**（可选，入队时带 `expect: {"max_dark_top":0.9, "min_warm_bottom":0.2}`）：按镜头语义配置阈值。
- **人工过审**：`config.human_review: none | per_episode`，默认 none（全自动），per_episode 时集末出拼图挂 `awaiting_review`。

## 8. 视频生成（I2V）全自动要点

- 经典布局：切「视频生成」模式；工作台布局：agent 按首帧附件+prompt 自动路由（M2 实测正确）。
- 首帧图本地文件 base64 直传（优先）或 CDN 签名 URL 挂附件 → 注入 I2V prompt → 自动发送。
- **工作台确认关卡**：agent 出方案后停在「正在询问用户」卡片，轮询中自动点「按要求生成」。
- 结果抓取：生成完成渲染 block-video 容器（xgplayer 懒挂载）→ 自动点块 → `<video>.currentSrc` 直出真实直链（douyinvod.com），下载存档 `成片/{E}/{f}.mp4`。
- 超时 15 分钟（含 agent 出方案 1-3 分钟）；超时后有 `harvest_video_from_chat` 回源收割通道（不重复消耗额度）。
- 视频配额消耗大，账号池 t2i 与 i2v 分开计量（`daily_quota_i2v` 独立计数）。

## 9. 风险与兜底

| 风险 | 兜底 |
|------|------|
| 豆包前端改版（DOM class 哈希变化） | 选择器集中在 config.yaml；`/api/health` 自检报警 |
| 豆包布局灰度漂移（经典/工作台） | `detect_layout()` 轮询识别，双通道附件（input+change / drop 事件序列），`switch_mode` 仅经典布局生效 |
| headless session 被作废 | 有头模式跑 :0 显示 + 去 enable-automation 指纹（M2 实测 headless 发送后必作废） |
| 工作台确认卡片未点 | 轮询内 `_auto_confirm()` 精确匹配；超时 GenerationTimeout 重试不消耗额度 |
| 视频生成完但没抓到 | `harvest_video_from_chat(chat_url)` 回源收割（会话服务端持久化） |
| CDN 参考图 URL 过期 | 本地文件 base64 直传优先；资产注册表记录 `source_chat`，失效自动回该会话重新 harvest |
| 风控升级 | 节流随机延迟、单账号串行、多 tab ≤2；rate_limited 自动降速 |
| Playwright 被检测 | persistent profile + `disable-blink-features=AutomationControlled`；极端情况退回 headed 模式人工观察 |

## 10. 目录结构与运行

```
自动出片引擎/
├── DESIGN.md          # 本文档
├── config.yaml        # 账号/风格块/违禁词/选择器/节流
├── server.py          # FastAPI + 后台 worker
├── driver.py          # Playwright 豆包驱动
├── accounts.py        # 账号池与额度治理
├── registry.py        # 任务状态机 + 资产注册表
├── verifier.py        # 程序化校验
├── requirements.txt
├── state/             # accounts_state.json / tasks.json
├── profiles/           # 每账号 Chrome persistent profile（gitignore）
└── downloads/          # 生成结果落盘
```

```bash
pip install -r requirements.txt
playwright install chromium
python -m server        # 或 uvicorn server:app --port 8000
# 首次：config.accounts[0].headless: false 打开浏览器手动登录一次，profile 落盘
```

## 11. 里程碑

- **M1 框架骨架**（2026-10-19 完成）：代码结构、状态机、账号池、注入逻辑移植。
- **M2 单镜实测**（2026-10-19 完成）：E03_f1 I2V 全链路跑通（备料→自动发送→自动确认→轮询挂载→下载→校验）；实测发现双布局/附件通道/headless 作废/确认卡片/懒挂载五大问题并修复固化（见 §0.5）。kf5 图像已在此前由 TRAE 内置浏览器完成（E03 五镜 t2i 全齐）。
- **M3 整集实测**（2026-10-19 API 层闭环验证，生成待额度恢复）：f2~f5 经 HTTP API 入队 → worker 执行 → **真实额度信号实战触发**：「近 7 天免费额度已用完，预计 10 月 26 日 18:51 恢复」→ 正确分类 quota_exhausted → 任务回队、账号标 exhausted、恢复时间从信号原文解析入库——额度治理闭环（原 M4 目标）提前实战验证。同时发现并修复任务乱序 bug（next_pending 改按 created_at 排序）。**f2~f5 留在队列，10 月 26 日 18:51 额度恢复后重启 server 自动续跑。**
- **M4 多账号**：第二账号登录、额度触发切换实测（信号分类与处置已实战验证，待有第二账号时验证轮换）。

### 真实额度模型修正（M3 实测）

豆包免费额度是 **7 天滚动窗口**（非每日重置）。耗尽信号原文：「近 7 天免费额度已用完，预计 10 月 26 日 18:51 恢复。开通订阅畅享更多额度。」恢复时间随信号原文动态给出 → `accounts._parse_recovery_time()` 从原文提取真实恢复时刻，解析不出才退回次日 daily_reset_hour。本地保守日上限（config daily_quota_*）仍保留，作为第一道闸。
