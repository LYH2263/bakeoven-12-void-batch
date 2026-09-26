"""作废（void）锁定：甘特、可开工窗口、冲突对手与幂等。"""

import pytest
from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.services.seed import seed_if_empty


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_if_empty(db)
    finally:
        db.close()
    with TestClient(app) as c:
        yield c


def _batches(client):
    return client.get("/api/batches").json()


def _by_code(client, code):
    return next(b for b in _batches(client) if b["code"] == code)


def _gantt(client):
    return client.get("/api/gantt").json()


def _phases(client, code):
    return sorted(
        (b["phase"], b["start_min"], b["end_min"])
        for b in _gantt(client)
        if b["code"] == code
    )


def test_seed_keeps_three_scheduled_batches_and_six_gantt_blocks(client):
    rows = _batches(client)
    assert len(rows) == 3
    assert all(b["status"] == "scheduled" for b in rows)
    # 三批 × 发酵+烘烤两段，不作废时条数与现状一致
    assert len(_gantt(client)) == 6


def test_void_frees_original_slot_and_keeps_other_batches(client):
    bo1000 = _by_code(client, "BO-1000")  # 布朗尼 @ 一层 2 号炉，10:00 开工
    oven_id = bo1000["oven_id"]
    product_id = bo1000["product_id"]

    # 作废前：原时段仍被占，同一炉同一时刻排不进去
    blocked = client.post(
        "/api/batches",
        json={"product_id": product_id, "oven_id": oven_id, "start_min": 10 * 60},
    )
    assert blocked.status_code == 409

    resp = client.post(f"/api/batches/{bo1000['id']}/void")
    assert resp.status_code == 200
    assert resp.json()["status"] == "void"

    # 重新拉取（离开再进来）仍是作废
    assert _by_code(client, "BO-1000")["status"] == "void"

    # 甘特不再画 BO-1000 的发酵和烘烤
    blocks = _gantt(client)
    assert len(blocks) == 4
    assert all(b["code"] != "BO-1000" for b in blocks)

    # 其他批次的开工和端点不变
    assert _phases(client, "BO-0900") == [("bake", 580, 615), ("ferment", 540, 580)]
    assert _phases(client, "BO-1030") == [("bake", 655, 675), ("ferment", 630, 655)]

    # 一层 2 号炉的可开工含回原时段：10:00 可以排进去了，且不再记冲突
    conflicts_before = len(client.get("/api/conflicts").json())
    freed = client.post(
        "/api/batches",
        json={"product_id": product_id, "oven_id": oven_id, "start_min": 10 * 60},
    )
    assert freed.status_code == 200
    assert len(client.get("/api/conflicts").json()) == conflicts_before


def test_void_twice_stays_void_and_adds_no_occupancy(client):
    bo1000 = _by_code(client, "BO-1000")
    first = client.post(f"/api/batches/{bo1000['id']}/void")
    assert first.status_code == 200
    blocks_after_first = len(_gantt(client))

    second = client.post(f"/api/batches/{bo1000['id']}/void")
    assert second.status_code == 200
    assert second.json()["status"] == "void"

    # 状态保持作废，仍只有一条 BO-1000，占炉条数不增加
    rows = [b for b in _batches(client) if b["code"] == "BO-1000"]
    assert len(rows) == 1
    assert rows[0]["status"] == "void"
    assert len(_gantt(client)) == blocks_after_first


def test_void_unknown_batch_is_404(client):
    assert client.post("/api/batches/9999/void").status_code == 404
