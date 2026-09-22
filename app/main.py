"""FastAPI 入口：POST /audits、GET /health。"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from .idempotency import (
    AcquireOutcome,
    IdempotencyStore,
    InvalidIdempotencyKey,
    canonical_fingerprint,
    validate_idempotency_key,
)
from .schemas import AuditRequest, AuditResponse, HealthResponse
from .service import audit

IDEMPOTENCY_HEADER = "X-Idempotency-Key"
REPLAY_HEADER = "X-Idempotency-Replayed"

app = FastAPI(
    title="Timeline Audit Service",
    description="跨系统事件时间线核对：差分约束可行性判定与矛盾闭环定位",
    version="1.1.0",
)

# 进程内幂等存储；单 worker 部署下生效（见 Dockerfile / docker-compose.yml）。
idempotency_store = IdempotencyStore()


@app.get("/health", response_model=HealthResponse, tags=["health"])
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/audits",
    response_model=AuditResponse,
    tags=["audits"],
    summary="核对一组带时间范围与间隔约束的事件",
    responses={
        409: {
            "description": "幂等键已被另一内容指纹的请求占用，原结果保持不变",
        }
    },
)
def create_audit(
    payload: AuditRequest,
    idempotency_key: Annotated[
        Optional[str],
        Header(
            alias=IDEMPOTENCY_HEADER,
            description=(
                "幂等键：同一 key 只执行一次求解，重试重放同一结果；"
                "同 key 不同请求体返回 409。不提供则不启用幂等。"
            ),
        ),
    ] = None,
):
    # 不提供 key：保持无幂等的现有行为
    if idempotency_key is None:
        return audit(payload)

    # 非法 key 与非法请求体一样按 422 处理
    try:
        validate_idempotency_key(idempotency_key)
    except InvalidIdempotencyKey as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    fingerprint = canonical_fingerprint(payload)
    outcome, cached_result = idempotency_store.begin(idempotency_key, fingerprint)

    if outcome is AcquireOutcome.REPLAY:
        # 完全相同的状态与结果（同一份 dict 序列化），并标记为重放
        return JSONResponse(
            cached_result,
            headers={REPLAY_HEADER: "true", IDEMPOTENCY_HEADER: idempotency_key},
        )
    if outcome is AcquireOutcome.CONFLICT:
        # 不触碰已有结果
        raise HTTPException(
            status_code=409,
            detail=(
                f"{IDEMPOTENCY_HEADER} 已用于内容指纹不同的另一请求；"
                "请更换幂等键，或使用与首次提交语义相同的请求体重试。"
            ),
        )

    # LEADER：本请求负责真正求解（并发下唯一一次）
    try:
        result = audit(payload)
    except Exception:
        # 业务 422 或意外异常都释放 key，允许客户端修正后用同 key 重试
        idempotency_store.fail(idempotency_key)
        raise
    idempotency_store.complete(idempotency_key, result)
    return JSONResponse(
        result,
        headers={REPLAY_HEADER: "false", IDEMPOTENCY_HEADER: idempotency_key},
    )
