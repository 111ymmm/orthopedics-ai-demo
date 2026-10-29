"""Local orthopedic planning workbench. Python 3.10+, Pillow required."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import mimetypes
import re
import sqlite3
import threading
import uuid
import warnings
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image, ImageOps
from planner import (ALGORITHM_VERSION, CATALOG_VERSION, LABELS, SCENARIOS,
                     analyze, cobb_from_points, display_value, validate_measurements)

APP_VERSION = "2.0.0"
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DB = DATA / "demo.sqlite3"
UPLOADS = DATA / "uploads"
_schema_lock = threading.Lock()
_initialized = set()
Image.MAX_IMAGE_PIXELS = 20_000_000
STATUSES = {"pending": "待复核", "approved": "已通过", "rejected": "已退回"}


class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def uid():
    return uuid.uuid4().hex


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def initialize(db):
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
    CREATE TABLE IF NOT EXISTS images (
      id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
      filename TEXT NOT NULL, original_filename TEXT NOT NULL, sha256 TEXT NOT NULL,
      width INTEGER NOT NULL, height INTEGER NOT NULL, byte_size INTEGER NOT NULL,
      created_at TEXT NOT NULL
    );
    """)
    additions = {
        "cases": {"input_revision": "INTEGER NOT NULL DEFAULT 1", "current_plan_id": "TEXT",
                  "current_image_id": "TEXT", "measurement_source": "TEXT NOT NULL DEFAULT '{}'", "updated_at": "TEXT"},
        "plans": {"revision": "INTEGER", "input_revision": "INTEGER NOT NULL DEFAULT 1",
                  "input_snapshot": "TEXT", "algorithm_version": "TEXT", "catalog_version": "TEXT"},
    }
    for table, fields in additions.items():
        existing = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for key, definition in fields.items():
            if key not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {key} {definition}")
    # V1 did not record immutable input evidence. Preserve history and disclose that limitation.
    for case in db.execute("SELECT * FROM cases").fetchall():
        rows = db.execute("SELECT * FROM plans WHERE case_id=? ORDER BY rowid", (case["id"],)).fetchall()
        for index, plan in enumerate(rows, 1):
            if plan["revision"] is None:
                snapshot = {"code": case["code"], "age": case["age"], "sex": case["sex"], "scenario": case["scenario"],
                            "measurements": json.loads(case["measurements"]), "input_revision": 1, "image": None,
                            "measurement_source": {}, "legacy_warning": "旧版未保存输入快照，历史测量与影像绑定无法确认；此处仅保留旧记录。"}
                db.execute("UPDATE plans SET revision=?,input_snapshot=?,algorithm_version='legacy-v1',catalog_version='legacy-demo' WHERE id=?",
                           (index, dumps(snapshot), plan["id"]))
        if rows and all(p["revision"] is None for p in rows):
            db.execute("UPDATE cases SET current_plan_id=? WHERE id=?", (rows[-1]["id"], case["id"]))
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS plan_revision_idx ON plans(case_id,revision)")
    db.execute("CREATE INDEX IF NOT EXISTS image_case_idx ON images(case_id,created_at)")
    db.commit()


@contextmanager
def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    UPLOADS.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    key = str(DB.resolve())
    try:
        with _schema_lock:
            if key not in _initialized:
                db.execute("PRAGMA journal_mode=WAL")
                initialize(db)
                _initialized.add(key)
        with db:
            yield db
    finally:
        db.close()


def record(db, entity, action, detail):
    db.execute("INSERT INTO audit(entity_id,action,detail,at) VALUES(?,?,?,?)", (entity, action, detail, now()))


def require_case(db, case_id):
    row = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        raise APIError(404, "病例不存在")
    return row


def image_dict(row):
    if not row:
        return None
    d = dict(row)
    d["image_url"] = "/uploads/" + d["filename"]
    d["original_url"] = "/uploads/" + d["original_filename"]
    return d


def case_dict(row):
    d = dict(row)
    d["measurements"] = json.loads(d["measurements"])
    d["measurement_source"] = json.loads(d["measurement_source"] or "{}")
    d["scenario_label"] = SCENARIOS[d["scenario"]]
    d["image_url"] = "/uploads/" + d["image_name"] if d["image_name"] else None
    return d


def plan_dict(row, case):
    d = dict(row)
    d["result"] = json.loads(d["result"])
    d["input_snapshot"] = json.loads(d["input_snapshot"] or "{}")
    d["is_current"] = d["id"] == case["current_plan_id"] and d["input_revision"] == case["input_revision"]
    return d


