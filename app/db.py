"""SQLite 持久化层：模式、事务性批量入库、查询。

设计要点：
- readings 以 (station, device, minute_epoch) 为主键，同键只接受更高修订号；
- 每批上传在单个事务中写入（读数 + 批次记录 + 行级明细 + 历史快照），
  任何异常整体回滚，不会留下半批新结果；
- 历史快照只追加：batch_readings 记录每批被接受的修订（同批同键取该批最终值），
  baseline_readings 记录启用历史查询时的有效读数；查询批次 N 时只回放
  「基线 + 批次号 ≤ N」，后续上传/失败/重启都不会改写已提交批次的结果；
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

-- 历史快照：每批被接受的修订（同批同键只存该批最终值）。
-- 只追加、永不修改；查询批次 N 时 = 基线 + 批次 ≤ N 的接受修订，逐键取最新批次。
-- 后续上传/失败/重启都不会改写已提交批次的结果。
CREATE TABLE IF NOT EXISTS batch_readings (
    batch_id     INTEGER NOT NULL REFERENCES batches (batch_id) ON DELETE CASCADE,
    station      TEXT    NOT NULL,
    device       TEXT    NOT NULL CHECK (device IN ('primary', 'backup')),
    minute_epoch INTEGER NOT NULL,
    counter      INTEGER NOT NULL CHECK (counter BETWEEN 0 AND 9999),
    revision     INTEGER NOT NULL CHECK (revision >= 0),
    PRIMARY KEY (batch_id, station, device, minute_epoch)
);
CREATE INDEX IF NOT EXISTS idx_batch_readings_station_minute
    ON batch_readings (batch_id, station, minute_epoch);
CREATE INDEX IF NOT EXISTS idx_batch_readings_lookup
    ON batch_readings (station, minute_epoch, device, batch_id);

-- 历史起点：启用历史查询时，以当时已生效的 readings 建立的基线快照。
-- 基线不属于任何上传批次，此后永不修改；启用前的旧批次无法回溯。
CREATE TABLE IF NOT EXISTS history_baseline (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    established_at        TEXT NOT NULL,
    enabled_after_batch_id INTEGER NOT NULL,
    row_count             INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS baseline_readings (
    station      TEXT    NOT NULL,
    device       TEXT    NOT NULL CHECK (device IN ('primary', 'backup')),
    minute_epoch INTEGER NOT NULL,
    counter      INTEGER NOT NULL CHECK (counter BETWEEN 0 AND 9999),
    revision     INTEGER NOT NULL CHECK (revision >= 0),
    PRIMARY KEY (station, device, minute_epoch)
);
CREATE INDEX IF NOT EXISTS idx_baseline_readings_station_minute
    ON baseline_readings (station, minute_epoch);
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


def enable_history(conn: sqlite3.Connection) -> dict:
    """启用历史查询：以当前有效 readings 建立不可变基线快照。

    幂等：已启用则原样返回首次启用信息，不会用后来的读数覆盖基线。
    基线快照与启用标记在同一事务中写入，失败整体回滚。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT established_at, enabled_after_batch_id, row_count"
            " FROM history_baseline WHERE id=1"
        ).fetchone()
        if existing is not None:
            conn.execute("COMMIT")
            return {
                "already_enabled": True,
                "established_at": existing["established_at"],
                "enabled_after_batch_id": existing["enabled_after_batch_id"],
                "row_count": existing["row_count"],
            }

        last_batch = conn.execute(
            "SELECT COALESCE(MAX(batch_id), 0) AS b FROM batches"
        ).fetchone()["b"]
        conn.execute(
            "INSERT INTO baseline_readings"
            " (station, device, minute_epoch, counter, revision)"
            " SELECT station, device, minute_epoch, counter, revision FROM readings"
        )
        row_count = conn.execute("SELECT COUNT(*) AS n FROM baseline_readings").fetchone()["n"]
        now = _utcnow_iso()
        conn.execute(
            "INSERT INTO history_baseline"
            " (id, established_at, enabled_after_batch_id, row_count) VALUES (1, ?, ?, ?)",
            (now, last_batch, row_count),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    return {
        "already_enabled": False,
        "established_at": now,
        "enabled_after_batch_id": last_batch,
        "row_count": row_count,
    }


def get_history_status(conn: sqlite3.Connection) -> dict:
    """返回历史查询是否已启用、起点与可查询批次范围。"""
    row = conn.execute(
        "SELECT established_at, enabled_after_batch_id, row_count"
        " FROM history_baseline WHERE id=1"
    ).fetchone()
    if row is None:
        return {"enabled": False}
    batches = conn.execute(
        "SELECT COUNT(*) AS n, MIN(batch_id) AS first_batch, MAX(batch_id) AS last_batch"
        " FROM batches WHERE batch_id > ?",
        (row["enabled_after_batch_id"],),
    ).fetchone()
    return {
        "enabled": True,
        "established_at": row["established_at"],
        "enabled_after_batch_id": row["enabled_after_batch_id"],
        "baseline_readings": row["row_count"],
        "first_tracked_batch": batches["first_batch"],
        "last_tracked_batch": batches["last_batch"],
        "tracked_batches": batches["n"],
    }


def _snapshot_accepted(conn: sqlite3.Connection, batch_id: int, accepted) -> None:
    """把本批被接受的读数落盘为历史快照（只追加）。

    仅在历史查询已启用时记录：启用前的批次由基线覆盖，不单独入快照，
    以保证「批次 N 的结果 = 启用基线 + 批次号 ≤ N 的接受修订」这一语义。
    同批多次修订同键时，以该批最终值为准（按接受顺序覆盖，只写最后一条）。
    在调用方的事务内执行，与本批读数、批次记录一同提交或回滚。
    """
    enabled = conn.execute(
        "SELECT 1 FROM history_baseline WHERE id=1"
    ).fetchone()
    if enabled is None:
        return
    final = {}
    for item in accepted:
        r = item["reading"]
        final[(r.station, r.device, r.minute_epoch)] = r
    if not final:
        return
    conn.executemany(
        "INSERT INTO batch_readings"
        " (batch_id, station, device, minute_epoch, counter, revision)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (batch_id, k[0], k[1], k[2], final[k].counter, final[k].revision)
            for k in sorted(final)
        ],
    )


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
        cur = conn.execute(
            "INSERT INTO batches (uploaded_at, filename, total_rows, accepted_rows, rejected_rows)"
            " VALUES (?, ?, ?, 0, 0)",
            (now, filename, total),
        )
        batch_id = cur.lastrowid

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

        for rej in rejected:
            conn.execute(
                "INSERT INTO batch_rows (batch_id, row_no, outcome, detail, raw)"
                " VALUES (?, ?, 'rejected', ?, ?)",
                (batch_id, rej["row_no"], rej["reason"], rej["raw"]),
            )

        # 历史快照与本批读数同一事务提交：查询结果此后不再被改写
        _snapshot_accepted(conn, batch_id, accepted)

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


