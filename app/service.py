"""请求校验（引用完整性、重复 id）与求解结果编排。"""
from __future__ import annotations

from fastapi import HTTPException

from . import solver
from .schemas import AuditRequest, CycleStep
from .timeutils import format_iso8601_duration, format_rfc3339_utc

GENERATED_PREFIX = "@"


def _validation_error(message: str) -> HTTPException:
    # 422 与 FastAPI/Pydantic 的请求校验错误保持一致
    return HTTPException(status_code=422, detail=message)


def audit(payload: AuditRequest) -> dict:
    events = payload.events
    constraints = payload.constraints

    # 1) 重复事件 id / 保留前缀
    seen_ids: set[str] = set()
    for index, event in enumerate(events):
        event_id = event.id
        if event_id.startswith(GENERATED_PREFIX):
            raise _validation_error(
                f"events[{index}].id={event_id!r} 非法：@ 前缀为系统保留"
            )
        if event_id in seen_ids:
            raise _validation_error(f"事件 id 重复：{event_id!r}")
        seen_ids.add(event_id)

    # 2) 约束引用必须存在；约束 id 不重复、不占用保留前缀
    seen_constraint_ids: set[str] = set()
    norm_constraints: list[
        tuple[str, str | None, int, str, str, int]
    ] = []
    for index, constraint in enumerate(constraints):
        if constraint.before not in seen_ids:
            raise _validation_error(
                f"constraints[{index}].before={constraint.before!r} 未在 events 中声明"
            )
        if constraint.after not in seen_ids:
            raise _validation_error(
                f"constraints[{index}].after={constraint.after!r} 未在 events 中声明"
            )
        if constraint.id is not None:
            if constraint.id.startswith(GENERATED_PREFIX):
                raise _validation_error(
                    f"constraints[{index}].id={constraint.id!r} 非法：@ 前缀为系统保留"
                )
            if constraint.id in seen_constraint_ids:
                raise _validation_error(f"约束 id 重复：{constraint.id!r}")
            seen_constraint_ids.add(constraint.id)
        norm_constraints.append(
            (
                constraint.kind,
                constraint.id,
                index,
                constraint.before,
                constraint.after,
                constraint.gap,
            )
        )

    # 3) 构造差分约束图并求解
    event_tuples = [(e.id, e.earliest, e.latest) for e in events]
    edges = solver.build_graph(event_tuples, norm_constraints)
    cycle, latest_ns, earliest_ns = solver.solve(len(events), edges)

    # 4a) 可行：输出每个事件收紧后的时间范围（保持输入顺序）
    if cycle is None:
        ranges = {
            event.id: {
                "earliest": (
                    None if earliest_ns[i] is None else format_rfc3339_utc(earliest_ns[i])
                ),
                "latest": (
                    None if latest_ns[i] is None else format_rfc3339_utc(latest_ns[i])
                ),
            }
            for i, event in enumerate(events)
        }
        return {"feasible": True, "ranges": ranges}

    # 4b) 不可行：把负权环映射回原始约束，组成矛盾闭环
    steps = [_cycle_step(edges[i]) for i in cycle.edge_indices]
    count = len(steps)
    explanation = (
        f"闭环包含 {count} 条约束，沿环相加得到 "
        f"0 <= {cycle.total_weight} ns；右端为负数，没有任何时间赋值能同时满足，"
        "因此该组记录不可能同时成立。"
    )
    return {
        "feasible": False,
        "contradiction": {
            "steps": steps,
            "total_weight_ns": cycle.total_weight,
            "explanation": explanation,
        },
    }


def _constraint_ref(desc: solver.ConstraintDescriptor) -> str:
    if desc.kind in (solver.AT_LEAST, solver.AT_MOST):
        return desc.constraint_id or f"@constraint-{desc.ref_index}"
    return f"@{desc.kind}:{desc.event_id}"


def _cycle_step(edge: solver.Edge) -> CycleStep:
    desc = edge.descriptor
    if desc.kind == solver.EARLIEST:
        detail = (
            f"事件 {desc.event_id!r} 不早于 {format_rfc3339_utc(desc.time_ns)}"
        )
    elif desc.kind == solver.LATEST:
        detail = (
            f"事件 {desc.event_id!r} 不晚于 {format_rfc3339_utc(desc.time_ns)}"
        )
    elif desc.kind == solver.AT_LEAST:
        detail = (
            f"事件 {desc.after!r} 比 {desc.before!r} 晚至少 "
            f"{format_iso8601_duration(desc.gap_ns)}"
        )
    else:  # AT_MOST
        detail = (
            f"事件 {desc.after!r} 比 {desc.before!r} 晚至多 "
            f"{format_iso8601_duration(desc.gap_ns)}"
        )
    return CycleStep(
        kind=desc.kind,
        ref=_constraint_ref(desc),
        source=desc.source_ref,
        target=desc.target_ref,
        detail=detail,
        weight_ns=edge.weight,
    )
