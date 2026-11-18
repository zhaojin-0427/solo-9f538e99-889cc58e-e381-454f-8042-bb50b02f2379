"""Flask 网页与查询 API。

网页展示与 API 使用同一个 compute_hour_result()，保证两边结果一致。
两种查询模式：
- 实时（未指定批次）：由 readings 表现算，反映当前最新读数；
- 历史（指定批次）：由该批次提交完成时的快照回放，此后上传/失败/重启都不改写。
"""
from __future__ import annotations

import os

from flask import (
    Flask, flash, g, jsonify, redirect, render_template, request, url_for,
)

from . import core, db


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path or os.environ.get("RAIN_DB", "rainfall.db")
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 上传上限 16MB
    app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32))

    # 启动时建库建表（幂等），保证重启后可直接查询
    conn = db.connect(app.config["DB_PATH"])
    try:
        db.init_db(conn)
    finally:
        conn.close()

    def get_conn() -> db.sqlite3.Connection:
        if "db_conn" not in g:
            g.db_conn = db.connect(app.config["DB_PATH"])
        return g.db_conn

    @app.teardown_appcontext
    def _close_conn(exc):
        conn = g.pop("db_conn", None)
        if conn is not None:
            conn.close()

    @app.template_filter("iso")
    def _iso_filter(epoch):
        return core.iso_utc(int(epoch))

    def compute_hour_result(station: str, hour_epoch: int) -> dict:
        """网页与 API 共用的实时小时计算入口。"""
        counters = db.fetch_hour_counters(get_conn(), station, hour_epoch)
        return core.compute_hour(counters, station, hour_epoch)

    def _batch_param() -> str:
        """接受 batch / batch_id 两种参数名。"""
        return (request.args.get("batch") or request.args.get("batch_id") or "").strip()

    def resolve_hour_query(station: str, hour_epoch: int, batch_text: str) -> dict:
        """实时或历史（按批次）查询，网页与 API 共用。

        返回 {"mode": "live"|"historical", "result": {...}, "batch": {...}|None}。
        历史查询不可回答时抛 core.HistoricalQueryError。
        """
        batch_text = (batch_text or "").strip()
        if not batch_text:
            return {"mode": "live", "result": compute_hour_result(station, hour_epoch),
                    "batch": None}

        try:
            batch_id = int(batch_text)
            if batch_id < 1:
                raise ValueError
        except ValueError:
            raise core.HistoricalQueryError(
                "bad_batch", f"批次号无效: {batch_text!r}"
            ) from None

        conn = get_conn()
        status = db.get_history_status(conn)
        if not status["enabled"]:
            raise core.HistoricalQueryError(
                "history_disabled",
                "历史批次查询尚未启用，所有批次都无法回溯；"
                "请先在“历史批次查询”页启用（以当前有效读数建立起点）",
            )

        batch = db.get_batch_meta(conn, batch_id)
        if batch is None:
            raise core.HistoricalQueryError(
                "unknown_batch", f"批次 #{batch_id} 不存在", 404
            )
        if batch_id <= status["enabled_after_batch_id"]:
            raise core.HistoricalQueryError(
                "before_history",
                f"批次 #{batch_id} 早于历史查询启用起点（{status['established_at']}，"
                f"启用时最后批次 #{status['enabled_after_batch_id']}），无法回溯",
            )

        counters = db.fetch_batch_hour_counters(conn, batch_id, station, hour_epoch)
        result = core.compute_hour(counters, station, hour_epoch)
        return {
            "mode": "historical",
            "result": result,
            "batch": {
                "batch_id": batch["batch_id"],
                "uploaded_at": batch["uploaded_at"],
                "filename": batch["filename"],
            },
        }

    def history_meta() -> dict:
        conn = get_conn()
        return {
            "status": db.get_history_status(conn),
            "batches": db.list_history_batches(conn),
        }

    def read_upload_text():
        """从 multipart 文件或文本框读取 CSV 文本，返回 (text, filename)。"""
        file = request.files.get("file")
        if file is not None and file.filename:
            try:
                return file.read().decode("utf-8-sig"), file.filename
            except UnicodeDecodeError:
                raise core.CsvFormatError("文件不是有效的 UTF-8 编码")
        text = request.form.get("csv_text", "")
        if text.strip():
            return text, None
        raise core.CsvFormatError("请选择 CSV 文件或粘贴 CSV 文本")

    # ---------------- 网页 ----------------

    @app.get("/")
    def index():
        conn = get_conn()
        return render_template(
            "index.html",
            stations=db.list_stations(conn),
            batches=db.list_batches(conn),
            history=db.get_history_status(conn),
        )

    @app.post("/upload")
    def upload_html():
        try:
            text, filename = read_upload_text()
            valid, errors = core.parse_csv_text(text)
        except core.CsvFormatError as exc:
            flash(f"上传失败：{exc}", "error")
            return redirect(url_for("index"))
        report = db.apply_batch(get_conn(), valid, errors, filename)
        return render_template(
            "batch.html", report=report,
            history_enabled=db.get_history_status(get_conn())["enabled"],
        )

    @app.get("/report")
    def report_html():
        station = request.args.get("station", "").strip()
        hour_text = request.args.get("hour", "").strip()
        batch_text = _batch_param()
        if not station or not hour_text:
            flash("请填写站点和整点小时", "error")
            if batch_text:
                return redirect(url_for("history_page", batch=batch_text))
            return redirect(url_for("index"))
        try:
            hour = core.parse_hour(hour_text)
        except ValueError as exc:
            flash(f"小时参数无效：{exc}", "error")
            return redirect(url_for("index"))
        try:
            query = resolve_hour_query(station, hour, batch_text)
        except core.HistoricalQueryError as exc:
            flash(str(exc), "error")
            if batch_text:
                return redirect(url_for("history_page", station=station,
                                        hour=hour_text, batch=batch_text))
            return redirect(url_for("history_page"))
        result = query["result"]
        return render_template(
            "report.html",
            r=result,
            batch=query["batch"],
            prev_hour=core.iso_utc(hour - core.HOUR_SECONDS),
            next_hour=core.iso_utc(hour + core.HOUR_SECONDS),
        )

    @app.get("/history")
    def history_page():
        conn = get_conn()
        meta = history_meta()
        status = meta["status"]
        old_batches = []
        if status["enabled"]:
            old_batches = [
                b for b in db.list_batches(conn, limit=100)
                if b["batch_id"] <= status["enabled_after_batch_id"]
            ]
        return render_template(
            "history.html",
            status=status,
            batches=meta["batches"],
            old_batches=old_batches,
            batch=request.args.get("batch", "").strip(),
            station=request.args.get("station", "").strip(),
            hour=_datetime_local_value(request.args.get("hour", "")),
        )

    @app.post("/history/enable")
    def history_enable_html():
        info = db.enable_history(get_conn())
        if info["already_enabled"]:
            flash(
                f"历史查询此前已启用（起点 {info['established_at']}，"
                f"基线 {info['row_count']} 条读数），本次未改动起点。",
                "info",
            )
        else:
            flash(
                f"历史查询已启用：以启用时的 {info['row_count']} 条有效读数建立起点，"
                f"此后每批被接受的修订可按批次回溯；启用前的旧批次无法回溯。",
                "info",
            )
        return redirect(url_for("history_page"))

    @app.get("/batches/<int:batch_id>")
    def batch_detail(batch_id):
        conn = get_conn()
        batch, rows = db.get_batch(conn, batch_id)
        if batch is None:
            flash("批次不存在", "error")
            return redirect(url_for("index"))
        status = db.get_history_status(conn)
        if not status["enabled"]:
            traceable = "disabled"
        elif batch_id <= status["enabled_after_batch_id"]:
            traceable = "before_history"
        else:
            traceable = "yes"
        return render_template(
            "batch_detail.html", batch=batch, rows=rows,
            traceable=traceable, status=status,
        )

    # ---------------- API（与网页同一计算入口，结果一致） ----------------

    @app.post("/api/upload")
    def upload_api():
        try:
            text, filename = read_upload_text()
            valid, errors = core.parse_csv_text(text)
        except core.CsvFormatError as exc:
            return jsonify({"error": str(exc)}), 400
        report = db.apply_batch(get_conn(), valid, errors, filename)
        return jsonify(_report_json(report))

    @app.get("/api/hourly")
    def hourly_api():
        station = request.args.get("station", "").strip()
        if not station:
            return jsonify({"error": "缺少 station 参数"}), 400
        try:
            hour = core.parse_hour(request.args.get("hour", ""))
        except ValueError as exc:
            return jsonify({"error": f"hour 参数无效: {exc}"}), 400
        try:
            query = resolve_hour_query(station, hour, _batch_param())
        except core.HistoricalQueryError as exc:
            return jsonify({"error": str(exc), "reason": exc.reason}), exc.status
        # 历史模式附带批次信息；雨量、间隔来源等字段与网页完全一致
        body = dict(query["result"])
        body["mode"] = query["mode"]
        body["batch"] = query["batch"]
        return jsonify(body)

    @app.post("/api/history/enable")
    def history_enable_api():
        info = db.enable_history(get_conn())
        return jsonify(info)

    @app.get("/api/history")
    def history_api():
        meta = history_meta()
        return jsonify({
            "status": meta["status"],
            "batches": [dict(b) for b in meta["batches"]],
        })

    @app.get("/api/stations")
    def stations_api():
        out = [{
            "station": r["station"],
            "readings": r["n"],
            "first_minute": core.iso_utc(r["first_minute"]),
            "last_minute": core.iso_utc(r["last_minute"]),
        } for r in db.list_stations(get_conn())]
        return jsonify(out)

    @app.get("/api/batches")
    def batches_api():
        return jsonify([dict(r) for r in db.list_batches(get_conn(), limit=100)])

    @app.get("/api/health")
    def health():
        get_conn().execute("SELECT 1")
        return jsonify({"status": "ok"})

    @app.errorhandler(413)
    def too_large(_):
        return "上传文件过大（上限 16MB）", 413

    return app


def _datetime_local_value(text: str) -> str:
    """把已输入的 UTC 小时回填到 datetime-local 控件所需的 YYYY-MM-DDTHH:MM。"""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        epoch = core.parse_hour(text)
    except ValueError:
        return text
    return core.iso_utc(epoch)[:16]


def _report_json(report: dict) -> dict:
    return {
        "batch_id": report["batch_id"],
        "filename": report["filename"],
        "uploaded_at": report["uploaded_at"],
        "total_rows": report["total_rows"],
        "accepted_count": report["accepted_count"],
        "rejected_count": report["rejected_count"],
        "accepted": [{
            "row_no": a["row_no"],
            "action": a["action"],
            "station": a["reading"].station,
            "device": a["reading"].device,
            "minute": core.iso_utc(a["reading"].minute_epoch),
            "counter": a["reading"].counter,
            "revision": a["reading"].revision,
        } for a in report["accepted"]],
        "rejected": report["rejected"],
    }


def main():
    app = create_app()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), threaded=True)


if __name__ == "__main__":
    main()
