"""差分约束系统（difference constraints）。

每条约束都写成标准形式 ``x[target] - x[source] <= weight``，
对应有向边 ``source -> target``，权为 ``weight``。

* 可行 ⇔ 图中没有负权环（Bellman-Ford 判定）；
* 可行时，对每个节点 v：
    latest[v]  = dist(ZERO, v)        —— x[v] 能取到的最大值
    earliest[v] = -dist(v, ZERO)      —— x[v] 能取到的最小值
  不可达即对应方向无界（None）。
* 不可行时，负权环本身就是一条由原约束组成的矛盾闭环。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# 虚拟参考节点的下标：事件节点为 0..n-1，参考节点固定为 n。
# x[zero] = 0（Unix 纪元）。
def zero_node(n: int) -> int:
    return n


EPOCH_ID: str = "@epoch"

# kind 取值
EARLIEST = "earliest"
LATEST = "latest"
AT_LEAST = "at_least"
AT_MOST = "at_most"


@dataclass(frozen=True)
class ConstraintDescriptor:
    """边对应的原始约束，用于把结果映射回用户输入。"""

    kind: str
    constraint_id: Optional[str]
    ref_index: int
    source_ref: str  # 边起点的对外名称（事件 id 或 @epoch）
    target_ref: str
    event_id: Optional[str] = None  # earliest/latest 作用的事件
    time_ns: Optional[int] = None  # earliest/latest 的绝对时刻
    before: Optional[str] = None
    after: Optional[str] = None
    gap_ns: Optional[int] = None  # at_least/at_most 的间隔（纳秒，非负）


@dataclass(frozen=True)
class Edge:
    source: int
    target: int
    weight: int
    descriptor: ConstraintDescriptor


@dataclass(frozen=True)
class ContradictionCycle:
    edge_indices: list[int]
    total_weight: int


def build_graph(
    events: list[tuple[str, Optional[int], Optional[int]]],
    constraints: list[
        tuple[
            str,  # kind
            Optional[str],  # id
            int,  # ref_index
            str,  # before
            str,  # after
            int,  # gap_ns
        ]
    ],
) -> list[Edge]:
    """根据校验过的输入构造差分约束图。

    events 元素为 (id, earliest_ns|None, latest_ns|None)；
    constraints 元素为 (kind, id, ref_index, before, after, gap_ns)。
    """
    index_of = {event_id: i for i, (event_id, _, _) in enumerate(events)}
    zero = zero_node(len(events))
    edges: list[Edge] = []

    for i, (event_id, earliest_ns, latest_ns) in enumerate(events):
        if earliest_ns is not None:
            # x[i] >= earliest  ⇔  x[zero] - x[i] <= -earliest
            edges.append(
                Edge(
                    i,
                    zero,
                    -earliest_ns,
                    ConstraintDescriptor(
                        kind=EARLIEST,
                        constraint_id=None,
                        ref_index=-1,
                        source_ref=event_id,
                        target_ref=EPOCH_ID,
                        event_id=event_id,
                        time_ns=earliest_ns,
                    ),
                )
            )
        if latest_ns is not None:
            # x[i] <= latest  ⇔  x[i] - x[zero] <= latest
            edges.append(
                Edge(
                    zero,
                    i,
                    latest_ns,
                    ConstraintDescriptor(
                        kind=LATEST,
                        constraint_id=None,
                        ref_index=-1,
                        source_ref=EPOCH_ID,
                        target_ref=event_id,
                        event_id=event_id,
                        time_ns=latest_ns,
                    ),
                )
            )

    for kind, constraint_id, ref_index, before, after, gap_ns in constraints:
        u = index_of[before]
        v = index_of[after]
        if kind == AT_LEAST:
            # after - before >= gap  ⇔  before - after <= -gap
            edges.append(
                Edge(
                    v,
                    u,
                    -gap_ns,
                    ConstraintDescriptor(
                        kind=AT_LEAST,
                        constraint_id=constraint_id,
                        ref_index=ref_index,
                        source_ref=after,
                        target_ref=before,
                        before=before,
                        after=after,
                        gap_ns=gap_ns,
                    ),
                )
            )
        else:  # AT_MOST
            # after - before <= gap
            edges.append(
                Edge(
                    u,
                    v,
                    gap_ns,
                    ConstraintDescriptor(
                        kind=AT_MOST,
                        constraint_id=constraint_id,
                        ref_index=ref_index,
                        source_ref=before,
                        target_ref=after,
                        before=before,
                        after=after,
                        gap_ns=gap_ns,
                    ),
                )
            )

    return edges


def find_negative_cycle(
    n: int, edges: list[Edge], max_nodes: Optional[int] = None
) -> Optional[ContradictionCycle]:
    """Bellman-Ford 判定负权环；找到则沿前驱链还原环上的边。

    ``n`` 为图的总节点数（事件节点 + 纪元参考节点）。
    所有节点距离初始为 0，等价于加入一个到各点距离为 0 的虚拟源，
    因此任意可达的负权环都能被检测到。
    """
    limit = max_nodes if max_nodes is not None else n
    dist = [0] * n
    predecessor: dict[int, int] = {}  # 节点 -> 产生最近一次松弛的边下标

    relaxed_node: Optional[int] = None
    for _ in range(n):
        relaxed_node = None
        for edge_index, edge in enumerate(edges):
            candidate = dist[edge.source] + edge.weight
            if candidate < dist[edge.target]:
                dist[edge.target] = candidate
                predecessor[edge.target] = edge_index
                relaxed_node = edge.target
        if relaxed_node is None:
            return None
    if relaxed_node is None:
        return None

    # 第 n 轮仍能松弛的节点必然可达某个负权环；
    # 沿前驱走最多 n 步必然回到环上。
    on_cycle_node = relaxed_node
    for _ in range(n):
        edge_index = predecessor[on_cycle_node]
        on_cycle_node = edges[edge_index].source
    start = on_cycle_node

    cycle_indices: list[int] = []
    node = start
    steps = 0
    while True:
        edge_index = predecessor[node]
        edge = edges[edge_index]
        cycle_indices.append(edge_index)
        node = edge.source
        steps += 1
        if node == start:
            break
        if steps > limit:  # 防御：理论上不会触发
            raise RuntimeError("负权环还原异常，请提交输入用于排查")

    # 环上松弛顺序是逆序记录的，翻转后便于顺着读。
    cycle_indices.reverse()
    total_weight = sum(edges[i].weight for i in cycle_indices)
    if total_weight >= 0:
        # 防御：理论上不可能（前驱链闭路上的总权必为负）。
        raise RuntimeError("还原出的环路权重非负，内部一致性检查失败")
    return ContradictionCycle(edge_indices=cycle_indices, total_weight=total_weight)


def shortest_paths_from(
    n: int, edges: list[Edge], source: int
) -> list[Optional[int]]:
    """已知无负权环时求单源最短距离；不可达返回 None。"""
    dist: list[Optional[int]] = [None] * n
    dist[source] = 0
    for _ in range(n - 1):
        changed = False
        for edge in edges:
            if dist[edge.source] is None:
                continue
            candidate = dist[edge.source] + edge.weight
            if dist[edge.target] is None or candidate < dist[edge.target]:
                dist[edge.target] = candidate
                changed = True
        if not changed:
            break
    return dist


def solve(
    n_events: int, edges: list[Edge]
) -> tuple[Optional[ContradictionCycle], list[Optional[int]], list[Optional[int]]]:
    """判定可行性并返回 (矛盾环|None, latest 界, earliest 界)。

    界以纳秒表示；None 表示该方向无界。返回的界只含事件节点（不含参考节点）。
    """
    total = n_events + 1  # 最后一个节点是纪元参考点
    cycle = find_negative_cycle(total, edges)
    if cycle is not None:
        return cycle, [None] * n_events, [None] * n_events

    zero = zero_node(n_events)
    forward = shortest_paths_from(total, edges, zero)  # latest[x]
    reverse_edges = [
        Edge(edge.target, edge.source, edge.weight, edge.descriptor)
        for edge in edges
    ]
    reverse = shortest_paths_from(total, reverse_edges, zero)  # dist(x, zero)
    # earliest[x] = -dist(x, zero)
    earliest = [None if d is None else -d for d in reverse[:n_events]]
    return None, forward[:n_events], earliest
