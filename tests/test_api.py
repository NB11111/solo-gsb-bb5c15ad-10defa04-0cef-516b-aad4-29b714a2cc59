from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_feasible_audit_returns_tightened_ranges():
    payload = {
        "events": [
            {"id": "login", "earliest": "2026-09-21T08:00:00Z",
             "latest": "2026-09-21T09:00:00Z"},
            {"id": "trade", "earliest": "2026-09-21T08:00:00Z",
             "latest": "2026-09-21T09:00:00Z"},
        ],
        "constraints": [
            {"kind": "at_least", "before": "login", "after": "trade",
             "gap": "PT10M"},
            {"kind": "at_most", "id": "window", "before": "login",
             "after": "trade", "gap": "PT30M"},
        ],
    }
    resp = client.post("/audits", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["feasible"] is True
    assert body["ranges"]["login"] == {
        "earliest": "2026-09-21T08:00:00Z",
        "latest": "2026-09-21T08:50:00Z",
    }
    assert body["ranges"]["trade"] == {
        "earliest": "2026-09-21T08:10:00Z",
        "latest": "2026-09-21T09:00:00Z",
    }


def test_infeasible_audit_returns_contradiction_cycle():
    payload = {
        "events": [
            {"id": "a", "earliest": "2026-09-21T09:00:00Z"},
            {"id": "b", "latest": "2026-09-21T08:00:00Z"},
        ],
        "constraints": [
            {"kind": "at_least", "id": "min-gap", "before": "a", "after": "b",
             "gap": "PT2H"},
        ],
    }
    resp = client.post("/audits", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["feasible"] is False
    contradiction = body["contradiction"]
    assert contradiction["total_weight_ns"] < 0
    refs = [step["ref"] for step in contradiction["steps"]]
    assert "min-gap" in refs
    assert any(step["kind"] == "earliest" for step in contradiction["steps"])
    assert any(step["kind"] == "latest" for step in contradiction["steps"])
    # 闭环闭合：每步 target 等于下一步 source
    steps = contradiction["steps"]
    for i, step in enumerate(steps):
        assert step["target"] == steps[(i + 1) % len(steps)]["source"]


def test_duplicate_event_id_rejected():
    payload = {
        "events": [
            {"id": "x"},
            {"id": "x"},
        ],
        "constraints": [],
    }
    resp = client.post("/audits", json=payload)
    assert resp.status_code == 422
    assert "重复" in resp.json()["detail"]


def test_unknown_reference_rejected():
    payload = {
        "events": [{"id": "x"}],
        "constraints": [
            {"kind": "at_most", "before": "x", "after": "ghost", "gap": "PT1M"}
        ],
    }
    resp = client.post("/audits", json=payload)
    assert resp.status_code == 422
    assert "ghost" in resp.json()["detail"]


def test_bad_timestamp_rejected():
    payload = {"events": [{"id": "x", "earliest": "2026-09-21 08:00:00"}]}
    resp = client.post("/audits", json=payload)
    assert resp.status_code == 422


def test_rfc3339_strict_letter_casing_rejected():
    for value in ("2026-09-21t08:00:00Z", "2026-09-21T08:00:00z"):
        resp = client.post("/audits", json={"events": [{"id": "x", "earliest": value}]})
        assert resp.status_code == 422, value


def test_pre_epoch_year_padding_roundtrip():
    # 4 位年份补齐、1970 前时间无损往返
    from app.timeutils import format_rfc3339_utc, parse_rfc3339_utc

    assert format_rfc3339_utc(parse_rfc3339_utc("0001-01-01T00:00:00Z")) == (
        "0001-01-01T00:00:00Z"
    )
    assert format_rfc3339_utc(parse_rfc3339_utc("1969-12-31T23:59:59Z")) == (
        "1969-12-31T23:59:59Z"
    )


def test_non_utc_timezone_rejected():
    payload = {"events": [{"id": "x", "earliest": "2026-09-21T08:00:00+08:00"}]}
    resp = client.post("/audits", json=payload)
    assert resp.status_code == 422


def test_bad_duration_rejected():
    payload = {
        "events": [{"id": "x"}, {"id": "y"}],
        "constraints": [
            {"kind": "at_least", "before": "x", "after": "y", "gap": "10m"}
        ],
    }
    resp = client.post("/audits", json=payload)
    assert resp.status_code == 422


def test_duration_with_months_rejected():
    payload = {
        "events": [{"id": "x"}, {"id": "y"}],
        "constraints": [
            {"kind": "at_least", "before": "x", "after": "y", "gap": "P1M"}
        ],
    }
    resp = client.post("/audits", json=payload)
    assert resp.status_code == 422


def test_unknown_kind_rejected():
    payload = {
        "events": [{"id": "x"}, {"id": "y"}],
        "constraints": [
            {"kind": "exactly", "before": "x", "after": "y", "gap": "PT1M"}
        ],
    }
    resp = client.post("/audits", json=payload)
    assert resp.status_code == 422


def test_unbounded_event_returns_nulls():
    payload = {"events": [{"id": "free"}], "constraints": []}
    resp = client.post("/audits", json=payload)
    assert resp.status_code == 200
    assert resp.json()["ranges"]["free"] == {"earliest": None, "latest": None}


def test_nanosecond_precision_roundtrip():
    payload = {
        "events": [
            {"id": "a", "earliest": "2026-09-21T08:00:00.123456789Z"},
            {"id": "b", "latest": "2026-09-21T08:00:00.123456788Z"},
        ],
        "constraints": [],
    }
    resp = client.post("/audits", json=payload)
    body = resp.json()
    # 两个事件本身互不约束、各自可行；精度无损回显
    assert body["feasible"] is True
    assert body["ranges"]["a"]["earliest"] == "2026-09-21T08:00:00.123456789Z"
    assert body["ranges"]["b"]["latest"] == "2026-09-21T08:00:00.123456788Z"


def test_empty_audit_is_feasible():
    resp = client.post("/audits", json={})
    assert resp.status_code == 200
    assert resp.json() == {"feasible": True, "ranges": {}}


def test_reserved_constraint_id_rejected():
    payload = {
        "events": [{"id": "x"}, {"id": "y"}],
        "constraints": [
            {"kind": "at_most", "id": "@c", "before": "x", "after": "y",
             "gap": "PT1M"}
        ],
    }
    resp = client.post("/audits", json=payload)
    assert resp.status_code == 422
