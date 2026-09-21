"""FastAPI 入口：POST /audits、GET /health。"""
from __future__ import annotations

from fastapi import FastAPI

from .schemas import AuditRequest, AuditResponse, HealthResponse
from .service import audit

app = FastAPI(
    title="Timeline Audit Service",
    description="跨系统事件时间线核对：差分约束可行性判定与矛盾闭环定位",
    version="1.0.0",
)


@app.get("/health", response_model=HealthResponse, tags=["health"])
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/audits",
    response_model=AuditResponse,
    tags=["audits"],
    summary="核对一组带时间范围与间隔约束的事件",
)
def create_audit(payload: AuditRequest) -> dict:
    return audit(payload)
