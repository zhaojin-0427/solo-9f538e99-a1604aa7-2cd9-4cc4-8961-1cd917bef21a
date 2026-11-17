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
    # 歧义状态下下载按钮禁用
    assert "下载（需全部唯一）" in body


def test_unique_allows_download(client):
    upload(client, UNIQUE_LEFT, UNIQUE_RIGHT)
    resp = client.get("/datasets/1/download")
    assert resp.status_code == 200
    text = resp.data.decode("utf-8-sig")
    assert "left_sample_id,right_sample_id" in text
    assert "L1,R1,0,6" in text
    assert "L2,R2,0,6" in text


def test_download_blocked_while_ambiguous(client):
    upload(client, AMBIG_LEFT, AMBIG_RIGHT)
    resp = client.get("/datasets/1/download", follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert "不得供数" in body


def test_disambiguation_flow_and_revision(client):
    upload(client, AMBIG_LEFT, AMBIG_RIGHT)
    # 第一次锁定 r1 -> r2
    resp = client.post("/datasets/1/lock", data={
        "revision": 1, "left_id": "L1", "right_id": "R2",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert "已锁定 L1 ↔ R2" in resp.get_data(as_text=True)
    # 锁定后 L2 唯一指向 R1，全部唯一，可下载
    resp = client.get("/datasets/1/download")
    assert resp.status_code == 200
    text = resp.data.decode("utf-8-sig")
    assert "L1,R2,0,6" in text and "L2,R1,0,6" in text
    # 修订号已变为 2
    assert "修订号 r2" in client.get("/datasets/1").get_data(as_text=True)


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
    assert "无解任务不能下载" in resp.get_data(as_text=True)


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
