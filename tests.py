"""Run: python -m unittest tests -v. All cases use synthetic data."""
import hashlib
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from PIL import Image
import app
from planner import analyze, cobb_from_points

SAMPLES = {
    "spine": {"cobb_angle": 48, "flexibility_pct": 55, "coronal_balance_mm": 28, "apex_region": "thoracic"},
    "joint": {"joint_type": "hip", "acetabular_diameter_mm": 52, "canal_width_mm": 13, "offset_mm": 42, "bone_quality": "normal", "side": "left"},
    "trauma": {"fracture_site": "tibia", "fracture_segment": "shaft", "fracture_pattern": "comminuted", "displacement_mm": 18, "articular_involvement": True, "soft_tissue": "compromised", "open_status": "closed"},
}


def png(width=400, height=800, color="gray"):
    output = io.BytesIO()
    Image.new("RGB", (width, height), color).save(output, format="PNG")
    return output.getvalue()


class PlannerTests(unittest.TestCase):
    def test_all_scenarios_return_ranked_explainable_options(self):
        for scenario, measurements in SAMPLES.items():
            r = analyze(scenario, measurements)
            scores = [o["score"] for o in r["ranked_options"]]
            self.assertEqual(scores, sorted(scores, reverse=True))
            self.assertTrue(r["method"])
            self.assertTrue(r["required_review"])

    def test_invalid_measurements_rejected(self):
        with self.assertRaises(ValueError):
            analyze("spine", {**SAMPLES["spine"], "cobb_angle": 200})

    def test_low_flexibility_branch_is_sorted(self):
        r = analyze("spine", {**SAMPLES["spine"], "flexibility_pct": 20})
        self.assertEqual([o["score"] for o in r["ranked_options"]], [76, 69])

    def test_spine_does_not_invent_fusion_levels(self):
        r = analyze("spine", SAMPLES["spine"])
        self.assertNotIn("上下各", r["summary"])
        self.assertIn("尚未生成融合节段", r["summary"])

    def test_joint_no_fit_stops_recommendation(self):
        r = analyze("joint", {**SAMPLES["joint"], "acetabular_diameter_mm": 90, "canal_width_mm": 25, "offset_mm": 70})
        self.assertEqual(r["ranked_options"], [])
        self.assertEqual(r["readiness"], "no_candidate")
        self.assertTrue(r["rejected_options"])
        self.assertNotIn("首选为", r["summary"])

    def test_hip_cup_and_stem_are_separate(self):
        first = analyze("joint", SAMPLES["joint"])["ranked_options"][0]
        self.assertEqual(first["components"], ["H52", "S13"])
        self.assertEqual(first["score"], 100)

    def test_legacy_hip_input_alias(self):
        m = dict(SAMPLES["joint"])
        m["bone_diameter_mm"] = m.pop("acetabular_diameter_mm")
        self.assertEqual(analyze("joint", m)["ranked_options"][0]["components"], ["H52", "S13"])

    def test_knee_uses_four_dimensions(self):
        m = {"joint_type": "knee", "bone_quality": "normal", "femoral_ap_mm": 62, "femoral_ml_mm": 66, "tibial_ap_mm": 46, "tibial_ml_mm": 70}
        self.assertEqual(analyze("joint", m)["ranked_options"][0]["components"], ["K62"])
        with self.assertRaises(ValueError):
            analyze("joint", {k: v for k, v in m.items() if k != "tibial_ml_mm"})

    def test_string_false_is_rejected(self):
        with self.assertRaises(ValueError):
            analyze("trauma", {**SAMPLES["trauma"], "articular_involvement": "false"})

    def test_boolean_and_nonfinite_numbers_rejected(self):
        for bad in (True, float("nan"), float("inf"), "48"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                analyze("spine", {**SAMPLES["spine"], "cobb_angle": bad})

    def test_external_fixation_not_in_internal_ranking(self):
        r = analyze("trauma", SAMPLES["trauma"])
        self.assertEqual({o["name"] for o in r["ranked_options"]}, {"锁定钢板", "髓内钉"})
        self.assertTrue(r["staged_options"])

    def test_missing_trauma_details_are_disclosed(self):
        r = analyze("trauma", {k: v for k, v in SAMPLES["trauma"].items() if k not in ("open_status", "fracture_segment")})
        self.assertEqual(r["readiness"], "needs_input")

    def test_four_point_angle_respects_image_aspect_ratio(self):
        self.assertEqual(cobb_from_points([[.1,.2],[.9,.2],[.1,.6],[.9,.8]], 400, 800), 26.6)

    def test_degenerate_measurement_rejected(self):
        with self.assertRaises(ValueError):
            cobb_from_points([[.1,.2],[.1,.2],[.1,.6],[.9,.8]], 400, 800)


class QuietHandler(app.Handler):
    def log_message(self, *args):
        pass


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="ortho-v2-tests-")
        cls.old_paths = app.DATA, app.DB, app.UPLOADS
        app.DATA = Path(cls.temp.name)
        app.DB = app.DATA / "test.sqlite3"
        app.UPLOADS = app.DATA / "uploads"
        with app.connect():
            pass
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.opener = build_opener(ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        app.DATA, app.DB, app.UPLOADS = cls.old_paths
        cls.temp.cleanup()

    def request(self, path, payload=None, raw=None, headers=None):
        data = json.dumps(payload).encode() if payload is not None else raw
        req = Request(self.base+path, data=data, headers=headers or ({"Content-Type": "application/json"} if payload is not None else {}))
        try:
            with self.opener.open(req, timeout=10) as response:
                body = response.read()
                return response.status, json.loads(body) if "application/json" in response.headers.get("Content-Type", "") else body
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def create(self, scenario="spine", measurements=None, **extra):
        payload = {"code": "TEST-"+scenario, "age": 35, "sex": "female", "scenario": scenario, "measurements": measurements or SAMPLES[scenario], **extra}
        status, result = self.request("/api/cases", payload)
        self.assertEqual(status, 201, result)
        return result["id"]

    def generate(self, cid):
        status, result = self.request(f"/api/cases/{cid}/analyze", {})
        self.assertEqual(status, 201, result)
        return result

    def detail(self, cid):
        return self.request("/api/cases/"+cid)[1]

    def review(self, pid, status="approved"):
        return self.request(f"/api/plans/{pid}/review", {"status": status, "reviewer": "Test Reviewer", "note": "Synthetic evidence checked"})

    def upload(self, cid, body=None):
        return self.request(f"/api/cases/{cid}/image", raw=body or png(), headers={"Content-Type": "image/png"})

    def test_closed_loop_for_all_three_scenarios(self):
        for scenario in SAMPLES:
            cid = self.create(scenario)
            plan = self.generate(cid)
            self.assertEqual(self.review(plan["id"])[0], 200)
            detail = self.detail(cid)
            self.assertEqual(detail["plans"][0]["status"], "approved")
            status, html = self.request(f"/api/plans/{plan['id']}/export")
            self.assertEqual(status, 200)
            self.assertIn("打印或保存为 PDF".encode(), html)

    def test_duplicate_review_rejected(self):
        plan = self.generate(self.create())
        self.assertEqual(self.review(plan["id"])[0], 200)
        self.assertEqual(self.review(plan["id"])[0], 409)

    def test_image_attachment_round_trip(self):
        cid = self.create()
        data = png()
        status, uploaded = self.upload(cid, data)
        self.assertEqual(status, 200)
        image = self.detail(cid)["images"][0]
        self.assertEqual(image["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(self.request(image["original_url"])[1], data)
        self.assertEqual(self.request(uploaded["image_url"])[0], 200)

    def test_same_timestamp_uses_revision_order(self):
        cid = self.create()
        with patch("app.now", return_value="2026-10-22T00:00:00+00:00"):
            plans = [self.generate(cid) for _ in range(3)]
        detail = self.detail(cid)
        self.assertEqual([p["revision"] for p in detail["plans"]], [3,2,1])
        self.assertEqual(detail["case"]["current_plan_id"], plans[-1]["id"])

    def test_truncated_image_rejected(self):
        cid = self.create()
        self.assertEqual(self.upload(cid, b"\x89PNG\r\n\x1a\n")[0], 400)
        self.assertEqual(self.detail(cid)["images"], [])

    def test_image_format_mismatch_rejected(self):
        cid = self.create()
        status, _ = self.request(f"/api/cases/{cid}/image", raw=png(), headers={"Content-Type": "image/jpeg"})
        self.assertEqual(status, 400)

    def test_approved_snapshot_keeps_old_image(self):
        cid = self.create()
        old_image = self.upload(cid)[1]
        pid = self.generate(cid)["id"]
        self.assertEqual(self.review(pid)[0], 200)
        self.upload(cid, png(color="white"))
        detail = self.detail(cid)
        plan = detail["plans"][0]
        self.assertEqual(plan["input_snapshot"]["image"]["id"], old_image["image_id"])
        self.assertEqual(plan["status"], "approved")
        self.assertFalse(plan["is_current"])
        self.assertEqual(self.request(old_image["image_url"])[0], 200)
        self.assertIsNone(detail["case"]["current_plan_id"])
        self.assertEqual(len(detail["images"]), 2)

    def test_export_uses_snapshot_and_trace_ids(self):
        cid = self.create()
        pid = self.generate(cid)["id"]
        c = self.detail(cid)["case"]
        self.request(f"/api/cases/{cid}/revise", {**c, "code": "CHANGED-CODE", "expected_revision": c["input_revision"]})
        _, html = self.request(f"/api/plans/{pid}/export")
        for text in (pid, cid, "TEST-spine", app.ALGORITHM_VERSION):
            self.assertIn(text.encode(), html)
        self.assertNotIn(b"CHANGED-CODE", html)

    def test_rejected_plan_can_be_revised_and_regenerated(self):
        cid = self.create()
        first = self.generate(cid)
        self.review(first["id"], "rejected")
        c = self.detail(cid)["case"]
        status, _ = self.request(f"/api/cases/{cid}/revise", {**c, "measurements": {**c["measurements"], "cobb_angle": 55}, "expected_revision": c["input_revision"]})
        self.assertEqual(status, 200)
        second = self.generate(cid)
        detail = self.detail(cid)
        self.assertEqual(second["revision"], 2)
        self.assertEqual(detail["plans"][1]["status"], "rejected")
        self.assertEqual(detail["plans"][1]["input_snapshot"]["measurements"]["cobb_angle"], 48)
        self.assertEqual(detail["plans"][0]["input_snapshot"]["measurements"]["cobb_angle"], 55)

    def test_stale_editor_cannot_overwrite(self):
        cid = self.create()
        c = self.detail(cid)["case"]
        payload = {**c, "expected_revision": c["input_revision"]}
        self.assertEqual(self.request(f"/api/cases/{cid}/revise", payload)[0], 200)
        self.assertEqual(self.request(f"/api/cases/{cid}/revise", payload)[0], 409)

    def test_stale_plan_review_rejected(self):
        cid = self.create()
        old = self.generate(cid)
        self.generate(cid)
        self.assertEqual(self.review(old["id"])[0], 409)

    def test_concurrent_review_only_one_wins(self):
        pid = self.generate(self.create())["id"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _: self.review(pid)[0], range(2)))
        self.assertEqual(sorted(statuses), [200,409])

    def test_no_candidate_cannot_be_approved(self):
        cid = self.create("joint", {**SAMPLES["joint"], "acetabular_diameter_mm": 90, "canal_width_mm": 25, "offset_mm": 70})
        pid = self.generate(cid)["id"]
        self.assertEqual(self.review(pid)[0], 409)
        self.assertEqual(self.review(pid, "rejected")[0], 200)

    def test_fractional_age_rejected(self):
        status, _ = self.request("/api/cases", {"code": "BAD-AGE", "age": 35.7, "sex": "female", "scenario": "spine", "measurements": SAMPLES["spine"]})
        self.assertEqual(status, 400)

    def test_string_false_rejected_over_http(self):
        status, _ = self.request("/api/cases", {"code": "BAD-BOOL", "age": 35, "sex": "female", "scenario": "trauma", "measurements": {**SAMPLES["trauma"], "articular_involvement": "false"}})
        self.assertEqual(status, 400)

    def test_index_and_static_assets(self):
        for path in ("/", "/index.html", "/static/app.js", "/static/style.css"):
            self.assertEqual(self.request(path)[0], 200)

    def test_four_point_measurement_is_versioned(self):
        cid = self.create()
        image = self.upload(cid)[1]
        old = self.generate(cid)
        c = self.detail(cid)["case"]
        points = [[.1,.2],[.9,.2],[.1,.6],[.9,.8]]
        status, result = self.request(f"/api/cases/{cid}/measure-cobb", {"image_id": image["image_id"], "points": points, "expected_revision": c["input_revision"]})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["angle"], 26.6)
        new = self.generate(cid)
        detail = self.detail(cid)
        self.assertEqual(detail["plans"][0]["input_snapshot"]["measurement_source"]["points"], points)
        self.assertEqual(detail["plans"][1]["id"], old["id"])
        self.assertNotEqual(old["id"], new["id"])

    def test_json_snapshot_download(self):
        pid = self.generate(self.create())["id"]
        status, data = self.request(f"/api/plans/{pid}/json")
        self.assertEqual(status, 200)
        self.assertEqual(data["id"], pid)
        self.assertIn("measurements", data["input_snapshot"])

    def test_cross_origin_request_rejected(self):
        status, _ = self.request("/api/cases", {}, headers={"Content-Type": "application/json", "Origin": "https://example.invalid"})
        self.assertEqual(status, 403)


class MigrationTests(unittest.TestCase):
    def test_legacy_schema_history_is_preserved(self):
        with tempfile.TemporaryDirectory(prefix="ortho-migration-") as td:
            db = sqlite3.connect(Path(td)/"legacy.sqlite3")
            db.row_factory = sqlite3.Row
            db.executescript("CREATE TABLE cases(id TEXT PRIMARY KEY,code TEXT,scenario TEXT,age INTEGER,sex TEXT,measurements TEXT,image_name TEXT,created_at TEXT); CREATE TABLE plans(id TEXT PRIMARY KEY,case_id TEXT,result TEXT,status TEXT,reviewer TEXT,review_note TEXT,reviewed_at TEXT,created_at TEXT);")
            db.execute("INSERT INTO cases VALUES(?,?,?,?,?,?,?,?)", ("legacy", "LEGACY-001", "spine", 30, "female", json.dumps(SAMPLES["spine"]), None, "2026-10-15"))
            for pid in ("old1", "old2"):
                db.execute("INSERT INTO plans VALUES(?,?,?,?,?,?,?,?)", (pid,"legacy",json.dumps(analyze("spine",SAMPLES["spine"])),"approved","Old Reviewer","Old note","2026-10-15","2026-10-15"))
            db.commit()
            app.initialize(db)
            rows = db.execute("SELECT * FROM plans ORDER BY revision").fetchall()
            self.assertEqual([r["revision"] for r in rows], [1,2])
            self.assertEqual(rows[0]["status"], "approved")
            self.assertIn("legacy_warning", json.loads(rows[0]["input_snapshot"]))
            app.initialize(db)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM plans").fetchone()[0], 2)
            db.close()


if __name__ == "__main__":
    unittest.main()
