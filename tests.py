"""Run with: python -m unittest tests.py -v"""

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app
from planner import analyze


SAMPLES = {
    "spine": {"cobb_angle": 48, "flexibility_pct": 55, "coronal_balance_mm": 28, "apex_region": "thoracic"},
    "joint": {"joint_type": "hip", "bone_diameter_mm": 52, "canal_width_mm": 13, "offset_mm": 42, "bone_quality": "normal"},
    "trauma": {"fracture_site": "tibia", "fracture_pattern": "comminuted", "displacement_mm": 18, "articular_involvement": True, "soft_tissue": "compromised"},
}


class PlannerTests(unittest.TestCase):
    def test_all_scenarios_return_ranked_explainable_options(self):
        for scenario, measurements in SAMPLES.items():
            with self.subTest(scenario=scenario):
                result = analyze(scenario, measurements)
                self.assertGreaterEqual(len(result["ranked_options"]), 2)
                self.assertTrue(result["method"])
                self.assertTrue(result["required_review"])
                scores = [x["score"] for x in result["ranked_options"]]
                self.assertEqual(scores, sorted(scores, reverse=True))

    def test_invalid_measurements_rejected(self):
        with self.assertRaises(ValueError):
            analyze("spine", {**SAMPLES["spine"], "cobb_angle": 200})

    def test_stiff_spine_prioritizes_staged_assessment(self):
        result = analyze("spine", {**SAMPLES["spine"], "cobb_angle": 70, "flexibility_pct": 15})
        self.assertEqual(result["ranked_options"][0]["name"], "分期评估路径")
        self.assertEqual(result["metrics"][1]["value"], "46.2°")

    def test_exact_hip_and_knee_matches_expose_zero_deviation(self):
        for joint, diameter, canal, name in [("hip", 52, 13, "H52"), ("knee", 66, 12, "K66")]:
            r = analyze("joint", {**SAMPLES["joint"], "joint_type": joint, "bone_diameter_mm": diameter, "canal_width_mm": canal})
            top = r["ranked_options"][0]
            self.assertIn(name, top["name"])
            self.assertEqual(top["score"], 100)
            self.assertEqual(top["deviations"], {"diameter": 0, "canal": 0, "offset": 0})

    def test_sensitivity_holds_other_inputs_and_does_not_mutate_case(self):
        original = dict(SAMPLES["spine"])
        r = analyze("spine", original)
        self.assertEqual(original, SAMPLES["spine"])
        ys = [p["y"] for p in r["sensitivity"]["points"]]
        self.assertEqual(ys, sorted(ys, reverse=True))
        for scenario in SAMPLES:
            self.assertGreaterEqual(len(analyze(scenario, SAMPLES[scenario])["sensitivity"]["points"]), 5)

    def test_boolean_measurements_require_boolean_type(self):
        with self.assertRaises(ValueError):
            analyze("trauma", {**SAMPLES["trauma"], "articular_involvement": "false"})


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        app.DATA = Path(cls.temp.name)
        app.DB = app.DATA / "test.sqlite3"
        app.UPLOADS = app.DATA / "uploads"
        with app.connect():
            pass
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.temp.cleanup()

    def request(self, path, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.base + path, body, headers={"Content-Type": "application/json"} if body else {})
        with urlopen(request) as response:
            return json.loads(response.read())

    def test_closed_loop_for_all_three_scenarios(self):
        for scenario, measurements in SAMPLES.items():
            with self.subTest(scenario=scenario):
                created = self.request("/api/cases", {"code": f"TEST-{scenario}", "age": 35,
                                                       "sex": "female", "scenario": scenario,
                                                       "measurements": measurements})
                case_id = created["id"]
                plan = self.request(f"/api/cases/{case_id}/analyze", {})
                result = self.request(f"/api/plans/{plan['id']}/review",
                                      {"status": "approved", "reviewer": "Test Reviewer", "note": "Parameters checked"})
                self.assertEqual(result["status"], "approved")
                detail = self.request(f"/api/cases/{case_id}")
                self.assertEqual(detail["plans"][0]["status"], "approved")
                self.assertEqual([a["action"] for a in detail["audit"]],
                                 ["case_created", "plan_generated", "plan_reviewed"])
                with urlopen(self.base + f"/api/plans/{plan['id']}/export") as response:
                    html = response.read().decode()
                self.assertIn("打印或保存为 PDF", html)
                self.assertIn(f"TEST-{scenario}", html)
                self.assertEqual(html.count("<caption>"), 3)
                self.assertIn("参数敏感性分析", html)

    def test_duplicate_review_rejected(self):
        created = self.request("/api/cases", {"code": "TEST-DUP", "age": 50, "sex": "male",
                                                   "scenario": "spine", "measurements": SAMPLES["spine"]})
        plan = self.request(f"/api/cases/{created['id']}/analyze", {})
        payload = {"status": "rejected", "reviewer": "Test Reviewer", "note": "Need more images"}
        self.request(f"/api/plans/{plan['id']}/review", payload)
        with self.assertRaises(HTTPError) as raised:
            self.request(f"/api/plans/{plan['id']}/review", payload)
        self.assertEqual(raised.exception.code, 400)

    def test_image_attachment_round_trip(self):
        created = self.request("/api/cases", {"code": "TEST-IMAGE", "age": 42, "sex": "female",
                                                   "scenario": "joint", "measurements": SAMPLES["joint"]})
        png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000b49444154789c636000020000050001a5f645400000000049454e44ae426082")
        request = Request(self.base + f"/api/cases/{created['id']}/image", png,
                          headers={"Content-Type": "image/png"})
        with urlopen(request) as response:
            image_url = json.loads(response.read())["image_url"]
        detail = self.request(f"/api/cases/{created['id']}")
        self.assertEqual(detail["case"]["image_url"], image_url)
        self.assertIn("image_uploaded", [a["action"] for a in detail["audit"]])
        with urlopen(self.base + image_url) as response:
            self.assertEqual(response.read(), png)
        for content_type, body in [("text/plain", png), ("image/png", b"not-a-png"), ("image/jpeg", png)]:
            with self.subTest(content_type=content_type), self.assertRaises(HTTPError) as raised:
                urlopen(Request(self.base + f"/api/cases/{created['id']}/image", body,
                                headers={"Content-Type": content_type}))
            self.assertEqual(raised.exception.code, 400)
        unchanged = self.request(f"/api/cases/{created['id']}")
        self.assertEqual(unchanged["case"]["image_url"], image_url)

    def test_new_version_preserves_reviewed_snapshot(self):
        created = self.request("/api/cases", {"code": "TEST-VERSIONS", "age": 30, "sex": "female",
                                             "scenario": "joint", "measurements": SAMPLES["joint"]})
        first = self.request(f"/api/cases/{created['id']}/analyze", {})
        self.request(f"/api/plans/{first['id']}/review", {"status": "approved", "reviewer": "Reviewer", "note": "Checked dimensions"})
        second = self.request(f"/api/cases/{created['id']}/analyze", {})
        detail = self.request(f"/api/cases/{created['id']}")
        self.assertEqual([p["id"] for p in detail["plans"]], [second["id"], first["id"]])
        self.assertEqual([p["status"] for p in detail["plans"]], ["pending", "approved"])
        self.assertEqual(detail["plans"][1]["result"], first["result"])

    def test_frontend_and_local_assets_are_available(self):
        for path in ["/", "/index.html", "/static/app.js", "/static/style.css", "/static/vendor/vue.global.prod.js", "/static/assets/medical-scenarios.png"]:
            with self.subTest(path=path), urlopen(self.base + path) as response:
                self.assertEqual(response.status, 200)
                self.assertGreater(len(response.read()), 100)
        with self.assertRaises(HTTPError):
            urlopen(self.base + "/static/../app.py")


if __name__ == "__main__":
    unittest.main()
