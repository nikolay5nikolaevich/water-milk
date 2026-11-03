"""Run: python app/server.py. Localhost only; Python standard library."""
import argparse
import csv
import io
import json
import logging
import secrets
import sqlite3
import sys
import threading
import uuid
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from amocrm import AmoClient, CRMError, positive_id, protect_token
from crm_browser import CRMBrowser
from evaluator import run_check, subscription_status, stop_all as stop_checks

from core import (ROOT, CRITERIA, active_kb, evaluation_kb, digest, demo_dialogues, demo_kb, dump, packet,
                  parse_report, parse_transcript, reference_report, text, total,
                  validate_dialogue, validate_kb, validate_report)

DB = ROOT / "data/workbench.sqlite3"
TOKEN = secrets.token_urlsafe(32)
CRM_SESSIONS = {}
CRM_LOCK = threading.RLock()
CRM_BROWSERS = {}
CHECK_LOCK = threading.RLock()
CHECK_ACTIVE = ("queued", "checking_login", "running", "correcting", "validating")


@contextmanager
def connect(path=None):
    db = sqlite3.connect(path or DB)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def init_db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS dialogues (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, dialogue_id TEXT NOT NULL,
            source TEXT NOT NULL, created TEXT NOT NULL, report TEXT NOT NULL, kb TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reviews (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL,
            created TEXT NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL, note TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS check_jobs (id TEXT PRIMARY KEY, dialogue_id TEXT NOT NULL,
            status TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '', run_id TEXT);
        """)
        db.execute("INSERT OR IGNORE INTO settings VALUES ('kb', ?)", (dump(demo_kb()),))


def now():
    return datetime.now(timezone.utc).isoformat()


def crm_session():
    key = str(DB.resolve())
    with CRM_LOCK:
        if key not in CRM_SESSIONS:
            with connect() as db:
                row = db.execute("SELECT value FROM settings WHERE key='amo_connection'").fetchone()
            if not row:
                raise CRMError("Сначала подключите аккаунт amoCRM в разделе «Сделки amoCRM»")
            saved = json.loads(row[0])
            CRM_SESSIONS[key] = {"client": AmoClient(saved["domain"], protect_token(saved["protected_token"], decrypt=True)),
                                 "metadata": saved["metadata"], "remember": True}
        return CRM_SESSIONS[key]


def crm_public():
    try:
        session = crm_session()
        return {"connected": True, "domain": session["client"].domain, "remember": session["remember"],
                **session["metadata"]}
    except CRMError as exc:
        return {"connected": False, "domain": "ivanovnn.amocrm.ru", "message": str(exc)}


def crm_connect(body):
    token = body.get("access_token", "")
    if not token:
        old = crm_session()["client"]
        if body.get("domain", "").strip().removeprefix("https://").rstrip("/") != old.domain:
            raise CRMError("Для другого аккаунта нужен его токен")
        token = old.token
    client = AmoClient(body.get("domain", ""), token)
    metadata = client.metadata()
    remember = body.get("remember") is True
    encrypted = None
    if remember:
        try:
            encrypted = protect_token(client.token)
        except CRMError:
            remember = False
            metadata["warnings"].append("Windows не смогла защитить токен. Подключение работает до перезапуска приложения; токен на диск не сохранён")
    with CRM_LOCK:
        with connect() as db:
            if remember:
                saved = {"domain": client.domain, "protected_token": encrypted, "metadata": metadata}
                db.execute("INSERT OR REPLACE INTO settings VALUES ('amo_connection', ?)", (dump(saved),))
            else:
                db.execute("DELETE FROM settings WHERE key='amo_connection'")
        CRM_SESSIONS[str(DB.resolve())] = {"client": client, "metadata": metadata, "remember": remember}
    return crm_public()


def crm_disconnect():
    crm_browser().stop()
    with CRM_LOCK:
        with connect() as db:
            db.execute("DELETE FROM settings WHERE key='amo_connection'")
        CRM_SESSIONS.pop(str(DB.resolve()), None)


def crm_browser():
    key = str(DB.resolve())
    with CRM_LOCK:
        if key not in CRM_BROWSERS:
            CRM_BROWSERS[key] = CRMBrowser(DB.parent / "crm-browser-profile")
        return CRM_BROWSERS[key]


def get_kb(db):
    return json.loads(db.execute("SELECT value FROM settings WHERE key='kb'").fetchone()[0])


def get_dialogue(db, did):
    row = db.execute("SELECT payload FROM dialogues WHERE id=?", (did,)).fetchone()
    if row is None:
        raise ValueError("Диалог не найден")
    return json.loads(row[0])


def save_run(db, d, kb, report, source):
    rid = uuid.uuid4().hex
    db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)",
               (rid, d["id"], source, now(), dump(report), dump(kb)))
    return rid


def check_job(job_id, path=None):
    with connect(path) as db:
        row = db.execute("SELECT * FROM check_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise ValueError("Проверка не найдена")
    return dict(row)


def start_check(dialogue_id, basis="auto"):
    path = DB.resolve()
    with CHECK_LOCK, connect(path) as db:
        active = db.execute("SELECT * FROM check_jobs WHERE status IN ('queued','checking_login','running','correcting','validating') ORDER BY created DESC LIMIT 1").fetchone()
        if active:
            if active["dialogue_id"] == dialogue_id:
                return dict(active)
            raise ValueError("Сейчас уже проверяется другой диалог. Дождитесь результата")
        d = copy.deepcopy(get_dialogue(db, dialogue_id))
        kb = copy.deepcopy(evaluation_kb(get_kb(db), d, basis))
        job_id, created = uuid.uuid4().hex, now()
        db.execute("INSERT INTO check_jobs VALUES (?,?,?,?,?,?,?)", (job_id, dialogue_id, "queued", created, created, "", None))
    worker = threading.Thread(target=perform_check, args=(job_id, d, kb, path), daemon=True, name="manager-answer-check")
    worker.start()
    return check_job(job_id, path)


def perform_check(job_id, d, kb, path):
    def progress(stage):
        with connect(path) as db:
            db.execute("UPDATE check_jobs SET status=?,updated=? WHERE id=?", (stage, now(), job_id))
    try:
        report = run_check(d, kb, progress)
        validate_report(report, d, kb)  # Preserve the validation boundary even if runner changes.
        with connect(path) as db:
            current = get_dialogue(db, d["id"])
            if digest(current) != digest(d):
                raise ValueError("Переписка изменилась во время проверки. Запустите проверку заново")
            rid = save_run(db, d, kb, report, "codex_subscription")
            db.execute("UPDATE check_jobs SET status='complete',updated=?,run_id=? WHERE id=?", (now(), rid, job_id))
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValueError) else "Не удалось завершить проверку. Повторите запуск"
        with connect(path) as db:
            db.execute("UPDATE check_jobs SET status='error',updated=?,error=? WHERE id=?", (now(), message[:1000], job_id))


def state(db):
    runs = []
    for row in db.execute("SELECT * FROM runs ORDER BY created DESC, rowid DESC"):
        r = dict(row)
        r["report"] = json.loads(r["report"])
        r["kb"] = json.loads(r["kb"])
        r["total"] = total(r["report"])
        r["reviews"] = [dict(x) for x in db.execute("SELECT * FROM reviews WHERE run_id=? ORDER BY id", (r["id"],))]
        runs.append(r)
    return {"criteria": CRITERIA, "kb": get_kb(db), "dialogues": [json.loads(r[0]) for r in db.execute("SELECT payload FROM dialogues ORDER BY id")], "runs": runs,
            "checks": [dict(r) for r in db.execute("SELECT * FROM check_jobs ORDER BY created DESC, rowid DESC LIMIT 100")]}


def csv_export(data):
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Диалог", "Источник", "Дата UTC", "Версия базы", *[v[0] for v in CRITERIA.values()], "Итог", "Критический флаг", "Решение", "Проверяющий", "Комментарий"])
    def safe(value):
        value = str(value)
        return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
    for run in data["runs"]:
        r = run["report"]
        review = run["reviews"][-1] if run["reviews"] else {}
        writer.writerow([safe(x) for x in [r["dialogue_id"], run["source"], run["created"], r["kb_version"],
                         *[r["scores"][k]["points"] if r["scores"][k]["points"] is not None else "Н/Д" for k in CRITERIA],
                         run["total"] if run["total"] is not None else "Н/Д",
                         any(f["kind"] == "critical" for f in r["findings"]), review.get("decision", "Ожидает проверки"), review.get("reviewer", ""), review.get("note", "")]])
    return ("\ufeff" + output.getvalue()).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # Do not log conversation bodies.

    def send(self, body, content_type="application/json; charset=utf-8", status=200, filename=None):
        if not isinstance(body, bytes):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def guarded(self):
        host = self.headers.get("Host", "")
        allowed = {f"localhost:{self.server.server_port}", f"127.0.0.1:{self.server.server_port}"}
        if host not in allowed:
            raise ValueError("Недопустимый Host")
        if self.command == "POST" and self.headers.get("X-Workbench-Token") != TOKEN:
            raise ValueError("Обновите страницу: токен сессии устарел")

    def do_GET(self):
        try:
            self.guarded()
            path = urlsplit(self.path).path
            if path in ("/", "/app.js", "/checks.js", "/crm.js", "/crm-browser.js", "/style.css", "/favicon.svg"):
                name = "index.html" if path == "/" else path[1:]
                mime = {"index.html": "text/html", "app.js": "text/javascript", "checks.js": "text/javascript", "crm.js": "text/javascript", "crm-browser.js": "text/javascript", "style.css": "text/css", "favicon.svg": "image/svg+xml"}[name]
                return self.send((ROOT / "app/static" / name).read_bytes(), mime + "; charset=utf-8")
            if path == "/api/state":
                with connect() as db:
                    data = state(db)
                data["token"] = TOKEN
                return self.send(dump(data))
            if path == "/api/crm/status":
                return self.send(dump(crm_public()))
            if path == "/api/crm/browser/status":
                return self.send(dump(crm_browser().status()))
            if path == "/api/check/status":
                return self.send(dump(subscription_status()))
            if path.startswith("/api/check/jobs/"):
                return self.send(dump(check_job(path.split("/")[-1])))
            if path.startswith("/api/packet/"):
                with connect() as db:
                    d = get_dialogue(db, path.split("/")[-1])
                    basis = parse_qs(urlsplit(self.path).query).get("basis", ["auto"])[0]
                    kb = evaluation_kb(get_kb(db), d, basis)
                return self.send(packet(d, kb), "text/plain; charset=utf-8", filename=f"packet-{d['id']}.txt")
            if path == "/api/export.csv":
                with connect() as db:
                    return self.send(csv_export(state(db)), "text/csv; charset=utf-8", filename="quality.csv")
            if path == "/api/backup.json":
                with connect() as db:
                    return self.send(dump(state(db)), filename="quality-backup.json")
            return self.send(dump({"error": "Не найдено"}), status=404)
        except (ValueError, KeyError, TypeError) as exc:
            self.send(dump({"error": str(exc)}), status=400)

    def do_POST(self):
        try:
            self.guarded()
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2_000_000:
                raise ValueError("Допустим JSON до 2 МБ")
            body = json.loads(self.rfile.read(length).decode("utf-8-sig"))
            path = urlsplit(self.path).path
            if path == "/api/check/start":
                return self.send(dump(start_check(body.get("dialogue_id"), body.get("basis", "auto"))))
            if path == "/api/crm/browser/start":
                session = crm_session()
                return self.send(dump(crm_browser().start(session["client"].domain)))
            if path == "/api/crm/browser/stop":
                crm_browser().stop()
                return self.send(dump({"ok": True}))
            if path == "/api/crm/browser/read":
                session = crm_session()
                lead_id = positive_id(body.get("lead_id"))
                lead = session["client"].get(f"/api/v4/leads/{lead_id}")
                talks = session["client"].collection("/api/v4/talks", "talks", {"filter[entity_type]": "lead", "filter[entity_id]": lead_id})
                users = session["client"].collection("/api/v4/users", "users", {"with": "amojo_id"})
                return self.send(dump(crm_browser().read(lead, talks, users)))
            if path == "/api/crm/browser/preview":
                return self.send(dump(crm_browser().preview(body.get("job_id"))))
            if path == "/api/crm/browser/import":
                scope, complete = body.get("scope"), body.get("complete")
                if scope not in ("company", "demo") or type(complete) is not bool:
                    raise CRMError("Укажите режим и полноту истории")
                d = crm_browser().import_dialogue(body.get("job_id"), body.get("revision"), scope, complete)
                with connect() as db:
                    exists = db.execute("SELECT 1 FROM dialogues WHERE id=?", (d["id"],)).fetchone()
                    if not exists:
                        db.execute("INSERT INTO dialogues VALUES (?,?)", (d["id"], dump(d)))
                return self.send(dump({"id": d["id"], "existing": bool(exists)}))
            if path == "/api/crm/connect":
                return self.send(dump(crm_connect(body)))
            if path == "/api/crm/disconnect":
                crm_disconnect()
                return self.send(dump({"ok": True}))
            if path == "/api/crm/leads":
                session = crm_session()
                query = body.get("query", "")
                if not isinstance(query, str):
                    raise CRMError("Поиск должен быть строкой")
                return self.send(dump(session["client"].leads(query, body.get("page", 1))))
            if path == "/api/crm/preview":
                session = crm_session()
                return self.send(dump(session["client"].dialogues(body.get("lead_id"), session["metadata"])))
            if path == "/api/crm/import":
                selected = body.get("selected")
                if not isinstance(selected, list) or not 1 <= len(selected) <= 25:
                    raise CRMError("Выберите от 1 до 25 диалогов")
                session = crm_session()
                grouped = {}
                for item in selected:
                    lead_id, note_id = positive_id(item["lead_id"]), positive_id(item["note_id"])
                    if not isinstance(item.get("id"), str):
                        raise CRMError("Обновите предпросмотр диалога")
                    grouped.setdefault(lead_id, {})[note_id] = item["id"]
                dialogues, errors = [], []
                # Re-fetch selected records; never trust the browser's transcript.
                for lead_id, selected_notes in grouped.items():
                    try:
                        current = session["client"].dialogues(lead_id, session["metadata"])
                        available = {d["source"]["note_id"]: d for d in current["candidates"]}
                        for note_id, revision in selected_notes.items():
                            d = available.get(note_id)
                            if not d or d["id"] != revision:
                                errors.append(f"Сделка #{lead_id}, примечание #{note_id}: данные изменились или недоступны. Обновите предпросмотр")
                            else:
                                dialogues.append(d)
                    except CRMError as exc:
                        errors.append(f"Сделка #{lead_id}: {exc}")
                added, existing = [], []
                with connect() as db:
                    for d in dialogues:
                        exists = db.execute("SELECT 1 FROM dialogues WHERE id=?", (d["id"],)).fetchone()
                        if exists:
                            existing.append(d["id"])
                        else:
                            db.execute("INSERT INTO dialogues VALUES (?,?)", (d["id"], dump(d)))
                            added.append(d["id"])
                return self.send(dump({"added": added, "existing": existing, "errors": errors}))
            with connect() as db:
                if path == "/api/demo":
                    kb = demo_kb()
                    for d in demo_dialogues():
                        db.execute("INSERT OR IGNORE INTO dialogues VALUES (?,?)", (d["id"], dump(d)))
                        if not db.execute("SELECT 1 FROM runs WHERE dialogue_id=? AND source='reference'", (d["id"],)).fetchone():
                            save_run(db, d, kb, reference_report(d, kb), "reference")
                elif path == "/api/dialogues":
                    d = {"id": "C-" + uuid.uuid4().hex[:12], "title": body["title"], "date": body["date"],
                         "scope": body["scope"], "complete": body["complete"], "context": body.get("context", ""),
                         "messages": parse_transcript(body["transcript"])}
                    validate_dialogue(d)
                    db.execute("INSERT INTO dialogues VALUES (?,?)", (d["id"], dump(d)))
                    return self.send_after_commit(db, {"id": d["id"]})
                elif path == "/api/kb":
                    kb = validate_kb(body)
                    old = get_kb(db)
                    if old["version"] == kb["version"] and dump(old) != dump(kb):
                        raise ValueError("При изменении базы задайте новую версию")
                    db.execute("UPDATE settings SET value=? WHERE key='kb'", (dump(kb),))
                elif path == "/api/import":
                    d = get_dialogue(db, body["dialogue_id"])
                    kb = evaluation_kb(get_kb(db), d, body.get("basis", "auto"))
                    incoming = parse_report(body["raw"])
                    if isinstance(incoming, dict) and incoming.get("rubric_version") == "QA-DEMO-1.0":
                        kb.pop("assessment_policy", None)  # Keep older packages under their original stricter policy.
                    report = validate_report(incoming, d, kb)
                    rid = save_run(db, d, kb, report, "chat_import")
                    return self.send_after_commit(db, {"id": rid})
                elif path == "/api/review":
                    run = db.execute("SELECT source FROM runs WHERE id=?", (body["run_id"],)).fetchone()
                    if run is None or run[0] == "reference":
                        raise ValueError("Подтверждать можно только фактический результат, не эталон")
                    reviewer = text(body.get("reviewer"), "Проверяющий", 100)
                    note = text(body.get("note"), "Комментарий проверяющего", 10000)
                    if body.get("decision") not in ("approved", "disputed", "rejected"):
                        raise ValueError("Неизвестное решение")
                    db.execute("INSERT INTO reviews(run_id,created,reviewer,decision,note) VALUES (?,?,?,?,?)",
                               (body["run_id"], now(), reviewer, body["decision"], note))
                else:
                    return self.send(dump({"error": "Не найдено"}), status=404)
            self.send(dump({"ok": True}))
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
            self.send(dump({"error": str(exc)}), status=400)
        except Exception:
            logging.exception("Workbench error")
            self.send(dump({"error": "Ошибка приложения; подробности в консоли"}), status=500)

    def send_after_commit(self, db, data):
        db.commit()
        self.send(dump(data))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="АкваМилк — локальная проверка качества без API")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    init_db()
    with connect() as db:
        db.execute("UPDATE check_jobs SET status='error',error='Приложение было перезапущено. Запустите проверку снова',updated=? WHERE status IN ('queued','checking_login','running','correcting','validating')", (now(),))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"АкваМилк: http://127.0.0.1:{args.port}  |  Остановка: Ctrl+C", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_checks()
        for reader in CRM_BROWSERS.values():
            reader.stop()
        server.server_close()
