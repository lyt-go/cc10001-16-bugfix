"""登录态管理业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "lmcfg"
REQUIRED_FIELDS = ["凭证编号", "目标站点", "认证方式"]
STATUS_ORDER = ["草稿", "验证中", "有效", "已过期"]
STATUS_DRAFT, STATUS_VERIFYING, STATUS_VALID, STATUS_EXPIRED = STATUS_ORDER

# 动作只能沿状态序列向前走；「已过期」要恢复「有效」必须先重新提交验证。
SUBMIT_ACTIONS = {"提交验证", "刷新凭证", "开始验证"}
# 「标记有效」是验证完成后的收口动作；它只能从「验证中」执行，不能替代验证本身。
VERIFY_PASS_ACTIONS = {"标记有效", "验证通过", "测试通过", "确认有效"}
VERIFY_FAIL_ACTIONS = {"验证失败", "测试失败", "验证不通过"}
EXPIRE_ACTIONS = {"标记过期", "确认过期"}
PASS_ACTIONS = VERIFY_PASS_ACTIONS
FAIL_ACTIONS = VERIFY_FAIL_ACTIONS
ACTION_RULES = {
    "提交验证": STATUS_VERIFYING,
    "刷新凭证": STATUS_VERIFYING,
    "开始验证": STATUS_VERIFYING,
    "验证通过": STATUS_VALID,
    "测试通过": STATUS_VALID,
    "验证失败": STATUS_EXPIRED,
    "测试失败": STATUS_EXPIRED,
    "验证不通过": STATUS_EXPIRED,
    "标记有效": STATUS_VALID,
    "确认有效": STATUS_VALID,
    "标记过期": STATUS_EXPIRED,
    "确认过期": STATUS_EXPIRED,
}

# 每个状态允许的动作；验证中只能由验证结果收口，草稿和已过期都不能直接点成有效。
ALLOWED_ACTIONS = {
    STATUS_DRAFT: SUBMIT_ACTIONS,
    STATUS_VERIFYING: VERIFY_PASS_ACTIONS | VERIFY_FAIL_ACTIONS | EXPIRE_ACTIONS,
    STATUS_VALID: SUBMIT_ACTIONS | EXPIRE_ACTIONS,
    STATUS_EXPIRED: SUBMIT_ACTIONS,
}
NEGATIVE_ACTIONS = FAIL_ACTIONS | EXPIRE_ACTIONS

COPY_FIELDS = [
    "凭证编号", "目标站点", "认证方式", "Cookie内容", "有效期至", "刷新策略",
    "最近验证时间", "绑定代理", "绑定域名", "绑定采集任务", "关联采集任务",
]
LINK_FIELDS = ["绑定采集任务", "关联采集任务"]
FAIL_REASON_FIELDS = ["失败原因", "验证失败原因", "失效原因", "原因", "备注", "验证结果", "验证结论"]
RESULT_FIELDS = ["验证结果", "验证结论", "结果"]
PASS_TOKENS = {"通过", "成功", "有效", "正常", "pass", "ok", "true", "yes"}
DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%Y年%m月%d日 %H:%M",
    "%Y年%m月%d日",
]


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_time(value: Any) -> datetime | None:
    """兼容示例数据和接口里常见的日期写法；解析不了就返回 None，不误判过期。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def split_values(value: Any) -> list[str]:
    """把绑定关系字段统一拆成字符串列表，兼容列表、JSON 数组和中文顿号。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except (ValueError, TypeError):
            pass
    for sep in ["、", "，", ",", ";", "；", "\n", "|"]:
        if sep in text:
            return [part.strip() for part in text.split(sep) if part.strip()]
    return [text]


def _value(values: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in values and values[name] not in (None, ""):
            return values[name]
    return None


def _reason(values: dict[str, Any], default: str) -> str:
    for field in FAIL_REASON_FIELDS:
        text = str(values.get(field) or "").strip()
        if text:
            return text
    return default


class LmcfgService:
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
            for row in rows:
                self.sync_entry(row)
            if keyword:
                rows = [row for row in rows if keyword in str(row.get("凭证编号", ""))]
            if status:
                rows = [row for row in rows if row.get("status") == status]
            total = len(rows)
            start = max(page - 1, 0) * size
            return [self.present(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is not None:
                self.sync_entry(entry)
            return self.present(entry) if entry is not None else None

    def find_by_code(self, code: str) -> dict[str, Any] | None:
        text = str(code or "").strip().lower()
        if not text:
            return None
        for row in store.rows(MODULE):
            if str(row.get("凭证编号", "")).strip().lower() == text:
                self.sync_entry(row)
                return row
        if text.isdigit():
            return store.find(MODULE, int(text))
        return None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.lock:
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            for field in COPY_FIELDS:
                if field in values and values[field] is not None:
                    entry[field] = values[field]
            entry["status"] = STATUS_DRAFT
            entry["pending"] = True
            entry["abnormal"] = False
            entry["验证状态"] = "未验证"
            entry["验证轮次"] = 0
            entry["最近验证成功时间"] = None
            rows.append(entry)
            return self.present(entry), []

    # ---------- 状态与结论 ----------

    def sync_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        """按有效期把已过期的凭证落到「已过期」，避免列表看似有效、任务却用旧 Cookie。

        只有「有效」状态会被日期自动降级；验证成功本身也会写「最近验证成功时间」，
        验证发生在标称有效期之后时，不再用旧日期回退刚验证通过的会话。
        """
        status = str(entry.get("status") or "")
        if status != STATUS_VALID:
            return entry
        expiry = parse_time(entry.get("有效期至"))
        if expiry is None or expiry >= datetime.now():
            return entry
        verified_at = parse_time(entry.get("最近验证成功时间"))
        if verified_at is not None and verified_at > expiry:
            return entry
        entry["status"] = STATUS_EXPIRED
        entry["pending"] = True
        entry["abnormal"] = True
        entry["验证状态"] = "已过期"
        entry["失效原因"] = f"登录凭证有效期已至 {entry.get('有效期至')}，需重新验证"
        return entry

    def is_usable(self, entry: dict[str, Any] | None) -> bool:
        return bool(entry is not None and entry.get("status") == STATUS_VALID)

    def present(self, entry: dict[str, Any]) -> dict[str, Any]:
        """返回列表/详情的稳定视图：状态、可执行动作和联动结论都不依赖前端猜测。"""
        item = dict(entry)
        item["状态"] = item.get("status")
        item["可执行动作"] = self.allowed_actions(item)
        if not item.get("失败原因") and item.get("失效原因"):
            item["失败原因"] = item["失效原因"]
        return item

    def allowed_actions(self, entry: dict[str, Any]) -> list[str]:
        status = str(entry.get("status") or "")
        if status == STATUS_VERIFYING:
            actions = ["标记有效", "验证失败", "标记过期"]
        elif status == STATUS_DRAFT:
            actions = ["提交验证"]
        elif status == STATUS_VALID:
            actions = ["提交验证", "标记过期"]
        elif status == STATUS_EXPIRED:
            actions = ["提交验证"]
        else:
            actions = []
        return actions

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
                return None, f"登录凭证 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于登录态管理可执行范围"
            self.sync_entry(entry)
            current = str(entry.get("status") or "")
            if action not in ALLOWED_ACTIONS.get(current, set()):
                return None, self._reject_message(entry, current, action)
            if action in SUBMIT_ACTIONS:
                return self._submit(entry, values)
            if action in PASS_ACTIONS:
                return self._finish(entry, values, success=True)
            if action in FAIL_ACTIONS:
                return self._finish(entry, values, success=False)
            return self._expire(entry, values)

    def verification_result(self, values: dict[str, Any]) -> str | None:
        """读取提交里附带的验证结论：passed / failed / None（未给出）。"""
        for field in RESULT_FIELDS:
            text = str(values.get(field) or "").strip()
            if not text:
                continue
            lower = text.lower()
            if any(token in lower for token in ("不通过", "未通过", "失败", "过期", "无效")):
                return "failed"
            if any(token in lower for token in PASS_TOKENS):
                return "passed"
        return None

    def _submit(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        entry["status"] = STATUS_VERIFYING
        entry["pending"] = True
        entry["abnormal"] = False
        entry["验证状态"] = "验证中"
        entry["验证开始时间"] = now_text()
        entry["验证轮次"] = int(entry.get("验证轮次") or 0) + 1
        # 提交验证只登记新一轮验证，不修改历史有效时间和最近验证时间。
        result = self.verification_result(values)
        if result == "passed":
            return self._finish(entry, values, success=True)
        if result == "failed":
            return self._finish(entry, values, success=False)
        return self.present(entry), "登录凭证已提交验证"

    def _finish(
        self,
        entry: dict[str, Any],
        values: dict[str, Any],
        *,
        success: bool,
    ) -> tuple[dict[str, Any] | None, str]:
        if not success:
            reason = _reason(values, "验证未通过，凭证需重新验证")
            # 失败的刷新必须保留历史有效时间：最近验证时间 / 最近验证成功时间都不动。
            entry["status"] = STATUS_EXPIRED
            entry["pending"] = True
            entry["abnormal"] = True
            entry["验证状态"] = "验证失败"
            entry["最近验证失败时间"] = now_text()
            entry["失败原因"] = reason
            entry["失效原因"] = reason
            return self.present(entry), f"登录凭证验证未通过：{reason}"

        cookie = _value(values, "Cookie内容", "cookie", "Cookie", "cookie内容")
        if cookie is not None:
            entry["Cookie内容"] = cookie
        expiry = _value(values, "有效期至", "expires_at", "到期时间")
        if expiry is not None:
            entry["有效期至"] = expiry
        stamp = now_text()
        entry["status"] = STATUS_VALID
        entry["pending"] = False
        entry["abnormal"] = False
        entry["验证状态"] = "验证通过"
        entry["最近验证时间"] = stamp
        entry["最近验证成功时间"] = stamp
        entry["验证通过时间"] = stamp
        entry.pop("失败原因", None)
        entry.pop("失效原因", None)
        return self.present(entry), "登录凭证验证通过，已恢复有效"

    def _expire(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        reason = _reason(values, "人工标记为已过期")
        entry["status"] = STATUS_EXPIRED
        entry["pending"] = True
        entry["abnormal"] = True
        entry["验证状态"] = "已过期"
        entry["失效原因"] = reason
        entry["失败原因"] = reason
        entry["标记过期时间"] = now_text()
        return self.present(entry), f"登录凭证已标记过期：{reason}"

    def _reject_message(self, entry: dict[str, Any], current: str, action: str) -> str:
        code = entry.get("凭证编号") or entry.get("id")
        if current == STATUS_EXPIRED and action in PASS_ACTIONS:
            return f"登录凭证 {code} 已过期，只有重新提交验证并通过后才能恢复有效"
        if current == STATUS_DRAFT and action in PASS_ACTIONS:
            return f"登录凭证 {code} 尚未提交验证，不能直接变为有效"
        if current == STATUS_VERIFYING and action in SUBMIT_ACTIONS:
            return f"登录凭证 {code} 正在验证中，请勿重复提交验证"
        if current == STATUS_EXPIRED and action in EXPIRE_ACTIONS:
            return f"登录凭证 {code} 已处于已过期状态"
        if current == STATUS_VERIFYING and action in VERIFY_PASS_ACTIONS | VERIFY_FAIL_ACTIONS:
            return f"登录凭证 {code} 当前状态不允许该验证结果"
        return f"登录凭证 {code} 的「{current}」状态不允许执行「{action}」"
