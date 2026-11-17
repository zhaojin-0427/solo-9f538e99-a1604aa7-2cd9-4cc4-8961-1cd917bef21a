"""SQLite 存储层测试：修订号、事务回滚（失败不改变已有判断）、重启持久化。"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.storage import Store

LOCI = [f"SNP{i}" for i in range(1, 7)]
LEFT = [("L1", [0, 0, 0, 0, 0, 0]), ("L2", [1, 1, 1, 1, 1, 1])]
RIGHT = [("R1", [0, 0, 0, 0, 0, 0]), ("R2", [1, 1, 1, 1, 1, 1])]


@pytest.fixture()
def store(tmp_path):
    return Store(str(tmp_path / "s.db"))


def test_create_and_read(store):
    ds = store.create_dataset("左", "右", LOCI, LEFT, RIGHT, optimum=0,
                              feasible=True, reason=None)
    row = store.get_dataset(ds)
    assert row["revision"] == 1 and row["feasible"] == 1 and row["optimum"] == 0
    assert store.get_samples(ds, "left") == LEFT
    assert store.get_samples(ds, "right") == RIGHT
    assert store.get_locks(ds) == {}


def test_lock_bumps_revision(store):
    ds = store.create_dataset("左", "右", LOCI, LEFT, RIGHT, 0, True, None)
    store.add_lock(ds, "L1", "R1")
    assert store.get_locks(ds) == {"L1": "R1"}
    assert store.get_dataset(ds)["revision"] == 2
    store.add_lock(ds, "L2", "R2")
    assert store.get_dataset(ds)["revision"] == 3
    store.remove_lock(ds, "L1")
    assert store.get_locks(ds) == {"L2": "R2"}
    assert store.get_dataset(ds)["revision"] == 4


def test_transaction_rollback_keeps_state(store):
    ds = store.create_dataset("左", "右", LOCI, LEFT, RIGHT, 0, True, None)
    store.add_lock(ds, "L1", "R1")  # revision -> 2
    before_rev = store.get_dataset(ds)["revision"]
    before_locks = store.get_locks(ds)

    # 模拟“校验通过后写入阶段出错”：事务中先加锁再触发约束异常
    with pytest.raises(sqlite3.Error):
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO locks (dataset_id, left_id, right_id, locked_at) "
                "VALUES (?, 'L2', 'R2', 'x')", (ds,))
            # 重复主键触发 IntegrityError
            conn.execute(
                "INSERT INTO locks (dataset_id, left_id, right_id, locked_at) "
                "VALUES (?, 'L2', 'R2', 'x')", (ds,))

    # 回滚后锁表与修订号均保持失败前状态
    assert store.get_locks(ds) == before_locks
    assert store.get_dataset(ds)["revision"] == before_rev


def test_persistence_across_store_recreate(tmp_path):
    db = str(tmp_path / "persist.db")
    s1 = Store(db)
    ds = s1.create_dataset("左", "右", LOCI, LEFT, RIGHT, 0, True, None)
    s1.add_lock(ds, "L1", "R1")
    # 重新打开同一文件（等价于进程重启）
    s2 = Store(db)
    assert s2.get_dataset(ds)["revision"] == 2
    assert s2.get_locks(ds) == {"L1": "R1"}
    assert s2.get_samples(ds, "left") == LEFT


def test_wal_mode_enabled(store):
    with store._connect() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
