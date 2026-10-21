"""server.py — FastAPI 引擎服务 + 后台 worker。

运行：
  python -m server        # 或 uvicorn server:app --port 8000
首次登录：config 中对应账号 headless 改 false，启动后手动登录一次，改回 true。

核心原则（DESIGN.md §3）：运行时零 LLM。
备料→发送→轮询→下载→校验→重试全代码化；prompt 由外部（agent/人）通过 API 提供。
"""
from __future__ import annotations

import asyncio
import random
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from accounts import AccountPool
from driver import DoubaoDriver, GenerationTimeout, QuotaSignal
from registry import AssetRegistry, TaskStore
from verifier import verify_image, verify_video

CFG = yaml.safe_load(Path(__file__).with_name("config.yaml").read_text(encoding="utf-8"))
STATE_DIR = Path(CFG["paths"]["state_dir"])
DL_DIR = Path(CFG["paths"]["download_dir"])
DL_DIR.mkdir(parents=True, exist_ok=True)

pool = AccountPool(CFG, str(STATE_DIR))
store = TaskStore(str(STATE_DIR))
assets = AssetRegistry(CFG["paths"]["assets_registry"])
drivers: dict[str, DoubaoDriver] = {}
worker_task = None


# ---------- LLM-free 门禁：违禁词扫描 ----------

def banned_word_check(prompt: str):
    hits = [w for w in CFG["banned_words"] if w in prompt]
    if hits:
        raise HTTPException(400, f"banned words hit: {hits} — 请改写 prompt 后重新入队")


# ---------- driver 缓存（每账号一个，串行复用） ----------

async def get_driver(account: dict) -> DoubaoDriver:
    name = account["name"]
    if name not in drivers:
        drivers[name] = DoubaoDriver(account, CFG)
        await drivers[name].start()
    return drivers[name]


# ---------- 单任务执行：备料→发送→轮询→下载→校验 ----------

async def run_task(t: dict, account: dict):
    drv = await get_driver(account)
    ttype = t["type"]  # t2i | i2v
    mode = "image" if ttype == "t2i" else "video"

    await drv.open_new_chat()
    await drv.switch_mode(mode)
    # refs 解析：存在的本地文件路径 → base64 直传（不受 CDN 签名过期影响）；
    # 否则视为资产名走注册表 → CDN URL
    ref_pairs, local_paths = [], []
    for r in t["refs"]:
        p = Path(r)
        if p.is_file():
            local_paths.append(str(p))
        else:
            ref_pairs.extend(assets.refs([r]))
    armed = await drv.arm(t["prompt"], ref_pairs or None, local_paths or None)
    store.set(t["id"], status="armed", **{"history": t["history"] + [f"armed:{armed}"]})

    await drv.auto_send()
    store.set(t["id"], status="sent")
    # 额度在「发送成功」即计消耗——M2 实测：GenerationTimeout 时生成往往已完成
    # （超时后回源收割到了视频），按成功才计数会导致超时重试双倍消耗额度
    pool.mark_used(account["name"], ttype)

    th = CFG["throttle"]
    timeout = th["image_timeout_sec"] if ttype == "t2i" else th["video_timeout_sec"]
    try:
        if ttype == "t2i":
            urls = await drv.wait_image(timeout, th["poll_interval_sec"])
            result_url = urls[0]
        else:
            result_url = await drv.wait_video(timeout, th["poll_interval_sec"])
    except GenerationTimeout:
        # 保额度：超时≠失败，M2 实测生成多半已完成。原地/回源补抓（零成本），
        # 抓到就免重试，避免同 prompt 重发二次消耗额度。
        if ttype == "i2v":
            try:
                result_url = await drv._poll_video_inplace(60)
                print(f"[run_task] 超时后原地补抓成功: {result_url[:80]}")
            except GenerationTimeout:
                chat_url = drv.page.url
                try:
                    result_url = await drv.harvest_video_from_chat(chat_url)
                    print(f"[run_task] 超时后回源补抓成功: {result_url[:80]}")
                except GenerationTimeout:
                    raise
        else:
            raise

    store.set(t["id"], status="downloading")
    ext = ".jpg" if ttype == "t2i" else ".mp4"
    out_name = t.get("output") or f"{t['id']}_{datetime.now().strftime('%H%M%S')}"
    if not out_name.endswith(ext):
        out_name += ext
    out = DL_DIR / out_name
    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as cx:
        r = await cx.get(result_url)
        r.raise_for_status()
        out.write_bytes(r.content)

    store.set(t["id"], status="verifying")
    if ttype == "t2i":
        v = verify_image(str(out), t.get("expect"))
    else:
        v = verify_video(str(out))
    store.set(t["id"], verify=v, result=str(out),
              status="done" if v["pass"] else "retry",
              error="; ".join(v["reasons"]) if not v["pass"] else None)


