import os

# Use SQLite for the app's own engine so importing app.main never needs Postgres.
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.services.seed import seed_if_empty


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    db = TestingSession()
    seed_if_empty(db)
    db.close()

    def override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _batch_id(client: TestClient, code: str) -> int:
    batches = client.get("/api/batches").json()
    return next(b["id"] for b in batches if b["code"] == code)


def _named_ids(client: TestClient):
    products = {p["name"]: p["id"] for p in client.get("/api/products").json()}
    ovens = {o["label"]: o["id"] for o in client.get("/api/ovens").json()}
    return products, ovens


def _gantt(client: TestClient):
    return client.get("/api/gantt").json()


def test_seed_gantt_block_count_unchanged(client):
    # 三批仍在排：每批发酵+烘烤各一条，共 6 条，与现状一致。
    blocks = _gantt(client)
    assert len(blocks) == 6
    assert {b["code"] for b in blocks} == {"BO-0900", "BO-1030", "BO-1000"}


def test_cancel_removes_gantt_blocks_and_status_persists(client):
    bid = _batch_id(client, "BO-1000")
    resp = client.post(f"/api/batches/{bid}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    # 重新拉取（离开再进来）仍是作废。
    batches = client.get("/api/batches").json()
    assert next(b for b in batches if b["code"] == "BO-1000")["status"] == "cancelled"

    # 甘特不再画出它的发酵和烘烤。
    blocks = _gantt(client)
    assert "BO-1000" not in {b["code"] for b in blocks}
    assert len(blocks) == 4


def test_cancel_frees_original_slot_for_scheduling(client):
    products, ovens = _named_ids(client)
    oven2 = ovens["一层 2 号炉"]
    brownie = products["布朗尼"]  # 0+30 min，BO-1000 原占 [600,630)

    # 未作废时原时段仍被占，排不进去。
    resp = client.post(
        "/api/batches",
        json={"product_id": brownie, "oven_id": oven2, "start_min": 600},
    )
    assert resp.status_code == 409

    bid = _batch_id(client, "BO-1000")
    client.post(f"/api/batches/{bid}/cancel")

    # 作废后原时段算成可排，冲突检测不再把它当作占炉对手。
    resp = client.post(
        "/api/batches",
        json={"product_id": brownie, "oven_id": oven2, "start_min": 600},
    )
    assert resp.status_code == 200
    assert resp.json()["start_min"] == 600


def test_cancel_does_not_move_other_batches(client):
    before = _gantt(client)
    bo900_before = sorted(
        (b["start_min"], b["end_min"]) for b in before if b["code"] == "BO-0900"
    )
    assert bo900_before == [(540, 580), (580, 615)]

    bid = _batch_id(client, "BO-1000")
    client.post(f"/api/batches/{bid}/cancel")

    after = _gantt(client)
    bo900_after = sorted(
        (b["start_min"], b["end_min"]) for b in after if b["code"] == "BO-0900"
    )
    assert bo900_after == bo900_before

    bo1030_after = sorted(
        (b["start_min"], b["end_min"]) for b in after if b["code"] == "BO-1030"
    )
    assert bo1030_after == [(630, 655), (655, 675)]

    # 批次行的开工与端点也不变。
    batches = {b["code"]: b for b in client.get("/api/batches").json()}
    assert batches["BO-0900"]["start_min"] == 540
    assert batches["BO-0900"]["ferment_end"] == 580
    assert batches["BO-0900"]["bake_end"] == 615
    assert batches["BO-1030"]["start_min"] == 630


def test_double_cancel_keeps_status_and_no_extra_occupancy(client):
    bid = _batch_id(client, "BO-1000")
    client.post(f"/api/batches/{bid}/cancel")
    blocks_after_first = _gantt(client)

    resp = client.post(f"/api/batches/{bid}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    # 占炉条数不增加，状态保持作废。
    assert len(_gantt(client)) == len(blocks_after_first) == 4
    batches = client.get("/api/batches").json()
    assert len(batches) == 3
    cancelled = [b for b in batches if b["status"] == "cancelled"]
    assert len(cancelled) == 1 and cancelled[0]["code"] == "BO-1000"


def test_cancel_unknown_batch_404(client):
    assert client.post("/api/batches/9999/cancel").status_code == 404
