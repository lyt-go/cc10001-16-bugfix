"""采集任务业务规则：状态流转、字段校验与筛选口径都收在这里。

采集任务不再只看自己的行状态：启动前必须读取登录态与反爬策略的同一份结论，
两边任一不可用时，任务卡只能展示不可启动原因，不能出现列表可用、任务却拿旧配置的分裂状态。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store
from app.services.blacklist import BlacklistService
from app.services.lmcfg import LINK_FIELDS as LMCFG_LINK_FIELDS
from app.services.lmcfg import LmcfgService, parse_time, split_values

MODULE = "taskcrawl"
REQUIRED_FIELDS = ["任务编号", "采集源名称", "规则编号"]
STATUS_ORDER = ["草稿", "待调度", "执行中", "已完成", "已终止"]
STATUS_DRAFT, STATUS_PENDING, STATUS_RUNNING, STATUS_COMPLETED, STATUS_STOPPED = STATUS_ORDER
ACTION_RULES = {"提交调度": STATUS_PENDING, "启动任务": STATUS_RUNNING, "终止任务": STATUS_STOPPED}
NEGATIVE_ACTIONS: set[str] = set()

START_ACTIONS = {"启动任务"}
STOP_ACTIONS = {"终止任务"}
SUBMIT_ACTIONS = {"提交调度"}
ALLOWED_ACTIONS = {
    STATUS_DRAFT: SUBMIT_ACTIONS,
    STATUS_PENDING: START_ACTIONS | STOP_ACTIONS,
    STATUS_RUNNING: STOP_ACTIONS,
    STATUS_COMPLETED: set(),
    STATUS_STOPPED: SUBMIT_ACTIONS,
}

CREDENTIAL_FIELDS = ["绑定凭证", "关联凭证", "凭证编号", "登录凭证", "绑定登录凭证"]
POLICY_FIELDS = ["绑定策略", "关联策略", "策略编号", "反爬策略", "绑定反爬策略"]
TASK_BIND_FIELDS = ["绑定采集任务", "关联采集任务"]
SITE_FIELDS = ["目标站点", "目标域名", "站点", "采集站点"]

lmcfg_service = LmcfgService()
blacklist_service = BlacklistService()


def _first(row: dict[str, Any], fields: list[str]) -> str:
    for field in fields:
        text = str(row.get(field) or "").strip()
        if text:
            return text
    return ""


def _bound_task_codes(task: dict[str, Any]) -> set[str]:
    codes: set[str] = set()
    code = str(task.get("任务编号") or "").strip().lower()
    if code:
        codes.add(code)
    for field in TASK_BIND_FIELDS:
        for text in split_values(task.get(field)):
            codes.add(text.lower())
    task_id = str(task.get("id") or "").strip()
    if task_id:
        codes.add(task_id)
    return codes


def _time_key(value: Any) -> float:
    parsed = parse_time(value)
    if parsed is None:
        return float("-inf")
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed.timestamp()


class TaskcrawlService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with store.lock:
            rows = store.rows(MODULE)
            if keyword:
                rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
            if status:
                rows = [row for row in rows if row.get("status") == status]
            total = len(rows)
            start = max(page - 1, 0) * size
            return [self.present(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            return self.present(entry) if entry is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.lock:
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            for field in ["调度周期", "增量字段", "批次大小", "起始水位", "截止水位"]:
                if field in values and values[field] is not None:
                    entry[field] = values[field]
            for field in CREDENTIAL_FIELDS + POLICY_FIELDS + SITE_FIELDS + TASK_BIND_FIELDS:
                if field in values and values[field] is not None:
                    entry[field] = values[field]
            entry["status"] = STATUS_DRAFT
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
            return self.present(entry), []

    # ---------- 会话与策略结论 ----------

    def _matches_task(self, row: dict[str, Any], task: dict[str, Any]) -> bool:
        codes = _bound_task_codes(task)
        for field in LMCFG_LINK_FIELDS + TASK_BIND_FIELDS:
            for text in split_values(row.get(field)):
                if text.lower() in codes:
                    return True
        return False

    def _same_site(self, row: dict[str, Any], task: dict[str, Any]) -> bool:
        row_site = _first(row, SITE_FIELDS).lower()
        task_site = _first(task, SITE_FIELDS).lower()
        if not row_site or not task_site:
            return False
        row_site = row_site.replace("http://", "").replace("https://", "").rstrip("/")
        task_site = task_site.replace("http://", "").replace("https://", "").rstrip("/")
        return row_site == task_site

    def _candidates(self, module: str, task: dict[str, Any]) -> list[dict[str, Any]]:
        rows = list(store.rows(module))
        matched = [row for row in rows if self._matches_task(row, task)]
        if not matched:
            matched = [row for row in rows if self._same_site(row, task)]
        return matched

    def _bound_codes(self, task: dict[str, Any], fields: list[str]) -> list[str]:
        codes: list[str] = []
        for field in fields:
            if field in task and task[field] not in (None, ""):
                codes.extend(split_values(task[field]))
        return codes

    def _resolve_credential(self, task: dict[str, Any]) -> dict[str, Any]:
        codes = self._bound_codes(task, CREDENTIAL_FIELDS)
        candidates: list[dict[str, Any]]
        if codes:
            candidates = []
            for code in codes:
                entry = lmcfg_service.find_by_code(code)
                if entry is not None and entry not in candidates:
                    candidates.append(entry)
            if not candidates:
                return {
                    "可用": False,
                    "凭证编号": "、".join(codes),
                    "凭证状态": "不存在",
                    "原因": f"绑定的登录凭证 {'、'.join(codes)} 不存在或已归档",
                    "最近验证成功时间": None,
                    "判定依据": "凭证缺失",
                }
        else:
            candidates = self._candidates("lmcfg", task)
        if not candidates:
            return {
                "可用": False,
                "凭证编号": None,
                "凭证状态": "未绑定",
                "原因": "未绑定登录凭证，采集任务无法启动",
                "最近验证成功时间": None,
                "判定依据": "无可用登录凭证",
            }
        # 冲突时按最近一次验证成功的会话判定；同等条件下取后登记的一条。
        candidates.sort(
            key=lambda row: (_time_key(row.get("最近验证成功时间")), int(row.get("id", 0))),
            reverse=True,
        )
        entry = candidates[0]
        lmcfg_service.sync_entry(entry)
        code_value = entry.get("凭证编号") or entry.get("id")
        status = str(entry.get("status") or "")
        if lmcfg_service.is_usable(entry):
            return {
                "可用": True,
                "凭证编号": code_value,
                "凭证状态": status,
                "原因": None,
                "最近验证成功时间": entry.get("最近验证成功时间") or entry.get("最近验证时间"),
                "Cookie内容": entry.get("Cookie内容"),
                "有效期至": entry.get("有效期至"),
                "判定依据": f"取最近验证成功的登录凭证 {code_value}（{entry.get('最近验证成功时间') or entry.get('最近验证时间') or '无验证时间'}）",
            }
        reason = str(entry.get("失败原因") or entry.get("失效原因") or "")
        if not reason:
            reason = f"登录凭证 {code_value} 当前状态为「{status}」，需重新验证"
        return {
            "可用": False,
            "凭证编号": code_value,
            "凭证状态": status,
            "原因": reason,
            "最近验证成功时间": entry.get("最近验证成功时间"),
            "判定依据": f"取最近验证成功的登录凭证 {code_value}，当前不可用",
        }

    def _resolve_policy(self, task: dict[str, Any]) -> dict[str, Any]:
        codes = self._bound_codes(task, POLICY_FIELDS)
        candidates: list[dict[str, Any]]
        if codes:
            candidates = []
            for code in codes:
                entry = blacklist_service.find_by_code(code)
                if entry is not None and entry not in candidates:
                    candidates.append(entry)
        else:
            candidates = self._candidates("blacklist", task)
        if not candidates:
            # 未绑定反爬策略时按无策略放行；显式绑定了却找不到则不可用。
            if codes:
                return {
                    "可用": False,
                    "策略编号": "、".join(codes),
                    "策略状态": "不存在",
                    "原因": f"绑定的反爬策略 {'、'.join(codes)} 不存在或已归档",
                    "判定依据": "策略缺失",
                }
            return {
                "可用": True,
                "策略编号": None,
                "策略状态": "未绑定",
                "原因": None,
                "最近测试通过时间": None,
                "判定依据": "未绑定反爬策略，按无策略执行",
            }
        candidates.sort(
            key=lambda row: (_time_key(row.get("测试通过时间")), int(row.get("id", 0))),
            reverse=True,
        )
        entry = candidates[0]
        code_value = entry.get("策略编号") or entry.get("id")
        status = str(entry.get("status") or "")
        if blacklist_service.is_usable(entry):
            return {
                "可用": True,
                "策略编号": code_value,
                "策略状态": status,
                "原因": None,
                "最近测试通过时间": entry.get("测试通过时间"),
                "UA模拟列表": entry.get("UA模拟列表"),
                "Referer配置": entry.get("Referer配置"),
                "验证码识别": entry.get("验证码识别"),
                "判定依据": f"取最近测试通过的反爬策略 {code_value}（{entry.get('测试通过时间') or '无测试时间'}）",
            }
        reason = str(entry.get("失败原因") or entry.get("失效原因") or "")
        if not reason:
            reason = f"反爬策略 {code_value} 当前状态为「{status}」，需完成策略测试并通过"
        return {
            "可用": False,
            "策略编号": code_value,
            "策略状态": status,
            "原因": reason,
            "最近测试通过时间": entry.get("测试通过时间"),
            "判定依据": f"取最近测试通过的反爬策略 {code_value}，当前不可用",
        }

    def readiness(self, task: dict[str, Any]) -> dict[str, Any]:
        """一次判定同时给出会话、策略和启动结论，列表/详情/启动动作都复用它。"""
        credential = self._resolve_credential(task)
        policy = self._resolve_policy(task)
        reasons: list[str] = []
        if not credential["可用"]:
            reasons.append(str(credential["原因"]))
        if not policy["可用"]:
            reasons.append(str(policy["原因"]))
        startable = not reasons
        if startable:
            card = "可启动"
        elif not credential["可用"]:
            card = "待重新验证"
        else:
            card = "待策略测试"
        return {
            "可启动": startable,
            "会话状态": "可用" if startable else "不可用",
            "任务卡状态": card,
            "不可启动原因": "；".join(reasons),
            "登录凭证": credential,
            "反爬策略": policy,
        }

    def present(self, entry: dict[str, Any]) -> dict[str, Any]:
        item = dict(entry)
        conclusion = self.readiness(entry)
        item["会话结论"] = conclusion
        item["可启动"] = conclusion["可启动"]
        item["会话状态"] = conclusion["会话状态"]
        item["任务卡状态"] = conclusion["任务卡状态"]
        item["不可启动原因"] = conclusion["不可启动原因"]
        item["可执行动作"] = self.allowed_actions(item)
        return item

    def allowed_actions(self, entry: dict[str, Any]) -> list[str]:
        status = str(entry.get("status") or "")
        if status == STATUS_DRAFT:
            return ["提交调度"]
        if status == STATUS_PENDING:
            return ["启动任务", "终止任务"]
        if status == STATUS_RUNNING:
            return ["终止任务"]
        if status == STATUS_STOPPED:
            return ["提交调度"]
        return []

    # ---------- 动作 ----------

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"采集任务 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于采集任务可执行范围"
            current = str(entry.get("status") or "")
            if action not in ALLOWED_ACTIONS.get(current, set()):
                return None, self._reject_message(entry, current, action)
            if action in START_ACTIONS:
                conclusion = self.readiness(entry)
                if not conclusion["可启动"]:
                    return None, f"采集任务 {entry.get('任务编号')} 不可启动：{conclusion['不可启动原因']}"
                entry["启动会话"] = {
                    "登录凭证": conclusion["登录凭证"]["凭证编号"],
                    "凭证状态": conclusion["登录凭证"]["凭证状态"],
                    "反爬策略": conclusion["反爬策略"]["策略编号"],
                    "策略状态": conclusion["反爬策略"]["策略状态"],
                    "判定依据": conclusion["登录凭证"]["判定依据"],
                }
                entry["不可启动原因"] = ""
            entry["status"] = ACTION_RULES[action]
            entry["pending"] = entry["status"] != STATUS_COMPLETED
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            return self.present(entry), f"采集任务已{action}"

    def _reject_message(self, entry: dict[str, Any], current: str, action: str) -> str:
        code = entry.get("任务编号") or entry.get("id")
        if current == STATUS_DRAFT and action in START_ACTIONS:
            return f"采集任务 {code} 尚未提交调度，不能直接启动"
        if current in (STATUS_COMPLETED, STATUS_STOPPED) and action in START_ACTIONS:
            return f"采集任务 {code} 当前为「{current}」，请先重新提交调度"
        if current == STATUS_RUNNING and action in SUBMIT_ACTIONS:
            return f"采集任务 {code} 正在执行中，不能重复提交调度"
        return f"采集任务 {code} 的「{current}」状态不允许执行「{action}」"
