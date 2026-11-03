import copy
import base64
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import amocrm
import core
import server

DOMAIN = "ivanovnn.amocrm.ru"
META = {"account": {"id": 1, "name": "Тестовый аккаунт"}, "users": [{"id": 7, "name": "Менеджер Алена"}],
        "pipelines": [{"id": 10, "name": "Тестовая", "statuses": [{"id": 20, "name": "В работе"}]}], "warnings": []}
LEAD = {"id": 101, "name": "Учебный тест D02", "pipeline_id": 10, "status_id": 20, "responsible_user_id": 7,
        "created_at": 1793610000, "updated_at": 1793696400}


def fixture_note(index=1):
    d = core.demo_dialogues()[index]
    messages = "\n".join(f'- {m["id"]}, {m["time"]}, {m["author"]}: «{m["text"]}».' for m in d["messages"])
    return {"id": 501, "note_type": "common", "created_at": 1793610000, "updated_at": 1793696400,
            "params": {"text": "УЧЕБНЫЙ СЦЕНАРИЙ. Вымышленные условия и сообщения; это не реальная переписка.\n"
                       "Сотрудник С01 в сценарии соответствует ответственному сделки.\n" + d["context"] + "\n" + messages}}


class ConnectorTests(unittest.TestCase):
    def test_accept_only_account_domains(self):
        self.assertEqual(DOMAIN, amocrm.domain_name("https://" + DOMAIN + "/"))
        for bad in ("http://" + DOMAIN, "localhost:8765", "evil.com", DOMAIN + ".evil.com", DOMAIN + "/api/v4/leads", "user:pass@" + DOMAIN, "a.amocrm.ru:443"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                amocrm.domain_name(bad)

    def test_no_credential_redirects(self):
        with self.assertRaises(amocrm.CRMError):
            amocrm.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.com")

    def test_parse_all_five_uploaded_scenarios(self):
        for index, expected in enumerate((3, 3, 3, 3, 2)):
            d = amocrm.note_dialogue(fixture_note(index), LEAD, DOMAIN, META)
            self.assertEqual(expected, len(d["messages"]))
            self.assertEqual(index != 4, d["complete"])
            self.assertEqual("2026-11-02", d["date"])
            self.assertEqual("demo", d["scope"])
            self.assertIn("Алена", d["messages"][1]["author"])

    def test_arbitrary_notes_are_not_dialogues(self):
        note = fixture_note()
        note["params"]["text"] = "Позвонить клиенту завтра, ожидает договор."
        self.assertIsNone(amocrm.note_dialogue(note, LEAD, DOMAIN, META))

    def test_talk_metadata_is_not_presented_as_message_history(self):
        client = amocrm.AmoClient(DOMAIN, "test-token")
        with patch.object(client, "get", return_value=LEAD), patch.object(client, "collection", side_effect=[[], [{"talk_id": 123}]]):
            result = client.dialogues(101, META)
        self.assertEqual([], result["candidates"])
        self.assertTrue(any("отдельный доступ" in warning for warning in result["warnings"]))

    def test_plain_roles_remain_partial_and_authorship_is_not_assumed(self):
        note = fixture_note()
        note["params"]["text"] = "Клиент: Цена?\nМенеджер С02: Уточню условия."
        d = amocrm.note_dialogue(note, LEAD, DOMAIN, META)
        self.assertEqual("company", d["scope"])
        self.assertFalse(d["complete"])
        self.assertEqual("Менеджер С02", d["messages"][1]["author"])

    def test_broken_role_or_message_not_silently_dropped(self):
        note = fixture_note()
        note["params"]["text"] += '\n- M04, 12:00, неизвестно: «Кто написал?». '
        self.assertIsNone(amocrm.note_dialogue(note, LEAD, DOMAIN, META))

    def test_revision_is_stable_and_changes_with_source(self):
        note = fixture_note()
        d1 = amocrm.note_dialogue(note, LEAD, DOMAIN, META)
        self.assertEqual(d1["id"], amocrm.note_dialogue(copy.deepcopy(note), LEAD, DOMAIN, META)["id"])
        note["params"]["text"] = note["params"]["text"].replace("1 000", "2 000")
        self.assertNotEqual(d1["id"], amocrm.note_dialogue(note, LEAD, DOMAIN, META)["id"])

    def test_pagination_uses_our_origin_not_next_url(self):
        client = amocrm.AmoClient(DOMAIN, "test-token")
        with patch.object(client, "get", side_effect=[{"_embedded": {"notes": [{"id": 1}]}, "_links": {"next": {"href": "https://evil.com"}}},
                                                      {"_embedded": {"notes": [{"id": 2}]}}]) as get:
            self.assertEqual([{"id": 1}, {"id": 2}], client.collection("/api/v4/leads/101/notes", "notes"))
            self.assertEqual("/api/v4/leads/101/notes", get.call_args.args[0])
            self.assertEqual(2, get.call_args.args[1]["page"])

    def test_truncation_is_an_error(self):
        client = amocrm.AmoClient(DOMAIN, "test-token")
        with patch.object(client, "get", return_value={"_embedded": {"notes": []}, "_links": {"next": {}}}), self.assertRaises(amocrm.CRMError):
            # Nonempty next link is required by the API.
            client.get.return_value["_links"]["next"] = {"href": "next"}
            client.collection("/api/v4/leads/101/notes", "notes", max_pages=1)

    def test_401_does_not_expose_token_or_response(self):
        client = amocrm.AmoClient(DOMAIN, "test-token")
        error = HTTPError("https://" + DOMAIN, 401, "Unauthorized", {}, io.BytesIO(b"private body"))
        with patch.object(client.opener, "open", side_effect=error), self.assertRaises(amocrm.CRMError) as ctx:
            client.get("/api/v4/account")
        self.assertNotIn("test-token", str(ctx.exception))
        self.assertNotIn("private body", str(ctx.exception))

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_windows_token_encryption_roundtrip(self):
        try:
            protected = amocrm.protect_token("synthetic-secret-token")
        except amocrm.CRMError:
            self.skipTest("В профиле песочницы Windows DPAPI недоступна")
        self.assertNotIn("synthetic-secret-token", protected)
        self.assertEqual("synthetic-secret-token", amocrm.protect_token(protected, decrypt=True))


class FakeClient:
    def __init__(self, domain, token):
        self.domain = amocrm.domain_name(domain)
        self.token = token

    def metadata(self):
        return copy.deepcopy(META)

    def leads(self, query="", page=1):
        return {"leads": [copy.deepcopy(LEAD)], "has_more": False, "page": page}

    def dialogues(self, lead_id, metadata):
        if int(lead_id) != LEAD["id"]:
            raise amocrm.CRMError("Нет прав на сделку")
        d = amocrm.note_dialogue(fixture_note(), LEAD, self.domain, metadata)
        return {"lead_id": 101, "lead_name": LEAD["name"], "candidates": [d], "other_notes": [], "warnings": []}


class CRMEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db = server.DB
        server.DB = Path(self.tmp.name) / "qa.sqlite3"
        server.init_db()
        self.fake = patch.object(server, "AmoClient", FakeClient)
        self.fake.start()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()
        server.CRM_SESSIONS.pop(str(server.DB.resolve()), None)
        server.DB = self.original_db
        self.fake.stop()
        self.tmp.cleanup()

    def request(self, path, body=None):
        req = Request(self.base + path, data=None if body is None else core.dump(body).encode(),
                      headers={"Content-Type": "application/json", "X-Workbench-Token": server.TOKEN})
        with urlopen(req, timeout=5) as response:
            return json.loads(response.read())

    def connect(self, remember=False):
        return self.request("/api/crm/connect", {"domain": DOMAIN, "access_token": "fake-secret", "remember": remember})

    def test_connect_list_preview_import_without_manual_transcript(self):
        self.assertFalse(self.request("/api/crm/status")["connected"])
        status = self.connect()
        self.assertTrue(status["connected"])
        self.assertNotIn("fake-secret", core.dump(status))
        leads = self.request("/api/crm/leads", {"page": 1})
        self.assertEqual(101, leads["leads"][0]["id"])
        preview = self.request("/api/crm/preview", {"lead_id": 101})
        d = preview["candidates"][0]
        selection = {"selected": [{"lead_id": 101, "note_id": 501, "id": d["id"]}]}
        result = self.request("/api/crm/import", selection)
        self.assertEqual([d["id"]], result["added"])
        self.assertEqual([d["id"]], self.request("/api/crm/import", selection)["existing"])
        state = self.request("/api/state")
        self.assertEqual(1, len(state["dialogues"]))
        self.assertEqual([], state["runs"])
        self.assertEqual("amocrm_note", state["dialogues"][0]["source"]["type"])
        self.assertNotIn("fake-secret", core.dump(self.request("/api/backup.json")))

    def test_stale_preview_is_not_imported(self):
        self.connect()
        result = self.request("/api/crm/import", {"selected": [{"lead_id": 101, "note_id": 501, "id": "old-revision"}]})
        self.assertFalse(result["added"])
        self.assertEqual(1, len(result["errors"]))

    def test_partial_batch_success_is_reported(self):
        self.connect()
        d = self.request("/api/crm/preview", {"lead_id": 101})["candidates"][0]
        result = self.request("/api/crm/import", {"selected": [{"lead_id": 101, "note_id": 501, "id": d["id"]},
                                                               {"lead_id": 999, "note_id": 501, "id": "unavailable"}]})
        self.assertEqual(1, len(result["added"]))
        self.assertEqual(1, len(result["errors"]))

    def test_disconnect_removes_access_but_keeps_imported_dialogues(self):
        self.connect()
        self.request("/api/crm/disconnect", {})
        self.assertFalse(self.request("/api/crm/status")["connected"])
        with self.assertRaises(HTTPError):
            self.request("/api/crm/leads", {})

    def test_persisted_token_is_encrypted_and_restores(self):
        # Test the storage boundary independently of OS crypto availability.
        def test_protector(value, decrypt=False):
            return base64.b64decode(value).decode() if decrypt else base64.b64encode(value.encode()).decode()
        with patch.object(server, "protect_token", side_effect=test_protector):
            self.connect(remember=True)
            with server.connect() as db:
                saved = db.execute("SELECT value FROM settings WHERE key='amo_connection'").fetchone()[0]
            self.assertNotIn("fake-secret", saved)
            server.CRM_SESSIONS.clear()
            self.assertTrue(self.request("/api/crm/status")["connected"])

    def test_unavailable_windows_crypto_keeps_token_in_memory_only(self):
        with patch.object(server, "protect_token", side_effect=amocrm.CRMError("Not available")):
            status = self.connect(remember=True)
        self.assertTrue(status["connected"])
        self.assertFalse(status["remember"])
        self.assertTrue(status["warnings"])
        with server.connect() as db:
            self.assertIsNone(db.execute("SELECT value FROM settings WHERE key='amo_connection'").fetchone())


if __name__ == "__main__":
    unittest.main()
