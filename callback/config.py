
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from .models import OUTCOME_GROUP, CampaignType, Group, Outcome, Urgency

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "campaign_rules.yaml"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Window:
    """Khung giờ được gọi trong ngày, tính bằng phút từ 0h. Không hỗ trợ khung qua đêm."""
    start: int
    end: int

    @classmethod
    def parse(cls, text: str) -> Window:
        m = re.fullmatch(r"(\d{2}):(\d{2})-(\d{2}):(\d{2})", str(text).strip())
        if not m:
            raise ConfigError(f"Khung giờ sai định dạng HH:MM-HH:MM: {text!r}")
        h1, m1, h2, m2 = map(int, m.groups())
        start, end = h1 * 60 + m1, h2 * 60 + m2
        if m1 >= 60 or m2 >= 60 or not 0 <= start < end <= 1440:
            raise ConfigError(f"Khung giờ không hợp lệ (cần bắt đầu < kết thúc, trong 00:00-24:00): {text!r}")
        return cls(start, end)

    @property
    def full_day(self) -> bool:
        return self.start == 0 and self.end == 1440

    def __str__(self) -> str:
        return f"{self.start // 60:02d}:{self.start % 60:02d}-{self.end // 60:02d}:{self.end % 60:02d}"


@dataclass(frozen=True)
class LevelConfig:
    max_calls_24h: int
    min_gap_minutes: int
    window: Window
    max_days: float
    zns_after_failures: int
    appointment_max_wait_minutes: int
    max_appointments: int


@dataclass(frozen=True)
class OutcomeConfig:
    delays: tuple[int, ...]
    zns_before: bool = False
    max_per_case: int | None = None


@dataclass(frozen=True)
class CampaignConfig:
    """Một campaign = một lý do gọi, với mức khẩn, mẫu ZNS và mục đích kịch bản riêng."""
    label: str                                # tên hiển thị cho dashboard / ĐTV
    script_purpose: str                       # bot nêu lý do gọi bằng câu này (session controller đọc)
    levels: Mapping[Urgency, LevelConfig]
    zns_templates: Mapping[str, str]          # pre_call / contact / final
    urgent_within_days: int = 0               # loan_due/statement_due: còn <= n ngày -> P2
    overdue_days_p1: int = 0                  # loan_due/statement_due: quá hạn >= n ngày -> P1
    lookahead_days: int = 0                   # job pull: lấy các khoản đến hạn trong n ngày tới
    max_overdue_days: int = 0                 # job pull: quá hạn hơn n ngày -> không còn là việc của bot


@dataclass(frozen=True)
class Config:
    tz: timezone
    docs_min_gap_minutes: int
    rolling_window: timedelta
    per_phone_cap_24h: int
    llm_min_confidence: float
    hotline: str                              # caller ID hiển thị khi bot gọi outbound
    pre_call_push_lead_minutes: int           # push iPay trước cuộc gọi đầu bao lâu
    short_hangup_max_per_case: int            # KH tắt máy sớm (< 5s) tối đa n lần
    alt_switch_after: int
    alt_max_attempts: int
    system_retry_max: int
    system_backoff_minutes: int
    appointment_default_minutes: int
    large_amount_vnd: int
    multi_alert_count: int
    dtv_recheck_minutes: int
    zns_max_per_case: int
    zns_templates: Mapping[str, str]
    opt_out_prompt: str
    levels: Mapping[Urgency, LevelConfig]     # = campaigns[card_fraud].levels (giữ cho code/test cũ)
    outcomes: Mapping[Outcome, OutcomeConfig]
    campaigns: Mapping[CampaignType, CampaignConfig]

    def level(self, campaign: CampaignType, urgency: Urgency) -> LevelConfig:
        return self.campaigns[campaign].levels[urgency]

    def zns_for(self, campaign: CampaignType) -> Mapping[str, str]:
        return self.campaigns[campaign].zns_templates


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path) if path else DEFAULT_PATH
    with open(p, encoding="utf-8") as f:
        return parse_config(yaml.safe_load(f))


def parse_config(raw: dict[str, Any]) -> Config:
    try:
        cfg = _build(raw)
    except ConfigError:
        raise
    except (KeyError, TypeError, ValueError) as e:
        raise ConfigError(f"Thiếu hoặc sai kiểu trường cấu hình: {e!r}") from e
    problems = _validate(cfg)
    if problems:
        raise ConfigError("Cấu hình campaign rules không hợp lệ:\n- " + "\n- ".join(problems))
    return cfg


