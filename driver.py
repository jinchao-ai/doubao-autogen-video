"""driver.py — Playwright 驱动豆包网页（单账号一个实例）。

注入/发送/轮询逻辑全部移植自已验证的页内 JS 模式：
- 附件：页内 fetch CDN 签名 URL → DataTransfer → 完整 drop 事件序列派发到
  file_drop_area（2026-10-19 实测：工作台布局 input[type=file] 不存在，
  DataTransfer 写 input + change 事件 app 不感知；drop 事件是唯一可行通道）
- prompt：base64 解码 → editor.commands.setContent（防逐字错乱）
- 视频：xgplayer 容器 video.currentSrc 抓 MSE 流 URL
- 布局：豆包有两套前端（经典对话布局 / 豆包工作工作台布局），
  detect_layout() 自动识别，switch_mode 仅在经典布局生效
"""
from __future__ import annotations

import asyncio
import base64
import random
import re
from pathlib import Path
from typing import Optional

from playwright.async_api import Browser, BrowserContext, Page, async_playwright


class QuotaSignal(Exception):
    """额度/风控/登录态异常，kind 见 config.quota_signals + dom_changed。"""

    def __init__(self, kind: str, detail: str = ""):
        self.kind = kind
        self.detail = detail
        super().__init__(f"[{kind}] {detail}")


class GenerationTimeout(Exception):
    pass


# ---------- 页内 JS（已验证模式，args 传入避免字符串拼接错乱） ----------

ATTACH_JS = """async (arg) => {
  const dt = new DataTransfer();
  if (arg.files) {           // 本地文件 base64 直传（推荐：不受 CDN 签名过期影响）
    for (const f of arg.files) {
      const bin = atob(f.b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      dt.items.add(new File([bytes], f.name, {type: f.mime || 'image/jpeg'}));
    }
  } else {                   // 页内 fetch CDN 签名 URL（跨集复用/多账号场景）
    for (const pair of arg.urls) {
      const r = await fetch(pair[1]);
      if (!r.ok) throw new Error('fetch fail ' + r.status + ' ' + pair[1].slice(0, 60));
      const blob = await r.blob();
      dt.items.add(new File([blob], pair[0], {type: 'image/jpeg'}));
    }
  }
  // 双通道：经典布局有 upload-file-input（change 事件，2026-10 前已验证模式）；
  // 工作台布局无 input，唯一通道 = drop 事件序列到 file_drop_area（M2 实测）。
  const inputs = document.querySelectorAll('input[type=file]');
  const input = document.querySelector(arg.sel_upload) || inputs[inputs.length - 1];
  if (input) {
    input.files = dt.files;
    input.dispatchEvent(new Event('change', {bubbles: true}));
    return 'input:' + dt.files.length;
  }
  const target = document.querySelector(arg.sel_drop) ||
                 document.querySelector('[data-testid=chat_input_input]');
  if (!target) throw new Error('no attach channel (no file input, no drop area)');
  const opts = {bubbles: true, composed: true, cancelable: true, dataTransfer: dt};
  target.dispatchEvent(new DragEvent('dragenter', opts));
  target.dispatchEvent(new DragEvent('dragover', opts));
  target.dispatchEvent(new DragEvent('drop', opts));
  return 'drop:' + dt.files.length;
}"""

ATTACH_CHECK_JS = """() =>
  document.querySelectorAll('[data-testid=attachment-image-card]').length +
  document.querySelectorAll('[class*=image-wrapper]').length"""

LAYOUT_JS = """() => ({
  workspace: !!document.querySelector('[data-testid=file_drop_area]'),
  classic_upload: document.querySelectorAll('input[type=file]').length > 0,
  mode_image: Array.from(document.querySelectorAll('button'))
      .some(b => (b.textContent || '').includes('图像生成')),
})"""

