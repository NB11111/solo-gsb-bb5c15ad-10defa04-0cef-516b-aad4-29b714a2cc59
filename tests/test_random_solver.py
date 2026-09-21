"""随机差分约束：Bellman-Ford 收紧界与小域穷举互相印证。"""
import itertools
import random

from app.solver import AT_LEAST, AT_MOST, build_graph, solve


def brute_force_ranges(events, constraints, domain):
    """在整数域 domain 内穷举可行解，返回各事件可行值的 (min, max)。

    若 domain 内没有任何可行解返回 None。
    """
    feasible_values = {eid: set() for eid, _, _ in events}
    for assignment in itertools.product(domain, repeat=len(events)):
        values = dict(zip([e[0] for e in events], assignment))
        ok = True
        for eid, lo, hi in events:
            if values[eid] < lo or (hi is not None and values[eid] > hi):
                ok = False
                break
        if not ok:
            continue
        for kind, _cid, _idx, before, after, gap in constraints:
            delta = values[after] - values[before]
            if kind == AT_LEAST and delta < gap:
                ok = False
                break
            if kind == AT_MOST and delta > gap:
                ok = False
                break
        if ok:
            for eid, value in values.items():
                feasible_values[eid].add(value)

    if not any(feasible_values.values()):
        return None
    return {
        eid: (min(vals), max(vals)) if vals else (None, None)
        for eid, vals in feasible_values.items()
    }


def test_tight_bounds_are_attainable():
    """收紧后的界自洽且可达：x[v]=latest[v] 必为一个可行赋值。"""
    rng = random.Random(424242)
    event_ids = ["a", "b", "c", "d"]
    hard_lo, hard_hi = -5, 10
    for _ in range(200):
        events = [(eid, hard_lo, hard_hi) for eid in event_ids]
        constraints = []
        for idx in range(rng.randint(1, 6)):
            kind = rng.choice([AT_LEAST, AT_MOST])
            before, after = rng.choice(event_ids), rng.choice(event_ids)
            constraints.append((kind, None, idx, before, after, rng.randint(0, 4)))
        edges = build_graph(events, constraints)
        cycle, latest, earliest = solve(len(events), edges)
        if cycle is not None:
            continue

        index_of = {eid: i for i, (eid, _, _) in enumerate(events)}
        assignment = {eid: latest[i] for i, (eid, _, _) in enumerate(events)}
        # 界自洽、落在原硬界内
        for i, (eid, lo, hi) in enumerate(events):
            assert lo <= earliest[i] <= latest[i] <= hi, (events, constraints, eid)
        # x[v] = dist(ZERO, v) 满足全部差分不等式 ⇒ 可行赋值，latest 可达
        for kind, _c, _i, before, after, gap in constraints:
            delta = assignment[after] - assignment[before]
            if kind == AT_LEAST:
                assert delta >= gap, (events, constraints)
            else:
                assert delta <= gap, (events, constraints)


def test_random_systems_match_brute_force():
    rng = random.Random(20260921)
    event_ids = ["a", "b", "c"]
    # 真实硬界固定在 [-4, 8]；穷举域向外放宽 20，
    # 因此穷举给出的可行值端点就是 Bellman-Ford 的真实收紧界。
    hard_lo, hard_hi = -4, 8
    domain = list(range(hard_lo - 20, hard_hi + 21))

    checked = 0
    for _ in range(120):
        events = [(eid, hard_lo, hard_hi) for eid in event_ids]
        constraints = []
        for idx in range(rng.randint(0, 5)):
            kind = rng.choice([AT_LEAST, AT_MOST])
            before, after = rng.choice(event_ids), rng.choice(event_ids)
            constraints.append((kind, None, idx, before, after, rng.randint(0, 3)))

        edges = build_graph(events, constraints)
        cycle, latest, earliest = solve(len(events), edges)
        brute = brute_force_ranges(events, constraints, domain)

        if cycle is not None:
            assert brute is None, (events, constraints)
            continue
        assert brute is not None
        for i, (eid, _lo, _hi) in enumerate(events):
            assert earliest[i] == brute[eid][0], (events, constraints, eid)
            assert latest[i] == brute[eid][1], (events, constraints, eid)
        checked += 1

    assert checked > 60  # 确保不只是测了一堆不可行系统
