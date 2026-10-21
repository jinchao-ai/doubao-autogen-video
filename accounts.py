"""accounts.py — 账号池与额度治理。

策略（DESIGN.md §4）：
- pick: active 且不在冷却期中选当日用量最少者
- rate_limited → 冷却 cooldown 分钟
- quota_exhausted → exhausted，reset_at = 次日 reset_hour + jitter
- 全部不可用 → worker 睡到最早 reset_at
状态持久化 state/accounts_state.json。
"""
from __future__ import annotations

import json
import random
import time
from datetime import datetime, timedelta
from pathlib import Path


class AccountPool:
    def __init__(self, cfg: dict, state_dir: str):
        self.cfg = cfg
        self.accounts = {a["name"]: dict(a) for a in cfg["accounts"]}
        self.state_file = Path(state_dir) / "accounts_state.json"
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load()

    # ---------- 持久化 ----------

    def _load(self) -> dict:
        if self.state_file.exists():
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        today = datetime.now().strftime("%Y-%m-%d")
        return {name: {"date": today, "t2i_used": 0, "i2v_used": 0,
                       "status": "active", "cooldown_until": 0, "reset_at": 0}
                for name in self.accounts}

    def _save(self):
        self.state_file.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _roll_date_if_needed(self, name: str):
        """跨天自动重置计数。"""
        today = datetime.now().strftime("%Y-%m-%d")
        s = self.state[name]
        if s["date"] != today:
            s.update({"date": today, "t2i_used": 0, "i2v_used": 0,
                      "status": "active", "reset_at": 0})

    # ---------- 调度 ----------

    def pick(self, task_type: str = "t2i") -> str | None:
        """返回可用账号名；全部不可用返回 None。"""
        now = time.time()
        best = None
        for name, s in self.state.items():
            self._roll_date_if_needed(name)
            if name not in self.accounts:
                continue
            if s["status"] in ("exhausted", "login_expired"):
                if s["reset_at"] and now >= s["reset_at"]:
                    s["status"] = "active"
                    s["reset_at"] = 0
                else:
                    continue
            if s["cooldown_until"] and now < s["cooldown_until"]:
                continue
            quota_key = "daily_quota_t2i" if task_type == "t2i" else "daily_quota_i2v"
            used_key = "t2i_used" if task_type == "t2i" else "i2v_used"
            if s[used_key] >= self.accounts[name][quota_key]:
                self.mark(name, "quota_exhausted")
                continue
            if best is None or s[used_key] < self.state[best][used_key]:
                best = name
        self._save()
        return best

    def mark_used(self, name: str, task_type: str = "t2i"):
        self._roll_date_if_needed(name)
        key = "t2i_used" if task_type == "t2i" else "i2v_used"
        self.state[name][key] += 1
        self._save()

    def mark(self, name: str, kind: str, detail: str = ""):
        """按 QuotaSignal.kind 标记账号。detail 传入信号原文用于解析真实恢复时间。

        M3 实测（2026-10-19）：豆包免费额度是「7 天滚动窗口」而非每日重置，
        耗尽信号原文如「近 7 天免费额度已用完，预计 10 月 26 日 18:51 恢复」。
        能解析出真实恢复时间就优先用，解析不出退回次日 daily_reset_hour。
        """
        now = time.time()
        s = self.state.get(name)
        if not s:
            return
        if kind == "rate_limited":
            cooldown = self.cfg["throttle"]["rate_limit_cooldown_min"] * 60
            s["cooldown_until"] = now + cooldown * random.uniform(0.8, 1.2)
        elif kind == "quota_exhausted":
            s["status"] = "exhausted"
            reset = self._parse_recovery_time(detail)
            if reset is None:
                reset = datetime.now().replace(
                    hour=self.cfg["daily_reset_hour"], minute=0, second=0, microsecond=0)
                if reset <= datetime.now():
                    reset += timedelta(days=1)
                jitter = self.cfg.get("daily_reset_jitter_min", 5)
                reset += timedelta(minutes=random.uniform(0, jitter))
            s["reset_at"] = reset.timestamp()
        elif kind == "login_expired":
            s["status"] = "login_expired"
            s["reset_at"] = 0  # 人工重登后由 health/手动恢复
        self._save()

    @staticmethod
    def _parse_recovery_time(detail: str):
        """从信号原文解析恢复时间：'预计 10 月 26 日 18:51 恢复'。"""
        import re as _re
        m = _re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(\d{1,2}):(\d{2})", detail)
        if not m:
            return None
        mon, day, hh, mm = (int(x) for x in m.groups())
        now = datetime.now()
        try:
            return datetime(now.year, mon, day, hh, mm)
        except ValueError:
            return None

    def seconds_until_next_available(self) -> float:
        """全部不可用时，睡到最早恢复时刻。"""
        now = time.time()
        candidates = []
        for name, s in self.state.items():
            if name not in self.accounts:
                continue
            times = [s["cooldown_until"]] if s["cooldown_until"] > now else []
            if s["status"] == "exhausted" and s["reset_at"] > now:
                times.append(s["reset_at"])
            if times:
                candidates.append(min(times))
        if not candidates:
            return 60  # 无明确恢复时间（如 login_expired），小步重查
        return max(0, min(candidates) - now)

    def snapshot(self) -> dict:
        now = time.time()
        return {name: {"status": s["status"],
                       "t2i_used": s["t2i_used"], "i2v_used": s["i2v_used"],
                       "cooldown_left": max(0, int(s["cooldown_until"] - now)),
                       "reset_in": max(0, int(s["reset_at"] - now))}
                for name, s in self.state.items()}
