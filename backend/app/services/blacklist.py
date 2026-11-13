"""反爬对抗业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services.lmcfg import now_text, split_values

MODULE = "blacklist"
REQUIRED_FIELDS = ["策略编号", "目标域名", "UA模拟列表"]
STATUS_ORDER = ["草稿", "策略测试中", "已启用", "已失效"]
STATUS_DRAFT, STATUS_TESTING, STATUS_ENABLED, STATUS_INVALID = STATUS_ORDER

SUBMIT_ACTIONS = {"提交策略测试", "重新提交测试", "开始测试"}
PASS_ACTIONS = {"测试通过", "策略测试通过"}
FAIL_ACTIONS = {"测试失败", "策略测试失败", "测试不通过"}
ENABLE_ACTIONS = {"启用策略"}
INVALIDATE_ACTIONS = {"标记失效", "停用策略"}
ACTION_RULES = {
    "提交策略测试": STATUS_TESTING,
    "重新提交测试": STATUS_TESTING,
    "开始测试": STATUS_TESTING,
    "测试通过": STATUS_ENABLED,
    "策略测试通过": STATUS_ENABLED,
    "测试失败": STATUS_INVALID,
    "策略测试失败": STATUS_INVALID,
    "测试不通过": STATUS_INVALID,
    "启用策略": STATUS_ENABLED,
    "标记失效": STATUS_INVALID,
    "停用策略": STATUS_INVALID,
}

# 启用只能发生在「策略测试中」且测试通过之后；草稿和已失效都不能直接启用。
ALLOWED_ACTIONS = {
    STATUS_DRAFT: SUBMIT_ACTIONS,
    STATUS_TESTING: PASS_ACTIONS | FAIL_ACTIONS | ENABLE_ACTIONS | INVALIDATE_ACTIONS,
    STATUS_ENABLED: SUBMIT_ACTIONS | INVALIDATE_ACTIONS,
    STATUS_INVALID: SUBMIT_ACTIONS,
}
NEGATIVE_ACTIONS = FAIL_ACTIONS | INVALIDATE_ACTIONS

COPY_FIELDS = [
    "策略编号", "目标域名", "UA模拟列表", "Referer配置", "随机延迟范围",
    "IP轮换策略", "验证码识别", "Cookie种子", "绑定站点", "关联站点",
]
TEST_REASON_FIELDS = ["失败原因", "测试失败原因", "原因", "备注", "测试结果", "测试结论"]
RESULT_FIELDS = ["测试结论", "测试结果", "结果", "验证结果"]
PASS_TOKENS = {"通过", "成功", "正常", "pass", "ok", "true", "yes"}

CAPTCHA_VALUES = {"开启", "启用", "已开启", "已启用", "是", "true", "True", "on", "1"}
CAPTCHA_OFF_VALUES = {"关闭", "未开启", "停用", "否", "false", "False", "off", "0", "无", "-"}


def _value(values: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in values and values[name] not in (None, ""):
            return values[name]
    return None


def _reason(values: dict[str, Any], default: str) -> str:
    for field in TEST_REASON_FIELDS:
        text = str(values.get(field) or "").strip()
        if text:
            return text
    return default


def ua_browser_kinds(value: Any) -> list[str]:
    """从 UA 模拟列表里识别浏览器种类，至少三种才算可用的 UA 轮换配置。"""
    items = split_values(value)
    kinds: set[str] = set()
    for item in items:
        lower = item.lower()
        if lower in {"chrome", "谷歌", "google"} or "chrome" in lower:
            kinds.add("chrome")
        if lower in {"safari", "苹果"} or "safari" in lower:
            kinds.add("safari")
        if lower in {"firefox", "狐狸", "火狐"} or "firefox" in lower:
            kinds.add("firefox")
        if lower in {"edge", "微软"} or "edge" in lower:
            kinds.add("edge")
        if lower in {"ie", "trident"} or "internet explorer" in lower:
            kinds.add("ie")
    return sorted(kinds)


def referer_points_to_home(value: Any, domain: Any) -> bool:
    """Referer 必须指向目标站点首页：与目标域名同站，且不带深层路径。"""
    text = str(value or "").strip()
    if not text:
        return False
    lower = text.lower()
    for prefix in ("http://", "https://", "//"):
        if lower.startswith(prefix):
            lower = lower[len(prefix):]
            break
    if "?" in lower or "#" in lower:
        return False
    site = str(domain or "").strip().lower()
    if site:
        for prefix in ("http://", "https://", "//"):
            if site.startswith(prefix):
                site = site[len(prefix):]
                break
        if not lower.startswith(site):
            return False
        rest = lower[len(site):]
        return rest in ("", "/", "/index.html", "/index.htm", "/home")
    # 目标域名缺失时，以「首页形态」作为兜底判断，避免误放行深层路径。
    return "://" in text and lower.endswith(("/", "/index.html", "/index.htm"))


def captcha_ok(value: Any) -> tuple[bool, str | None]:
    """验证码识别配置必须给出明确的开启/关闭结论（含识别方式），不能空着放行。"""
    if value is None:
        return False, "验证码识别未配置（需明确开启并填写识别方式，或明确关闭）"
    if isinstance(value, dict):
        enabled = value.get("enabled")
        mode = str(value.get("mode") or value.get("识别方式") or "").strip()
        if enabled is False:
            return True, None
        if enabled is True and mode:
            return True, None
        return False, "验证码识别配置缺少开启结论或识别方式"
    if isinstance(value, (list, tuple, set)):
        return (True, None) if any(str(item).strip() for item in value) else (False, "验证码识别未配置")
    text = str(value).strip()
    lower = text.lower()
    if not text:
        return False, "验证码识别未配置（需明确开启并填写识别方式，或明确关闭）"
    if any(token in lower for token in ("不可用", "未配置", "无法识别", "识别失败", "未识别")):
        return False, f"验证码识别配置不可用：{text}"
    if lower in CAPTCHA_OFF_VALUES:
        return True, None
    # 「开启」「OCR」或者带识别方式的长描述都算明确配置。
    return True, None


class BlacklistService:
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
                rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
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
            if str(row.get("策略编号", "")).strip().lower() == text:
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
            entry["测试状态"] = "未测试"
            entry["测试轮次"] = 0
            rows.append(entry)
            return self.present(entry), []

    # ---------- 配置校验 ----------

    def config_failures(self, entry: dict[str, Any]) -> list[str]:
        """返回阻止策略启用的配置缺陷；空列表代表配置具备启用条件。"""
        problems: list[str] = []
        kinds = ua_browser_kinds(entry.get("UA模拟列表"))
        if len(kinds) < 3:
            shown = "、".join(kinds) if kinds else "无"
            problems.append(f"UA模拟列表需覆盖至少三种浏览器（当前识别到 {len(kinds)} 种：{shown}）")
        if not referer_points_to_home(entry.get("Referer配置"), entry.get("目标域名")):
            problems.append("Referer配置需指向目标站点首页（与目标域名同站且不带深层路径）")
        ok, message = captcha_ok(entry.get("验证码识别"))
        if not ok and message:
            problems.append(message)
        return problems

    # ---------- 状态与结论 ----------

    def sync_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        return entry

    def is_usable(self, entry: dict[str, Any] | None) -> bool:
        return bool(entry is not None and entry.get("status") == STATUS_ENABLED)

    def present(self, entry: dict[str, Any]) -> dict[str, Any]:
        item = dict(entry)
        problems = self.config_failures(item)
        item["配置阻断原因"] = problems
        item["可执行动作"] = self.allowed_actions(item)
        item["状态"] = item.get("status")
        if not item.get("失败原因") and item.get("测试失败原因"):
            item["失败原因"] = item["测试失败原因"]
        return item

    def allowed_actions(self, entry: dict[str, Any]) -> list[str]:
        status = str(entry.get("status") or "")
        if status == STATUS_TESTING:
            return ["测试通过", "测试失败", "标记失效"]
        if status == STATUS_DRAFT:
            return ["提交策略测试"]
        if status == STATUS_ENABLED:
            return ["标记失效"]
        if status == STATUS_INVALID:
            return ["提交策略测试"]
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
                return None, f"对抗策略 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于反爬对抗可执行范围"
            current = str(entry.get("status") or "")
            if action not in ALLOWED_ACTIONS.get(current, set()):
                return None, self._reject_message(entry, current, action)
            if action in SUBMIT_ACTIONS:
                return self._submit(entry, values)
            if action in PASS_ACTIONS:
                # 「测试通过」本身就是通过的结论，显式传入的结论优先。
                return self._enable(entry, {**{"测试结论": "通过"}, **values})
            if action in FAIL_ACTIONS:
                return self._finish_test(entry, values, success=False)
            if action in ENABLE_ACTIONS:
                return self._enable(entry, values)
            return self._invalidate(entry, values)

    def _submit(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        problems = self.config_failures(entry)
        if problems:
            # 配置不具备测试条件时也不能被直接启用：停在草稿并保留阻断原因。
            entry["测试状态"] = "未测试"
            entry["测试阻断原因"] = problems
            return self.present(entry), "策略配置不完整，无法提交测试：" + "；".join(problems)
        entry["status"] = STATUS_TESTING
        entry["pending"] = True
        entry["abnormal"] = False
        entry["测试状态"] = "测试中"
        entry["测试开始时间"] = now_text()
        entry["测试轮次"] = int(entry.get("测试轮次") or 0) + 1
        # 新一轮测试必须重新给出结论，历史测试通过时间不能复用。
        entry.pop("测试通过时间", None)
        entry.pop("测试结论", None)
        entry.pop("测试阻断原因", None)
        result = self.test_result(values)
        if result is not None:
            # 提交时已带测试结论（同步测试的场景）：直接按结论收口，仍需通过启用校验。
            return self._enable(entry, values)
        return self.present(entry), "对抗策略已提交测试"

    def _finish_test(
        self,
        entry: dict[str, Any],
        values: dict[str, Any],
        *,
        success: bool,
    ) -> tuple[dict[str, Any] | None, str]:
        if success:
            return self._enable(entry, {**{"测试结论": "通过"}, **values})

        reason = _reason(values, "策略测试未通过，需修正后重新测试")
        entry["status"] = STATUS_INVALID
        entry["pending"] = True
        entry["abnormal"] = True
        entry["测试状态"] = "测试失败"
        entry["测试失败时间"] = now_text()
        entry["测试失败原因"] = reason
        entry["失败原因"] = reason
        return self.present(entry), f"对抗策略测试未通过：{reason}"

    def test_result(self, values: dict[str, Any]) -> str | None:
        """读取提交里附带的测试结论：passed / failed / None（未给出）。"""
        for field in RESULT_FIELDS:
            text = str(values.get(field) or "").strip()
            if not text:
                continue
            lower = text.lower()
            if any(token in lower for token in ("不通过", "未通过", "失败", "异常")):
                return "failed"
            if any(token in lower for token in PASS_TOKENS):
                return "passed"
        return None

    def _enable(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        result = self.test_result(values)
        if result == "failed":
            return self._finish_test(entry, values, success=False)

        passed_at = entry.get("测试通过时间")
        if result is not None:
            entry["测试结论"] = values.get("测试结论") or values.get("测试结果") or values.get("结果")
            passed_at = now_text()
            entry["测试通过时间"] = passed_at
        if not passed_at:
            reason = "策略测试尚未通过，不能启用"
            entry["测试阻断原因"] = [reason]
            return self.present(entry), f"对抗策略 {entry.get('策略编号') or entry.get('id')} 未通过策略测试，不能启用"

        problems = self.config_failures(entry)
        if problems:
            reason = "；".join(problems)
            entry["status"] = STATUS_INVALID
            entry["pending"] = True
            entry["abnormal"] = True
            entry["测试状态"] = "测试失败"
            entry["测试失败原因"] = reason
            entry["失败原因"] = reason
            return self.present(entry), f"策略配置不满足启用条件：{reason}"

        entry["status"] = STATUS_ENABLED
        entry["pending"] = False
        entry["abnormal"] = False
        entry["测试状态"] = "测试通过"
        entry.pop("测试失败原因", None)
        entry.pop("失败原因", None)
        entry.pop("失效原因", None)
        entry.pop("测试阻断原因", None)
        return self.present(entry), "对抗策略测试通过，已启用"

    def _invalidate(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        reason = _reason(values, "策略已标记失效")
        entry["status"] = STATUS_INVALID
        entry["pending"] = True
        entry["abnormal"] = True
        entry["测试状态"] = "已失效"
        entry["失效原因"] = reason
        entry["失败原因"] = reason
        entry["失效时间"] = now_text()
        return self.present(entry), f"对抗策略已标记失效：{reason}"

    def _reject_message(self, entry: dict[str, Any], current: str, action: str) -> str:
        code = entry.get("策略编号") or entry.get("id")
        if current == STATUS_DRAFT and action in PASS_ACTIONS:
            return f"对抗策略 {code} 尚未提交策略测试，不能直接启用"
        if current == STATUS_INVALID and action in PASS_ACTIONS:
            return f"对抗策略 {code} 已失效，必须重新提交策略测试并通过后才能启用"
        if current == STATUS_TESTING and action in ENABLE_ACTIONS:
            return f"对抗策略 {code} 尚未取得测试通过结论，不能启用"
        if current == STATUS_TESTING and action in SUBMIT_ACTIONS:
            return f"对抗策略 {code} 正在策略测试中，请勿重复提交"
        if current == STATUS_ENABLED and action in PASS_ACTIONS:
            return f"对抗策略 {code} 已处于已启用状态"
        return f"对抗策略 {code} 的「{current}」状态不允许执行「{action}」"