INJECT_JS = """(arg) => {
  const bytes = Uint8Array.from(atob(arg.b64), c => c.charCodeAt(0));
  const t = new TextDecoder('utf-8').decode(bytes).trimEnd();
  const ed = document.querySelector(arg.sel_editor);
  if (!ed || !ed.editor) throw new Error('editor not ready');
  ed.editor.commands.setContent('<p>' + t + '</p>');
  return ed.textContent.length;
}"""

POLL_IMAGE_JS = """() => {
  const arr = []; const seen = {};
  document.querySelectorAll('img').forEach(i => {
    const s = i.src || '';
    if (s.indexOf('rc_gen_image') >= 0 && s.indexOf('cthumb') < 0 && !seen[s]) {
      seen[s] = 1; arr.push(s);
    }
  });
  return arr;
}"""

POLL_VIDEO_JS = """() => {
  for (const v of document.querySelectorAll('video')) {
    const s = v.currentSrc || v.src || '';
    if (s.startsWith('http')) return s;   // xgplayer currentSrc 直出真实地址
  }
  return null;
}"""

# 工作台 agent 出方案后停在「正在询问用户」卡片等确认：
# 点「按要求生成」= 启动高消耗生成；「暂不生成」绝不点击（精确全等匹配防误触）
CONFIRM_JS = """() => {
  const affirmatives = ['按要求生成', '确认生成', '开始生成'];
  for (const el of document.querySelectorAll('button, [role=button], span, div')) {
    if (el.children.length !== 0) continue;
    const t = (el.textContent || '').trim();
    if (affirmatives.includes(t)) { el.click(); return t; }
  }
  return null;
}"""

# 工作台布局：生成完成只渲染 block-video 容器（xgplayer 懒挂载），
# 点击后 <video> 才出现，currentSrc 即真实地址（M2 实测 douyinvod.com 直链）
VIDEO_MOUNT_JS = """() => {
  if (document.querySelector('video')) return 'present';
  const blk = document.querySelector('[class*=block-video], [class*=video-player-wrapper]');
  if (!blk) return null;
  blk.click();
  return 'clicked';
}"""

BODY_TAIL_JS = """() => document.body.innerText.slice(-800)"""


