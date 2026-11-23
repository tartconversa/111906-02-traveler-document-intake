"""SQLite 持久化：旅客批次、授权记录、原件、送审包、逐项结论与补件。

所有归属均以（旅客 × 出行批次 × 材料类型）为主线。转团后旧批次的记录保留可查，
但新批次必须重新建立受理归属；共用文件按内容 SHA256 只存一份原件，
授权与受理行仍然逐旅客独立保存。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS travelers (
    traveler_id    TEXT NOT NULL,
    trip           TEXT NOT NULL,
    family         TEXT NOT NULL,
    purpose        TEXT NOT NULL,
    delegate       TEXT NOT NULL,
    delegate_until TEXT NOT NULL,
    observed_at    TEXT NOT NULL,
    PRIMARY KEY (traveler_id, trip)
);
CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    file_name   TEXT NOT NULL,
    sha256      TEXT NOT NULL UNIQUE,
    stored_path TEXT NOT NULL,
    purpose     TEXT NOT NULL,
    seen_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS downloads (
    traveler_id TEXT NOT NULL,
    trip        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    document_id TEXT NOT NULL,
    allowed     INTEGER NOT NULL,
    reason      TEXT,
    sha256      TEXT,
    at          TEXT NOT NULL,
    PRIMARY KEY (traveler_id, trip, kind, document_id, at)
);
CREATE TABLE IF NOT EXISTS requirements (
    traveler_id TEXT NOT NULL,
    trip        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    document_id TEXT NOT NULL,
    PRIMARY KEY (traveler_id, trip, kind)
);
CREATE TABLE IF NOT EXISTS submissions (
    package_ref  TEXT PRIMARY KEY,
    traveler_id  TEXT NOT NULL,
    trip         TEXT NOT NULL,
    receipt      TEXT,
    state        TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    reconciled_at TEXT
);
CREATE TABLE IF NOT EXISTS submission_files (
    package_ref TEXT NOT NULL,
    kind        TEXT NOT NULL,
    document_id TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    file_name   TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    PRIMARY KEY (package_ref, kind)
);
CREATE TABLE IF NOT EXISTS reviews (
    receipt  TEXT NOT NULL,
    kind     TEXT NOT NULL,
    result   TEXT NOT NULL,
    at       TEXT NOT NULL,
    PRIMARY KEY (receipt, kind)
);
CREATE TABLE IF NOT EXISTS supplements (
    supp_ref    TEXT PRIMARY KEY,
    receipt     TEXT NOT NULL,
    traveler_id TEXT NOT NULL,
    trip        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    document_id TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    state       TEXT NOT NULL,
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sub_traveler ON submissions(traveler_id, trip);
CREATE INDEX IF NOT EXISTS idx_dl_traveler ON downloads(traveler_id, trip);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """线程内串行使用；连接自带读写锁，保证中断后库文件保持一致。"""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def set_meta(self, key: str, value: str) -> None:
        with self.tx() as conn:
            conn.execute(
                """INSERT INTO meta(key,value) VALUES(?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, value),
            )

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else None

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ---- 旅客 / 批次 -------------------------------------------------
    def upsert_traveler(self, traveler: dict[str, Any]) -> None:
        with self.tx() as conn:
            conn.execute(
                """INSERT INTO travelers(traveler_id,trip,family,purpose,delegate,
                                        delegate_until,observed_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(traveler_id,trip) DO UPDATE SET
                     family=excluded.family, purpose=excluded.purpose,
                     delegate=excluded.delegate, delegate_until=excluded.delegate_until,
                     observed_at=excluded.observed_at""",
                (
                    traveler["id"], traveler["trip"], traveler["family"],
                    traveler["purpose"], traveler["delegate"],
                    traveler["delegate_until"], utcnow(),
                ),
            )

    def get_traveler(self, traveler_id: str, trip: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM travelers WHERE traveler_id=? AND trip=?",
                (traveler_id, trip),
            ).fetchone()
        return dict(row) if row else None

    def known_travelers(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM travelers ORDER BY traveler_id, observed_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def latest_trip(self, traveler_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT trip FROM travelers WHERE traveler_id=?
                   ORDER BY observed_at DESC LIMIT 1""",
                (traveler_id,),
            ).fetchone()
        return row["trip"] if row else None

    def known_trips(self, traveler_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT trip FROM travelers WHERE traveler_id=? ORDER BY trip",
                (traveler_id,),
            ).fetchall()
        return [r["trip"] for r in rows]

    def replace_requirements(
        self, traveler_id: str, trip: str, required: dict[str, str]
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                "DELETE FROM requirements WHERE traveler_id=? AND trip=?",
                (traveler_id, trip),
            )
            for kind, document_id in required.items():
                conn.execute(
                    """INSERT INTO requirements(traveler_id,trip,kind,document_id)
                       VALUES(?,?,?,?)""",
                    (traveler_id, trip, kind, document_id),
                )

    def requirements(self, traveler_id: str, trip: str) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind,document_id FROM requirements WHERE traveler_id=? AND trip=?",
                (traveler_id, trip),
            ).fetchall()
        return {r["kind"]: r["document_id"] for r in rows}

    # ---- 原件与授权 ---------------------------------------------------
    def register_document(
        self, document_id: str, file_name: str, data: bytes,
        stored_path: Path, purpose: str,
    ) -> str:
        sha = hashlib.sha256(data).hexdigest()
        with self.tx() as conn:
            conn.execute(
                """INSERT INTO documents(document_id,file_name,sha256,stored_path,purpose,seen_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(document_id) DO UPDATE SET
                     file_name=excluded.file_name, sha256=excluded.sha256,
                     stored_path=excluded.stored_path, purpose=excluded.purpose,
                     seen_at=excluded.seen_at""",
                (document_id, file_name, sha, str(stored_path), purpose, utcnow()),
            )
        return sha

    def document(self, document_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM documents WHERE document_id=?", (document_id,)
            ).fetchone()
        return dict(row) if row else None

    def record_download(
        self, traveler_id: str, trip: str, kind: str, document_id: str,
        allowed: bool, reason: str | None, sha256: str | None,
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                """INSERT INTO downloads(traveler_id,trip,kind,document_id,
                                         allowed,reason,sha256,at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (traveler_id, trip, kind, document_id, int(allowed), reason,
                 sha256, utcnow()),
            )

    def latest_allowed_download(
        self, traveler_id: str, trip: str, kind: str
    ) -> dict[str, Any] | None:
        """该旅客在该批次下对某类材料最近一次成功领取的授权记录。"""
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM downloads
                   WHERE traveler_id=? AND trip=? AND kind=? AND allowed=1
                   ORDER BY at DESC LIMIT 1""",
                (traveler_id, trip, kind),
            ).fetchone()
        return dict(row) if row else None

    def latest_blocker(
        self, traveler_id: str, trip: str, kind: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM downloads
                   WHERE traveler_id=? AND trip=? AND kind=? AND allowed=0
                   ORDER BY at DESC LIMIT 1""",
                (traveler_id, trip, kind),
            ).fetchone()
        return dict(row) if row else None

    # ---- 送审包 -------------------------------------------------------
    def create_submission(
        self, package_ref: str, traveler_id: str, trip: str,
        metadata: dict[str, Any], files: list[dict[str, str]],
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                """INSERT INTO submissions(package_ref,traveler_id,trip,receipt,
                                           state,metadata_json,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (package_ref, traveler_id, trip, None, "pending_reconcile",
                 json.dumps(metadata, ensure_ascii=False, sort_keys=True), utcnow()),
            )
            for item in files:
                conn.execute(
                    """INSERT INTO submission_files(package_ref,kind,document_id,
                                                    sha256,file_name,stored_path)
                       VALUES(?,?,?,?,?,?)""",
                    (package_ref, item["kind"], item["document_id"], item["sha256"],
                     item["file_name"], item["stored_path"]),
                )

    def get_submission(self, package_ref: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM submissions WHERE package_ref=?", (package_ref,)
            ).fetchone()
        return dict(row) if row else None

    def submission_exists(self, package_ref: str) -> bool:
        return self.get_submission(package_ref) is not None

    def submission_files(self, package_ref: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM submission_files WHERE package_ref=? ORDER BY kind",
                (package_ref,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_receipt(self, package_ref: str, receipt: str, state: str) -> None:
        with self.tx() as conn:
            conn.execute(
                """UPDATE submissions SET receipt=?, state=?, reconciled_at=?
                   WHERE package_ref=?""",
                (receipt, state, utcnow(), package_ref),
            )

    def pending_reconcile(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM submissions WHERE state='pending_reconcile'"
            ).fetchall()
        return [dict(r) for r in rows]

    def submissions_for(self, traveler_id: str, trip: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM submissions WHERE traveler_id=? AND trip=?
                   ORDER BY created_at""",
                (traveler_id, trip),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- 逐项结论与补件 -----------------------------------------------
    def upsert_review(self, receipt: str, kind: str, result: str) -> None:
        with self.tx() as conn:
            conn.execute(
                """INSERT INTO reviews(receipt,kind,result,at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(receipt,kind) DO UPDATE SET
                     result=excluded.result, at=excluded.at""",
                (receipt, kind, result, utcnow()),
            )

    def reviews_for(self, receipt: str) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind,result FROM reviews WHERE receipt=?", (receipt,)
            ).fetchall()
        return {r["kind"]: r["result"] for r in rows}

    def create_supplement(
        self, supp_ref: str, receipt: str, traveler_id: str, trip: str,
        kind: str, document_id: str, sha256: str, state: str,
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                """INSERT INTO supplements(supp_ref,receipt,traveler_id,trip,kind,
                                           document_id,sha256,state,at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(supp_ref) DO UPDATE SET state=excluded.state""",
                (supp_ref, receipt, traveler_id, trip, kind, document_id,
                 sha256, state, utcnow()),
            )

    def supplement_exists(self, supp_ref: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM supplements WHERE supp_ref=?", (supp_ref,)
            ).fetchone()
        return row is not None

    def supplements_for_receipt(self, receipt: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM supplements WHERE receipt=? ORDER BY at", (receipt,)
            ).fetchall()
        return [dict(r) for r in rows]
