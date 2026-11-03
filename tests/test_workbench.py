import copy
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import core
import server


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.kb = core.demo_kb()
        self.dialogues = core.demo_dialogues()
        self.d = self.dialogues[1]
        self.report = core.reference_report(self.d, self.kb)

    def test_five_teaching_cases_keep_expected_scores(self):
        self.assertEqual([100, 90, 70, 80, None], [core.total(core.reference_report(core.validate_dialogue(d), self.kb)) for d in self.dialogues])
        self.assertEqual([3, 3, 3, 3, 2], [len(d["messages"]) for d in self.dialogues])

    def test_only_approved_and_date_applicable_rules_are_exposed(self):
        self.kb["rules"][0]["status"] = "draft"
        self.kb["rules"][1]["valid_to"] = "2026-11-01"
        ids = {r["id"] for r in core.active_kb(self.kb, self.d)["rules"]}
        self.assertNotIn("F01", ids)
        self.assertNotIn("F02", ids)

    def test_no_demo_rules_for_company_dialogue(self):
        self.d["scope"] = "company"
        with self.assertRaises(ValueError):
            core.active_kb(self.kb, self.d)

    def test_reject_fabricated_quote(self):
        self.report["findings"][0]["quote"] = "Такого сообщения нет"
        with self.assertRaisesRegex(ValueError, "Цитата"):
            core.validate_report(self.report, self.d, self.kb)

    def test_reject_unknown_rule(self):
        self.report["findings"][0]["rule_ids"] = ["F99"]
        with self.assertRaisesRegex(ValueError, "правило"):
            core.validate_report(self.report, self.d, self.kb)

    def test_reject_wrong_message_role(self):
        self.report["questions"][0]["answer_message_ids"] = ["M01"]
        with self.assertRaisesRegex(ValueError, "менеджера"):
            core.validate_report(self.report, self.d, self.kb)

    def test_reject_stale_dialogue_or_kb(self):
        for field in ("input_hash", "kb_hash", "kb_version", "dialogue_id", "rubric_version"):
            report = copy.deepcopy(self.report)
            report[field] = "old"
            with self.subTest(field=field), self.assertRaises(ValueError):
                core.validate_report(report, self.d, self.kb)

    def test_reject_non_rubric_scores_and_bool(self):
        for points in (17, True, "20"):
            self.report["scores"]["tone"]["points"] = points
            with self.subTest(points=points), self.assertRaises(ValueError):
                core.validate_report(self.report, self.d, self.kb)

    def test_deduction_requires_evidence(self):
        self.report["findings"] = []
        with self.assertRaisesRegex(ValueError, "доказанного"):
            core.validate_report(self.report, self.d, self.kb)

    def test_partial_history_cannot_receive_total(self):
        d = self.dialogues[-1]
        report = core.reference_report(d, self.kb)
        self.assertIsNone(core.total(report))
        report["scores"]["completeness"]["points"] = 20
        with self.assertRaisesRegex(ValueError, "Неполная история"):
            core.validate_report(report, d, self.kb)

    def test_missing_card_cannot_receive_compliance_score(self):
        report = copy.deepcopy(self.report)
        self.d["context"] = ""
        report["input_hash"] = core.digest(self.d)
        with self.assertRaisesRegex(ValueError, "карточки"):
            core.validate_report(report, self.d, self.kb)

    def test_packet_does_not_include_reference_answers_or_drafts(self):
        self.kb["rules"][0]["status"] = "draft"
        kb = core.active_kb(self.kb, self.d)
        packet = core.packet(self.d, kb)
        self.assertIn(core.digest(self.d), packet)
        self.assertNotIn('"status": "draft"', packet)
        self.assertNotIn("Авторский учебный ориентир", packet)

    def test_fenced_json_supported(self):
        self.assertEqual(self.report, core.parse_report("```json\n" + core.dump(self.report) + "\n```"))

    def test_transcript_preserves_colons_and_speaker(self):
        messages = core.parse_transcript("Клиент: До 18:00?\nМенеджер С02: Уточню: сообщу позже.")
        self.assertEqual("До 18:00?", messages[0]["text"])
        self.assertEqual("Менеджер С02", messages[1]["author"])


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = server.DB
        server.DB = Path(self.tmp.name) / "test.sqlite3"
        server.init_db()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()
        server.DB = self.old_db
        self.tmp.cleanup()

    def request(self, path, body=None, token=server.TOKEN):
        headers = {"Content-Type": "application/json", "X-Workbench-Token": token}
        req = Request(self.base + path, data=None if body is None else core.dump(body).encode(), headers=headers)
        with urlopen(req, timeout=5) as response:
            return response.status, response.read(), response.headers

    def test_full_subscription_import_review_export_lifecycle(self):
        self.request("/api/demo", {})
        self.request("/api/demo", {})
        _, raw, _ = self.request("/api/state")
        data = json.loads(raw)
        self.assertEqual(5, len(data["dialogues"]))
        self.assertEqual(5, len(data["runs"]))  # Idempotent demo load.
        _, raw, headers = self.request("/api/packet/D02")
        self.assertIn("text/plain", headers["Content-Type"])
        self.assertIn("АКТИВНАЯ БАЗА", raw.decode())
        report = next(r["report"] for r in data["runs"] if r["dialogue_id"] == "D02")
        _, raw, _ = self.request("/api/import", {"dialogue_id": "D02", "raw": core.dump(report)})
        rid = json.loads(raw)["id"]
        self.request("/api/review", {"run_id": rid, "reviewer": "Тест", "decision": "approved", "note": "Проверено в интеграционном тесте"})
        self.request("/api/review", {"run_id": rid, "reviewer": "Тест", "decision": "disputed", "note": "Проверка сохранения истории решений"})
        _, raw, _ = self.request("/api/state")
        run = next(r for r in json.loads(raw)["runs"] if r["id"] == rid)
        self.assertEqual(90, run["total"])
        self.assertEqual(2, len(run["reviews"]))
        _, csv, _ = self.request("/api/export.csv")
        self.assertTrue(csv.startswith(b"\xef\xbb\xbf"))
        self.assertIn("disputed", csv.decode())
        _, backup, _ = self.request("/api/backup.json")
        self.assertNotIn("token", json.loads(backup))

    def test_references_cannot_be_approved(self):
        self.request("/api/demo", {})
        _, raw, _ = self.request("/api/state")
        rid = json.loads(raw)["runs"][0]["id"]
        with self.assertRaises(HTTPError) as err:
            self.request("/api/review", {"run_id": rid, "reviewer": "Тест", "decision": "approved", "note": "Тест"})
        self.assertEqual(400, err.exception.code)

    def test_csrf_protection(self):
        with self.assertRaises(HTTPError) as err:
            self.request("/api/demo", {}, token="wrong")
        self.assertEqual(400, err.exception.code)

    def test_static_and_no_database_served(self):
        for path in ("/", "/app.js", "/style.css"):
            status, _, headers = self.request(path)
            self.assertEqual(200, status)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        with self.assertRaises(HTTPError) as err:
            self.request("/data/workbench.sqlite3")
        self.assertEqual(404, err.exception.code)

    def test_company_dialogue_gets_general_rubric_without_demo_conditions(self):
        _, raw, _ = self.request("/api/dialogues", {"title": "Рабочий тест", "date": "2026-11-03", "scope": "company", "complete": False,
                              "transcript": "Клиент: Цена?\nМенеджер С01: Уточню", "context": ""})
        did = json.loads(raw)["id"]
        _, packet, _ = self.request("/api/packet/" + did)
        self.assertIn(b'GENERAL-QA-1.0', packet)
        self.assertNotIn(b'"id": "F01"', packet)

    def test_kb_update_requires_new_version_and_keeps_snapshot(self):
        self.request("/api/demo", {})
        kb = core.demo_kb()
        kb["rules"][0]["text"] = "Изменение"
        with self.assertRaises(HTTPError):
            self.request("/api/kb", kb)
        kb["version"] = "DEMO-1.1"
        self.request("/api/kb", kb)
        _, raw, _ = self.request("/api/state")
        data = json.loads(raw)
        self.assertEqual("DEMO-1.1", data["kb"]["version"])
        self.assertTrue(all(r["kb"]["version"] == "DEMO-1.0" for r in data["runs"]))


if __name__ == "__main__":
    unittest.main()
