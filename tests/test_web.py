"""端到端 Web 流程测试：上传、消歧锁定、乐观并发、下载门禁、重启持久化。"""

import io
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app

HEADER = "sample_id,SNP1,SNP2,SNP3,SNP4,SNP5,SNP6\n"


def csv_bytes(lines):
    return (HEADER + "".join(lines)).encode("utf-8")


def upload(client, left_bytes, right_bytes):
    return client.post(
        "/upload",
        data={
            "left_csv": (io.BytesIO(left_bytes), "left.csv"),
            "right_csv": (io.BytesIO(right_bytes), "right.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def post_delivery(client, dataset_id, handover_no, revision):
    return client.post(
        "/datasets/%d/deliver" % dataset_id,
        data={"handover_no": handover_no, "revision": revision},
        follow_redirects=True,
    )


def delivery_csv(client, dataset_id, delivery_id):
    resp = client.get(
        "/datasets/%d/deliveries/%d/download" % (dataset_id, delivery_id))
    assert resp.status_code == 200
    return resp.data


@pytest.fixture()
def app(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"))
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


# 三份典型数据 ---------------------------------------------------------------
AMBIG_LEFT = csv_bytes([
    "L1,0,0,0,0,0,0\n",
    "L2,0,0,0,0,0,0\n",
    "L3,2,2,2,2,2,2\n",
])
AMBIG_RIGHT = csv_bytes([
    "R1,0,0,0,0,0,0\n",
    "R2,0,0,0,0,0,0\n",
    "R3,2,2,2,2,2,2\n",
])

UNIQUE_LEFT = csv_bytes([
    "L1,0,0,0,0,0,0\n",
    "L2,1,1,1,1,1,1\n",
])
UNIQUE_RIGHT = csv_bytes([
    "R1,0,0,0,0,0,0\n",
    "R2,1,1,1,1,1,1\n",
])

NOFEAS_LEFT = csv_bytes([
    "L1,0,0,0,0,0,0\n",
    "L2,1,1,1,1,1,1\n",
])
NOFEAS_RIGHT = csv_bytes([
    "R1,0,0,0,0,0,0\n",
    "R2,0,0,0,0,0,0\n",  # 与 L2 六位点全不同，L2 无可配对象
])


def test_upload_and_ambiguous_page(client):
    resp = upload(client, AMBIG_LEFT, AMBIG_RIGHT)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "最优不一致位点总数：0" in body
    assert "歧义 2 选 1" in body
    # 歧义状态下供数按钮禁用
    assert "生成对照 CSV 并登记（需全部唯一）" in body


def test_unique_allows_delivery_and_download(client):
    upload(client, UNIQUE_LEFT, UNIQUE_RIGHT)
    resp = post_delivery(client, 1, "HO-001", revision=1)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "HO-001" in body and "仍有效" in body
    assert "L1" in body and "R1" in body
    text = delivery_csv(client, 1, 1).decode("utf-8-sig")
    assert "left_sample_id,right_sample_id" in text
    assert "L1,R1,0,6" in text
    assert "L2,R2,0,6" in text


def test_download_link_requires_registration(client):
    upload(client, UNIQUE_LEFT, UNIQUE_RIGHT)
    resp = client.get("/datasets/1/download", follow_redirects=True)
    assert "请填写交接编号并登记供数记录" in resp.get_data(as_text=True)
    assert "text/csv" not in resp.headers.get("Content-Type", "")


def test_download_blocked_while_ambiguous(client):
    upload(client, AMBIG_LEFT, AMBIG_RIGHT)
    resp = client.get("/datasets/1/download", follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert "不得供数" in body or "不能供数" in body


def test_disambiguation_flow_and_revision(client):
    upload(client, AMBIG_LEFT, AMBIG_RIGHT)
    # 第一次锁定 r1 -> r2
    resp = client.post("/datasets/1/lock", data={
        "revision": 1, "left_id": "L1", "right_id": "R2",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert "已锁定 L1 ↔ R2" in resp.get_data(as_text=True)
    # 锁定后 L2 唯一指向 R1，全部唯一，可按交接编号登记供数
    resp = post_delivery(client, 1, "HO-002", revision=2)
    assert resp.status_code == 200
    text = delivery_csv(client, 1, 1).decode("utf-8-sig")
    assert "L1,R2,0,6" in text and "L2,R1,0,6" in text
    # 修订号已变为 3（锁定 1 次 + 供数登记 1 次）
    assert "修订号 r3" in client.get("/datasets/1").get_data(as_text=True)


def test_stale_revision_rejected(client):
    upload(client, AMBIG_LEFT, AMBIG_RIGHT)
    # 用户 A 锁定成功，修订号 -> 2
    client.post("/datasets/1/lock",
                data={"revision": 1, "left_id": "L1", "right_id": "R2"})
    # 用户 B 仍拿着 r1 的旧页面提交 -> 409
    resp = client.post("/datasets/1/lock", data={
        "revision": 1, "left_id": "L2", "right_id": "R1",
    })
    assert resp.status_code == 409
    assert "修订号已过期" in resp.get_data(as_text=True)
    # 旧页面解锁同样被拒绝
    resp = client.post("/datasets/1/unlock",
                       data={"revision": 1, "left_id": "L1"})
    assert resp.status_code == 409
    # 新页面 r2 可以继续操作
    resp = client.post("/datasets/1/unlock",
                       data={"revision": 2, "left_id": "L1"},
                       follow_redirects=True)
    assert "已解除 L1" in resp.get_data(as_text=True)


def test_lock_rejected_when_destroys_optimum(client):
    # 唯一解数据：锁 L1->R2 会导致 L2 无可配对象
    upload(client, UNIQUE_LEFT, UNIQUE_RIGHT)
    resp = client.post("/datasets/1/lock", data={
        "revision": 1, "left_id": "L1", "right_id": "R2",
    }, follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert "锁定被拒绝" in body
    # 锁定表里没有任何记录，修订号仍为 1（保存失败不改变已有判断）
    assert "修订号 r1" in body


def test_duplicate_right_lock_rejected(client):
    upload(client, AMBIG_LEFT, AMBIG_RIGHT)
    client.post("/datasets/1/lock",
                data={"revision": 1, "left_id": "L1", "right_id": "R1"})
    resp = client.post("/datasets/1/lock", data={
        "revision": 2, "left_id": "L2", "right_id": "R1",
    }, follow_redirects=True)
    assert "一对一对应不允许共用" in resp.get_data(as_text=True)


def test_no_feasible_shows_reason_and_blocks_download(client):
    upload(client, NOFEAS_LEFT, NOFEAS_RIGHT)
    body = client.get("/datasets/1").get_data(as_text=True)
    assert "无解原因" in body
    assert "L2" in body
    resp = client.get("/datasets/1/download", follow_redirects=True)
    assert "不能供数" in resp.get_data(as_text=True)


def test_bad_upload_returns_400(client):
    bad = csv_bytes(["L1,0,3,0,0,0,0\n"])  # 非法值
    resp = upload(client, bad, UNIQUE_RIGHT)
    assert resp.status_code == 400
    assert "非法" in resp.get_data(as_text=True)


def test_restart_keeps_inputs_and_locks(tmp_path):
    db = str(tmp_path / "persist.db")
    app = create_app(db_path=db)
    c1 = app.test_client()
    upload(c1, AMBIG_LEFT, AMBIG_RIGHT)
    c1.post("/datasets/1/lock",
            data={"revision": 1, "left_id": "L1", "right_id": "R2"})
    # 模拟进程重启：重新 create_app 指向同一个数据库文件
    app2 = create_app(db_path=db)
    c2 = app2.test_client()
    body = c2.get("/datasets/1").get_data(as_text=True)
    assert "已锁定 → R2" in body
    assert "修订号 r2" in body
    # 输入的样本数据仍在，且候选仍可正确重算
    assert "L3" in body and "R3" in body


def test_index_lists_datasets(client):
    upload(client, UNIQUE_LEFT, UNIQUE_RIGHT)
    body = client.get("/").get_data(as_text=True)
    assert "#1" in body and "可配对" in body


# ------------------------------------------------------------ 供数记录
def test_delivery_requires_unique(client):
    upload(client, AMBIG_LEFT, AMBIG_RIGHT)
    resp = post_delivery(client, 1, "HO-X", revision=1)
    body = resp.get_data(as_text=True)
    assert "供数失败" in body and "不唯一" in body
    # 没有生成记录：详情页仍显示尚无供数记录
    assert "尚无供数记录" in body
    # 也没有产生任何供数记录可供下载
    assert client.get("/datasets/1/deliveries/1").status_code == 404


def test_delivery_requires_feasible(client):
    upload(client, NOFEAS_LEFT, NOFEAS_RIGHT)
    body = post_delivery(client, 1, "HO-X", revision=1).get_data(as_text=True)
    assert "供数失败" in body and "无解" in body
    assert "尚无供数记录" in body


def test_delivery_retry_same_handover_returns_original(client):
    upload(client, AMBIG_LEFT, AMBIG_RIGHT)
    client.post("/datasets/1/lock", data={
        "revision": 1, "left_id": "L1", "right_id": "R2"})
    first = post_delivery(client, 1, "HO-100", revision=2)
    assert "仍有效" in first.get_data(as_text=True)
    original_csv = delivery_csv(client, 1, 1)
    original_record = first.get_data(as_text=True)

    # 解除锁定制造歧义，再改动锁定，然后重试同一交接编号
    client.post("/datasets/1/unlock", data={"revision": 3, "left_id": "L1"})
    client.post("/datasets/1/lock", data={
        "revision": 4, "left_id": "L2", "right_id": "R2"})
    retry = post_delivery(client, 1, "HO-100", revision=5)
    body = retry.get_data(as_text=True)
    assert "返回原记录与原文件" in body
    # 仍指向同一条记录、原始对照未被改写
    assert "r2" in body and "L1" in body and "R2" in body
    assert delivery_csv(client, 1, 1) == original_csv
    # 页面只多出这一条供数记录
    detail = client.get("/datasets/1").get_data(as_text=True)
    assert detail.count("HO-100") == 1


def test_delivery_retry_ignores_stale_revision(client):
    upload(client, UNIQUE_LEFT, UNIQUE_RIGHT)
    post_delivery(client, 1, "HO-200", revision=1)
    # 即使拿着过期修订号重试同一编号，也应幂等返回原记录而不是 409
    retry = post_delivery(client, 1, "HO-200", revision=1)
    assert retry.status_code == 200
    assert "返回原记录与原文件" in retry.get_data(as_text=True)
    assert delivery_csv(client, 1, 1).decode("utf-8-sig").count("\n") == 3


def test_delivery_split_revision_409(client):
    upload(client, UNIQUE_LEFT, UNIQUE_RIGHT)
    resp = client.post("/datasets/1/deliver", data={
        "handover_no": "HO-300", "revision": 99,
    })
    assert resp.status_code == 409
    assert "修订号已过期" in resp.get_data(as_text=True)
    # 未登记
    assert "尚无供数记录" in client.get("/datasets/1").get_data(as_text=True)


def test_delivery_status_transitions(client):
    # 歧义数据：锁定 L1->R2 后唯一并登记
    upload(client, AMBIG_LEFT, AMBIG_RIGHT)
    client.post("/datasets/1/lock", data={
        "revision": 1, "left_id": "L1", "right_id": "R2"})  # r2
    post_delivery(client, 1, "HO-400", revision=2)          # r3

    def status_text():
        return client.get("/datasets/1").get_data(as_text=True)

    assert "仍有效" in status_text()

    # 解锁 -> 出现歧义 -> 待复核
    client.post("/datasets/1/unlock", data={"revision": 3, "left_id": "L1"})  # r4
    body = client.get("/datasets/1").get_data(as_text=True)
    assert "待复核" in body

    # 换成完全不同的唯一对应（L1->R1）-> 原记录应撤回
    client.post("/datasets/1/lock", data={
        "revision": 4, "left_id": "L1", "right_id": "R1"})  # r5
    body = client.get("/datasets/1").get_data(as_text=True)
    assert "应撤回" in body

    # 改回原对应 -> 再次有效
    client.post("/datasets/1/unlock", data={"revision": 5, "left_id": "L1"})  # r6
    client.post("/datasets/1/lock", data={
        "revision": 6, "left_id": "L1", "right_id": "R2"})  # r7
    assert "仍有效" in client.get("/datasets/1").get_data(as_text=True)


def test_old_record_not_rewritten_by_new_lock(client):
    upload(client, AMBIG_LEFT, AMBIG_RIGHT)
    client.post("/datasets/1/lock", data={
        "revision": 1, "left_id": "L1", "right_id": "R2"})
    post_delivery(client, 1, "HO-500", revision=2)
    # 后来的锁定改变了对应
    client.post("/datasets/1/unlock", data={"revision": 3, "left_id": "L1"})
    client.post("/datasets/1/lock", data={
        "revision": 4, "left_id": "L1", "right_id": "R1"})
    # 记录详情仍显示登记时的 L1->R2，并提示应撤回
    body = client.get("/datasets/1/deliveries/1").get_data(as_text=True)
    assert "应撤回" in body
    text = delivery_csv(client, 1, 1).decode("utf-8-sig")
    assert "L1,R2" in text and "L1,R1" not in text


def test_delivery_bad_handover_no_rejected(client):
    upload(client, UNIQUE_LEFT, UNIQUE_RIGHT)
    body = post_delivery(client, 1, "   ", revision=1).get_data(as_text=True)
    assert "请填写交接编号" in body
    body = post_delivery(client, 1, "X" * 65, revision=1).get_data(as_text=True)
    assert "不能超过 64 个字符" in body
    assert "尚无供数记录" in body


def test_two_deliveries_status_judged_individually(client):
    upload(client, AMBIG_LEFT, AMBIG_RIGHT)
    client.post("/datasets/1/lock", data={
        "revision": 1, "left_id": "L1", "right_id": "R2"})       # r2
    post_delivery(client, 1, "HO-OLD", revision=2)                # r3, L1->R2
    # 改成另一组唯一对应后再次供数
    client.post("/datasets/1/unlock", data={"revision": 3, "left_id": "L1"})  # r4
    client.post("/datasets/1/lock", data={
        "revision": 4, "left_id": "L1", "right_id": "R1"})        # r5
    post_delivery(client, 1, "HO-NEW", revision=5)                # r6, L1->R1
    body = client.get("/datasets/1").get_data(as_text=True)
    # 一条应撤回、一条仍有效（按表格中的状态标签计数，避开说明文字）
    assert body.count('class="tag tag-ok">仍有效') == 1
    assert body.count('class="tag tag-bad">应撤回') == 1
    old = client.get("/datasets/1/deliveries/1").get_data(as_text=True)
    new = client.get("/datasets/1/deliveries/2").get_data(as_text=True)
    assert "应撤回" in old and "仍有效" in new
    assert "L1,R2" in delivery_csv(client, 1, 1).decode("utf-8-sig")
    assert "L1,R1" in delivery_csv(client, 1, 2).decode("utf-8-sig")


def test_restart_keeps_delivery_records(tmp_path):
    db = str(tmp_path / "deliver_persist.db")
    app = create_app(db_path=db)
    c1 = app.test_client()
    upload(c1, UNIQUE_LEFT, UNIQUE_RIGHT)
    post_delivery(c1, 1, "HO-600", revision=1)
    original = delivery_csv(c1, 1, 1)

    app2 = create_app(db_path=db)
    c2 = app2.test_client()
    body = c2.get("/datasets/1").get_data(as_text=True)
    assert "HO-600" in body and "仍有效" in body
    assert delivery_csv(c2, 1, 1) == original
    # 重试同一编号在重启后仍幂等
    retry = post_delivery(c2, 1, "HO-600", revision=1)
    assert "返回原记录与原文件" in retry.get_data(as_text=True)


def test_delivery_record_is_immutable_at_storage_layer(app):
    import sqlite3
    import pytest as _pytest
    store = app.extensions["store"]
    db_path = store.db_path
    c = app.test_client()
    upload(c, UNIQUE_LEFT, UNIQUE_RIGHT)
    post_delivery(c, 1, "HO-700", revision=1)
    with sqlite3.connect(db_path) as conn:
        with _pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE deliveries SET csv_content = X'00'")
        with _pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM deliveries")


def test_concurrent_delivery_same_handover_is_idempotent(app):
    """并发用同一交接编号登记：只落一条记录，两个请求拿到同一文件。"""
    import threading

    c = app.test_client()
    upload(c, UNIQUE_LEFT, UNIQUE_RIGHT)
    barrier = threading.Barrier(2)
    results = {}

    def worker(name):
        client = app.test_client()
        barrier.wait()
        try:
            resp = client.post(
                "/datasets/1/deliver",
                data={"handover_no": "HO-RACE", "revision": 1},
            )
            results[name] = (resp.status_code, resp.headers.get("Location"))
        except Exception as exc:  # pragma: no cover - 诊断用
            results[name] = ("error", repr(exc))

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert all(v[0] in (200, 302) for v in results.values()), results
    # 两个请求最终都指向同一条记录
    locations = {v[1] for v in results.values()}
    assert len(locations) == 1, results
    detail = app.test_client().get("/datasets/1").get_data(as_text=True)
    assert detail.count("HO-RACE") == 1


def test_lock_after_delivery_with_stale_revision_is_rejected(client):
    upload(client, AMBIG_LEFT, AMBIG_RIGHT)
    client.post("/datasets/1/lock", data={
        "revision": 1, "left_id": "L1", "right_id": "R2"})  # r2
    post_delivery(client, 1, "HO-800", revision=2)          # r3
    # 旧页面（r2）此刻提交解锁：被 409 拒绝，已登记记录与当前对应均不变
    resp = client.post("/datasets/1/unlock",
                       data={"revision": 2, "left_id": "L1"})
    assert resp.status_code == 409
    body = client.get("/datasets/1").get_data(as_text=True)
    assert "修订号 r3" in body and "仍有效" in body
    assert "L1,R2" in delivery_csv(client, 1, 1).decode("utf-8-sig")
