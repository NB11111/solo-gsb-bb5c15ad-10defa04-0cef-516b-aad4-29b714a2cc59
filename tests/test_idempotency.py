"""幂等提交（X-Idempotency-Key）的接口与并发测试。"""
from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi import HTTPException

import app.main as main_module
from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)

FEASIBLE_PAYLOAD = {
    "events": [
        {"id": "login", "earliest": "2026-09-21T08:00:00Z",
         "latest": "2026-09-21T09:00:00Z"},
        {"id": "trade", "earliest": "2026-09-21T08:00:00Z",
         "latest": "2026-09-21T09:00:00Z"},
    ],
    "constraints": [
        {"kind": "at_least", "before": "login", "after": "trade",
         "gap": "PT10M"},
        {"kind": "at_most", "before": "login", "after": "trade",
         "gap": "PT30M"},
    ],
}

INFEASIBLE_PAYLOAD = {
    "events": [
        {"id": "a", "earliest": "2026-09-21T09:00:00Z"},
        {"id": "b", "latest": "2026-09-21T08:00:00Z"},
    ],
    "constraints": [
        {"kind": "at_least", "before": "a", "after": "b", "gap": "PT2H"},
    ],
}


@pytest.fixture(autouse=True)
def reset_store():
    # 每个用例使用全新的存储，避免 key 相互污染
    main_module.idempotency_store = main_module.IdempotencyStore()
    yield
    main_module.idempotency_store = main_module.IdempotencyStore()


def test_no_key_keeps_legacy_behavior():
    resp = client.post("/audits", json=FEASIBLE_PAYLOAD)
    assert resp.status_code == 200
    assert "X-Idempotency-Replayed" not in resp.headers
    assert "X-Idempotency-Key" not in resp.headers


def test_first_request_is_not_replayed_and_retry_is_replayed():
    key = "audit-001"
    first = client.post("/audits", json=FEASIBLE_PAYLOAD,
                        headers={"X-Idempotency-Key": key})
    assert first.status_code == 200
    assert first.headers["X-Idempotency-Replayed"] == "false"

    second = client.post("/audits", json=FEASIBLE_PAYLOAD,
                         headers={"X-Idempotency-Key": key})
    assert second.status_code == 200
    assert second.headers["X-Idempotency-Replayed"] == "true"
    # 状态与结果完全一致（含同一序列化字节串）
    assert second.content == first.content


def test_replay_works_for_infeasible_result_and_keeps_one_cycle():
    key = "audit-002"
    first = client.post("/audits", json=INFEASIBLE_PAYLOAD,
                        headers={"X-Idempotency-Key": key})
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["feasible"] is False
    first_cycle = first_body["contradiction"]["steps"]

    for _ in range(3):
        again = client.post("/audits", json=INFEASIBLE_PAYLOAD,
                            headers={"X-Idempotency-Key": key})
        assert again.status_code == 200
        assert again.headers["X-Idempotency-Replayed"] == "true"
        body = again.json()
        assert body == first_body
        # 重放不重新求解：矛盾闭环逐字相同，不会生成另一份（可能不同的）环
        assert body["contradiction"]["steps"] == first_cycle


def test_same_key_different_fingerprint_conflicts_and_keeps_original():
    key = "audit-003"
    first = client.post("/audits", json=FEASIBLE_PAYLOAD,
                        headers={"X-Idempotency-Key": key})
    assert first.status_code == 200
    original = first.json()

    changed = {
        **FEASIBLE_PAYLOAD,
        "constraints": [
            {"kind": "at_least", "before": "login", "after": "trade",
             "gap": "PT20M"},
            {"kind": "at_most", "before": "login", "after": "trade",
             "gap": "PT30M"},
        ],
    }
    conflict = client.post("/audits", json=changed,
                           headers={"X-Idempotency-Key": key})
    assert conflict.status_code == 409

    # 原结果不变，原请求仍可重放
    replay = client.post("/audits", json=FEASIBLE_PAYLOAD,
                         headers={"X-Idempotency-Key": key})
    assert replay.status_code == 200
    assert replay.headers["X-Idempotency-Replayed"] == "true"
    assert replay.json() == original


def test_distinct_keys_are_independent():
    r1 = client.post("/audits", json=FEASIBLE_PAYLOAD,
                     headers={"X-Idempotency-Key": "key-a"})
    r2 = client.post("/audits", json=FEASIBLE_PAYLOAD,
                     headers={"X-Idempotency-Key": "key-b"})
    assert r1.headers["X-Idempotency-Replayed"] == "false"
    assert r2.headers["X-Idempotency-Replayed"] == "false"
    assert r1.json() == r2.json()


def test_canonical_json_makes_equivalent_retries_same_fingerprint():
    # 键顺序、空白、Z 与 +00:00、时长的等价写法都应命中同一指纹
    key = "audit-004"
    first_raw = (
        '{"constraints":[{"gap":"PT1H","after":"trade","before":"login",'
        '"kind":"at_least"}],"events":[{"id":"login",'
        '"earliest":"2026-09-21T08:00:00Z"},'
        '{"id":"trade","latest":"2026-09-21T09:00:00Z"}]}'
    )
    retry_raw = (
        '{\n "events": [\n'
        '  {"id": "login", "earliest": "2026-09-21T08:00:00+00:00"},\n'
        '  {"id": "trade", "latest": "2026-09-21T09:00:00Z"}\n'
        ' ],\n'
        ' "constraints": [\n'
        '  {"kind": "at_least", "before": "login", "after": "trade", '
        '"gap": "PT60M"}\n'
        ' ]\n}'
    )
    first = client.post(
        "/audits", content=first_raw,
        headers={"X-Idempotency-Key": key, "Content-Type": "application/json"},
    )
    assert first.status_code == 200
    replay = client.post(
        "/audits", content=retry_raw,
        headers={"X-Idempotency-Key": key, "Content-Type": "application/json"},
    )
    assert replay.status_code == 200
    assert replay.headers["X-Idempotency-Replayed"] == "true"
    assert replay.content == first.content


