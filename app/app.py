"""Flask 网页与查询 API。

网页展示与 API 使用同一个 compute_hour_result()，保证两边结果一致；
小时雨量由 readings 表现算现算，补传/修订整点读数提交后，
前后两小时立即同时反映新值，不存在缓存不一致。

历史回溯（/history 与 /api/history）同样由网页与 API 共用同一入口
compute_history_result()：按批次从不可变快照 reading_history 还原该批
提交完成时的读数，再走与实时查询相同的 core.compute_hour 计算，
主备取值、回绕、缺口规则完全一致。
"""
from __future__ import annotations

import os

from flask import (
    Flask, flash, g, jsonify, redirect, render_template, request, url_for,
)

from . import core, db


class HistoryLookupError(Exception):
    """历史查询参数/状态错误。status 为对应的 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


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
        """网页与 API 共用的小时计算入口。"""
        counters = db.fetch_hour_counters(get_conn(), station, hour_epoch)
        return core.compute_hour(counters, station, hour_epoch)

    def compute_history_result(station: str, hour_epoch: int, batch_id: int) -> dict:
        """网页与 API 共用的历史回溯入口，保证两边结果一致。

        校验启用状态与批次可回溯性后，从不可变快照还原该批提交完成时的
        61 个端点读数，再走与实时查询相同的 core.compute_hour 计算。
        """
        conn = get_conn()
        meta = db.get_history_meta(conn)
        if meta is None:
            raise HistoryLookupError(
                "历史回溯尚未启用：启用前的批次无法回溯，请先在历史回溯页启用", 409
            )
        baseline = meta["baseline_batch_id"]
        if not db.batch_exists(conn, batch_id):
            raise HistoryLookupError(f"批次 #{batch_id} 不存在", 404)
        if batch_id < baseline:
            raise HistoryLookupError(
                f"批次 #{batch_id} 是历史回溯启用前的旧批次，无法回溯"
                f"（历史起点为批次 #{baseline}，启用时间 {meta['enabled_at']}）",
                409,
            )
        counters = db.fetch_history_counters(conn, station, hour_epoch, batch_id)
        result = core.compute_hour(counters, station, hour_epoch)
        result["batch_id"] = batch_id
        result["history"] = {
            "enabled_at": meta["enabled_at"],
            "baseline_batch_id": baseline,
            "note": "该结果基于批次提交时的不可变快照，后续上传、失败或重启均不会改写",
        }
        return result

    def _parse_batch_id(raw: str) -> int:
        s = (raw or "").strip()
        if not s:
            raise HistoryLookupError("缺少 batch 参数", 400)
        try:
            value = int(s, 10)
        except ValueError:
            raise HistoryLookupError(f"batch 参数必须是整数批次号: {s!r}", 400) from None
        if value <= 0:
            raise HistoryLookupError(f"batch 参数必须为正整数: {value}", 400)
        return value

    def _history_meta_json() -> dict | None:
        meta = db.get_history_meta(get_conn())
        if meta is None:
            return None
        return {
            "enabled_at": meta["enabled_at"],
            "baseline_batch_id": meta["baseline_batch_id"],
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
        return render_template("batch.html", report=report)

    @app.get("/report")
    def report_html():
        station = request.args.get("station", "").strip()
        hour_text = request.args.get("hour", "").strip()
        if not station or not hour_text:
            flash("请填写站点和整点小时", "error")
            return redirect(url_for("index"))
        try:
            hour = core.parse_hour(hour_text)
        except ValueError as exc:
            flash(f"小时参数无效：{exc}", "error")
            return redirect(url_for("index"))
        result = compute_hour_result(station, hour)
        return render_template(
            "report.html",
            r=result,
            prev_hour=core.iso_utc(hour - core.HOUR_SECONDS),
            next_hour=core.iso_utc(hour + core.HOUR_SECONDS),
        )

    @app.get("/batches/<int:batch_id>")
    def batch_detail(batch_id):
        batch, rows = db.get_batch(get_conn(), batch_id)
        if batch is None:
            flash("批次不存在", "error")
            return redirect(url_for("index"))
        return render_template("batch_detail.html", batch=batch, rows=rows)

    # ---------------- 历史回溯网页 ----------------

    @app.get("/history")
    def history_html():
        conn = get_conn()
        station = request.args.get("station", "").strip()
        hour_text = request.args.get("hour", "").strip()
        batch_text = request.args.get("batch", "").strip()
        result = None
        error = None

        if station or hour_text or batch_text:
            # 任一查询参数存在即视为一次查询尝试，缺参数/非法参数明确报错
            if not station or not hour_text or not batch_text:
                error = "请同时填写站点、整点小时和批次号"
            else:
                try:
                    hour = core.parse_hour(hour_text)
                    batch_id = _parse_batch_id(batch_text)
                    result = compute_history_result(station, hour, batch_id)
                except ValueError as exc:  # 小时参数解析失败
                    error = f"小时参数无效：{exc}"
                except HistoryLookupError as exc:
                    error = str(exc)

        return render_template(
            "history.html",
            meta=_history_meta_json(),
            batches=db.list_batches_with_history_flag(conn),
            stations=db.list_stations(conn),
            result=result,
            error=error,
            form={"station": station, "hour": hour_text, "batch": batch_text},
        )

    @app.post("/history/enable")
    def history_enable_html():
        info = db.enable_history(get_conn())
        if info.get("already_enabled"):
            flash(
                f"历史回溯已启用，历史起点批次 #{info['baseline_batch_id']}（{info['enabled_at']}）",
                "info",
            )
        else:
            flash(
                f"历史回溯已启用：以当前 {info['snapshot_readings']} 条有效读数建立起点"
                f"（批次 #{info['baseline_batch_id']}，{info['enabled_at']}）；启用前的旧批次无法回溯",
                "info",
            )
        return redirect(url_for("history_html"))

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
        return jsonify(compute_hour_result(station, hour))

    # ---------------- 历史回溯 API（与网页同一入口，结果一致） ----------------

    @app.get("/api/history")
    def history_api():
        station = request.args.get("station", "").strip()
        if not station:
            return jsonify({"error": "缺少 station 参数"}), 400
        try:
            hour = core.parse_hour(request.args.get("hour", ""))
        except ValueError as exc:
            return jsonify({"error": f"hour 参数无效: {exc}"}), 400
        try:
            batch_id = _parse_batch_id(request.args.get("batch", ""))
        except HistoryLookupError as exc:
            return jsonify({"error": str(exc)}), exc.status
        try:
            return jsonify(compute_history_result(station, hour, batch_id))
        except HistoryLookupError as exc:
            return jsonify({"error": str(exc)}), exc.status

    @app.get("/api/history/status")
    def history_status_api():
        meta = _history_meta_json()
        if meta is None:
            return jsonify({"enabled": False})
        return jsonify({"enabled": True, **meta})

    @app.post("/api/history/enable")
    def history_enable_api():
        info = db.enable_history(get_conn())
        return jsonify(info), 200

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
