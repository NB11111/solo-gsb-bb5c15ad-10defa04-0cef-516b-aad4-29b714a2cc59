"""RFC 3339 (UTC) 时间与 ISO 8601 时长的解析/格式化。

内部统一用「自 Unix 纪元起的纳秒整数」表示时刻与时长，
全程整数运算，避免浮点误差导致约束判断不稳定。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

NS_PER_US = 1_000
NS_PER_MS = 1_000_000
NS_PER_SECOND = 1_000_000_000
NS_PER_MINUTE = 60 * NS_PER_SECOND
NS_PER_HOUR = 60 * NS_PER_MINUTE
NS_PER_DAY = 24 * NS_PER_HOUR
NS_PER_WEEK = 7 * NS_PER_DAY

# RFC 3339 date-time（仅接受 UTC：Z 或数值偏移 ±00:00；
# 严格遵循 ABNF，分隔符 T 与 Z 均为大写）
_RFC3339_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T"
    r"(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?"
    r"(?:Z|[+-]00:?00)$"
)

# ISO 8660 durations（P[nW] 或 P[n]DT[n]H[n]M[n]S），
# 年/月语义含糊（长度不固定），显式拒绝。
_DURATION_RE = re.compile(
    r"^P"
    r"(?:(?P<weeks>\d+(?:[.,]\d+)?)W)?"
    r"(?:(?P<days>\d+(?:[.,]\d+)?)D)?"
    r"(?:T"
    r"(?:(?P<hours>\d+(?:[.,]\d+)?)H)?"
    r"(?:(?P<minutes>\d+(?:[.,]\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:[.,]\d+)?)S)?"
    r")?$"
)

_DATETIME_MIN = datetime(1, 1, 1, tzinfo=timezone.utc)
_DATETIME_MAX = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


def parse_rfc3339_utc(value: str) -> int:
    """把 RFC 3339 UTC 时间字符串解析为纪元起纳秒。

    仅接受 UTC（``Z`` 或 ``+00:00``）；非 UTC 时区、历法不存在的日期、
    超过纳秒精度的小数秒一律拒绝。
    """
    if not isinstance(value, str):
        raise ValueError("时间必须是 RFC 3339 字符串")
    match = _RFC3339_RE.match(value)
    if not match:
        raise ValueError(
            f"时间 {value!r} 不是合法的 RFC 3339 UTC 时间"
            "（示例：2026-09-21T08:00:00Z）"
        )
    year, month, day, hour, minute, second = (int(g) for g in match.groups()[:6])
    frac = match.group(7)
    if frac is not None and len(frac) > 9:
        raise ValueError(f"时间 {value!r} 小数秒精度超过纳秒，无法精确处理")
    nanos = int(frac.ljust(9, "0")) if frac else 0
    try:
        dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"时间 {value!r} 不是合法的日历日期时间：{exc}") from exc
    if not _DATETIME_MIN <= dt <= _DATETIME_MAX:
        raise ValueError(f"时间 {value!r} 超出可表示范围（0001-9999 年）")
    return int(dt.timestamp()) * NS_PER_SECOND + nanos


def format_rfc3339_utc(nanos: int) -> str:
    """把纪元起纳秒格式化为 ``...Z`` 形式的 RFC 3339 UTC 字符串。"""
    seconds, rem = divmod(nanos, NS_PER_SECOND)
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    # 手动补齐 4 位年份（strftime 的 %Y 在 Linux 上不补零）
    base = f"{dt.year:04d}" + dt.strftime("-%m-%dT%H:%M:%S")
    if rem:
        return f"{base}.{rem:09d}Z"
    return f"{base}Z"


def _decimal_to_nanos(value: str, unit_nanos: int) -> int:
    """把非负十进制数字串按单位换算成整数纳秒（小数部分向下取整到纳秒）。"""
    normalized = value.replace(",", ".")
    if "." not in normalized:
        return int(normalized) * unit_nanos
    whole, frac = normalized.split(".", 1)
    total = int(whole or "0") * unit_nanos
    if frac:
        if len(frac) > 9:
            frac = frac[:9]
        total += int(frac) * unit_nanos // 10**len(frac)
    return total


def parse_iso8601_duration(value: str) -> int:
    """把 ISO 8601 时长解析为非负整数纳秒；拒绝年/月与负数。"""
    if not isinstance(value, str) or not value:
        raise ValueError("时长必须是非空 ISO 8601 字符串")
    text = value.strip()
    if text.startswith("-"):
        raise ValueError(f"时长 {value!r} 不能为负")
    match = _DURATION_RE.match(text)
    if not match or text in {"P", "PT"}:
        raise ValueError(
            f"时长 {value!r} 不是合法的 ISO 8601 duration"
            "（示例：PT2H30M、P1D、PT0.5S；不支持年/月）"
        )
    groups = match.groupdict()
    total = 0
    total += _decimal_to_nanos(groups["weeks"], NS_PER_WEEK) if groups["weeks"] else 0
    total += _decimal_to_nanos(groups["days"], NS_PER_DAY) if groups["days"] else 0
    total += _decimal_to_nanos(groups["hours"], NS_PER_HOUR) if groups["hours"] else 0
    total += (
        _decimal_to_nanos(groups["minutes"], NS_PER_MINUTE)
        if groups["minutes"]
        else 0
    )
    total += (
        _decimal_to_nanos(groups["seconds"], NS_PER_SECOND)
        if groups["seconds"]
        else 0
    )
    return total


def format_iso8601_duration(nanos: int) -> str:
    """把非负整数纳秒格式化为最简 ISO 8601 duration（天/时/分/秒）。"""
    if nanos < 0:
        raise ValueError("时长不能为负")
    days, rem = divmod(nanos, NS_PER_DAY)
    hours, rem = divmod(rem, NS_PER_HOUR)
    minutes, rem = divmod(rem, NS_PER_MINUTE)
    seconds, sub_ns = divmod(rem, NS_PER_SECOND)

    parts = ["P"]
    if days:
        parts.append(f"{days}D")
    if hours or minutes or seconds or sub_ns:
        parts.append("T")
        if hours:
            parts.append(f"{hours}H")
        if minutes:
            parts.append(f"{minutes}M")
        if seconds or sub_ns:
            if sub_ns:
                frac = str(sub_ns).rstrip("0")
                parts.append(f"{seconds}.{frac}S")
            else:
                parts.append(f"{seconds}S")
    if len(parts) == 1:
        return "PT0S"
    return "".join(parts)