def test_event_order_is_part_of_fingerprint():
    key = "audit-005"
    payload_a = {"events": [{"id": "a"}, {"id": "b"}], "constraints": []}
    payload_b = {"events": [{"id": "b"}, {"id": "a"}], "constraints": []}
    first = client.post("/audits", json=payload_a,
                        headers={"X-Idempotency-Key": key})
    assert first.status_code == 200
    conflict = client.post("/audits", json=payload_b,
                           headers={"X-Idempotency-Key": key})
    assert conflict.status_code == 409


def test_empty_key_is_422():
    resp = client.post("/audits", json=FEASIBLE_PAYLOAD,
                       headers={"X-Idempotency-Key": ""})
    assert resp.status_code == 422


def test_key_with_space_is_422():
    resp = client.post("/audits", json=FEASIBLE_PAYLOAD,
                       headers={"X-Idempotency-Key": "abc def"})
    assert resp.status_code == 422


def test_too_long_key_is_422():
    resp = client.post("/audits", json=FEASIBLE_PAYLOAD,
                       headers={"X-Idempotency-Key": "k" * 256})
    assert resp.status_code == 422


def test_invalid_body_with_key_is_still_422():
    resp = client.post(
        "/audits", json={"events": [{"id": "x"}], "constraints": [
            {"kind": "at_most", "before": "x", "after": "ghost",
             "gap": "PT1M"}
        ]},
        headers={"X-Idempotency-Key": "audit-006"},
    )
    assert resp.status_code == 422
    assert "ghost" in resp.json()["detail"]


def test_failed_key_is_released_for_corrected_retry():
    key = "audit-007"
    bad = client.post(
        "/audits", json={"events": [{"id": "x"}], "constraints": [
            {"kind": "at_most", "before": "x", "after": "ghost",
             "gap": "PT1M"}
        ]},
        headers={"X-Idempotency-Key": key},
    )
    assert bad.status_code == 422
    good = client.post("/audits", json={"events": [{"id": "x"}]},
                       headers={"X-Idempotency-Key": key})
    assert good.status_code == 200
    assert good.headers["X-Idempotency-Replayed"] == "false"


def test_conflict_during_in_flight_waits_then_409():
    # LEADER 还在求解时，不同指纹的并发请求必须等待：
    # 既不抢先 409，也不参与求解；结果固化后返回 409。
    from app.schemas import AuditRequest

    key = "audit-008"
    model_a = AuditRequest.model_validate(FEASIBLE_PAYLOAD)
    model_b = AuditRequest.model_validate(
        {"events": [{"id": "only-b"}]}
    )
    seen = []
    order = []

    def run(model, tag, delay=0.0):
        time.sleep(delay)
        try:
            order.append(("start", tag))
            result = main_module.create_audit(model, key)
            seen.append((tag, "ok", result))
        except HTTPException as exc:
            seen.append((tag, exc.status_code, exc.detail))
        finally:
            order.append(("end", tag))

    original_audit = main_module.audit

    def slow_audit(payload):
        time.sleep(0.3)
        return original_audit(payload)

    main_module.audit = slow_audit
    try:
        t1 = threading.Thread(target=run, args=(model_a, "a"))
        t2 = threading.Thread(target=run, args=(model_b, "b", 0.1))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    finally:
        main_module.audit = original_audit

    results = {tag: outcome for tag, outcome, _ in seen}
    assert results == {"a": "ok", "b": 409}
    # b 在 a 完成之后才结束，说明它等待了在途求解（而非抢先 409）
    assert order == [
        ("start", "a"), ("start", "b"), ("end", "a"), ("end", "b")
    ]


def test_concurrent_same_fingerprint_solves_once():
    # 同 key 同指纹并发：只有一个 LEADER 真正求解，其余全部重放同一结果。
    from app.schemas import AuditRequest

    key = "audit-009"
    model = AuditRequest.model_validate(FEASIBLE_PAYLOAD)
    solve_calls = []

    original_audit = main_module.audit

    def counting_audit(payload):
        solve_calls.append(1)
        time.sleep(0.3)  # 放大窗口，保证其余线程进入等待
        return original_audit(payload)

    results: list[dict] = []
    errors: list[BaseException] = []

    def worker():
        try:
            response = main_module.create_audit(model, key)
            results.append(json.loads(response.body))
        except BaseException as exc:  # noqa: BLE001 - 测试中记录所有失败
            errors.append(exc)

    main_module.audit = counting_audit
    try:
        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        main_module.audit = original_audit

    assert errors == []
    assert len(solve_calls) == 1
    assert len(results) == 6
    expected = original_audit(model)
    for body in results:
        assert body == expected


def test_audit_service_is_pure_and_replay_is_byte_identical():
    # 不使用 TestClient，直接核对重放序列化字节级一致
    from app.schemas import AuditRequest

    key = "audit-010"
    model = AuditRequest.model_validate(INFEASIBLE_PAYLOAD)
    first = main_module.create_audit(model, key)
    replay = main_module.create_audit(model, key)
    assert replay.body == first.body
    # 两次产生的矛盾闭环逐字相同
    first_body = json.loads(first.body)
    replay_body = json.loads(replay.body)
    assert (
        replay_body["contradiction"]["steps"]
        == first_body["contradiction"]["steps"]
    )