def _levels(raw_levels: dict[str, Any]) -> dict[Urgency, LevelConfig]:
    return {
        Urgency(k): LevelConfig(
            max_calls_24h=int(v["max_calls_24h"]),
            min_gap_minutes=int(v["min_gap_minutes"]),
            window=Window.parse(v["window"]),
            max_days=float(v["max_days"]),
            zns_after_failures=int(v["zns_after_failures"]),
            appointment_max_wait_minutes=int(v["appointment_max_wait_minutes"]),
            max_appointments=int(v["max_appointments"]),
        )
        for k, v in raw_levels.items()
    }


def _campaigns(raw: dict[str, Any], base_levels: dict[Urgency, LevelConfig],
               base_zns: dict[str, str]) -> dict[CampaignType, CampaignConfig]:
    """card_fraud mặc định dùng `levels` và `zns.templates` ở top-level; campaign khác phải khai báo đủ."""
    out: dict[CampaignType, CampaignConfig] = {}
    for key, v in (raw.get("campaigns") or {}).items():
        ct = CampaignType(key)
        if ct is CampaignType.CARD_FRAUD:
            levels = _levels(v["levels"]) if "levels" in v else base_levels
            zns = dict(v.get("zns_templates") or base_zns)
        else:
            levels = _levels(v["levels"])
            zns = dict(v["zns_templates"])
        out[ct] = CampaignConfig(
            label=str(v["label"]),
            script_purpose=str(v["script_purpose"]),
            levels=levels,
            zns_templates=zns,
            urgent_within_days=int(v.get("urgent_within_days", 0)),
            overdue_days_p1=int(v.get("overdue_days_p1", 0)),
            lookahead_days=int(v.get("lookahead_days", 0)),
            max_overdue_days=int(v.get("max_overdue_days", 0)),
        )
    if CampaignType.CARD_FRAUD not in out:
        out[CampaignType.CARD_FRAUD] = CampaignConfig(
            label="Xác thực giao dịch thẻ nghi gian lận",
            script_purpose=str(raw.get("call_script", {}).get("purpose_card_fraud", "")),
            levels=base_levels, zns_templates=base_zns,
        )
    return out


def _build(raw: dict[str, Any]) -> Config:
    levels = _levels(raw["levels"])
    zns_templates = dict(raw["zns"]["templates"])
    outcomes = {
        Outcome(k): OutcomeConfig(
            delays=tuple(int(x) for x in v["delays"]),
            zns_before=bool(v.get("zns_before", False)),
            max_per_case=None if v.get("max_per_case") is None else int(v["max_per_case"]),
        )
        for k, v in raw["outcomes"].items()
    }
    return Config(
        tz=timezone(timedelta(hours=float(raw["utc_offset_hours"]))),
        docs_min_gap_minutes=int(raw["docs_min_gap_minutes"]),
        rolling_window=timedelta(hours=float(raw["rolling_window_hours"])),
        per_phone_cap_24h=int(raw["per_phone_cap_24h"]),
        llm_min_confidence=float(raw["llm_min_confidence"]),
        hotline=str(raw["hotline"]),
        pre_call_push_lead_minutes=int(raw["pre_call"]["push_lead_minutes"]),
        short_hangup_max_per_case=int(raw["pre_call"]["short_hangup_max_per_case"]),
        alt_switch_after=int(raw["alt_phone"]["switch_after_failures"]),
        alt_max_attempts=int(raw["alt_phone"]["max_attempts"]),
        system_retry_max=int(raw["system_retry"]["max_in_a_row"]),
        system_backoff_minutes=int(raw["system_retry"]["backoff_minutes"]),
        appointment_default_minutes=int(raw["appointment"]["default_minutes"]),
        large_amount_vnd=int(raw["urgency"]["large_amount_vnd"]),
        multi_alert_count=int(raw["urgency"]["multi_alert_count"]),
        dtv_recheck_minutes=int(raw.get("dtv_recheck_minutes", 5)),
        zns_max_per_case=int(raw["zns"]["max_per_case"]),
        zns_templates=zns_templates,
        opt_out_prompt=str(raw.get("call_script", {}).get("opt_out_prompt", "")),
        levels=levels,
        outcomes=outcomes,
        campaigns=_campaigns(raw, levels, zns_templates),
    )


