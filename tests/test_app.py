"""关键场景自测：核心计算、修订规则、事务原子性、网页/API 一致性、重启持久化。

运行：python3 -m unittest discover -s tests -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import core, db  # noqa: E402
from app.app import create_app  # noqa: E402


def epoch(y, mo, d, h, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp())


# 2026-11-17 09:00Z / 10:00Z / 11:00Z
T09, T10, T11 = epoch(2026, 11, 17, 9), epoch(2026, 11, 17, 10), epoch(2026, 11, 17, 11)


def csv_rows(station, device, start_epoch, counters, revision=1):
    """生成连续分钟的 CSV 行（无表头）。"""
    return [
        f"{station},{device},{core.iso_utc(start_epoch + i * 60)},{c},{revision}"
        for i, c in enumerate(counters)
    ]


class CoreParseTests(unittest.TestCase):
    def test_english_header(self):
        text = "station,device,minute,counter,revision\nS1,primary,2026-11-17T10:00:00Z,123,1\n"
        valid, errors = core.parse_csv_text(text)
        self.assertEqual(errors, [])
        self.assertEqual(len(valid), 1)
        r = valid[0][2]
        self.assertEqual((r.station, r.device, r.counter, r.revision), ("S1", "primary", 123, 1))
        self.assertEqual(r.minute_epoch, T10)

    def test_chinese_header_and_aliases(self):
        text = "站点,主备标记,UTC时间,计数,修订号\n站A,主,2026-11-17 10:00,5,2\n站A,备,2026-11-17 10:00,7,2\n"
        valid, errors = core.parse_csv_text(text)
        self.assertEqual(errors, [])
        self.assertEqual([v[2].device for v in valid], ["primary", "backup"])
        self.assertEqual(valid[0][2].minute_epoch, T10)  # 未带时区按 UTC

    def test_no_header(self):
        text = "S1,backup,2026-11-17T10:00Z,9999,3\n"
        valid, errors = core.parse_csv_text(text)
        self.assertEqual(errors, [])
        self.assertEqual(valid[0][2].counter, 9999)

    def test_timezone_offset_converted_to_utc(self):
        text = "S1,primary,2026-11-17T18:00:00+08:00,1,1\n"
        valid, _ = core.parse_csv_text(text)
        self.assertEqual(valid[0][2].minute_epoch, T10)

    def test_bad_rows_rejected_individually(self):
        text = "\n".join([
            "S1,primary,2026-11-17T10:00Z,100,1",      # ok
            "S1,primary,2026-11-17T10:01Z,10000,1",    # 计数超界
            "S1,primary,2026-11-17T10:02Z,-1,1",       # 计数为负
            "S1,primary,2026-11-17T10:03Z,abc,1",      # 计数非整数
            "S1,primary,2026-11-17T10:04:30Z,1,1",     # 秒非 00
            "S1,third,2026-11-17T10:05Z,1,1",          # 设备标记无法识别
            "S1,primary,2026-11-17T10:06Z,1,-2",       # 修订号为负
            "S1,primary,2026-11-17T10:07Z,1",          # 列数不足
            "S1,primary,2026-11-17T10:08Z,1,1,EXTRA",  # 多余数据列
            ",primary,2026-11-17T10:09Z,1,1",          # 站点为空
        ])
        valid, errors = core.parse_csv_text(text)
        self.assertEqual(len(valid), 1)
        self.assertEqual(len(errors), 9)
        self.assertEqual([e.row_no for e in errors], list(range(2, 11)))

    def test_file_level_errors(self):
        with self.assertRaises(core.CsvFormatError):
            core.parse_csv_text("")
        with self.assertRaises(core.CsvFormatError):
            core.parse_csv_text("station,device,minute,counter\n")  # 表头缺修订号列
        with self.assertRaises(core.CsvFormatError):
            core.parse_csv_text("station,device,minute,counter,revision\n")  # 只有表头

    def test_trailing_comma_tolerated(self):
        valid, errors = core.parse_csv_text("S1,primary,2026-11-17T10:00Z,1,1,\n")
        self.assertEqual(errors, [])
        self.assertEqual(len(valid), 1)

    def test_parse_hour_alignment(self):
        self.assertEqual(core.parse_hour("2026-11-17T10:00Z"), T10)
        self.assertEqual(core.parse_hour("2026-11-17 10:00"), T10)
        with self.assertRaises(ValueError):
            core.parse_hour("2026-11-17T10:30Z")


class CoreComputeTests(unittest.TestCase):
    def test_counter_delta_wraparound(self):
        self.assertEqual(core.counter_delta(9998, 3), 5)   # 下降按一次 10000 回绕
        self.assertEqual(core.counter_delta(100, 250), 150)
        self.assertEqual(core.counter_delta(7, 7), 0)

    def test_full_primary_hour(self):
        counters = {("primary", T10 + i * 60): 1000 + i for i in range(61)}
        r = core.compute_hour(counters, "S1", T10)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["total_pulses"], 60)
        self.assertEqual(r["rainfall_mm"], 12.0)  # 60 × 0.2
        self.assertEqual(r["missing_intervals"], 0)
        self.assertTrue(all(i["source"] == "primary" for i in r["intervals"]))

    def test_backup_fallback_and_no_cross_device(self):
        counters = {("primary", T10 + i * 60): 100 + i for i in range(61)}
        # 删掉主计 10:30，并放备计 10:30/10:31
        del counters[("primary", T10 + 30 * 60)]
        counters[("backup", T10 + 30 * 60)] = 500
        counters[("backup", T10 + 31 * 60)] = 504
        r = core.compute_hour(counters, "S1", T10)
        self.assertEqual(r["status"], "incomplete")
        self.assertIsNone(r["rainfall_mm"])
        # 第 29 段（10:29~10:30）：主计缺 10:30、备计缺 10:29，跨设备不可减 -> 缺失
        self.assertEqual(r["intervals"][29]["source"], "missing")
        # 第 30 段（10:30~10:31）：备计两端齐全 -> 备计差值
        self.assertEqual(r["intervals"][30]["source"], "backup")
        self.assertEqual(r["intervals"][30]["pulses"], 4)
        # 第 31 段（10:31~10:32）：主计齐全 -> 主计
        self.assertEqual(r["intervals"][31]["source"], "primary")
        self.assertEqual(r["missing_intervals"], 1)
        self.assertEqual(r["source_counts"]["backup"], 1)

    def test_missing_interval_makes_hour_unavailable(self):
        counters = {("primary", T10 + i * 60): i for i in range(61)}
        del counters[("primary", T10 + 45 * 60)]
        r = core.compute_hour(counters, "S1", T10)
        self.assertEqual(r["status"], "incomplete")
        self.assertIsNone(r["rainfall_mm"])
        self.assertIsNone(r["total_pulses"])
        self.assertEqual(r["missing_intervals"], 2)  # 44~45 与 45~46 两段都缺端点

    def test_wraparound_in_interval(self):
        # 计数器在 10:00~10:01 间从 9998 回绕到 3 -> 5 脉冲，其余分钟不变
        counters = {("primary", T10 + i * 60): 3 for i in range(61)}
        counters[("primary", T10)] = 9998
        r = core.compute_hour(counters, "S1", T10)
        self.assertEqual(r["intervals"][0]["pulses"], 5)
        self.assertEqual(r["rainfall_mm"], 1.0)

    def test_hour_must_align(self):
        with self.assertRaises(ValueError):
            core.compute_hour({}, "S1", T10 + 60)


class DbTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.db")
        self.conn = db.connect(self.path)
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def _reading(self, minute, counter, rev, device="primary", station="S1"):
        return core.Reading(station, device, minute, counter, rev)

    def test_revision_rule(self):
        rows = [(1, "raw", self._reading(T10, 100, 1))]
        rep = db.apply_batch(self.conn, rows, [], "a.csv")
        self.assertEqual(rep["accepted_count"], 1)

        # 同修订号、低修订号都被拒绝
        rep = db.apply_batch(self.conn, [(1, "raw", self._reading(T10, 200, 1))], [], "b.csv")
        self.assertEqual(rep["accepted_count"], 0)
        self.assertEqual(rep["rejected_count"], 1)
        rep = db.apply_batch(self.conn, [(1, "raw", self._reading(T10, 300, 0))], [], "c.csv")
        self.assertEqual(rep["accepted_count"], 0)

        # 更高修订号被接受并覆盖
        rep = db.apply_batch(self.conn, [(1, "raw", self._reading(T10, 400, 2))], [], "d.csv")
        self.assertEqual(rep["accepted_count"], 1)
        self.assertEqual(rep["accepted"][0]["action"], "updated")
        got = db.fetch_hour_counters(self.conn, "S1", T10)
        self.assertEqual(got[("primary", T10)], 400)

    def test_same_key_within_one_batch(self):
        rows = [
            (1, "r1", self._reading(T10, 100, 1)),
            (2, "r2", self._reading(T10, 150, 2)),  # 更高修订，覆盖同批前一行
            (3, "r3", self._reading(T10, 120, 2)),  # 同修订号，拒绝
        ]
        rep = db.apply_batch(self.conn, rows, [], "e.csv")
        self.assertEqual(rep["accepted_count"], 2)
        self.assertEqual(rep["rejected_count"], 1)
        got = db.fetch_hour_counters(self.conn, "S1", T10)
        self.assertEqual(got[("primary", T10)], 150)

    def test_atomic_rollback_no_partial_batch(self):
        # 第二行设备标记非法（绕过解析层直接构造），触发 CHECK 约束失败
        bad = core.Reading("S1", "tertiary", T10 + 60, 5, 1)
        rows = [(1, "ok", self._reading(T10, 100, 1)), (2, "bad", bad)]
        with self.assertRaises(sqlite3.Error):
            db.apply_batch(self.conn, rows, [], "f.csv")
        # 回滚：读数与批次记录都不留
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM batch_rows").fetchone()[0], 0)

    def test_persistence_across_reopen(self):
        rows = [(1, "raw", self._reading(T10, 100, 1))]
        db.apply_batch(self.conn, rows, [], "g.csv")
        self.conn.close()
        conn2 = db.connect(self.path)
        try:
            got = db.fetch_hour_counters(conn2, "S1", T10)
            self.assertEqual(got[("primary", T10)], 100)
        finally:
            conn2.close()
        self.conn = db.connect(self.path)  # tearDown 用


class AppTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "app.db")
        self.app = create_app(self.path)
        self.client = self.app.test_client()

    def _upload(self, text):
        return self.client.post("/api/upload", data={"csv_text": text})

    def _hourly(self, station, hour_text):
        resp = self.client.get(f"/api/hourly?station={station}&hour={hour_text}")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_upload_and_query_flow(self):
        text = "\n".join(csv_rows("S1", "primary", T10, [1000 + i for i in range(61)]))
        resp = self._upload(text)
        self.assertEqual(resp.status_code, 200)
        rep = resp.get_json()
        self.assertEqual(rep["accepted_count"], 61)
        self.assertEqual(rep["rejected_count"], 0)

        r = self._hourly("S1", "2026-11-17T10:00Z")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["rainfall_mm"], 12.0)

        # 网页与 API 一致
        html = self.client.get("/report?station=S1&hour=2026-11-17T10:00Z").get_data(as_text=True)
        self.assertIn("12.0 mm", html)
        self.assertIn("可供数", html)

    def test_boundary_revision_changes_both_hours_consistently(self):
        # 09:00~11:00 共 121 个主计读数，每分钟 +10 脉冲
        text = "\n".join(csv_rows("S1", "primary", T09, [1000 + 10 * i for i in range(121)]))
        self.assertEqual(self._upload(text).get_json()["accepted_count"], 121)
        self.assertEqual(self._hourly("S1", "2026-11-17T09:00Z")["rainfall_mm"], 120.0)
        self.assertEqual(self._hourly("S1", "2026-11-17T10:00Z")["rainfall_mm"], 120.0)

        # 修订整点 10:00 读数：1600 -> 1595（修订号 2），同时影响前后两小时
        rev = f"S1,primary,{core.iso_utc(T10)},1595,2"
        rep = self._upload(rev).get_json()
        self.assertEqual(rep["accepted_count"], 1)
        self.assertEqual(rep["accepted"][0]["action"], "updated")

        r09 = self._hourly("S1", "2026-11-17T09:00Z")
        r10 = self._hourly("S1", "2026-11-17T10:00Z")
        self.assertEqual(r09["rainfall_mm"], 119.0)  # 末段少 5 脉冲
        self.assertEqual(r10["rainfall_mm"], 121.0)  # 首段多 5 脉冲

        # 网页与 API 对两小时都一致
        h09 = self.client.get("/report?station=S1&hour=2026-11-17T09:00Z").get_data(as_text=True)
        h10 = self.client.get("/report?station=S1&hour=2026-11-17T10:00Z").get_data(as_text=True)
        self.assertIn("119.0 mm", h09)
        self.assertIn("121.0 mm", h10)

    def test_stale_revision_rejected_and_data_unchanged(self):
        text = "\n".join(csv_rows("S1", "primary", T10, [1000 + i for i in range(61)], revision=2))
        self._upload(text)
        before = self._hourly("S1", "2026-11-17T10:00Z")

        stale = f"S1,primary,{core.iso_utc(T10)},5555,1"   # 低修订
        same = f"S1,primary,{core.iso_utc(T10 + 60)},5555,2"  # 同修订
        rep = self._upload("\n".join([stale, same])).get_json()
        self.assertEqual(rep["accepted_count"], 0)
        self.assertEqual(rep["rejected_count"], 2)
        after = self._hourly("S1", "2026-11-17T10:00Z")
        self.assertEqual(before, after)

    def test_incomplete_hour_and_gap_display(self):
        # 缺 10:45 一个端点 -> 两段缺失，整小时不可供数
        counters = [1000 + i for i in range(61)]
        lines = csv_rows("S1", "primary", T10, counters)
        del lines[45]
        self._upload("\n".join(lines))
        r = self._hourly("S1", "2026-11-17T10:00Z")
        self.assertEqual(r["status"], "incomplete")
        self.assertIsNone(r["rainfall_mm"])
        self.assertEqual(r["missing_intervals"], 2)
        html = self.client.get("/report?station=S1&hour=2026-11-17T10:00Z").get_data(as_text=True)
        self.assertIn("不可供数", html)
        self.assertIn("缺口 2 / 60", html)

    def test_bad_file_leaves_nothing(self):
        resp = self._upload("station,device,minute,counter\n")  # 表头缺列
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/upload", data={})  # 空上传
        self.assertEqual(resp.status_code, 400)
        batches = self.client.get("/api/batches").get_json()
        self.assertEqual(batches, [])

    def test_partial_rows_atomic_report(self):
        good = csv_rows("S1", "primary", T10, [1000 + i for i in range(61)])
        good[10] = "S1,primary,not-a-time,1,1"  # 一行坏行
        rep = self._upload("\n".join(good)).get_json()
        self.assertEqual(rep["accepted_count"], 60)
        self.assertEqual(rep["rejected_count"], 1)
        self.assertIn("时间", rep["rejected"][0]["reason"])
        # 缺 10:10 端点 -> 不可供数
        r = self._hourly("S1", "2026-11-17T10:00Z")
        self.assertEqual(r["status"], "incomplete")

    def test_restart_persistence(self):
        text = "\n".join(csv_rows("S1", "primary", T10, [1000 + i for i in range(61)]))
        self._upload(text)
        # 模拟重启：同一数据库文件新建应用实例
        app2 = create_app(self.path)
        client2 = app2.test_client()
        r = client2.get("/api/hourly?station=S1&hour=2026-11-17T10:00Z").get_json()
        self.assertEqual(r["rainfall_mm"], 12.0)

    def test_health_and_stations(self):
        self.assertEqual(self.client.get("/api/health").get_json()["status"], "ok")
        text = "\n".join(csv_rows("S1", "backup", T10, [i for i in range(61)]))
        self._upload(text)
        stations = self.client.get("/api/stations").get_json()
        self.assertEqual(stations[0]["station"], "S1")
        self.assertEqual(stations[0]["readings"], 61)

    def test_hour_param_validation(self):
        resp = self.client.get("/api/hourly?station=S1&hour=2026-11-17T10:30Z")
        self.assertEqual(resp.status_code, 400)
        resp = self.client.get("/api/hourly?hour=2026-11-17T10:00Z")
        self.assertEqual(resp.status_code, 400)


class HistoryTests(unittest.TestCase):
    """历史批次查询：起点基线、按批快照、不可改写、网页/API 一致。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "hist.db")
        self.app = create_app(self.path)
        self.client = self.app.test_client()

    def _upload(self, text):
        return self.client.post("/api/upload", data={"csv_text": text}).get_json()

    def _hourly(self, station, hour_text, batch=None):
        url = f"/api/hourly?station={station}&hour={hour_text}"
        if batch is not None:
            url += f"&batch={batch}"
        return self.client.get(url)

    def _html(self, station, hour_text, batch=None):
        url = f"/report?station={station}&hour={hour_text}"
        if batch is not None:
            url += f"&batch={batch}"
        return self.client.get(url, follow_redirects=True).get_data(as_text=True)

    def _enable(self):
        resp = self.client.post("/api/history/enable")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_query_before_enable_is_rejected(self):
        rep = self._upload("\n".join(csv_rows("S1", "primary", T10, [1000 + i for i in range(61)])))
        self.assertEqual(rep["accepted_count"], 61)
        self.assertFalse(self.client.get("/api/history").get_json()["status"]["enabled"])

        resp = self._hourly("S1", "2026-11-17T10:00Z", batch=rep["batch_id"])
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["reason"], "history_disabled")
        self.assertIn("尚未启用", self._html("S1", "2026-11-17T10:00Z", batch=rep["batch_id"]))
        # 实时查询不受影响
        self.assertEqual(self._hourly("S1", "2026-11-17T10:00Z").get_json()["rainfall_mm"], 12.0)

    def test_enable_uses_current_readings_as_start_and_old_batch_not_traceable(self):
        old = self._upload("\n".join(csv_rows("S1", "primary", T10, [1000 + i for i in range(61)])))
        info = self._enable()
        self.assertEqual(info["row_count"], 61)
        self.assertEqual(info["enabled_after_batch_id"], old["batch_id"])
        self.assertFalse(info["already_enabled"])

        # 旧批次明确提示无法回溯（网页与 API 语义一致）
        r = self._hourly("S1", "2026-11-17T10:00Z", batch=old["batch_id"]).get_json()
        self.assertEqual(r["reason"], "before_history")
        self.assertIn("无法回溯", r["error"])
        self.assertIn("无法回溯", self._html("S1", "2026-11-17T10:00Z", batch=old["batch_id"]))

        # 未知批次
        resp = self._hourly("S1", "2026-11-17T10:00Z", batch=999)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["reason"], "unknown_batch")

    def test_enable_is_idempotent_and_baseline_is_frozen(self):
        self._upload("\n".join(csv_rows("S1", "primary", T10, [1000 + i for i in range(61)])))
        first = self._enable()
        # 启用后再上传，不改变起点与基线
        self._upload("\n".join(csv_rows("S1", "backup", T10, [5 + i for i in range(61)])))
        again = self._enable()
        self.assertTrue(again["already_enabled"])
        self.assertEqual(again["established_at"], first["established_at"])
        self.assertEqual(again["enabled_after_batch_id"], first["enabled_after_batch_id"])
        self.assertEqual(again["row_count"], first["row_count"])

        conn = db.connect(self.path)
        try:
            baseline = conn.execute(
                "SELECT COUNT(*) FROM baseline_readings").fetchone()[0]
            # 启用前的批次不单独入快照，避免与基线重复
            snap = conn.execute(
                "SELECT COUNT(*) FROM batch_readings WHERE batch_id=1").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(baseline, first["row_count"])
        self.assertEqual(snap, 0)

    def test_post_enable_batches_are_snapshotted(self):
        self._upload("\n".join(csv_rows("S1", "primary", T10, [1000 + i for i in range(61)])))
        self._enable()
        # 11:00 起的读数，修订号 2 以覆盖共用的 11:00 端点
        b = self._upload("\n".join(csv_rows("S1", "primary", T11, [5 + i for i in range(61)], revision=2)))
        self.assertEqual(b["accepted_count"], 61)
        conn = db.connect(self.path)
        try:
            rows = conn.execute(
                "SELECT batch_id, COUNT(*) FROM batch_readings GROUP BY batch_id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual([(r[0], r[1]) for r in rows], [(b["batch_id"], 61)])

    def test_historical_snapshot_not_rewritten_by_later_uploads(self):
        self._upload("\n".join(csv_rows("S1", "primary", T10, [1000 + i for i in range(61)])))
        self._enable()
        b1 = self._upload("\n".join(csv_rows("S1", "primary", T10, [1000 + 2 * i for i in range(61)], revision=2)))
        self.assertEqual(b1["accepted_count"], 61)
        r1 = self._hourly("S1", "2026-11-17T10:00Z", batch=b1["batch_id"]).get_json()
        self.assertEqual(r1["mode"], "historical")
        self.assertEqual(r1["rainfall_mm"], 24.0)
        self.assertEqual(r1["batch"]["batch_id"], b1["batch_id"])

        # 后续修订：批次 #b1 的查询结果必须保持当时的 24.0
        b2 = self._upload("\n".join(csv_rows("S1", "primary", T10, [1000 + 3 * i for i in range(61)], revision=3)))
        self.assertEqual(b2["accepted_count"], 61)
        again = self._hourly("S1", "2026-11-17T10:00Z", batch=b1["batch_id"]).get_json()
        self.assertEqual(again["rainfall_mm"], 24.0)
        self.assertEqual(again["intervals"], r1["intervals"])
        # 批次 #b2 反映新值；实时结果也反映新值
        self.assertEqual(self._hourly("S1", "2026-11-17T10:00Z", batch=b2["batch_id"]).get_json()["rainfall_mm"], 36.0)
        self.assertEqual(self._hourly("S1", "2026-11-17T10:00Z").get_json()["rainfall_mm"], 36.0)

    def test_failed_batch_leaves_no_snapshot(self):
        self._upload("\n".join(csv_rows("S1", "primary", T10, [1000 + i for i in range(61)])))
        self._enable()
        before = self.client.get("/api/history").get_json()
        # 整文件不可用 -> 不产生批次与快照
        resp = self.client.post("/api/upload", data={"csv_text": "station,device,minute,counter\n"})
        self.assertEqual(resp.status_code, 400)
        after = self.client.get("/api/history").get_json()
        self.assertEqual(before["status"], after["status"])
        self.assertEqual(len(before["batches"]), len(after["batches"]))

    def test_same_key_multiple_revisions_in_one_batch_final_value_wins(self):
        # 基线全部为 3：首段 10:00 计数改为 9998 时按回绕得 5 脉冲，便于验证最终值生效
        self._upload("\n".join(csv_rows("S1", "primary", T10, [3 for _ in range(61)])))
        self._enable()
        # 同批对同一分钟修订两次（修订号 2、3），快照只保留该批最终值
        rep = self._upload("\n".join([
            f"S1,primary,{core.iso_utc(T10)},100,2",
            f"S1,primary,{core.iso_utc(T10)},9998,3",
        ]))
        self.assertEqual(rep["accepted_count"], 2)
        bid = rep["batch_id"]
        r = self._hourly("S1", "2026-11-17T10:00Z", batch=bid).get_json()
        self.assertEqual(r["intervals"][0]["pulses"], 5)   # 9998 -> 3 回绕 5 脉冲
        self.assertEqual(r["rainfall_mm"], 1.0)
        # 若误用了同批前一条 100（应为 9903），这里会是 1980.6
        self.assertEqual(self._hourly("S1", "2026-11-17T10:00Z").get_json()["rainfall_mm"], 1.0)

    def test_rejected_revision_does_not_change_snapshot(self):
        self._upload("\n".join(csv_rows("S1", "primary", T10, [1000 + i for i in range(61)])))
        self._enable()
        b1 = self._upload("\n".join(csv_rows("S1", "primary", T10, [1000 + i for i in range(61)])))
        base = self._hourly("S1", "2026-11-17T10:00Z", batch=b1["batch_id"]).get_json()
        # 低修订被拒绝，不进入快照
        rep = self._upload(f"S1,primary,{core.iso_utc(T10)},5555,0")
        self.assertEqual(rep["rejected_count"], 1)
        bid = rep["batch_id"]
        got = self._hourly("S1", "2026-11-17T10:00Z", batch=bid).get_json()
        self.assertEqual(got["rainfall_mm"], base["rainfall_mm"])
        self.assertEqual(got["intervals"], base["intervals"])

    def test_historical_applies_primary_backup_wrap_and_gap_rules(self):
        lines = csv_rows("S1", "primary", T10, [1000 + i for i in range(61)])
        # 主计缺 10:30，备计只补回 10:30 一个端点；计数在 10:00~10:01 回绕
        del lines[30]
        lines[0] = f"S1,primary,{core.iso_utc(T10)},9998,1"
        lines.append(f"S1,backup,{core.iso_utc(T10 + 30 * 60)},500,1")
        self._upload("\n".join(lines))
        self._enable()
        b = self._upload("\n".join(csv_rows("S1", "backup", T11, [10 + i for i in range(61)])))
        r = self._hourly("S1", "2026-11-17T10:00Z", batch=b["batch_id"]).get_json()
        self.assertEqual(r["status"], "incomplete")               # 缺端点 -> 整小时不供数
        self.assertEqual(r["intervals"][0]["pulses"], 1003)       # 9998 -> 1001 回绕
        self.assertEqual(r["intervals"][29]["source"], "missing")  # 主计缺 10:30，备计无 10:29，不跨设备
        self.assertEqual(r["intervals"][30]["source"], "missing")  # 备计只有 10:30，无 10:31
        self.assertEqual(r["source_counts"]["primary"], 58)
        self.assertEqual(r["source_counts"]["missing"], 2)

    def test_web_and_api_results_identical(self):
        self._upload("\n".join(csv_rows("S1", "primary", T10, [1000 + i for i in range(61)])))
        self._enable()
        b = self._upload("\n".join(csv_rows("S1", "primary", T10, [1000 + 2 * i for i in range(61)], revision=2)))
        self.assertEqual(b["accepted_count"], 61)
        api = self._hourly("S1", "2026-11-17T10:00Z", batch=b["batch_id"]).get_json()
        html = self._html("S1", "2026-11-17T10:00Z", batch=b["batch_id"])
        self.assertIn("24.0 mm", html)
        self.assertIn("历史批次 #%d" % b["batch_id"], html)
        for it in api["intervals"]:
            self.assertIn(it["start"], html)
        # 与实时结果同字段一致（除 mode/batch）
        live = self._hourly("S1", "2026-11-17T10:00Z").get_json()
        for key in ("status", "rainfall_mm", "total_pulses", "missing_intervals",
                    "source_counts", "intervals", "station", "hour_start"):
            self.assertEqual(api[key], live[key])

    def test_history_survives_restart(self):
        self._upload("\n".join(csv_rows("S1", "primary", T10, [1000 + i for i in range(61)])))
        self._enable()
        b1 = self._upload("\n".join(csv_rows("S1", "primary", T10, [1000 + 2 * i for i in range(61)], revision=2)))
        self._upload("\n".join(csv_rows("S1", "primary", T10, [1000 + 3 * i for i in range(61)], revision=3)))

        app2 = create_app(self.path)
        client2 = app2.test_client()
        r = client2.get(f"/api/hourly?station=S1&hour=2026-11-17T10:00Z&batch={b1['batch_id']}").get_json()
        self.assertEqual(r["rainfall_mm"], 24.0)
        # 重启后仍明确拒绝启用前的批次
        r_old = client2.get("/api/hourly?station=S1&hour=2026-11-17T10:00Z&batch=1").get_json()
        self.assertEqual(r_old["reason"], "before_history")

    def test_history_page_shows_status_and_batch_list(self):
        self._upload("\n".join(csv_rows("S1", "primary", T10, [1000 + i for i in range(61)])))
        html = self.client.get("/history").get_data(as_text=True)
        self.assertIn("未启用", html)
        self.assertIn("无法回溯", html)
        self.client.post("/history/enable")
        self._upload("\n".join(csv_rows("S1", "primary", T10, [1000 + i for i in range(61)], revision=2)))
        html = self.client.get("/history").get_data(as_text=True)
        self.assertIn("已启用", html)
        self.assertIn("可回溯批次", html)


if __name__ == "__main__":
    unittest.main()
