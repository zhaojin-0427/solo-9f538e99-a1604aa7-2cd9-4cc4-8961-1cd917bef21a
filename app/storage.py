"""SQLite 持久化层。

表结构
------
datasets：一次上传（两份 CSV 构成一个核对任务），带乐观锁版本号 revision；
samples ：样本及其 6 个位点基因型、所属侧别；
locks   ：数据员锁定的 left_id -> right_id 对应。

并发策略
--------
- 修订号 revision 随任何成功的锁定/解锁自增；页面渲染时带回当前 revision，
  提交时服务端比对，旧页面（revision 过期）一律拒绝（HTTP 409）。
- 所有写操作在单事务内完成，校验不通过即 rollback，保证“保存失败不改变
  已有判断”。
- 数据库开启 WAL，允许 gunicorn 多 worker 下读写并发；写事务使用
  IMMEDIATE 立即拿锁，避免写-写冲突时的死锁重试。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    revision      INTEGER NOT NULL DEFAULT 1,
    left_label    TEXT NOT NULL,
    right_label   TEXT NOT NULL,
    loci_json     TEXT NOT NULL,
    optimum       INTEGER,
    feasible     INTEGER NOT NULL DEFAULT 0,
    reason        TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS samples (
    dataset_id    INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    side          TEXT NOT NULL CHECK(side IN ('left', 'right')),
    sample_id     TEXT NOT NULL,
    idx           INTEGER NOT NULL,
    genotype_json TEXT NOT NULL,
    PRIMARY KEY (dataset_id, side, sample_id)
);

CREATE TABLE IF NOT EXISTS locks (
    dataset_id    INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    left_id       TEXT NOT NULL,
    right_id      TEXT NOT NULL,
    locked_at     TEXT NOT NULL,
    PRIMARY KEY (dataset_id, left_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ 读
    def get_dataset(self, dataset_id: int) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM datasets WHERE id = ?", (dataset_id,)
            ).fetchone()
            return row

    def list_datasets(self) -> List[sqlite3.Row]:
        with self._connect() as conn:
            return list(conn.execute(
                "SELECT * FROM datasets ORDER BY id DESC"
            ).fetchall())

    def get_samples(self, dataset_id: int, side: str
                    ) -> List[Tuple[str, List[Optional[int]]]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT sample_id, genotype_json FROM samples "
                "WHERE dataset_id = ? AND side = ? ORDER BY idx",
                (dataset_id, side),
            ).fetchall()
            return [(r["sample_id"], json.loads(r["genotype_json"])) for r in rows]

    def get_locks(self, dataset_id: int) -> Dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT left_id, right_id FROM locks WHERE dataset_id = ?",
                (dataset_id,),
            ).fetchall()
            return {r["left_id"]: r["right_id"] for r in rows}

    # ------------------------------------------------------------------ 写
    def create_dataset(self,
                       left_label: str,
                       right_label: str,
                       loci: Sequence[str],
                       left_samples: Sequence[Tuple[str, Sequence[Optional[int]]]],
                       right_samples: Sequence[Tuple[str, Sequence[Optional[int]]]],
                       optimum: Optional[int],
                       feasible: bool,
                       reason: Optional[str]) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO datasets (revision, left_label, right_label, "
                "loci_json, optimum, feasible, reason, created_at) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
                (left_label, right_label, json.dumps(list(loci)),
                 optimum, 1 if feasible else 0, reason, _now()),
            )
            dataset_id = int(cur.lastrowid)
            for idx, (sid, geno) in enumerate(left_samples):
                conn.execute(
                    "INSERT INTO samples (dataset_id, side, sample_id, idx, genotype_json) "
                    "VALUES (?, 'left', ?, ?, ?)",
                    (dataset_id, sid, idx, json.dumps(list(geno))),
                )
            for idx, (sid, geno) in enumerate(right_samples):
                conn.execute(
                    "INSERT INTO samples (dataset_id, side, sample_id, idx, genotype_json) "
                    "VALUES (?, 'right', ?, ?, ?)",
                    (dataset_id, sid, idx, json.dumps(list(geno))),
                )
            return dataset_id

    def bump_revision(self, conn: sqlite3.Connection, dataset_id: int) -> None:
        conn.execute(
            "UPDATE datasets SET revision = revision + 1 WHERE id = ?",
            (dataset_id,),
        )

    def add_lock(self, dataset_id: int, left_id: str, right_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO locks (dataset_id, left_id, right_id, locked_at) "
                "VALUES (?, ?, ?, ?)",
                (dataset_id, left_id, right_id, _now()),
            )
            self.bump_revision(conn, dataset_id)

    def remove_lock(self, dataset_id: int, left_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM locks WHERE dataset_id = ? AND left_id = ?",
                (dataset_id, left_id),
            )
            self.bump_revision(conn, dataset_id)

    def update_feasibility(self, conn: sqlite3.Connection, dataset_id: int,
                           feasible: bool, optimum: Optional[int],
                           reason: Optional[str]) -> None:
        conn.execute(
            "UPDATE datasets SET feasible = ?, optimum = ?, reason = ? WHERE id = ?",
            (1 if feasible else 0, optimum, reason, dataset_id),
        )
