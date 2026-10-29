"""Local demo server. Run: python app.py"""

from __future__ import annotations

import json
import mimetypes
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from planner import analyze


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DB = DATA / "demo.sqlite3"
UPLOADS = DATA / "uploads"
SCENARIOS = {"spine": "脊柱侧弯", "joint": "关节置换", "trauma": "骨折内固定"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    DATA.mkdir(exist_ok=True)
    UPLOADS.mkdir(exist_ok=True)
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS cases (
      id TEXT PRIMARY KEY, code TEXT NOT NULL, scenario TEXT NOT NULL,
      age INTEGER NOT NULL, sex TEXT NOT NULL, measurements TEXT NOT NULL,
      image_name TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS plans (
      id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
      result TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
      reviewer TEXT, review_note TEXT, reviewed_at TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS audit (
      id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id TEXT NOT NULL,
      action TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL
    );
    """)
    try:
        with db:
            yield db
    finally:
        db.close()


def record(db, entity_id, action, detail):
    db.execute("INSERT INTO audit(entity_id,action,detail,at) VALUES(?,?,?,?)",
               (entity_id, action, detail, now()))


def case_dict(row):
    d = dict(row)
    d["measurements"] = json.loads(d["measurements"])
    d["scenario_label"] = SCENARIOS[d["scenario"]]
    d["image_url"] = f"/uploads/{d['image_name']}" if d["image_name"] else None
    return d


def plan_dict(row):
    d = dict(row)
    d["result"] = json.loads(d["result"])
    return d


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{now()}] {self.address_string()} {fmt % args}")

    def respond(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("Request too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html") or path.startswith("/static/"):
                file = ROOT / "static/index.html" if path in ("/", "/index.html") else (ROOT / path.lstrip("/")).resolve()
                if not file.is_relative_to((ROOT / "static").resolve()):
                    return self.respond(404, {"error": "Not found"})
                self.file_response(file)
                return
            if path.startswith("/uploads/"):
                name = path.removeprefix("/uploads/")
                if name != Path(name).name:
                    return self.respond(404, {"error": "Not found"})
                self.file_response(UPLOADS / name)
                return
            if path == "/api/health":
                return self.respond(200, {"status": "ok", "mode": "local_demo"})
            if path == "/api/catalog":
                return self.respond(200, {"scenarios": SCENARIOS})
            with connect() as db:
                if path == "/api/cases":
                    rows = db.execute("""SELECT cases.*,
                      (SELECT status FROM plans WHERE case_id=cases.id ORDER BY created_at DESC,rowid DESC LIMIT 1) AS latest_plan_status,
                      (SELECT COUNT(*) FROM plans WHERE case_id=cases.id) AS plan_count
                      FROM cases ORDER BY created_at DESC,rowid DESC""").fetchall()
                    return self.respond(200, {"cases": [case_dict(r) for r in rows]})
                if path.startswith("/api/cases/"):
                    case_id = path.split("/")[3]
                    row = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
                    if not row:
                        return self.respond(404, {"error": "Case not found"})
                    plans = db.execute("SELECT * FROM plans WHERE case_id=? ORDER BY created_at DESC,rowid DESC", (case_id,)).fetchall()
                    audit = db.execute("SELECT action,detail,at FROM audit WHERE entity_id=? ORDER BY id", (case_id,)).fetchall()
                    return self.respond(200, {"case": case_dict(row), "plans": [plan_dict(p) for p in plans], "audit": [dict(a) for a in audit]})
                if path.startswith("/api/plans/") and path.endswith("/export"):
                    plan_id = path.split("/")[3]
                    row = db.execute("SELECT plans.*,cases.code,cases.scenario,cases.measurements FROM plans JOIN cases ON cases.id=plans.case_id WHERE plans.id=?", (plan_id,)).fetchone()
                    if not row:
                        return self.respond(404, {"error": "Plan not found"})
                    return self.export_plan(row)
            self.respond(404, {"error": "Not found"})
        except Exception as exc:
            self.respond(400, {"error": str(exc)})

    def file_response(self, file):
        if not file.is_file():
            return self.respond(404, {"error": "Not found"})
        body = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(file)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/cases":
                payload = self.read_json()
                scenario = payload.get("scenario")
                if scenario not in SCENARIOS:
                    raise ValueError("Invalid scenario")
                code = str(payload.get("code", "")).strip()
                if not 2 <= len(code) <= 32:
                    raise ValueError("Case code must have 2–32 characters")
                age = int(payload.get("age", 0))
                if not 1 <= age <= 120:
                    raise ValueError("Age must be 1–120")
                sex = payload.get("sex")
                if sex not in ("female", "male", "other"):
                    raise ValueError("Invalid sex")
                measurements = payload.get("measurements")
                if not isinstance(measurements, dict):
                    raise ValueError("Measurements required")
                analyze(scenario, measurements)  # validate before saving
                case_id = uuid.uuid4().hex[:12]
                with connect() as db:
                    db.execute("INSERT INTO cases(id,code,scenario,age,sex,measurements,created_at) VALUES(?,?,?,?,?,?,?)",
                               (case_id, code, scenario, age, sex, json.dumps(measurements, ensure_ascii=False), now()))
                    record(db, case_id, "case_created", f"创建{SCENARIOS[scenario]}病例")
                return self.respond(201, {"id": case_id})
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "cases"] and parts[3] == "image":
                return self.upload_image(parts[2])
            if len(parts) == 4 and parts[:2] == ["api", "cases"] and parts[3] == "analyze":
                case_id = parts[2]
                with connect() as db:
                    case = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
                    if not case:
                        return self.respond(404, {"error": "Case not found"})
                    result = analyze(case["scenario"], json.loads(case["measurements"]))
                    plan_id = uuid.uuid4().hex[:12]
                    db.execute("INSERT INTO plans(id,case_id,result,created_at) VALUES(?,?,?,?)",
                               (plan_id, case_id, json.dumps(result, ensure_ascii=False), now()))
                    record(db, case_id, "plan_generated", f"生成方案 {plan_id}")
                return self.respond(201, {"id": plan_id, "result": result})
            if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[3] == "review":
                payload = self.read_json()
                status = payload.get("status")
                reviewer = str(payload.get("reviewer", "")).strip()
                note = str(payload.get("note", "")).strip()
                if status not in ("approved", "rejected") or not 2 <= len(reviewer) <= 50 or not 2 <= len(note) <= 500:
                    raise ValueError("Review requires decision, reviewer and note")
                with connect() as db:
                    row = db.execute("SELECT case_id,status FROM plans WHERE id=?", (parts[2],)).fetchone()
                    if not row:
                        return self.respond(404, {"error": "Plan not found"})
                    if row["status"] != "pending":
                        raise ValueError("Plan already reviewed")
                    db.execute("UPDATE plans SET status=?,reviewer=?,review_note=?,reviewed_at=? WHERE id=?",
                               (status, reviewer, note, now(), parts[2]))
                    record(db, row["case_id"], "plan_reviewed", f"方案 {parts[2]}：{status}；{reviewer}；{note}")
                return self.respond(200, {"status": status})
            self.respond(404, {"error": "Not found"})
        except Exception as exc:
            self.respond(400, {"error": str(exc)})

    def upload_image(self, case_id):
        content_type = self.headers.get("Content-Type", "")
        ext = {"image/png": ".png", "image/jpeg": ".jpg"}.get(content_type)
        if not ext:
            raise ValueError("Only PNG or JPEG images are accepted")
        length = int(self.headers.get("Content-Length", "0"))
        if not 1 <= length <= 5_000_000:
            raise ValueError("Image must be 1 byte to 5 MB")
        data = self.rfile.read(length)
        if not ((ext == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n")) or
                (ext == ".jpg" and data.startswith(b"\xff\xd8\xff"))):
            raise ValueError("Image signature does not match type")
        with connect() as db:
            row = db.execute("SELECT image_name FROM cases WHERE id=?", (case_id,)).fetchone()
            if not row:
                return self.respond(404, {"error": "Case not found"})
            filename = uuid.uuid4().hex + ext
            (UPLOADS / filename).write_bytes(data)
            db.execute("UPDATE cases SET image_name=? WHERE id=?", (filename, case_id))
            record(db, case_id, "image_uploaded", "上传影像附件（仅存档，不自动识别）")
            if row["image_name"]:
                (UPLOADS / row["image_name"]).unlink(missing_ok=True)
        return self.respond(200, {"image_url": f"/uploads/{filename}"})

    def export_plan(self, row):
        result = json.loads(row["result"])
        measures = json.loads(row["measurements"])
        esc = lambda value: escape(str(value))
        options = "".join(f"<tr><td>{esc(x['name'])}</td><td>{x['score']}</td><td>{esc(x['reason'])}</td></tr>" for x in result["ranked_options"])
        flags = "".join(f"<li>{esc(x)}</li>" for x in result["flags"])
        fields = "".join(f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in measures.items())
        status = {"pending": "待复核", "approved": "已通过", "rejected": "已退回"}[row["status"]]
        calculations = "".join(f"<li>{esc(x)}</li>" for x in result.get("calculation", []))
        sensitivity_html = ""
        if result.get("sensitivity"):
            sens = result["sensitivity"]
            points = "".join(f"<tr><td>{esc(p['x'])} {esc(sens['unit'])}</td><td>{esc(p['y'])}</td><td>{esc(p['option'])}</td></tr>" for p in sens["points"])
            sensitivity_html = f"<h2>参数敏感性分析</h2><p>{esc(sens['note'])}</p><table><caption>表 3　{esc(sens['label'])}变化与结果响应</caption><tr><th>输入值</th><th>{esc(sens['metric'])}</th><th>规则首选</th></tr>{points}</table>"
        html = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>骨科规划报告 {esc(row['code'])}</title>
<style>body{{font:15px/1.8 'Microsoft YaHei',sans-serif;max-width:850px;margin:50px auto;color:#24494c}}h1{{font-size:26px}}h2{{font-size:18px;margin-top:30px}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #dce9e5;padding:9px;text-align:left}}th{{background:#eaf5f0}}caption{{caption-side:top;font-weight:700;padding:10px;font-size:14px}}.note{{color:#7b9588}}.toolbar{{text-align:right}}@media print{{.toolbar{{display:none}}body{{margin:10mm}}table{{break-inside:avoid}}}}</style>
<div class="toolbar"><button onclick="window.print()">打印或保存为 PDF</button></div><h1>骨科专科智能规划报告</h1><p><strong>病例编号：</strong>{esc(row['code'])}　<strong>场景：</strong>{esc(SCENARIOS[row['scenario']])}　<strong>状态：</strong>{esc(status)}</p>
<p class="note">教学演示结果，不可直接用于诊断、手术或器械选型。</p><h2>输入参数</h2><table><caption>表 1　病例测量输入参数</caption>{fields}</table><h2>建议摘要</h2><p>{esc(result['summary'])}</p><h2>候选方案</h2><table><caption>表 2　候选方案评分与依据</caption><tr><th>方案</th><th>分数</th><th>依据</th></tr>{options}</table>{sensitivity_html}<h2>风险提醒</h2><ul>{flags}</ul><h2>复核记录</h2><p>复核人：{esc(row['reviewer'] or '待复核')}　结论：{esc(status)}<br>备注：{esc(row['review_note'] or '暂无')}</p><h2>算法说明</h2><p>{esc(result['method'])}</p><ul>{calculations}</ul><p>{esc(result.get('model_version', 'ORTHO-RULES 1.0'))}</p></html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    with connect():
        pass
    print("Orthopedics demo: http://127.0.0.1:8765")
    ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
