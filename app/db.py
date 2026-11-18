"""SQLite 持久化层：模式、事务性批量入库、查询、历史回溯快照。

设计要点：
- readings 以 (station, device, minute_epoch) 为主键，同键只接受更高修订号；
- 每批上传在单个事务中写入（读数 + 批次记录 + 行级明细 + 历史快照），
  任何异常整体回滚，不会留下半批新结果；
- 历史功能启用时，先以现有有效读数建立起点（baseline_batch_id 批次快照），
  此后每批被接受的修订按“同批同键最终值”写入 reading_history；
  快照行随批次提交后永不更新或删除，重启、后续上传/失败均不改写已提交结果；
- 数据库文件落在磁盘（Docker 中挂载卷），重启后仍可查询。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    station      TEXT    NOT NULL,
    device       TEXT    NOT NULL CHECK (device IN ('primary', 'backup')),
    minute_epoch INTEGER NOT NULL,
    counter      INTEGER NOT NULL CHECK (counter BETWEEN 0 AND 9999),
    revision     INTEGER NOT NULL CHECK (revision >= 0),
    updated_at   TEXT    NOT NULL,
    PRIMARY KEY (station, device, minute_epoch)
);
CREATE INDEX IF NOT EXISTS idx_readings_station_minute
    ON readings (station, minute_epoch);

CREATE TABLE IF NOT EXISTS batches (
    batch_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    uploaded_at   TEXT NOT NULL,
    filename      TEXT,
    total_rows    INTEGER NOT NULL,
    accepted_rows INTEGER NOT NULL,
    rejected_rows INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_rows (
    batch_id INTEGER NOT NULL REFERENCES batches (batch_id) ON DELETE CASCADE,
    row_no   INTEGER NOT NULL,
    outcome  TEXT NOT NULL CHECK (outcome IN ('accepted', 'rejected')),
    detail   TEXT,
    raw      TEXT
);
CREATE INDEX IF NOT EXISTS idx_batch_rows_batch ON batch_rows (batch_id);

-- 历史回溯：功能启用后，每个接受的修订键在其所属批次留一条不可变快照。
-- baseline_batch_id 为启用批次号：该批写入启用时全部有效读数（起点）；
-- 之后每批只写该批实际改动的键（增量）。任何批次提交后这些行不再被改写。
CREATE TABLE IF NOT EXISTS reading_history (
    batch_id     INTEGER NOT NULL,
    station      TEXT    NOT NULL,
    device       TEXT    NOT NULL CHECK (device IN ('primary', 'backup')),
    minute_epoch INTEGER NOT NULL,
    counter      INTEGER NOT NULL CHECK (counter BETWEEN 0 AND 9999),
    revision     INTEGER NOT NULL CHECK (revision >= 0),
    PRIMARY KEY (batch_id, station, device, minute_epoch)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_history_key_batch
    ON reading_history (station, device, minute_epoch, batch_id);

-- 功能启用元数据（单行）；无此行表示尚未启用，启用前的批次无法回溯。
CREATE TABLE IF NOT EXISTS history_meta (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    enabled_at        TEXT NOT NULL,
    baseline_batch_id INTEGER NOT NULL
);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: str) -> sqlite3.Connection:
    """新建连接。autocommit 模式，写操作由 apply_batch 显式开事务。"""
    conn = sqlite3.connect(db_path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def apply_batch(conn: sqlite3.Connection, valid_rows, row_errors, filename=None) -> dict:
    """在同一事务中应用一批读数；任何异常整体回滚，不留半批结果。

    valid_rows: [(row_no, raw, Reading)]，按文件顺序逐行处理；
    row_errors: 解析阶段已拒绝的 RowError 列表。
    同键（站点,设备,分钟）只接受更高修订号，否则记为拒绝。
    返回批次报告 dict。
    """
    now = _utcnow_iso()
    accepted = []
    rejected = [{"row_no": e.row_no, "reason": e.reason, "raw": e.raw} for e in row_errors]
    total = len(valid_rows) + len(row_errors)

    conn.execute("BEGIN IMMEDIATE")
    try:
        history_on = get_history_meta(conn) is not None
        cur = conn.execute(
            "INSERT INTO batches (uploaded_at, filename, total_rows, accepted_rows, rejected_rows)"
            " VALUES (?, ?, ?, 0, 0)",
            (now, filename, total),
        )
        batch_id = cur.lastrowid

        # 同批多次修订同键时逐行沿用修订号规则；历史快照只留该批最终值
        history_final = {}
        for row_no, raw, r in valid_rows:
            existing = conn.execute(
                "SELECT revision FROM readings"
                " WHERE station=? AND device=? AND minute_epoch=?",
                (r.station, r.device, r.minute_epoch),
            ).fetchone()
            if existing is not None and r.revision <= existing["revision"]:
                rejected.append({
                    "row_no": row_no,
                    "reason": f"修订号 {r.revision} 不高于已存的 {existing['revision']}，同键只接受更高修订",
                    "raw": raw,
                })
                continue
            if existing is None:
                conn.execute(
                    "INSERT INTO readings (station, device, minute_epoch, counter, revision, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (r.station, r.device, r.minute_epoch, r.counter, r.revision, now),
                )
                action = "inserted"
            else:
                conn.execute(
                    "UPDATE readings SET counter=?, revision=?, updated_at=?"
                    " WHERE station=? AND device=? AND minute_epoch=?",
                    (r.counter, r.revision, now, r.station, r.device, r.minute_epoch),
                )
                action = "updated"
            accepted.append({"row_no": row_no, "action": action, "reading": r})
            conn.execute(
                "INSERT INTO batch_rows (batch_id, row_no, outcome, detail, raw)"
                " VALUES (?, ?, 'accepted', ?, ?)",
                (batch_id, row_no, action, raw),
            )
            if history_on:
                history_final[(r.station, r.device, r.minute_epoch)] = r

        for rej in rejected:
            conn.execute(
                "INSERT INTO batch_rows (batch_id, row_no, outcome, detail, raw)"
                " VALUES (?, ?, 'rejected', ?, ?)",
                (batch_id, rej["row_no"], rej["reason"], rej["raw"]),
            )

        # 每个接受的修订键一条快照；同批同键以最终接受值为准（主键去重）
        for (station, device, minute_epoch), r in history_final.items():
            conn.execute(
                "INSERT INTO reading_history"
                " (batch_id, station, device, minute_epoch, counter, revision)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (batch_id, station, device, minute_epoch, r.counter, r.revision),
            )

        conn.execute(
            "UPDATE batches SET accepted_rows=?, rejected_rows=? WHERE batch_id=?",
            (len(accepted), len(rejected), batch_id),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise

    rejected.sort(key=lambda x: x["row_no"])
    return {
        "batch_id": batch_id,
        "filename": filename,
        "uploaded_at": now,
        "total_rows": total,
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "accepted": accepted,
        "rejected": rejected,
    }


def fetch_hour_counters(conn: sqlite3.Connection, station: str, hour_start_epoch: int) -> dict:
    """取某站某小时 61 个端点（含下一整点）的全部读数。"""
    rows = conn.execute(
        "SELECT device, minute_epoch, counter FROM readings"
        " WHERE station=? AND minute_epoch BETWEEN ? AND ?",
        (station, hour_start_epoch, hour_start_epoch + 3600),
    ).fetchall()
    return {(r["device"], r["minute_epoch"]): r["counter"] for r in rows}


def list_stations(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT station, COUNT(*) AS n, MIN(minute_epoch) AS first_minute,"
        " MAX(minute_epoch) AS last_minute"
        " FROM readings GROUP BY station ORDER BY station"
    ).fetchall()


def list_batches(conn: sqlite3.Connection, limit: int = 20):
    return conn.execute(
        "SELECT * FROM batches ORDER BY batch_id DESC LIMIT ?", (limit,)
    ).fetchall()


def get_batch(conn: sqlite3.Connection, batch_id: int):
    batch = conn.execute(
        "SELECT * FROM batches WHERE batch_id=?", (batch_id,)
    ).fetchone()
    if batch is None:
        return None, []
    rows = conn.execute(
        "SELECT row_no, outcome, detail, raw FROM batch_rows"
        " WHERE batch_id=? ORDER BY row_no",
        (batch_id,),
    ).fetchall()
    return batch, rows


# ---------------- 历史回溯 ----------------

def get_history_meta(conn: sqlite3.Connection):
    """返回启用元数据行（enabled_at / baseline_batch_id）；未启用返回 None。"""
    return conn.execute("SELECT * FROM history_meta WHERE id=1").fetchone()


def enable_history(conn: sqlite3.Connection) -> dict:
    """启用历史回溯（幂等）：以现有有效读数建立历史起点。

    在单个事务中：
    - 新建一个批次号作为 baseline_batch_id（不产生任何读数变更）；
    - 把当前 readings 全量复制为该批次的快照，作为“启用时”的历史起点；
    - 写入 history_meta。已启用时直接返回既有元数据，不重复建起点。
    """
    existing = get_history_meta(conn)
    if existing is not None:
        return {
            "enabled": True,
            "already_enabled": True,
            "enabled_at": existing["enabled_at"],
            "baseline_batch_id": existing["baseline_batch_id"],
        }

    now = _utcnow_iso()
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO batches (uploaded_at, filename, total_rows, accepted_rows, rejected_rows)"
            " VALUES (?, ?, 0, 0, 0)",
            (now, "【历史回溯启用-起点快照】"),
        )
        baseline_batch_id = cur.lastrowid
        conn.execute(
            "INSERT INTO reading_history"
            " (batch_id, station, device, minute_epoch, counter, revision)"
            " SELECT ?, station, device, minute_epoch, counter, revision FROM readings",
            (baseline_batch_id,),
        )
        snapshot_count = conn.execute(
            "SELECT COUNT(*) AS n FROM reading_history WHERE batch_id=?",
            (baseline_batch_id,),
        ).fetchone()["n"]
        conn.execute(
            "INSERT INTO history_meta (id, enabled_at, baseline_batch_id) VALUES (1, ?, ?)",
            (now, baseline_batch_id),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    return {
        "enabled": True,
        "already_enabled": False,
        "enabled_at": now,
        "baseline_batch_id": baseline_batch_id,
        "snapshot_readings": snapshot_count,
    }


def batch_exists(conn: sqlite3.Connection, batch_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM batches WHERE batch_id=?", (batch_id,)
    ).fetchone() is not None


def fetch_history_counters(
    conn: sqlite3.Connection, station: str, hour_start_epoch: int, batch_id: int
) -> dict:
    """取某批次提交后、某站某小时 61 个端点的历史读数。

    对每个 (设备, 分钟) 键取 batch_id 及之前最近一条快照的值，
    即“该批提交完成时”的有效读数；快照不可变，之后任何上传/重启都不影响结果。
    """
    rows = conn.execute(
        "SELECT h.device AS device, h.minute_epoch AS minute_epoch, h.counter AS counter"
        " FROM reading_history AS h"
        " JOIN ("
        "   SELECT device, minute_epoch, MAX(batch_id) AS mb"
        "   FROM reading_history"
        "   WHERE station=? AND minute_epoch BETWEEN ? AND ? AND batch_id<=?"
        "   GROUP BY device, minute_epoch"
        " ) AS latest"
        " ON latest.device=h.device AND latest.minute_epoch=h.minute_epoch"
        " AND latest.mb=h.batch_id"
        " WHERE h.station=? AND h.minute_epoch BETWEEN ? AND ? AND h.batch_id<=?",
        (
            station, hour_start_epoch, hour_start_epoch + 3600, batch_id,
            station, hour_start_epoch, hour_start_epoch + 3600, batch_id,
        ),
    ).fetchall()
    return {(r["device"], r["minute_epoch"]): r["counter"] for r in rows}


def list_batches_with_history_flag(conn: sqlite3.Connection, limit: int = 100):
    """批次列表，并标注每个批次是否可历史回溯（batch_id >= 起点批次号）。"""
    meta = get_history_meta(conn)
    baseline = meta["baseline_batch_id"] if meta is not None else None
    rows = conn.execute(
        "SELECT * FROM batches ORDER BY batch_id DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["history_queryable"] = baseline is not None and d["batch_id"] >= baseline
        out.append(d)
    return out