async def worker():
    """主循环：取任务 → 选账号 → 执行 → 异常分类处理。"""
    while True:
        try:
            t = store.next_pending()
            if not t:
                await asyncio.sleep(2)
                continue
            if t["status"] == "retry" and t["attempts"] >= 2:
                store.set(t["id"], status="needs_rewrite",
                          error=(t.get("error") or "") + " | 自动重试已用尽，等待 prompt 重写")
                continue

            ttype = "t2i" if t["type"] == "t2i" else "i2v"
            account_name = pool.pick(ttype)
            if account_name is None:
                wait = pool.seconds_until_next_available()
                print(f"[worker] 无可用账号，休眠 {wait/60:.1f} 分钟等待额度重置/冷却")
                await asyncio.sleep(max(wait, 30))
                continue
            account = _find_account(account_name)

            store.set(t["id"], status="generating", attempts=t["attempts"] + 1)
            try:
                await run_task(t, account)
            except QuotaSignal as q:
                pool.mark(account_name, q.kind, q.detail)  # 传原文，解析真实恢复时间
                store.set(t["id"], status="pending",
                          error=f"{q.kind}: {q.detail}",
                          **{"history": t["history"] + [f"quota:{q.kind}"]})
                print(f"[worker] {account_name} → {q.kind}，任务 {t['id']} 换号重排")
            except GenerationTimeout as e:
                store.set(t["id"], status="retry", error=str(e))
            except Exception as e:
                store.set(t["id"], status="retry", error=f"{type(e).__name__}: {e}")

            await asyncio.sleep(random.uniform(
                CFG["throttle"]["min_delay_sec"], CFG["throttle"]["max_delay_sec"]))
        except Exception as e:
            print(f"[worker] loop error: {e}")
            await asyncio.sleep(5)


def _find_account(name: str) -> dict:
    for a in CFG["accounts"]:
        if a["name"] == name:
            return a
    raise KeyError(name)


# ---------- FastAPI ----------

class T2IReq(BaseModel):
    prompt: str
    refs: list[str] = []   # 本地文件路径（优先）或资产名
    expect: dict | None = None
    output: str | None = None   # 命名输出，如 "E03_kf6_洞口远眺"


class I2VReq(BaseModel):
    prompt: str
    refs: list[str] = []   # 首帧图等：本地文件路径（优先）或资产名
    expect: dict | None = None
    output: str | None = None


class EpisodeReq(BaseModel):
    shots: list[dict]      # [{type, prompt, refs, expect}, ...]


class RewriteReq(BaseModel):
    prompt: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    global worker_task
    worker_task = asyncio.create_task(worker())
    yield
    worker_task.cancel()
    for d in drivers.values():
        await d.stop()


app = FastAPI(title="夜叉国自动出片引擎", lifespan=lifespan)


@app.post("/api/t2i")
async def api_t2i(req: T2IReq):
    banned_word_check(req.prompt)
    tid = store.create("t2i", req.prompt, req.refs, req.expect, req.output)
    return {"task_id": tid, "status": "pending"}


@app.post("/api/i2v")
async def api_i2v(req: I2VReq):
    banned_word_check(req.prompt)
    tid = store.create("i2v", req.prompt, req.refs, req.expect, req.output)
    return {"task_id": tid, "status": "pending"}


@app.post("/api/episodes")
async def api_episode(req: EpisodeReq):
    ids = []
    for shot in req.shots:
        banned_word_check(shot["prompt"])
        ids.append(store.create(shot.get("type", "t2i"), shot["prompt"],
                                shot.get("refs", []), shot.get("expect"),
                                shot.get("output")))
    return {"task_ids": ids, "queued": len(ids)}


@app.post("/api/tasks/{tid}/rewrite")
async def api_rewrite(tid: str, req: RewriteReq):
    t = store.get(tid)
    if not t:
        raise HTTPException(404, "task not found")
    banned_word_check(req.prompt)
    store.set(tid, prompt=req.prompt, status="rewrite", error=None, attempts=0)
    return {"task_id": tid, "status": "rewrite"}


@app.get("/api/tasks/{tid}")
async def api_task(tid: str):
    t = store.get(tid)
    if not t:
        raise HTTPException(404, "task not found")
    return t


@app.get("/api/queue")
async def api_queue():
    return {"pending": store.pending_count(), "tasks": {
        k: {"status": v["status"], "type": v["type"], "error": v["error"]}
        for k, v in store.tasks.items()}}


@app.get("/api/health")
async def api_health():
    out = {"accounts": pool.snapshot(), "drivers": {}}
    for name, d in drivers.items():
        try:
            out["drivers"][name] = await d.health()
        except Exception as e:
            out["drivers"][name] = {"healthy": False, "error": str(e)}
    return out


@app.post("/api/accounts/{name}/relogin_done")
async def api_relogin(name: str):
    """人工重登完成后恢复账号。"""
    pool.state.setdefault(name, {"status": "active"})["status"] = "active"
    pool._save()
    return {"account": name, "status": "active"}
