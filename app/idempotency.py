"""幂等提交：规范化指纹与进程内并发去重存储。

客户端通过 ``X-Idempotency-Key`` 标识一次审计请求；服务端对校验通过后的
请求模型做规范化（canonical）JSON 序列化并计算 SHA-256 指纹：

* 同一 key + 同一指纹：并发下只真正求解一次，其余请求等待并重放同一结果；
* 同一 key + 不同指纹：返回冲突（调用方映射为 409），原结果保持不变；
* 求解过程中若请求触发 422（业务校验失败），该 key 立即释放，
  客户端修正请求体后允许用同一个 key 重新提交。

存储为进程内、线程安全；部署时需保证单进程（单 worker），见 Dockerfile。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .schemas import AuditRequest

# 可见 ASCII（0x21..0x7E），长度 1..255；不允许空格与控制字符，
# 避免因首尾空白等不可见差异让两个“相同”的 key 无法命中。
_KEY_PATTERN = re.compile(r"[\x21-\x7e]{1,255}")


class InvalidIdempotencyKey(ValueError):
    """``X-Idempotency-Key`` 不合法（接口层应映射为 422）。"""


def validate_idempotency_key(key: str) -> None:
    if not isinstance(key, str) or _KEY_PATTERN.fullmatch(key) is None:
        raise InvalidIdempotencyKey(
            "X-Idempotency-Key 必须是 1-255 个可见 ASCII 字符"
            "（不含空格与控制字符）"
        )


def canonical_fingerprint(payload: AuditRequest) -> str:
    """对规范化后的请求计算 SHA-256 指纹（十六进制）。

    规范化基于 Pydantic 校验后的模型：时间/间隔已折算为纳秒整数，
    因此键顺序、空白、``Z`` 与 ``+00:00``、``PT1H`` 与 ``PT60M``
    等等价写法得到同一指纹；events/constraints 的列表顺序保留
    （顺序不同视为不同请求）。
    """
    normalized = payload.model_dump(mode="json")
    canonical = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _State(Enum):
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"


@dataclass
class _Entry:
    state: _State
    fingerprint: str
    result: Optional[dict] = None


class AcquireOutcome(str, Enum):
    LEADER = "leader"  # 本请求负责真正求解
    REPLAY = "replay"  # 已有完成结果，直接重放
    CONFLICT = "conflict"  # 同 key 已被另一指纹占用


class IdempotencyStore:
    """线程安全的进程内幂等存储。

    每个 key 对应一个条目；``begin`` 在单条件变量上自旋等待在途请求，
    保证同一 key 的并发请求只有一个 LEADER 进入求解。
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._entries: dict[str, _Entry] = {}

    def begin(
        self, key: str, fingerprint: str
    ) -> tuple[AcquireOutcome, Optional[dict]]:
        """登记一次请求，返回 (结局, 已完成结果|None)。

        拿到 LEADER 后必须最终调用 ``complete`` 或 ``fail`` 之一，
        否则同 key 的后续请求会一直等待。
        """
        with self._cond:
            while True:
                entry = self._entries.get(key)
                if entry is None or entry.state is _State.FAILED:
                    # 首次提交，或上一次以 422 失败、key 已释放：抢做 LEADER。
                    self._entries[key] = _Entry(_State.IN_PROGRESS, fingerprint)
                    return AcquireOutcome.LEADER, None
                if entry.state is _State.DONE:
                    if entry.fingerprint == fingerprint:
                        return AcquireOutcome.REPLAY, entry.result
                    return AcquireOutcome.CONFLICT, None
                # 同 key 的 LEADER 仍在求解：等待其 complete/fail 后重判。
                self._cond.wait()

    def complete(self, key: str, result: dict) -> None:
        """LEADER 求解成功：固化结果并放行所有等待者。"""
        with self._cond:
            entry = self._entries.get(key)
            if entry is None or entry.state is not _State.IN_PROGRESS:
                return  # 防御：正常流程不会走到
            entry.state = _State.DONE
            entry.result = result
            self._cond.notify_all()

    def fail(self, key: str) -> None:
        """LEADER 求解失败（业务 422 等）：释放 key，允许重试抢占。"""
        with self._cond:
            entry = self._entries.get(key)
            if entry is not None and entry.state is _State.IN_PROGRESS:
                entry.state = _State.FAILED
                self._cond.notify_all()
