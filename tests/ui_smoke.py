"""Optional UI checks: python tests/ui_smoke.py (Playwright and Chrome required).

Serve real static files with synthetic API responses. Never connects to CRM or Codex.
"""
import copy
import json
import sys
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from core import CRITERIA, demo_dialogues, demo_kb, reference_report


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class InterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(ROOT / "app/static")))
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)
        cls.base = f"http://127.0.0.1:{cls.http.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.requests = []
        self.data = {"token": "test-only", "criteria": CRITERIA, "kb": demo_kb(), "dialogues": [], "runs": [], "checks": []}
        self.connected = True
        self.phase = "closed"
        self.start_polls = 0
        self.jobs = []
        self.start_error = self.read_error = self.import_error = self.check_error = ""
        self.dialogue = copy.deepcopy(demo_dialogues()[0])
        self.dialogue.update(id="CHAT-test", title="Доставка в офис · Telegram", scope="company", complete=False)
        self.page.route("**/api/**", self.api)

    def tearDown(self):
        self.context.close()
        self.assertEqual([], self.errors)

    def api(self, route):
        path = route.request.url.split(self.base)[-1]
        body = route.request.post_data_json if route.request.method == "POST" else None
        self.requests.append((path, body))
        status, data = 200, {}
        if path == "/api/state":
            data = self.data
        elif path == "/api/crm/status":
            data = {"connected": self.connected, "domain": "example.amocrm.ru", "account": {"name": "Тестовая компания"}, "remember": True, "warnings": [], "message": "Подключите аккаунт amoCRM", "users": [{"id": 7, "name": "Анна Иванова"}], "pipelines": [{"id": 1, "name": "Продажи", "statuses": [{"id": 2, "name": "В работе"}]}]}
        elif path == "/api/crm/leads":
            data = {"leads": [{"id": 101, "name": "Доставка в офис", "responsible_user_id": 7, "pipeline_id": 1, "status_id": 2, "updated_at": 1793696400}], "page": 1, "has_more": False}
        elif path == "/api/crm/browser/start":
            if self.start_error:
                status, data = 400, {"error": self.start_error}
            else:
                self.phase, self.start_polls = "starting", 0
                data = self.browser_status()
        elif path == "/api/crm/browser/status":
            if self.phase == "starting":
                self.start_polls += 1
                if self.start_polls >= 2:
                    self.phase = "open"
            data = self.browser_status()
        elif path == "/api/crm/browser/read":
            if self.read_error:
                status, data = 400, {"error": self.read_error}
            else:
                self.jobs = [{"id": "job-test", "lead_id": 101, "lead_name": "Доставка в офис", "message_count": 3, "error": "", "stage": "messages_loaded", "history_responses": 1, "last_http_status": 200}]
                data = self.browser_status()
        elif path == "/api/crm/browser/preview":
            data = {"dialogue": self.dialogue, "revision": "test-revision", "error": ""}
        elif path == "/api/crm/browser/import":
            if self.import_error:
                status, data = 400, {"error": self.import_error}
            else:
                self.dialogue.update(scope=body["scope"], complete=body["complete"])
                self.data["dialogues"] = [self.dialogue]
                data = {"id": self.dialogue["id"], "existing": False}
        elif path == "/api/check/start":
            if self.check_error:
                status, data = 400, {"error": self.check_error}
            else:
                data = {"id": "check-test", "dialogue_id": self.dialogue["id"], "status": "queued"}
                self.data["checks"] = [data]
        elif path == "/api/check/jobs/check-test":
            data = {"id": "check-test", "dialogue_id": self.dialogue["id"], "status": "complete"}
            self.data["checks"] = [data]
            self.data["runs"] = [{"id": "run-test", "dialogue_id": self.dialogue["id"], "source": "codex_subscription", "report": reference_report(demo_dialogues()[0], demo_kb()), "kb": demo_kb(), "total": 100, "created": "2026-11-03T10:00:00Z", "reviews": []}]
        elif path == "/api/crm/preview":
            status, data = 400, {"error": "Примечания недоступны"}
        else:
            raise AssertionError(f"Unexpected API request: {path}")
        route.fulfill(status=status, content_type="application/json", body=json.dumps(data, ensure_ascii=False))

    def browser_status(self):
        return {"phase": self.phase, "error": "", "jobs": self.jobs}

    def open_app(self):
        self.page.goto(self.base)
        expect(self.page.locator("#crm-workspace")).to_be_visible()
        expect(self.page.locator("#crm-leads .primary")).to_be_enabled()

    def open_chat(self):
        self.open_app()
        self.page.locator("#crm-leads .primary").click()
        expect(self.page.locator("[data-browser-preview]")).to_be_enabled(timeout=10000)
        self.page.locator("[data-browser-preview]").click()
        expect(self.page.locator("#browser-preview")).to_be_visible()

    def test_direct_chat_import_and_check_without_notes(self):
        self.open_chat()
        self.page.locator("#browser-import").dblclick()
        expect(self.page.locator("#detail")).to_be_visible()
        expect(self.page.locator("#auto-check-status")).to_have_text("Проверка завершена. Результат сохранён.", timeout=10000)
        paths = [p for p, _ in self.requests]
        self.assertNotIn("/api/crm/preview", paths)
        self.assertEqual(1, paths.count("/api/crm/browser/import"))
        self.assertEqual(1, paths.count("/api/check/start"))
        self.assertEqual("auto", next(b for p, b in self.requests if p == "/api/check/start")["basis"])
        self.assertFalse(next(b for p, b in self.requests if p == "/api/crm/browser/import")["complete"])
        expect(self.page.get_by_role("heading", name="Результат проверки")).to_be_visible()

    def test_save_without_check_and_explicit_demo_basis(self):
        self.open_chat()
        self.page.locator("#browser-scope").select_option("demo")
        self.page.locator("#browser-complete").check()
        self.page.locator("#browser-save").click()
        expect(self.page.locator("#detail")).to_be_visible()
        self.assertNotIn("/api/check/start", [p for p, _ in self.requests])
        expect(self.page.locator("#assessment-basis")).to_have_value("demo")
        self.assertTrue(self.data["dialogues"][0]["complete"])
        self.assertEqual("demo", self.data["dialogues"][0]["scope"])

    def test_notes_failure_keeps_direct_chat_available(self):
        self.open_app()
        self.page.locator("#crm-leads [data-crm-preview]").click()
        expect(self.page.locator("#crm-previews")).to_contain_text("Примечания недоступны")
        self.page.locator("#crm-leads .primary").click()
        expect(self.page.locator("[data-browser-preview]")).to_be_enabled(timeout=10000)

    def test_start_failure_can_be_dismissed_and_retried(self):
        self.start_error = "Chrome не найден"
        self.open_app()
        self.page.locator("#crm-leads .primary").click()
        expect(self.page.locator("#notice")).to_contain_text(self.start_error)
        self.page.locator("#notice-close").click()
        expect(self.page.locator("#notice")).to_be_hidden()
        self.start_error = ""
        self.page.locator("#crm-leads .primary").click()
        expect(self.page.locator("[data-browser-preview]")).to_be_enabled(timeout=10000)

    def test_old_closed_status_does_not_cancel_new_chat(self):
        self.open_app()
        self.page.evaluate("""() => {
            const originalFetch = window.fetch;
            let hold = true;
            window.fetch = (url, options) => {
                if (url === '/api/crm/browser/status' && hold) {
                    hold = false;
                    return new Promise(resolve => {
                        window.releaseOldStatus = () => resolve(new Response(JSON.stringify({phase:'closed',error:'',jobs:[]})));
                    });
                }
                return originalFetch(url, options);
            };
            browserPoll();
        }""")
        self.page.locator("#crm-leads .primary").click()
        expect(self.page.locator("#browser-start")).to_be_disabled()
        self.page.evaluate("releaseOldStatus()")
        expect(self.page.locator("[data-browser-preview]")).to_be_enabled(timeout=10000)

    def test_read_error_survives_status_refresh(self):
        self.read_error = "У сделки нет доступных бесед"
        self.open_app()
        self.page.locator("#crm-leads .primary").click()
        expect(self.page.locator("#browser-status")).to_have_text(self.read_error, timeout=10000)
        self.page.evaluate("browserPoll()")
        expect(self.page.locator("#browser-status")).to_have_text(self.read_error)

    def test_changed_history_blocks_import_and_check(self):
        self.open_chat()
        self.import_error = "История изменилась. Обновите предпросмотр"
        self.page.locator("#browser-import").click()
        expect(self.page.locator("#notice")).to_contain_text(self.import_error)
        expect(self.page.locator("#browser-import")).to_be_enabled()
        expect(self.page.locator("#browser-save")).to_be_enabled()
        self.assertNotIn("/api/check/start", [p for p, _ in self.requests])

    def test_check_error_keeps_saved_dialogue_and_allows_retry(self):
        self.open_chat()
        self.check_error = "Нужно войти в Codex"
        self.page.locator("#browser-import").click()
        expect(self.page.locator("#auto-check-status")).to_have_text(self.check_error)
        expect(self.page.locator("#auto-check")).to_be_enabled()
        self.assertEqual(1, len(self.data["dialogues"]))

    def test_mobile_has_no_page_overflow(self):
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.open_chat()
        self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        expect(self.page.locator("#browser-import")).to_be_visible()

    def test_disconnected_account_shows_connection_form(self):
        self.connected = False
        self.page.goto(self.base)
        expect(self.page.locator("#crm-connection")).to_have_attribute("open", "")
        expect(self.page.locator("#crm-workspace")).to_be_hidden()
        expect(self.page.locator("[name=access_token]")).to_be_visible()


if __name__ == "__main__":
    unittest.main(verbosity=2)
