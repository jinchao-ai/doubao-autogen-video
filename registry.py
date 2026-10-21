"""registry.py — 资产注册表 + 任务状态机。

- assets.json：资产名 → CDN 签名 URL / 本地文件 / desc_prompt_lock / source_chat
  （URL 过期 → 回 source_chat 重新 harvest）
- tasks.json：任务持久化，状态机见 DESIGN.md §6
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path


class AssetRegistry:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.assets = json.loads(self.path.read_text(encoding="utf-8")) \
            if self.path.exists() else {}

    def save(self):
        self.path.write_text(
            json.dumps(self.assets, ensure_ascii=False, indent=2), encoding="utf-8")

    def refs(self, names: list[str]) -> list[tuple[str, str]]:
        """资产名列表 → [(文件名, CDN_URL)]；缺资产时报错列出。"""
        out, missing = [], []
        for n in names:
            a = self.assets.get(n)
            if a and a.get("cdn_url"):
                out.append((a.get("filename", f"{n}.jpg"), a["cdn_url"]))
            else:
                missing.append(n)
        if missing:
            raise KeyError(f"assets missing or no cdn_url: {missing}")
        return out

    def register(self, name: str, cdn_url: str, filename: str,
                 desc_prompt_lock: str = "", source_chat: str = ""):
        self.assets[name] = {"cdn_url": cdn_url, "filename": filename,
                             "desc_prompt_lock": desc_prompt_lock,
                             "source_chat": source_chat, "status": "locked"}
        self.save()

    def stale(self, name: str):
        """URL 失效标记，等待重 harvest。"""
        self.assets[name]["status"] = "stale"
        self.save()


class TaskStore:
    """任务持久化 + 状态机。"""

    def __init__(self, state_dir: str):
        self.file = Path(state_dir) / "tasks.json"
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.tasks = json.loads(self.file.read_text(encoding="utf-8")) \
            if self.file.exists() else {}

    def save(self):
        self.file.write_text(
            json.dumps(self.tasks, ensure_ascii=False, indent=2), encoding="utf-8")

    def create(self, task_type: str, prompt: str, refs: list[str],
               expect: dict | None = None, output: str | None = None) -> str:
        tid = f"t_{uuid.uuid4().hex[:8]}"
        self.tasks[tid] = {
            "id": tid, "type": task_type, "prompt": prompt, "refs": refs,
            "expect": expect or {}, "output": output,
            "status": "pending",
            "created_at": time.time(), "attempts": 0, "result": None,
            "verify": None, "error": None, "history": [],
        }
        self.save()
        return tid

    def set(self, tid: str, **kw):
        if tid in self.tasks:
            self.tasks[tid].update(kw)
            self.save()

    def get(self, tid: str) -> dict | None:
        return self.tasks.get(tid)

    def next_pending(self) -> dict | None:
        """按入队时间先后取任务（M3 实测发现：按随机 tid 排序会乱序执行镜头）。"""
        for tid in sorted(self.tasks, key=lambda k: self.tasks[k]["created_at"]):
            t = self.tasks[tid]
            if t["status"] in ("pending", "retry", "rewrite"):
                return t
        return None

    def pending_count(self) -> int:
        return sum(1 for t in self.tasks.values()
                   if t["status"] in ("pending", "retry", "rewrite"))
