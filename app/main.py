"""FastAPI 入口：POST /audits、GET /health。"""
from __future__ import annotations

from fastapi import Depends, FastAPI, Request, Response

from . import idempotency
from .schemas import AuditRequest, AuditResponse, HealthResponse
from .service import audit

app = FastAPI(
    title="Timeline Audit Service",
    description="跨系统事件时间线核对：差分约束可行性判定与矛盾闭环定位",
    version="1.1.0",
)

# 幂等头在端点内自行校验（允许省略、可定制 422 文案），
# 这里只补充 OpenAPI 元数据，使 /docs 与实际行为一致。
_IDEMPOTENCY_PARAM = {
    "name": idempotency.IDEMPOTENCY_HEADER,
    "in": "header",
    "required": False,
    "description": (
        "客户端为一次审计请求生成的幂等键。同键同内容的重试与并发请求"
        "只求解一次，返回完全相同的结果；同键不同内容返回 409。"
        "省略时每次请求照常求解。"
    ),
    "schema": {"type": "string", "maxLength": idempotency.MAX_KEY_LENGTH},
}
_REPLAY_HEADER_SCHEMA = {
    "description": (
        "携带幂等键时恒有此头：`false` 表示本次为首次执行，"
        "`true` 表示回放同键首次请求的结果。"
    ),
    "schema": {"type": "string", "enum": ["true", "false"]},
}


async def _raw_body(request: Request) -> bytes:
    """读取原始请求体字节。

    AuditRequest 的 JSON 解析与本函数共用 Starlette 的 body 缓存，
    不会重复读流；放在 async 依赖里是为了让同步端点继续在线程池中执行。
    """
    return await request.body()


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
            "description": (
                "幂等冲突：同一 X-Idempotency-Key 此前已用于另一份请求内容"
                "（规范化指纹不同）。原结果保持不变。"
            ),
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {"detail": {"type": "string"}},
                        "required": ["detail"],
                    }
                }
            },
        },
    },
    openapi_extra={
        "parameters": [_IDEMPOTENCY_PARAM],
        "responses": {
            # 200 与回放的 4xx 都带 X-Idempotency-Replayed
            "200": {"headers": {idempotency.REPLAY_HEADER: _REPLAY_HEADER_SCHEMA}},
            "422": {"headers": {idempotency.REPLAY_HEADER: _REPLAY_HEADER_SCHEMA}},
        },
    },
)
def create_audit(
    request: Request,
    payload: AuditRequest,
    response: Response,
    raw_body: bytes = Depends(_raw_body),
) -> dict:
    key = idempotency.parse_idempotency_key(request.headers)
    if key is None:
        # 未提供幂等键：保持原有行为，求解每次请求照常执行
        return audit(payload)

    fingerprint = idempotency.canonical_fingerprint(raw_body)
    body, replayed = idempotency.execute(
        key, fingerprint, lambda: audit(payload)
    )
    response.headers[idempotency.REPLAY_HEADER] = (
        "true" if replayed else "false"
    )
    return body
