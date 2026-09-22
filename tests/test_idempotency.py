"""POST /audits 的 X-Idempotency-Key 幂等提交测试。"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

import app.solver as solver
from app import idempotency
from app.main import app


FEASIBLE_BODY = {"events": [{"id": "x"}], "constraints": []}

INFEASIBLE_BODY = {
    "events": [
        {"id": "a", "earliest": "2026-09-21T09:00:00Z"},
        {"id": "b", "latest": "2026-09-21T08:00:00Z"},
    ],
    "constraints": [
        {"kind": "at_least", "before": "a", "after": "b", "gap": "PT2H"}
    ],
}


@pytest.fixture(autouse=True)
def _reset_store():
    idempotency._reset()
    yield
    idempotency._reset()


def test_request_without_key_has_no_replay_header_and_executes_each_time():
    client = TestClient(app)
    r1 = client.post("/audits", json=FEASIBLE_BODY)
    r2 = client.post("/audits", json=FEASIBLE_BODY)
    assert r1.status_code == r2.status_code == 200
    assert r1.headers.get("x-idempotency-replayed") is None
    assert r2.headers.get("x-idempotency-replayed") is None
    assert r1.json() == r2.json()


def test_first_request_is_marked_false_replay_true():
    client = TestClient(app)
    r1 = client.post(
        "/audits", json=FEASIBLE_BODY, headers={"X-Idempotency-Key": "k1"}
    )
    assert r1.status_code == 200
    assert r1.headers["x-idempotency-replayed"] == "false"

    r2 = client.post(
        "/audits", json=FEASIBLE_BODY, headers={"X-Idempotency-Key": "k1"}
    )
    assert r2.status_code == 200
    assert r2.headers["x-idempotency-replayed"] == "true"
    # 完全相同的状态与结果
    assert r2.content == r1.content


def test_infeasible_result_replayed_without_regenerating_cycle():
    client = TestClient(app)
    r1 = client.post(
        "/audits", json=INFEASIBLE_BODY, headers={"X-Idempotency-Key": "k2"}
    )
    r2 = client.post(
        "/audits", json=INFEASIBLE_BODY, headers={"X-Idempotency-Key": "k2"}
    )
    assert r1.status_code == r2.status_code == 200
    b1, b2 = r1.json(), r2.json()
    assert b1["feasible"] is False
    assert r2.headers["x-idempotency-replayed"] == "true"
    # 回放的是首次生成的那一条矛盾闭环，而不是重新求解出的可能不同的环
    assert b2 == b1


def test_same_key_different_fingerprint_conflicts_and_keeps_original():
    client = TestClient(app)
    r1 = client.post(
        "/audits", json=FEASIBLE_BODY, headers={"X-Idempotency-Key": "k3"}
    )
    assert r1.status_code == 200

    conflict = client.post(
        "/audits",
        json={"events": [{"id": "y"}], "constraints": []},
        headers={"X-Idempotency-Key": "k3"},
    )
    assert conflict.status_code == 409
    # 冲突响应本身不标记回放
    assert conflict.headers.get("x-idempotency-replayed") is None
    assert "k3" in conflict.json()["detail"]

    # 原结果保持不变，且仍可回放
    r3 = client.post(
        "/audits", json=FEASIBLE_BODY, headers={"X-Idempotency-Key": "k3"}
    )
    assert r3.status_code == 200
    assert r3.content == r1.content
    assert r3.headers["x-idempotency-replayed"] == "true"


def test_distinct_keys_execute_independently():
    client = TestClient(app)
    r1 = client.post(
        "/audits", json=FEASIBLE_BODY, headers={"X-Idempotency-Key": "k4"}
    )
    r2 = client.post(
        "/audits",
        json={"events": [{"id": "y"}], "constraints": []},
        headers={"X-Idempotency-Key": "k5"},
    )
    assert r1.status_code == r2.status_code == 200
    assert set(r1.json()["ranges"]) == {"x"}
    assert set(r2.json()["ranges"]) == {"y"}
    assert r1.headers["x-idempotency-replayed"] == "false"
    assert r2.headers["x-idempotency-replayed"] == "false"


def test_canonical_json_ignores_whitespace_and_key_order():
    client = TestClient(app)
    raw1 = b'{"events":[{"id":"x"}],"constraints":[]}'
    raw2 = b'{\n  "constraints": [],\n  "events": [ { "id": "x" } ]\n}'
    headers = {"X-Idempotency-Key": "k6", "Content-Type": "application/json"}
    r1 = client.post("/audits", content=raw1, headers=headers)
    r2 = client.post("/audits", content=raw2, headers=headers)
    assert r1.status_code == r2.status_code == 200
    assert r2.headers["x-idempotency-replayed"] == "true"
    assert r2.content == r1.content


def test_numeric_literal_difference_is_distinct_fingerprint():
    # 规范化不抹平数字字面量差异（1 与 1.0 视为不同请求）
    client = TestClient(app)
    h = {"Content-Type": "application/json"}
    r1 = client.post(
        "/audits",
        content=b'{"events":[],"constraints":[]}',
        headers={**h, "X-Idempotency-Key": "k7"},
    )
    r2 = client.post(
        "/audits",
        content=b'{"events" : [ ], "constraints" : [ ]}',
        headers={**h, "X-Idempotency-Key": "k7"},
    )
    assert r2.status_code == 200
    assert r2.headers["x-idempotency-replayed"] == "true"


def test_service_level_422_is_replayed():
    client = TestClient(app)
    bad_body = {"events": [{"id": "x"}, {"id": "x"}], "constraints": []}
    r1 = client.post(
        "/audits", json=bad_body, headers={"X-Idempotency-Key": "k8"}
    )
    r2 = client.post(
        "/audits", json=bad_body, headers={"X-Idempotency-Key": "k8"}
    )
    assert r1.status_code == r2.status_code == 422
    assert r1.headers["x-idempotency-replayed"] == "false"
    assert r2.headers["x-idempotency-replayed"] == "true"
    assert r1.json() == r2.json()


def test_pydantic_422_still_returned_with_key():
    client = TestClient(app)
    bad_body = {"events": [{"id": "x", "earliest": "not-a-time"}]}
    r = client.post(
        "/audits", json=bad_body, headers={"X-Idempotency-Key": "k9"}
    )
    assert r.status_code == 422
    # 非法请求体不占用幂等键：随后同 key 的合法请求应作为首请求执行
    ok = client.post(
        "/audits", json=FEASIBLE_BODY, headers={"X-Idempotency-Key": "k9"}
    )
    assert ok.status_code == 200
    assert ok.headers["x-idempotency-replayed"] == "false"


@pytest.mark.parametrize("value", ["", "   ", "x" * 256])
def test_invalid_idempotency_key_rejected_with_422(value):
    client = TestClient(app)
    r = client.post(
        "/audits", json=FEASIBLE_BODY, headers={"X-Idempotency-Key": value}
    )
    assert r.status_code == 422


def test_non_ascii_key_rejected_with_422():
    # 非法字节经 latin-1 解码后会落在可打印 ASCII 范围之外
    from starlette.datastructures import Headers

    raw_value = "bad-key/é".encode("utf-8").decode("latin-1")
    with pytest.raises(Exception) as exc_info:
        idempotency.parse_idempotency_key(
            Headers(raw=[(b"x-idempotency-key", raw_value.encode("latin-1"))])
        )
    assert exc_info.value.status_code == 422


def test_duplicate_idempotency_header_rejected_with_422():
    client = TestClient(app)
    r = client.post(
        "/audits",
        json=FEASIBLE_BODY,
        headers=[
            ("X-Idempotency-Key", "a"),
            ("X-Idempotency-Key", "b"),
        ],
    )
    assert r.status_code == 422


def test_key_is_trimmed():
    client = TestClient(app)
    r1 = client.post(
        "/audits", json=FEASIBLE_BODY, headers={"X-Idempotency-Key": " k10 "}
    )
    r2 = client.post(
        "/audits", json=FEASIBLE_BODY, headers={"X-Idempotency-Key": "k10"}
    )
    assert r1.headers["x-idempotency-replayed"] == "false"
    assert r2.headers["x-idempotency-replayed"] == "true"
    assert r2.content == r1.content


def test_concurrent_requests_execute_solver_once():
    orig_solve = solver.solve
    entered = threading.Event()
    release = threading.Event()
    call_lock = threading.Lock()
    calls = 0

    def gated_solve(*args, **kwargs):
        nonlocal calls
        with call_lock:
            calls += 1
        entered.set()
        assert release.wait(10), "等待闸门超时"
        return orig_solve(*args, **kwargs)

    solver.solve = gated_solve
    try:
        results: list[tuple[int, str, bytes]] = []

        def worker():
            client = TestClient(app)
            resp = client.post(
                "/audits",
                json=INFEASIBLE_BODY,
                headers={"X-Idempotency-Key": "k11"},
            )
            results.append(
                (
                    resp.status_code,
                    resp.headers.get("x-idempotency-replayed", ""),
                    resp.content,
                )
            )

        threads = [threading.Thread(target=worker) for _ in range(4)]
        threads[0].start()
        assert entered.wait(5), "首请求未进入求解"
        for t in threads[1:]:
            t.start()
        time.sleep(0.5)  # 等待其余请求在结果事件上挂起
        release.set()
        for t in threads:
            t.join(10)
    finally:
        solver.solve = orig_solve

    assert len(results) == 4
    assert calls == 1  # 求解（含矛盾闭环生成）只执行一次
    bodies = {r[2] for r in results}
    assert len(bodies) == 1
    markers = sorted(r[1] for r in results)
    assert markers == ["false", "true", "true", "true"]


def test_unexpected_leader_failure_frees_key_for_retry(monkeypatch):
    client = TestClient(app, raise_server_exceptions=False)
    import app.main as main_module

    def boom(_payload):
        raise RuntimeError("boom")

    monkeypatch.setattr(main_module, "audit", boom)
    r1 = client.post(
        "/audits", json=FEASIBLE_BODY, headers={"X-Idempotency-Key": "k12"}
    )
    assert r1.status_code == 500

    monkeypatch.undo()
    r2 = client.post(
        "/audits", json=FEASIBLE_BODY, headers={"X-Idempotency-Key": "k12"}
    )
    assert r2.status_code == 200
    assert r2.headers["x-idempotency-replayed"] == "false"
