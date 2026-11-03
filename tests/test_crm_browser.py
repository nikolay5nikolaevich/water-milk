import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from amocrm import CRMError
from core import demo_kb, report_template, validate_report
from crm_browser import CRMBrowser, captured_dialogue, message_batch, normalize_message, response_chat_id, timeline_lead_id

USERS = [{"id": 7, "amojo_id": "staff-chat-id", "name": "Менеджер Алена"}]
INCOMING = {"timestamp": 1793696400, "sender": {"id": "external-chat-id", "client_id": "telegram-123"},
            "message": {"id": "m-client", "type": "text", "text": "Когда доставите?"}}
OUTGOING = {"timestamp": 1793696460, "sender": {"id": "staff-chat-id", "name": "Подставное имя"},
            "receiver": {"id": "external-chat-id", "client_id": "telegram-123"},
            "message": {"id": "m-manager", "type": "text", "text": "Уточню согласованный срок."}}


def job():
    return {"domain": "ivanovnn.amocrm.ru", "lead_id": 101, "lead_name": "Доставка",
            "chat_id": "chat-123", "url": "https://ivanovnn.amocrm.ru/leads/detail/101", "users": USERS,
            "records": {}, "error": ""}


def native_timeline():
    # Same schema as the observed CRM response; IDs and text are synthetic.
    return {"_embedded": {"items": [
        {"id": "event-in", "type": 89, "element_id": 303, "element_type": 1,
         "data": {"id": "timeline-in", "chat_id": "chat-123", "created_at": 1793696400,
                  "author": {"id": "external-chat-id", "origin": "telegram"},
                  "message": {"type": "text", "text": "Когда доставите?"}}},
        {"id": "event-out", "type": 90, "element_id": 303, "element_type": 1,
         "data": {"id": "timeline-out", "chat_id": "chat-123", "msec_created_at": 1793696460081,
                  "author": {"id": "staff-chat-id", "origin": "amocrm"},
                  "message": {"type": "text", "text": "Уточню срок."}}}],
        "chats": {"chat-123": {"contacts": {"external-chat-id": {"id": 303}}, "users": {"staff-chat-id": {"id": 7}}}}}}


