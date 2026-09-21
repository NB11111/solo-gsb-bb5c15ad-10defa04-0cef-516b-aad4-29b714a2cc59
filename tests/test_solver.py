import pytest

from app.solver import (
    AT_LEAST,
    AT_MOST,
    EPOCH_ID,
    build_graph,
    find_negative_cycle,
    solve,
    zero_node,
)


def make_graph(events, constraints):
    return build_graph(events, constraints)


def test_feasible_tight_ranges():
    # A in [0, 100]; B in [0, 100]; B-A >= 30; B-A <= 60  (ns)
    events = [("a", 0, 100), ("b", 0, 100)]
    constraints = [
        (AT_LEAST, None, 0, "a", "b", 30),
        (AT_MOST, None, 1, "a", "b", 60),
    ]
    edges = make_graph(events, constraints)
    cycle, latest, earliest = solve(2, edges)
    assert cycle is None
    assert earliest == [0, 30]
    assert latest == [70, 100]


def test_chain_propagation():
    # a >= 0, b-a >= 10, c-b >= 10, c <= 50
    events = [("a", 0, None), ("b", None, None), ("c", None, 50)]
    constraints = [
        (AT_LEAST, None, 0, "a", "b", 10),
        (AT_LEAST, None, 1, "b", "c", 10),
    ]
    edges = make_graph(events, constraints)
    cycle, latest, earliest = solve(3, edges)
    assert cycle is None
    assert earliest == [0, 10, 20]
    assert latest == [30, 40, 50]


def test_unbounded_event():
    # 完全没有约束的事件：两侧都无界
    edges = make_graph([("free", None, None)], [])
    cycle, latest, earliest = solve(1, edges)
    assert cycle is None
    assert earliest == [None]
    assert latest == [None]


def test_empty_system():
    edges = make_graph([], [])
    cycle, latest, earliest = solve(0, edges)
    assert cycle is None
    assert latest == [] and earliest == []
    # 单参考节点、没有边：无负环
    assert find_negative_cycle(1, edges) is None


def test_simple_contradiction():
    # a in [10, 5]：earliest > latest，自身矛盾
    edges = make_graph([("a", 10, 5)], [])
    cycle, latest, earliest = solve(1, edges)
    assert cycle is not None
    assert cycle.total_weight == 5 - 10
    assert len(cycle.edge_indices) == 2


def test_interval_contradiction_cycle():
    # B-A >= 30 且 B-A <= 20：负环 B->A(-30), A->B(20)，和为 -10
    events = [("a", None, None), ("b", None, None)]
    constraints = [
        (AT_LEAST, "g1", 0, "a", "b", 30),
        (AT_MOST, "g2", 1, "a", "b", 20),
    ]
    edges = make_graph(events, constraints)
    cycle, latest, earliest = solve(2, edges)
    assert cycle is not None
    assert cycle.total_weight == -10
    assert len(cycle.edge_indices) == 2
    # 环上每条边的描述符都能映射回原约束
    kinds = {edges[i].descriptor.kind for i in cycle.edge_indices}
    assert kinds == {AT_LEAST, AT_MOST}


def test_three_event_contradiction_with_epoch():
    # a >= 100, b-a >= 0, b <= 50 → 负环经过参考节点
    events = [("a", 100, None), ("b", None, 50)]
    constraints = [(AT_LEAST, None, 0, "a", "b", 0)]
    edges = make_graph(events, constraints)
    cycle, _, _ = solve(2, edges)
    assert cycle is not None
    assert cycle.total_weight < 0
    # 环路上节点引用要么是事件，要么是 @epoch
    for i in cycle.edge_indices:
        edge = edges[i]
        assert edge.descriptor.source_ref in {"a", "b", EPOCH_ID}
        assert edge.descriptor.target_ref in {"a", "b", EPOCH_ID}


def test_self_reference_zero_gap_is_feasible():
    events = [("a", 0, 10)]
    constraints = [(AT_LEAST, None, 0, "a", "a", 0)]
    edges = make_graph(events, constraints)
    cycle, latest, earliest = solve(1, edges)
    assert cycle is None
    assert earliest == [0] and latest == [10]


def test_zero_node_index_collision():
    # 回归：参考节点不能与最后一个事件节点同下标
    events = [("a", 0, 10), ("b", 0, 10)]
    edges = make_graph(events, [])
    zero = zero_node(2)
    assert zero == 2
    assert all(e.source <= zero and e.target <= zero for e in edges)
