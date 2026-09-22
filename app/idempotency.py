"""POST /audits 的幂等提交支持。

客户端通过 ``X-Idempotency-Key`` 请求头标识一次审计请求，服务端对
规范化（canonical JSON）后的请求体计算 SHA-256 指纹：

* key 相同、指纹相同：并发请求与重试都只执行一次求解，其余请求回放
  首次执行留下的状态码与响应体（包括 422 校验失败），不重复生成矛盾闭环；
* key 相同、指纹不同：返回 409，已有结果保持不变；
* 不携带该头：调用方完全走原有路径，不经过本模块。

记录保存在进程内存中（字典 + 每条记录一个 :class:`threading.Event`），
因此幂等保证的作用域是单个进程；部署时需固定单 worker
（见 Dockerfile / docker-compose.yml 的 ``--workers 1``）。
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from fastapi import HTTPException
from starlette.datastructures import Headers

#: 幂等键请求头与回放标记响应头
IDEMPOTENCY_HEADER = "X-Idempotency-Key"
REPLAY_HEADER = "X-Idempotency-Replayed"

#: 幂等键长度上限（去除首尾空白后）
MAX_KEY_LENGTH = 255

#: 并发重试等待首次请求完成的最长秒数；超时返回 409，避免无限挂起
WAIT_TIMEOUT_SECONDS = 30.0

#: 内存中最多保留多少条已完成的幂等记录，超出后淘汰最旧的已完成记录
MAX_STORED_RECORDS = 10_000


def _invalid_key(message: str) -> HTTPException:
    # 与 service._validation_error 一致：422 + 纯文本 detail
    return HTTPException(status_code=422, detail=message)


def parse_idempotency_key(headers: Headers) -> Optional[str]:
    """从请求头读取并校验幂等键；未提供时返回 None，非法时抛 422。

    规则：去除首尾空白后非空、长度不超过 255、仅含可打印 ASCII 字符，
    且只能通过一个同名请求头提供。
    """
    if IDEMPOTENCY_HEADER not in headers:
        return None
    if len(headers.getlist(IDEMPOTENCY_HEADER)) > 1:
        raise _invalid_key(f"{IDEMPOTENCY_HEADER} 请求头只能提供一个")
    key = headers[IDEMPOTENCY_HEADER].strip()
    if not key:
        raise _invalid_key(f"{IDEMPOTENCY_HEADER} 请求头不能为空")
    if len(key) > MAX_KEY_LENGTH:
        raise _invalid_key(
            f"{IDEMPOTENCY_HEADER} 长度不能超过 {MAX_KEY_LENGTH} 个字符"
        )
    if any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in key):
        raise _invalid_key(
            f"{IDEMPOTENCY_HEADER} 只能包含可打印 ASCII 字符（含内部空格）"
        )
    return key


def canonical_fingerprint(raw_body: bytes) -> str:
    """对原始 JSON 请求体做规范化并返回 SHA-256 十六进制指纹。

    规范化规则：递归按字典序排列对象键、去除所有多余空白、UTF-8 编码。
    因此仅对象键顺序或排版空白不同的请求视为同一请求；而 ``1`` 与
    ``1.0`` 这类数字字面量差异仍会得到不同指纹。
    """
    try:
        data = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        # 正常流程下请求体已通过 AuditRequest 校验，这里只是防御性处理
        raise HTTPException(status_code=422, detail=f"请求体不是合法 JSON：{exc}")
    canonical = json.dumps(
        data,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class _Record:
    """一次幂等执行的结果槽位。"""

    fingerprint: str
    completed: threading.Event = field(default_factory=threading.Event)
    status_code: Optional[int] = None
    body: Any = None  # 200 响应体
    detail: Any = None  # 非 200 响应的 detail
    abandoned: bool = False  # 首请求因非预期错误失败，槽位已作废


_registry_lock = threading.Lock()
_registry: dict[str, _Record] = {}


def _finish(
    record: _Record, status_code: int, body: Any, detail: Any
) -> None:
    record.status_code = status_code
    record.body = body
    record.detail = detail
    # 先写结果再置位：等待方从 wait() 返回即可读到完整记录
    record.completed.set()


def _evict_oldest() -> None:
    """超出容量时按插入顺序淘汰最旧的**已完成**记录。

    进行中的记录不淘汰；容量上限远高于单个 key 并发窗口内的等待者数量，
    淘汰只影响之后到来的新重试（会重新执行一次）。
    """
    with _registry_lock:
        if len(_registry) <= MAX_STORED_RECORDS:
            return
        for key in list(_registry):
            if len(_registry) <= MAX_STORED_RECORDS:
                break
            if _registry[key].completed.is_set() and not _registry[key].abandoned:
                del _registry[key]


def _abandon(key: str, record: _Record) -> None:
    with _registry_lock:
        # 仅当槽位仍是自己时移除，避免删掉等待方重新竞争出的新记录
        if _registry.get(key) is record:
            del _registry[key]
    record.abandoned = True
    record.completed.set()


def execute(
    key: str, fingerprint: str, worker: Callable[[], dict]
) -> tuple[dict, bool]:
    """在幂等保护下执行 ``worker``，返回 ``(响应体, 是否为回放)``。

    * 首个请求（leader）执行 ``worker`` 并记录结果；
    * 同 key 同指纹的并发/重试请求等待后回放原结果，回放失败响应时抛出
      带 ``X-Idempotency-Replayed: true`` 的同状态 HTTPException；
    * 同 key 不同指纹抛出 409，且不改动原记录；
    * leader 自身的 4xx（HTTPException）会被记录后原样抛出（首请求
      响应标记 ``false``）；其他异常作废槽位后抛出，允许后续请求重试。
    """
    while True:
        with _registry_lock:
            existing = _registry.get(key)
            if existing is None:
                record = _Record(fingerprint=fingerprint)
                _registry[key] = record
                is_leader = True
            else:
                record = existing
                is_leader = False

        if is_leader:
            try:
                body = worker()
            except HTTPException as exc:
                _finish(record, exc.status_code, None, exc.detail)
                _evict_oldest()
                headers = dict(exc.headers or {})
                headers.setdefault(REPLAY_HEADER, "false")
                exc.headers = headers
                raise
            except BaseException:
                # 非预期异常（含求解器内部错误）：不缓存，腾出 key 供重试
                _abandon(key, record)
                raise
            _finish(record, 200, body, None)
            _evict_oldest()
            return body, False

        # 等待方
        if record.fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"幂等键 {key!r} 已用于另一份请求内容（指纹不一致），"
                    "原结果保持不变；如确需发起新请求，请更换新的幂等键。"
                ),
            )
        if not record.completed.wait(WAIT_TIMEOUT_SECONDS):
            raise HTTPException(
                status_code=409,
                detail=f"幂等键 {key!r} 的首次请求仍在处理中，请稍后重试",
            )
        if record.abandoned:
            # leader 非预期失败、槽位已作废，重新竞争成为执行者或等待新 leader
            continue
        if record.status_code == 200:
            return record.body, True
        raise HTTPException(
            status_code=record.status_code,
            detail=record.detail,
            headers={REPLAY_HEADER: "true"},
        )


def _reset() -> None:
    """清空全部幂等记录（仅供测试使用）。"""
    with _registry_lock:
        _registry.clear()