class BrowserReaderTests(unittest.TestCase):
    def reader_response(self, payload, url="https://ivanovnn.amocrm.ru/ajax/v3/leads/101/events_timeline", status=200):
        reader = CRMBrowser('not-launched-test-profile')
        reader.jobs['j'] = job()
        response = Mock(url=url, status=status)
        response.body.return_value = json.dumps(payload).encode()
        reader._response(response)
        return reader, response

    def test_native_timeline_reads_contact_messages_inside_selected_lead(self):
        reader, response = self.reader_response(native_timeline())
        status = reader.status()['jobs'][0]
        self.assertEqual(2, status['message_count'])
        self.assertEqual('messages_loaded', status['stage'])
        self.assertEqual(1, status['history_responses'])
        self.assertEqual(200, status['last_http_status'])
        d = reader.preview('j')['dialogue']
        self.assertEqual(['client', 'manager'], [m['role'] for m in d['messages']])
        self.assertFalse(d['complete'])
        reader._response(response)
        self.assertEqual(2, reader.status()['jobs'][0]['message_count'])

    def test_native_timeline_does_not_capture_other_leads_or_domains(self):
        for url in ('https://ivanovnn.amocrm.ru/ajax/v3/leads/999/events_timeline',
                    'https://other.amocrm.ru/ajax/v3/leads/101/events_timeline'):
            reader, response = self.reader_response(native_timeline(), url)
            response.body.assert_not_called()
            self.assertEqual(0, reader.status()['jobs'][0]['message_count'])

    def test_native_timeline_does_not_capture_other_chats_or_contacts(self):
        payload = native_timeline()
        payload['_embedded']['items'][0]['data']['chat_id'] = 'other-chat'
        payload['_embedded']['items'][1]['element_id'] = 9999
        reader, _ = self.reader_response(payload)
        self.assertEqual(0, reader.status()['jobs'][0]['message_count'])
        self.assertEqual('empty_response', reader.status()['jobs'][0]['stage'])

    def test_native_timeline_blocks_unknown_senders(self):
        payload = native_timeline()
        payload['_embedded']['items'][0]['data']['author']['id'] = 'unknown'
        reader, _ = self.reader_response(payload)
        status = reader.status()['jobs'][0]
        self.assertEqual(0, status['message_count'])
        self.assertEqual('parse_error', status['stage'])
        self.assertTrue(status['error'])

    def test_http_failure_is_explicit_instead_of_silent_zero(self):
        reader, response = self.reader_response({}, status=403)
        response.body.assert_not_called()
        status = reader.status()['jobs'][0]
        self.assertEqual('request_error', status['stage'])
        self.assertIn('403', status['error'])

    def test_capture_only_history_endpoint_on_chat_service(self):
        self.assertEqual("chat-123", response_chat_id("https://amojo.amocrm.ru/v1.0/chats/chat-123/messages?offset=0"))
        for bad in ("https://evil.com/v1.0/chats/chat-123/messages", "http://amojo.amocrm.ru/v1.0/chats/chat-123/messages",
                    "https://amojo.amocrm.ru/token", "https://amojo.amocrm.ru.evil.com/chats/chat-123/messages"):
            self.assertIsNone(response_chat_id(bad))

    def test_external_and_internal_sender_identity(self):
        incoming, outgoing = [normalize_message(m, USERS) for m in (INCOMING, OUTGOING)]
        self.assertEqual("client", incoming["role"])
        self.assertEqual("manager", outgoing["role"])
        self.assertEqual("Менеджер Алена", outgoing["author"])

    def test_unknown_sender_not_attributed_by_display_name(self):
        item = copy.deepcopy(OUTGOING)
        item["sender"] = {"id": "unknown-bot", "name": "Менеджер Алена"}
        with self.assertRaises(CRMError):
            normalize_message(item, USERS)

    def test_media_not_silently_dropped(self):
        item = copy.deepcopy(INCOMING)
        item["message"]["type"] = "voice"
        with self.assertRaises(CRMError):
            normalize_message(item, USERS)

    def test_unknown_payload_rejected(self):
        with self.assertRaises(CRMError):
            message_batch({"access_token": "not-to-be-stored"})

    def test_messages_sorted_and_history_partial_by_default(self):
        records = {m["message"]["id"]: normalize_message(m, USERS) for m in (OUTGOING, INCOMING)}
        d = captured_dialogue(job(), records)
        self.assertEqual("m-client", d["messages"][0]["id"])
        self.assertFalse(d["complete"])
        self.assertFalse(d["source"]["card_verified"])
        self.assertEqual("2026-11-03", d["date"])
        self.assertEqual(d["id"], captured_dialogue(job(), records)["id"])
        self.assertNotEqual(d["id"], captured_dialogue(job(), records, complete=True)["id"])

    def test_only_client_messages_cannot_be_evaluated(self):
        item = normalize_message(INCOMING, USERS)
        with self.assertRaises(CRMError):
            captured_dialogue(job(), {item["id"]: item})

    def test_full_history_does_not_prove_card_compliance(self):
        records = {m["message"]["id"]: normalize_message(m, USERS) for m in (INCOMING, OUTGOING)}
        d = captured_dialogue(job(), records, complete=True)
        report = report_template(d, demo_kb())
        report["scores"]["compliance"]["points"] = 10
        with self.assertRaisesRegex(ValueError, "карточки"):
            validate_report(report, d, demo_kb())

    def test_response_capture_deduplicates_without_retaining_tokens(self):
        reader = CRMBrowser('not-launched-test-profile')
        reader.jobs['j'] = job()
        response = Mock(url='https://amojo.amocrm.ru/v1.0/chats/chat-123/messages', status=200)
        response.body.return_value = json.dumps({"messages": [OUTGOING, INCOMING], "access_token": "should-not-be-retained"}).encode()
        reader._response(response)
        reader._response(response)
        self.assertEqual(2, reader.status()["jobs"][0]["message_count"])
        self.assertNotIn('should-not-be-retained', json.dumps(reader.jobs))

    def test_unselected_chat_response_is_never_read(self):
        reader = CRMBrowser('not-launched-test-profile')
        reader.jobs['j'] = job()
        response = Mock(url='https://amojo.amocrm.ru/v1.0/chats/unselected/messages', status=200)
        reader._response(response)
        response.body.assert_not_called()

    def test_partial_batch_on_unsupported_message_not_imported(self):
        reader = CRMBrowser('not-launched-test-profile')
        reader.jobs['j'] = job()
        unknown = copy.deepcopy(OUTGOING)
        unknown["sender"] = {"id": "unknown"}
        response = Mock(url='https://amojo.amocrm.ru/v1.0/chats/chat-123/messages', status=200)
        response.body.return_value = json.dumps({"messages": [INCOMING, unknown]}).encode()
        reader._response(response)
        self.assertEqual(0, reader.status()["jobs"][0]["message_count"])
        self.assertTrue(reader.status()["jobs"][0]["error"])

    def test_stale_revision_cannot_be_imported(self):
        reader = CRMBrowser('not-launched-test-profile')
        reader.jobs['j'] = job()
        with self.assertRaises(CRMError):
            reader.import_dialogue('j', 'stale', 'company', False)


if __name__ == '__main__':
    unittest.main()
