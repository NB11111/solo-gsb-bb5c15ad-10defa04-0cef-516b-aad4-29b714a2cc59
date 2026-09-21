"""请求/响应模型与自定义校验。"""
from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field
from pydantic.functional_validators import BeforeValidator

from .timeutils import parse_iso8601_duration, parse_rfc3339_utc

# 先把字符串解析为纳秒整数（解析失败转 422），再交给 int 内核。
Rfc3339Utc = Annotated[int, BeforeValidator(parse_rfc3339_utc)]
IsoDuration = Annotated[int, BeforeValidator(parse_iso8601_duration)]


class EventIn(BaseModel):
    id: str = Field(..., min_length=1, description="事件唯一标识")
    earliest: Optional[Rfc3339Utc] = Field(
        default=None, description="最早可能发生时间（RFC 3339 UTC）"
    )
    latest: Optional[Rfc3339Utc] = Field(
        default=None, description="最晚可能发生时间（RFC 3339 UTC）"
    )


class AtLeastConstraint(BaseModel):
    kind: Literal["at_least"]
    id: Optional[str] = Field(default=None, description="约束的可选编号，便于引用")
    before: str = Field(..., min_length=1, description="较早事件的 id")
    after: str = Field(..., min_length=1, description="较晚事件的 id")
    gap: IsoDuration = Field(
        ..., description="至少间隔（ISO 8601 duration，如 PT30M、P1D）"
    )


class AtMostConstraint(BaseModel):
    kind: Literal["at_most"]
    id: Optional[str] = Field(default=None, description="约束的可选编号，便于引用")
    before: str = Field(..., min_length=1, description="较早事件的 id")
    after: str = Field(..., min_length=1, description="较晚事件的 id")
    gap: IsoDuration = Field(
        ..., description="至多间隔（ISO 8601 duration，如 PT2H、PT0.5S）"
    )


ConstraintIn = Annotated[
    Union[AtLeastConstraint, AtMostConstraint],
    Field(discriminator="kind"),
]


class AuditRequest(BaseModel):
    events: list[EventIn] = Field(default_factory=list)
    constraints: list[ConstraintIn] = Field(default_factory=list)


class TightRange(BaseModel):
    earliest: Optional[str] = Field(
        ..., description="收紧后的最早时间；无下界时为 null"
    )
    latest: Optional[str] = Field(
        ..., description="收紧后的最晚时间；无上界时为 null"
    )


class FeasibleResponse(BaseModel):
    feasible: Literal[True]
    ranges: dict[str, TightRange]


class CycleStep(BaseModel):
    """矛盾闭环中的一步，即一条原始约束。"""

    kind: Literal["earliest", "latest", "at_least", "at_most"]
    ref: str = Field(
        ..., description="约束引用：用户提供的 id，或按序号生成的 @constraint-N"
    )
    source: str = Field(..., description="不等式左节点（事件 id 或 @epoch）")
    target: str = Field(..., description="不等式右节点（事件 id 或 @epoch）")
    detail: str = Field(..., description="还原为自然语言的原约束")
    weight_ns: int = Field(..., description="差分不等式 x[target]-x[source] 的上界")


class Contradiction(BaseModel):
    steps: list[CycleStep]
    total_weight_ns: int = Field(
        ..., description="闭环权重之和；为负即各不等式相加后得到矛盾（0<=-正数）"
    )
    explanation: str


class InfeasibleResponse(BaseModel):
    feasible: Literal[False]
    contradiction: Contradiction


AuditResponse = Union[FeasibleResponse, InfeasibleResponse]


class HealthResponse(BaseModel):
    status: Literal["ok"]
