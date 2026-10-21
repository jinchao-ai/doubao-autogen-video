# doubao-autogen-video

豆包（Doubao）网页出图/出片的本地全自动引擎。通过 Playwright 驱动真实 Chrome 浏览器，把「挂参考图 → 注入 prompt → 发送 → 确认 → 轮询 → 下载 → 校验 → 命名落盘」整条链路包装成本地 HTTP API，支持文生图（T2I）与图生视频（I2V）。

**运行时零 LLM**：所有程序化环节（备料/发送/轮询/下载/校验/账号轮换）均为纯代码执行；LLM 仅用于离线的 prompt 扩写与校验失败重写。

## 架构

```
FastAPI (server.py)          ← HTTP API：t2i / i2v / episodes / tasks / queue / health
    ↓ 后台 worker（串行调度 + 镜间节流）
DoubaoDriver (driver.py)      ← Playwright 有头驱动真实豆包网页
    ↓
豆包 Web（图像生成 / 视频生成智能体）

支撑层：
accounts.py    账号池 + 额度信号分类 + 7天滚动窗口恢复时间解析
registry.py    资产注册表（参考图 CDN URL 跨集复用）+ 任务状态机
verifier.py    校验（图片 9:16 比例带 + 三分区色板；视频规格）
config.yaml    选择器/违禁词/额度信号正则集中管理，前端改版只改配置
```

## 快速开始

```bash
pip install -r requirements.txt
playwright install chromium
cp config.example.yaml config.yaml

# 首次登录（弹出真实浏览器，扫码后自动落盘登录态到 profiles/）
python3 login_once.py

# 启动引擎（有头模式跑在桌面显示器上，headless 会被豆包服务端作废 session）
DISPLAY=:0 python3 -m uvicorn server:app --host 127.0.0.1 --port 8000
```

### 提交任务

```bash
# 图生视频（refs 传首帧关键帧本地路径，引擎自动 base64 直传）
curl -s -X POST http://127.0.0.1:8000/api/i2v \
  -H "Content-Type: application/json" \
  -d '{"prompt": "…运镜指令…", "refs": ["/abs/frame.jpg"], "output": "ep01_f1"}'

# 文生图（refs 可传场景图/角色图多张）
curl -s -X POST http://127.0.0.1:8000/api/t2i \
  -H "Content-Type: application/json" \
  -d '{"prompt": "…", "refs": ["/abs/scene.jpg"], "output": "ep01_kf1"}'

# 查进度 / 查额度
curl -s http://127.0.0.1:8000/api/tasks/{task_id}
curl -s http://127.0.0.1:8000/api/health
```

完成后产物自动落盘 `downloads/{output}.mp4|.jpg` 并附校验报告。任务状态机：`pending → armed → sent → generating → downloading → verifying → done`（失败自动重试，重试耗尽转 `needs_rewrite`，可改写 prompt 后 `POST /api/tasks/{id}/rewrite` 续跑）。

### 额度治理（实战验证）

- 豆包免费额度为 **7 天滚动窗口**（非每日重置），耗尽信号原文示例：「近 7 天免费额度已用完，预计 10 月 26 日 18:51 恢复」
- 引擎扫描到额度信号后自动分类：`rate_limited`（冷却后重试）/ `quota_exhausted`（从信号原文解析精确恢复时刻，任务安全留队，恢复后自动续跑）/ `login_expired`（提示重登）
- 本地另有保守日上限双保险（`config.yaml` 的 `daily_quota_t2i/i2v`），宁可少跑不烧光额度

## 实测踩坑记录（详见 DESIGN.md）

本项目所有结论均来自真实生产链路（敦煌重彩五集短剧）实测：

| 坑 | 结论 |
|---|---|
| headless session 作废 | 豆包服务端会作废 headless 会话，必须有头模式跑真实桌面 |
| 发送按钮 isTrusted 校验 | 纯脚本点击无效，Playwright 可信事件可过 |
| 工作台双布局 | 新账号默认「豆包工作」布局，附件必须走 dragenter/dragover/drop 事件序列 |
| 生成确认卡片 | 视频智能体先出方案等确认，需自动点「按要求生成」 |
| 视频懒挂载 | 生成完无 `<video>`，需点击块挂载 xgplayer 后从 `currentSrc` 取直链 |
| 内容审核关键词 | 「夜叉」「跪」等词在否定句中也会被拦截，引擎内置违禁词门禁入队前拦截 |

## ⚠️ 免责声明

- 本项目**仅供个人学习与技术研究**，不用于任何商业用途。
- 本项目通过浏览器自动化操作 [豆包](https://www.doubao.com/) 网页，属于非官方接入，**可能违反豆包服务条款**，由此产生的账号风控（限流、封禁等）由使用者自行承担。
- 请勿用于批量爬取、倒卖生成结果或任何违反平台规则与法律法规的用途。使用的账号与额度归你自己所有，请合理使用。
- 豆包前端随时可能改版导致选择器失效（改版后更新 `config.yaml` 选择器即可），本项目不承诺持续可用。
- 本项目不收集任何数据，登录态仅保存在本地 `profiles/`（已被 `.gitignore` 排除，请勿提交或分发）。

## License

MIT