class DoubaoDriver:
    """一个账号 = 一个 persistent Chrome profile。串行使用，不做并发。"""

    def __init__(self, account_cfg: dict, engine_cfg: dict):
        self.name = account_cfg["name"]
        self.user_data_dir = account_cfg["user_data_dir"]
        self.headless = account_cfg.get("headless", True)
        self.cfg = engine_cfg
        self.sel = engine_cfg["selectors"]
        self._pw = None
        self.ctx: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    # ---------- 生命周期 ----------

    async def start(self):
        if self.ctx:
            return
        self._pw = await async_playwright().start()
        self.ctx = await self._pw.chromium.launch_persistent_context(
            self.user_data_dir,
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-first-run", "--no-default-browser-check"],
            ignore_default_args=["--enable-automation"],  # 去掉自动化指纹（M2 实测：
            # 无头模式发送生成请求后 session 被服务端作废，改为有头真窗口运行）
            viewport={"width": 1200, "height": 1000},
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()

    async def stop(self):
        if self.ctx:
            await self.ctx.close()
        if self._pw:
            await self._pw.stop()
        self.ctx = self.page = None

    # ---------- 基础操作 ----------

    async def open_new_chat(self):
        await self.page.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded")
        await self.page.wait_for_timeout(4000)  # 布局灰度会话间漂移，DOM 需充分就绪
        await self._check_login()

    async def _check_login(self):
        tail = await self.page.evaluate(BODY_TAIL_JS)
        for pat in self.cfg["quota_signals"]["login_expired"]:
            if re.search(pat, tail):
                raise QuotaSignal("login_expired", f"login signal: {pat}")

    async def health(self) -> dict:
        """自检：登录态 / 编辑器 / 附件通道。任一失败即 dom_changed 风险。"""
        ok_login = ok_editor = ok_attach = False
        try:
            ok_login = "login" not in self.page.url
            ok_editor = bool(await self.page.query_selector(self.sel["editor"]))
            ok_attach = (bool(await self.page.query_selector(self.sel["drop_area"]))
                         or bool(await self.page.query_selector("input[type=file]")))
        except Exception:
            pass
        return {"account": self.name, "login": ok_login,
                "editor": ok_editor, "attach_channel": ok_attach,
                "healthy": ok_login and ok_editor and ok_attach}

    async def detect_layout(self) -> str:
        """classic=经典对话布局（有模式按钮）| workspace=豆包工作工作台布局。
        轮询等待决定性特征（懒渲染时序：file_drop_area 可能晚数秒才挂载）。"""
        for _ in range(6):  # 最多 ~18s
            info = await self.page.evaluate(LAYOUT_JS)
            if info["workspace"] or info["mode_image"] or info["classic_upload"]:
                return "workspace" if info["workspace"] and not info["mode_image"] else "classic"
            await self.page.wait_for_timeout(3000)
        raise QuotaSignal("dom_changed",
                          "layout detection failed: no decisive feature in 18s")

    async def switch_mode(self, mode: str):
        """mode: 'image' | 'video'。经典布局点模式按钮；工作台布局无此按钮，
        主 agent 依赖 prompt+附件自行路由（M2 实测验证），直接跳过。"""
        layout = await self.detect_layout()
        if layout == "workspace":
            await self.page.wait_for_timeout(800)
            return  # 工作台无模式切换，靠附件+prompt 路由
        key = "mode_image" if mode == "image" else "mode_video"
        btn = self.page.locator(self.sel[key]).first
        try:
            await btn.click(timeout=5000)
        except Exception as e:
            raise QuotaSignal("dom_changed", f"switch_mode({mode}) failed: {e}")
        await self.page.wait_for_timeout(1500)

    # ---------- 备料：挂附件 + 注 prompt ----------

    async def arm(self, prompt_text: str,
                  ref_pairs: list[tuple[str, str]] | None = None,
                  local_paths: list[str] | None = None) -> dict:
        """ref_pairs: [(文件名, CDN签名URL), ...]；local_paths: 本地文件路径列表
        （base64 直传，优先于 CDN URL，不受签名过期影响）。返回备料结果。
        挂载后校验附件预览卡片数，未感知即 dom_changed/路由异常，不发送。"""
        n_refs = len(ref_pairs or []) + len(local_paths or [])
        channel = None
        if local_paths or ref_pairs:
            payload: dict = {"sel_drop": self.sel["drop_area"],
                             "sel_upload": self.sel.get("upload_input", "")}
            if local_paths:
                payload["files"] = [
                    {"name": Path(p).name,
                     "b64": base64.b64encode(Path(p).read_bytes()).decode("ascii"),
                     "mime": "image/jpeg" if p.lower().endswith((".jpg", ".jpeg")) else "image/png"}
                    for p in local_paths]
            if ref_pairs:
                payload["urls"] = [list(p) for p in ref_pairs]
            channel = await self.page.evaluate(ATTACH_JS, payload)
            await self.page.wait_for_timeout(3000)
            n_cards = await self.page.evaluate(ATTACH_CHECK_JS)
            if n_cards < n_refs:
                raise QuotaSignal(
                    "dom_changed",
                    f"attachments not perceived: {channel}, cards={n_cards}/{n_refs}")
        b64 = base64.b64encode(prompt_text.encode("utf-8")).decode("ascii")
        n_chars = await self.page.evaluate(
            INJECT_JS, {"b64": b64, "sel_editor": self.sel["editor"]})
        return {"channel": channel, "refs": n_refs, "cards": n_cards if n_refs else 0,
                "prompt_chars": n_chars}

    # ---------- 发送 ----------

    async def auto_send(self):
        for sel in self.sel["send_button_candidates"]:
            loc = self.page.locator(sel).first
            try:
                await loc.click(timeout=3000)
                await self.page.wait_for_timeout(1500)
                return
            except Exception:
                continue
        raise QuotaSignal("dom_changed", "send button not found by any candidate selector")

    # ---------- 轮询与信号分类 ----------

    async def _scan_quota_signal(self):
        tail = await self.page.evaluate(BODY_TAIL_JS)
        for kind in ("rate_limited", "quota_exhausted"):
            for pat in self.cfg["quota_signals"][kind]:
                if re.search(pat, tail):
                    raise QuotaSignal(kind, f"matched '{pat}' in: ...{tail[-120:]}")

    async def _auto_confirm(self):
        """工作台 agent 出方案后等确认 → 自动点「按要求生成」启动生成。"""
        clicked = await self.page.evaluate(CONFIRM_JS)
        if clicked:
            print(f"[driver] 已自动点击确认:「{clicked}」")
            await self.page.wait_for_timeout(1500)

    async def wait_image(self, timeout_sec: int, poll_sec: int = 5) -> list[str]:
        """轮询生成图原图 URL（过滤 cthumb 缩略图）；期间自动点确认卡片。"""
        import time
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            await self._scan_quota_signal()
            await self._auto_confirm()
            urls = await self.page.evaluate(POLL_IMAGE_JS)
            if urls:
                return urls
            await self.page.wait_for_timeout(poll_sec * 1000)
        raise GenerationTimeout(f"image not ready in {timeout_sec}s")

    async def wait_video(self, timeout_sec: int, poll_sec: int = 5) -> str:
        """轮询生成视频真实地址（currentSrc）；期间自动点确认卡片、
        点 block-video 挂载懒加载的 xgplayer。"""
        import time
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            await self._scan_quota_signal()
            await self._auto_confirm()
            src = await self.page.evaluate(POLL_VIDEO_JS)
            if src:
                return src
            mounted = await self.page.evaluate(VIDEO_MOUNT_JS)
            if mounted == "clicked":
                print("[driver] 已点击视频块，挂载播放器")
                await self.page.wait_for_timeout(2000)
                src = await self.page.evaluate(POLL_VIDEO_JS)
                if src:
                    return src
            await self.page.wait_for_timeout(poll_sec * 1000)
        raise GenerationTimeout(f"video not ready in {timeout_sec}s")

    async def harvest_video_from_chat(self, chat_url: str, timeout_sec: int = 120) -> str:
        """超时恢复通道：生成已完成的会话回源收视频（服务端会话持久化）。"""
        await self.page.goto(chat_url, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(5000)
        return await self._poll_video_inplace(timeout_sec)

    async def _poll_video_inplace(self, timeout_sec: int) -> str:
        import time
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            src = await self.page.evaluate(POLL_VIDEO_JS)
            if src:
                return src
            await self.page.evaluate(VIDEO_MOUNT_JS)
            await self.page.wait_for_timeout(3000)
        raise GenerationTimeout("video present but not mounted in time")

    # ---------- 资产 harvest（参考图 URL 过期时回源会话重采） ----------

    async def harvest_upload_urls(self, chat_url: str) -> list[str]:
        """进入历史会话页，抓用户上传附件的原图签名 URL。"""
        await self.page.goto(chat_url, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(2500)
        js = """() => {
            const out = []; const seen = {};
            document.querySelectorAll('img').forEach(i => {
                const s = i.src || '';
                // 用户上传原图：~tplv-a9rns2rl98-image.png；生成图：rc_gen_image
                if ((s.indexOf('~tplv-a9rns2rl98-image.png') >= 0 ||
                     s.indexOf('rc_gen_image') >= 0) &&
                    s.indexOf('qvalue') < 0 && s.indexOf('/icon') < 0 && !seen[s]) {
                    seen[s] = 1; out.push(s);
                }
            });
            return out;
        }"""
        return await self.page.evaluate(js)

    async def throttle_sleep(self):
        t = self.cfg["throttle"]
        await self.page.wait_for_timeout(
            random.randint(t["min_delay_sec"], t["max_delay_sec"]) * 1000)