def _validate_levels(cfg: Config, levels: Mapping[Urgency, LevelConfig], prefix: str) -> list[str]:
    errs: list[str] = []
    for u in Urgency:
        if u not in levels:
            errs.append(f"{prefix}thiếu cấu hình mức {u.value}")
    for u, lv in levels.items():
        if lv.min_gap_minutes < cfg.docs_min_gap_minutes:
            errs.append(f"{prefix}{u.value}: min_gap_minutes={lv.min_gap_minutes} < {cfg.docs_min_gap_minutes} phút theo docs")
        if lv.max_calls_24h < 1:
            errs.append(f"{prefix}{u.value}: max_calls_24h phải >= 1")
        if lv.max_days <= 0:
            errs.append(f"{prefix}{u.value}: max_days phải > 0")
        if lv.zns_after_failures < 1:
            errs.append(f"{prefix}{u.value}: zns_after_failures phải >= 1")
        if lv.appointment_max_wait_minutes <= 0 or lv.max_appointments < 0:
            errs.append(f"{prefix}{u.value}: cấu hình hẹn gọi lại không hợp lệ")
    return errs


def _validate_zns_templates(templates: Mapping[str, str], prefix: str) -> list[str]:
    errs: list[str] = []
    for key in ("pre_call", "contact", "final"):
        text = templates.get(key)
        if not text:
            errs.append(f"{prefix}thiếu mẫu ZNS '{key}'")
        elif "{" in text or "}" in text:
            errs.append(f"{prefix}mẫu ZNS '{key}' không được có biến - KHÔNG chứa chi tiết giao dịch")
    return errs


def _validate(cfg: Config) -> list[str]:
    errs: list[str] = []
    errs += _validate_levels(cfg, cfg.levels, "")
    for ct in CampaignType:
        if ct not in cfg.campaigns:
            errs.append(f"thiếu cấu hình campaign '{ct.value}'")
    for ct, camp in cfg.campaigns.items():
        prefix = f"campaign {ct.value}: "
        errs += _validate_levels(cfg, camp.levels, prefix)
        errs += _validate_zns_templates(camp.zns_templates, prefix)
        if not camp.label.strip() or not camp.script_purpose.strip():
            errs.append(prefix + "thiếu label hoặc script_purpose")
        if ct is not CampaignType.CARD_FRAUD:
            if camp.urgent_within_days < 0 or camp.overdue_days_p1 < 1:
                errs.append(prefix + "urgent_within_days phải >= 0 và overdue_days_p1 phải >= 1")
            if camp.lookahead_days < 0 or camp.max_overdue_days < camp.overdue_days_p1:
                errs.append(prefix + "lookahead_days phải >= 0 và max_overdue_days phải >= overdue_days_p1")

    need = {o for o, (g, _) in OUTCOME_GROUP.items() if g in (Group.RETRY_CUSTOMER, Group.RETRY_SYSTEM)}
    for o in sorted(need - set(cfg.outcomes), key=lambda o: o.value):
        errs.append(f"thiếu khoảng chờ cho outcome '{o.value}'")
    for o, oc in cfg.outcomes.items():
        if not oc.delays or any(d <= 0 for d in oc.delays):
            errs.append(f"outcome '{o.value}': delays phải là danh sách số phút > 0")
        if oc.max_per_case is not None and oc.max_per_case < 1:
            errs.append(f"outcome '{o.value}': max_per_case phải >= 1")

    errs += _validate_zns_templates(cfg.zns_templates, "")
    if cfg.zns_max_per_case < 1:
        errs.append("zns.max_per_case phải >= 1")
    if not cfg.hotline.strip():
        errs.append("thiếu hotline (số hiển thị khi bot gọi outbound)")
    if cfg.pre_call_push_lead_minutes < 0:
        errs.append("pre_call.push_lead_minutes phải >= 0")
    if cfg.short_hangup_max_per_case < 1:
        errs.append("pre_call.short_hangup_max_per_case phải >= 1")
    if not cfg.opt_out_prompt.strip():
        errs.append("thiếu call_script.opt_out_prompt (chuẩn quốc tế yêu cầu bot phải cho KH chọn opt-out)")
    if not 0 < cfg.llm_min_confidence <= 1:
        errs.append("llm_min_confidence phải trong (0, 1]")
    if cfg.per_phone_cap_24h < 1:
        errs.append("per_phone_cap_24h phải >= 1")
    if cfg.dtv_recheck_minutes < 1:
        errs.append("dtv_recheck_minutes phải >= 1")
    if cfg.alt_switch_after < 1 or cfg.alt_max_attempts < 0:
        errs.append("cấu hình alt_phone không hợp lệ")
    if cfg.system_retry_max < 0 or cfg.system_backoff_minutes <= 0:
        errs.append("cấu hình system_retry không hợp lệ")
    return errs