def fetch_batch_hour_counters(conn: sqlite3.Connection, batch_id: int,
                              station: str, hour_start_epoch: int) -> dict:
    """取某批次时某站某小时 61 个端点的读数（历史回放，只读快照）。

    快照 = 基线 + 批次号 ≤ batch_id 的全部被接受修订，同键取最新批次
    （同批多次修订同键已在写入时归并为该批最终值）。
    """
    base = conn.execute(
        "SELECT enabled_after_batch_id FROM history_baseline WHERE id=1"
    ).fetchone()
    cutoff = base["enabled_after_batch_id"] if base is not None else 0
    if batch_id <= cutoff:
        return {}
    rows = conn.execute(
        "SELECT device, minute_epoch, counter FROM ("
        "  SELECT device, minute_epoch, counter,"
        "         ROW_NUMBER() OVER ("
        "           PARTITION BY device, minute_epoch ORDER BY src DESC) AS rn"
        "    FROM ("
        "      SELECT device, minute_epoch, counter, 0 AS src FROM baseline_readings"
        "       WHERE station=? AND minute_epoch BETWEEN ? AND ?"
        "      UNION ALL"
        "      SELECT device, minute_epoch, counter, batch_id AS src FROM batch_readings"
        "       WHERE station=? AND minute_epoch BETWEEN ? AND ?"
        "         AND batch_id > ? AND batch_id <= ?"
        "    )"
        ") WHERE rn = 1",
        (
            station, hour_start_epoch, hour_start_epoch + 3600,
            station, hour_start_epoch, hour_start_epoch + 3600,
            cutoff, batch_id,
        ),
    ).fetchall()
    return {(r["device"], r["minute_epoch"]): r["counter"] for r in rows}


def get_history_status(conn: sqlite3.Connection) -> dict:
    """返回历史查询是否已启用、起点与可查询批次范围。"""
    row = conn.execute(
        "SELECT established_at, enabled_after_batch_id, row_count"
        " FROM history_baseline WHERE id=1"
    ).fetchone()
    if row is None:
        return {"enabled": False}
    batches = conn.execute(
        "SELECT COUNT(*) AS n, MIN(batch_id) AS first_batch, MAX(batch_id) AS last_batch"
        " FROM batches WHERE batch_id > ?",
        (row["enabled_after_batch_id"],),
    ).fetchone()
    return {
        "enabled": True,
        "established_at": row["established_at"],
        "enabled_after_batch_id": row["enabled_after_batch_id"],
        "baseline_readings": row["row_count"],
        "first_tracked_batch": batches["first_batch"],
        "last_tracked_batch": batches["last_batch"],
        "tracked_batches": batches["n"],
    }


def list_history_batches(conn: sqlite3.Connection, limit: int = 100):
    """可做历史查询的批次（晚于启用起点），附带被接受修订条数。"""
    row = conn.execute(
        "SELECT enabled_after_batch_id FROM history_baseline WHERE id=1"
    ).fetchone()
    if row is None:
        return []
    return conn.execute(
        "SELECT b.*, (SELECT COUNT(*) FROM batch_readings br WHERE br.batch_id=b.batch_id)"
        " AS snapshot_rows"
        " FROM batches b WHERE b.batch_id > ? ORDER BY b.batch_id DESC LIMIT ?",
        (row["enabled_after_batch_id"], limit),
    ).fetchall()


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


def get_batch_meta(conn: sqlite3.Connection, batch_id: int):
    """只取批次记录（不加载行级明细）。"""
    return conn.execute(
        "SELECT * FROM batches WHERE batch_id=?", (batch_id,)
    ).fetchone()


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