def validate_case(payload):
    if not isinstance(payload, dict):
        raise ValueError("请求必须为 JSON 对象")
    scenario = payload.get("scenario")
    if scenario not in SCENARIOS:
        raise ValueError("请选择有效场景")
    code = payload.get("code")
    if not isinstance(code, str) or not 2 <= len(code.strip()) <= 32:
        raise ValueError("病例编号应为 2–32 个字符")
    age = payload.get("age")
    if isinstance(age, bool) or not isinstance(age, (int, float)) or not 1 <= age <= 120 or int(age) != age:
        raise ValueError("年龄应为 1–120 之间的整数")
    sex = payload.get("sex")
    if sex not in ("female", "male", "other"):
        raise ValueError("请选择有效性别")
    return code.strip(), int(age), sex, scenario, validate_measurements(scenario, payload.get("measurements"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logging.info("%s %s", self.address_string(), fmt % args)

    def send_body(self, status, body, content_type, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def respond(self, status, data):
        if getattr(self, "_defer_response", False):
            self._pending_response = (status, data)
            return
        self.send_body(status, dumps(data).encode(), "application/json; charset=utf-8")

    def read_body(self, maximum=1_000_000):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("请求长度无效") from None
        if not 0 <= length <= maximum:
            raise APIError(413, "文件或请求超过大小限制")
        self.connection.settimeout(15)
        data = self.rfile.read(length)
        if len(data) != length:
            raise ValueError("上传未完成，请重试")
        return data

    def read_json(self):
        payload = json.loads(self.read_body() or b"{}", parse_constant=lambda _: (_ for _ in ()).throw(ValueError("不接受非有限数值")))
        if not isinstance(payload, dict):
            raise ValueError("请求必须为 JSON 对象")
        return payload

    def guarded(self, callback):
        try:
            callback()
        except APIError as exc:
            self.respond(exc.status, {"error": exc.message})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.respond(400, {"error": str(exc)})
        except ConnectionError:
            pass
        except Exception:
            logging.exception("Request failed")
            self.respond(500, {"error": "处理失败，请重试；详细信息已记录在本地日志"})

    def do_GET(self):
        self.guarded(self.get)

    def do_POST(self):
        # Emit the response only after the enclosing database context has committed.
        self._defer_response = True
        self._pending_response = None
        self.guarded(self.post)
        self._defer_response = False
        if self._pending_response is not None:
            try:
                self.respond(*self._pending_response)
            except ConnectionError:
                pass

    def get(self):
        self.check_local_request()
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self.file_response(ROOT / "static" / "index.html")
        if path in ("/static/app.js", "/static/style.css", "/static/measurement-demo.png"):
            return self.file_response(ROOT / path.lstrip("/"))
        if path.startswith("/uploads/"):
            name = path.removeprefix("/uploads/")
            if not re.fullmatch(r"[0-9a-f]{32}(?:\.source)?\.(?:png|jpg)", name):
                raise APIError(404, "附件不存在")
            return self.file_response(UPLOADS / name)
        if path == "/api/health":
            return self.respond(200, {"status": "ok", "version": APP_VERSION, "mode": "local_demo"})
        if path == "/api/catalog":
            return self.respond(200, {"scenarios": SCENARIOS, "algorithm_version": ALGORITHM_VERSION, "catalog_version": CATALOG_VERSION})
        parts = path.strip("/").split("/")
        with connect() as db:
            if path == "/api/cases":
                rows = db.execute("SELECT c.*,p.status AS plan_status FROM cases c LEFT JOIN plans p ON p.id=c.current_plan_id ORDER BY c.rowid DESC").fetchall()
                return self.respond(200, {"cases": [case_dict(row) for row in rows]})
            if len(parts) == 3 and parts[:2] == ["api", "cases"]:
                case = require_case(db, parts[2])
                plans = db.execute("SELECT * FROM plans WHERE case_id=? ORDER BY revision DESC", (parts[2],)).fetchall()
                images = db.execute("SELECT * FROM images WHERE case_id=? ORDER BY rowid DESC", (parts[2],)).fetchall()
                audit = db.execute("SELECT * FROM audit WHERE entity_id=? ORDER BY id DESC", (parts[2],)).fetchall()
                return self.respond(200, {"case": case_dict(case), "plans": [plan_dict(p, case) for p in plans], "images": [image_dict(i) for i in images], "audit": [dict(a) for a in audit]})
            if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[3] in ("export", "json"):
                plan = db.execute("SELECT * FROM plans WHERE id=?", (parts[2],)).fetchone()
                if not plan:
                    raise APIError(404, "方案不存在")
                case = require_case(db, plan["case_id"])
                result = plan_dict(plan, case)
                if parts[3] == "json":
                    return self.send_body(200, dumps(result).encode(), "application/json; charset=utf-8", f"ortho-plan-v{plan['revision']}.json")
                return self.export_plan(result)
        raise APIError(404, "页面或接口不存在")

    def file_response(self, file):
        if not file.is_file():
            raise APIError(404, "文件不存在")
        self.send_body(200, file.read_bytes(), mimetypes.guess_type(file)[0] or "application/octet-stream")

    def post(self):
        self.check_local_request()
        path = urlparse(self.path).path
        parts = path.strip("/").split("/")
        if path == "/api/cases":
            payload = self.read_json()
            code, age, sex, scenario, m = validate_case(payload)
            case_id, at = uid(), now()
            with connect() as db:
                db.execute("INSERT INTO cases(id,code,age,sex,scenario,measurements,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (case_id, code, age, sex, scenario, dumps(m), at, at))
                record(db, case_id, "case_created", f"创建{SCENARIOS[scenario]}病例，输入 r1")
            return self.respond(201, {"id": case_id})
        if len(parts) == 4 and parts[:2] == ["api", "cases"]:
            case_id, action = parts[2:]
            if action == "image":
                return self.upload_image(case_id)
            payload = self.read_json()
            with connect() as db:
                db.execute("BEGIN IMMEDIATE")
                case = require_case(db, case_id)
                if action == "revise":
                    # Optimistic revision check prevents a stale editor from overwriting newer input.
                    if payload.get("expected_revision") != case["input_revision"]:
                        raise APIError(409, "输入已更新，请刷新后重新编辑")
                    code, age, sex, scenario, m = validate_case(payload)
                    if scenario != case["scenario"]:
                        raise ValueError("已建病例不能更换场景，请新建病例")
                    db.execute("UPDATE cases SET code=?,age=?,sex=?,measurements=?,measurement_source='{}',input_revision=input_revision+1,current_plan_id=NULL,updated_at=? WHERE id=?", (code, age, sex, dumps(m), now(), case_id))
                    record(db, case_id, "input_revised", f"人工修改参数，输入 r{case['input_revision']+1}；旧方案保留")
                    return self.respond(200, {"input_revision": case["input_revision"]+1})
                if action == "analyze":
                    image = image_dict(db.execute("SELECT * FROM images WHERE id=?", (case["current_image_id"],)).fetchone())
                    snapshot = {k: case[k] for k in ("code", "age", "sex", "scenario", "input_revision")}
                    snapshot.update(measurements=json.loads(case["measurements"]), image=image,
                                    measurement_source=json.loads(case["measurement_source"] or "{}"))
                    result = analyze(case["scenario"], snapshot["measurements"])
                    revision = db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM plans WHERE case_id=?", (case_id,)).fetchone()[0]
                    plan_id = uid()
                    db.execute("INSERT INTO plans(id,case_id,result,created_at,revision,input_revision,input_snapshot,algorithm_version,catalog_version) VALUES(?,?,?,?,?,?,?,?,?)", (plan_id, case_id, dumps(result), now(), revision, case["input_revision"], dumps(snapshot), ALGORITHM_VERSION, CATALOG_VERSION))
                    db.execute("UPDATE cases SET current_plan_id=?,updated_at=? WHERE id=?", (plan_id, now(), case_id))
                    record(db, case_id, "plan_generated", f"生成方案 v{revision}，输入 r{case['input_revision']}，ID {plan_id}")
                    return self.respond(201, {"id": plan_id, "revision": revision, "result": result})
                if action == "measure-cobb":
                    if case["scenario"] != "spine":
                        raise ValueError("四点测量仅用于脊柱场景")
                    image = db.execute("SELECT * FROM images WHERE id=? AND case_id=?", (payload.get("image_id"), case_id)).fetchone()
                    if not image or image["id"] != case["current_image_id"]:
                        raise APIError(409, "测量影像已更新，请重新打开测量工具")
                    if payload.get("expected_revision") != case["input_revision"]:
                        raise APIError(409, "输入已更新，请重新测量")
                    angle = cobb_from_points(payload.get("points"), image["width"], image["height"])
                    m = json.loads(case["measurements"])
                    m["cobb_angle"] = angle
                    m = validate_measurements("spine", m)
                    source = {"kind": "manual_four_point", "points": payload["points"], "angle": angle, "image_id": image["id"], "image_sha256": image["sha256"], "at": now(), "method": "两终板线的无符号锐角；人工选择端点"}
                    db.execute("UPDATE cases SET measurements=?,measurement_source=?,input_revision=input_revision+1,current_plan_id=NULL,updated_at=? WHERE id=?", (dumps(m), dumps(source), now(), case_id))
                    record(db, case_id, "measurement_saved", f"四点人工辅助测量 {angle}°，输入 r{case['input_revision']+1}")
                    return self.respond(200, {"angle": angle, "input_revision": case["input_revision"]+1})
        if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[3] == "review":
            payload = self.read_json()
            status, reviewer, note = payload.get("status"), payload.get("reviewer"), payload.get("note")
            if status not in ("approved", "rejected") or not isinstance(reviewer, str) or not 2 <= len(reviewer.strip()) <= 50 or not isinstance(note, str) or not 2 <= len(note.strip()) <= 500:
                raise ValueError("请选择结论，填写复核人（2–50 字）及意见（2–500 字）")
            with connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM plans WHERE id=?", (parts[2],)).fetchone()
                if not row:
                    raise APIError(404, "方案不存在")
                case = require_case(db, row["case_id"])
                if row["status"] != "pending":
                    raise APIError(409, "该版本已经复核，结论不可覆盖")
                if row["input_revision"] != case["input_revision"] or case["current_plan_id"] != row["id"]:
                    raise APIError(409, "这是历史版本，请对当前输入生成的新方案进行复核")
                if status == "approved" and json.loads(row["result"]).get("readiness") in ("no_candidate", "needs_input"):
                    raise APIError(409, "当前方案缺少候选或必要输入，请退回补充资料")
                updated = db.execute("UPDATE plans SET status=?,reviewer=?,review_note=?,reviewed_at=? WHERE id=? AND status='pending'", (status, reviewer.strip(), note.strip(), now(), parts[2]))
                if updated.rowcount != 1:
                    raise APIError(409, "该版本已经复核")
                record(db, row["case_id"], "plan_reviewed", f"方案 v{row['revision']}：{STATUSES[status]}；{reviewer.strip()}；{note.strip()}")
            return self.respond(200, {"status": status})
        raise APIError(404, "接口不存在")

    def check_local_request(self):
        port = self.server.server_address[1]
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed_hosts:
            raise APIError(403, "请通过本机地址打开工作台")
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in allowed_hosts}:
            raise APIError(403, "不接受其他网站发起的请求")

    def upload_image(self, case_id):
        kind = self.headers.get("Content-Type", "").split(";")[0].strip()
        if kind not in ("image/png", "image/jpeg"):
            raise ValueError("请选择 PNG 或 JPEG 图片")
        data = self.read_body(5_000_000)
        expected_format = "PNG" if kind == "image/png" else "JPEG"
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    if image.format != expected_format:
                        raise ValueError("图片内容与文件类型不符")
                    image.verify()
                with Image.open(io.BytesIO(data)) as image:
                    image.load()
                    preview = ImageOps.exif_transpose(image).convert("RGB")
                    width, height = preview.size
                    if width*height > 20_000_000:
                        raise ValueError("图片像素超过 2000 万限制")
                    output = io.BytesIO()
                    preview.save(output, format="PNG")
        except Exception as exc:
            raise ValueError("图片损坏、解码失败或尺寸过大，请重新导出 PNG/JPEG") from exc
        image_id = uid()
        filename = image_id + ".png"
        original_filename = image_id + (".source.png" if kind == "image/png" else ".source.jpg")
        written = []
        try:
            with connect() as db:
                db.execute("BEGIN IMMEDIATE")
                case = require_case(db, case_id)
                for name, body in ((filename, output.getvalue()), (original_filename, data)):
                    (UPLOADS / name).write_bytes(body)
                    written.append(UPLOADS / name)
                db.execute("INSERT INTO images VALUES(?,?,?,?,?,?,?,?,?)", (image_id, case_id, filename, original_filename, hashlib.sha256(data).hexdigest(), width, height, len(data), now()))
                db.execute("UPDATE cases SET current_image_id=?,image_name=?,measurement_source='{}',input_revision=input_revision+1,current_plan_id=NULL,updated_at=? WHERE id=?", (image_id, filename, now(), case_id))
                record(db, case_id, "image_uploaded", f"保存影像新版本 {image_id[:8]}，输入 r{case['input_revision']+1}；旧影像保留")
        except Exception:
            for file in written:
                file.unlink(missing_ok=True)
            raise
        return self.respond(200, {"image_id": image_id, "image_url": "/uploads/"+filename, "input_revision": case["input_revision"]+1})

    def export_plan(self, plan):
        snap, result = plan["input_snapshot"], plan["result"]
        esc = lambda value: escape(str(value))
        fields = "".join(f"<tr><td>{esc(LABELS.get(k,k))}</td><td>{esc(display_value(v))}</td></tr>" for k, v in snap.get("measurements", {}).items())
        options = "".join(f"<tr><td>{esc(o['name'])}</td><td>{esc(o['score'])}</td><td>{esc(o['reason'])}</td></tr>" for o in result.get("ranked_options", []))
        stages = "".join(f"<p>{esc(o['name'])}：{esc(o['reason'])}</p>" for o in result.get("staged_options", []))
        flags = "".join(f"<li>{esc(flag)}</li>" for flag in result.get("flags", []))
        image = snap.get("image")
        image_html = f"<p>影像 ID：{esc(image['id'])}<br>原文件 SHA256：{esc(image['sha256'])}</p><img src='{esc(image['image_url'])}' alt='本方案绑定影像' style='max-width:300px;max-height:320px'>" if image else "<p>本方案未绑定可确认的影像版本。</p>"
        provenance = "四点人工辅助测量" if snap.get("measurement_source", {}).get("kind") == "manual_four_point" else "人工参数录入"
        legacy = f"<p>{esc(snap['legacy_warning'])}</p>" if snap.get("legacy_warning") else ""
        html = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>骨科规划 {esc(snap.get('code',''))} v{plan['revision']}</title>
<style>body{{font:14px/1.7 'Microsoft YaHei',sans-serif;max-width:900px;margin:36px auto;color:#1b3542;padding:20px}}h1{{font-size:26px}}h2{{font-size:18px;margin-top:28px}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #cddddd;padding:10px;text-align:left}}th{{background:#eef5f4}}.meta{{overflow-wrap:anywhere;color:#526b73}}button{{padding:10px 20px}}@media print{{button{{display:none}}body{{margin:0;padding:0}}tr,img{{break-inside:avoid}}}}</style>
<button onclick="window.print()">打印或保存为 PDF</button><h1>骨科专科辅助规划报告</h1>
<p>病例：{esc(snap.get('code',''))} · {esc(SCENARIOS.get(snap.get('scenario'),''))} · 方案 v{plan['revision']} · {esc(STATUSES[plan['status']])} · {'当前版本' if plan['is_current'] else '历史版本'}</p>
<p>教学与研究演示。器械目录及规则未做临床有效性验证，分数不代表成功率。</p>{legacy}
<p class="meta">病例 ID：{esc(plan['case_id'])}<br>方案 ID：{esc(plan['id'])}<br>输入版本：r{snap.get('input_revision','?')}<br>生成时间（UTC）：{esc(plan['created_at'])}<br>规则版本：{esc(plan['algorithm_version'])}<br>器械目录：{esc(plan['catalog_version'])}<br>测量来源：{esc(provenance)}</p>
<h2>输入快照</h2><table>{fields}</table><h2>结果与候选</h2><p>{esc(result['summary'])}</p><table><tr><th>候选</th><th>规则分</th><th>依据</th></tr>{options or '<tr><td colspan="3">无可用候选</td></tr>'}</table>{stages}
<h2>风险及待确认信息</h2><ul>{flags}</ul><h2>绑定影像</h2>{image_html}
<h2>复核记录</h2><p>结论：{esc(STATUSES[plan['status']])}<br>复核人：{esc(plan['reviewer'] or '待复核')}<br>复核时间（UTC）：{esc(plan['reviewed_at'] or '待复核')}<br>意见：{esc(plan['review_note'] or '暂无')}</p>
<h2>方法说明</h2><p>{esc(result['method'])}</p></html>"""
        self.send_body(200, html.encode(), "text/html; charset=utf-8")


def main():
    parser = argparse.ArgumentParser(description="骨科专科辅助规划工作台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    parser.add_argument("--data-dir", type=Path, help="使用指定数据目录")
    args = parser.parse_args()
    global DATA, DB, UPLOADS
    if args.data_dir:
        DATA = args.data_dir.resolve()
        DB, UPLOADS = DATA / "demo.sqlite3", DATA / "uploads"
    with connect():
        pass
    logging.basicConfig(filename=DATA / "server.log", level=logging.INFO, encoding="utf-8", format="%(asctime)s %(levelname)s %(message)s")
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        print(f"端口 {args.port} 已占用。可尝试 python app.py --port {args.port+1} --open")
        raise SystemExit(1)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Orthopedics Workbench {APP_VERSION}: {url}\n按 Ctrl+C 停止服务。")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
