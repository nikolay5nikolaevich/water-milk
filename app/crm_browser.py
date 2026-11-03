"""Read chat-history responses from a separate, user-authenticated CRM browser.

No cookies/tokens are copied from another browser, no webhook is changed.
Frontend APIs are not a stable public contract: unsupported responses fail closed.
"""
import copy
import queue
import re
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from amocrm import CRMError, MOSCOW, domain_name
from core import digest, validate_dialogue


def response_chat_id(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ("amojo.amocrm.ru", "amojo.amocrm.com", "amojo.kommo.com"):
        return None
    match = re.search(r"/chats/([^/]+)/(?:messages|history)(?:/|$)", parts.path)
    return unquote(match.group(1)) if match else None


def timeline_lead_id(url, domain):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != domain:
        return None
    match = re.fullmatch(r"/ajax/v3/leads/(\d+)/events_timeline/?", parts.path)
    return int(match.group(1)) if match else None


def timeline_batch(payload, job):
    """Observed native CRM timeline, scoped by BOTH lead URL and selected chat ID."""
    embedded = payload.get("_embedded", {})
    items = embedded.get("items")
    if not isinstance(items, list) or len(items) > 2000:
        raise CRMError("Получен ответ ленты сделки, но список событий не распознан")
    chat = embedded.get("chats", {}).get(job["chat_id"], {})
    contacts = chat.get("contacts", {})
    contact_ids = {str(c.get("id")) for c in contacts.values() if isinstance(c, dict)} if isinstance(contacts, dict) else set()
    users = {str(u.get("amojo_id")): u for u in job["users"] if u.get("amojo_id")}
    result = []
    for event in items:
        data = event.get("data") or {}
        if data.get("chat_id") != job["chat_id"] or not isinstance(data.get("message"), dict):
            continue
        # The lead timeline contains chat events attached to the linked CONTACT.
        entity_id, entity_type = str(event.get("element_id", "")), event.get("element_type")
        if event.get("element_id") is not None and not ((entity_type == 1 and entity_id in contact_ids)
                                                       or (entity_type == 2 and entity_id == str(job["lead_id"]))):
            continue
        author = data.get("author") or {}
        sid = str(author.get("id", ""))
        if sid in users:
            sender = {"id": sid}
        elif isinstance(contacts, dict) and sid in contacts:
            sender = {"id": sid, "client_id": "crm-contact-" + str(contacts[sid].get("id", sid))}
        else:
            raise CRMError("В ленте сделки не сопоставлен автор сообщения. История не импортирована")
        message = dict(data["message"])
        message["id"] = data.get("id") or event.get("id")
        result.append({"sender": sender, "timestamp": data.get("msec_created_at") or data.get("created_at") or event.get("date_create"),
                       "message": message})
    return result


def message_batch(payload):
    if isinstance(payload, dict):
        items = payload.get("messages")
        if items is None:
            items = payload.get("_embedded", {}).get("messages")
        if items is None:
            items = payload.get("response", {}).get("messages")
    else:
        items = None
    if not isinstance(items, list) or len(items) > 2000:
        raise CRMError("Ответ истории получен, но формат списка сообщений пока не поддерживается")
    return items


def normalize_message(item, users):
    if not isinstance(item, dict):
        raise CRMError("Неожиданный формат сообщения")
    message = item.get("message", item)
    if not isinstance(message, dict):
        raise CRMError("Неожиданный формат текста сообщения")
    sender = item.get("sender", message.get("sender", {}))
    if not isinstance(message, dict) or not isinstance(sender, dict):
        raise CRMError("Не удалось определить отправителя сообщения")
    mid = message.get("id") or item.get("id")
    timestamp = item.get("timestamp") or item.get("created_at") or message.get("created_at")
    if isinstance(mid, bool) or not isinstance(mid, (str, int)) or not timestamp:
        raise CRMError("Для сообщения отсутствуют ID или время; импорт остановлен")
    try:
        stamp = float(timestamp)
        if stamp > 10**12:
            stamp /= 1000
        moment = datetime.fromtimestamp(stamp, MOSCOW)
    except (ValueError, TypeError, OverflowError, OSError):
        raise CRMError("Не удалось прочитать дату сообщения") from None
    # Names, direction guesses and CRM assignee are insufficient evidence of authorship.
    sid = str(sender.get("id", ""))
    internal = {str(u["amojo_id"]): u for u in users if u.get("amojo_id")}
    if sid in internal:
        role, author = "manager", internal[sid]["name"]
    elif sender.get("client_id"):
        role, author = "client", "Клиент"
    else:
        raise CRMError("Автор не сопоставлен: нужен ID сотрудника в сервисе чатов или client_id клиента. Переподключите CRM с доступом к пользователям")
    body = message.get("text")
    if message.get("type", "text") != "text" or not isinstance(body, str) or not body.strip():
        raise CRMError("В истории есть вложение или неподдерживаемое сообщение. Полный текстовый импорт пока невозможен")
    if len(body) > 10000:
        raise CRMError("Сообщение слишком большое для текущего формата проверки")
    return {"id": str(mid), "role": role, "author": author, "text": body,
            "time": moment.strftime("%d.%m.%Y %H:%M:%S"), "timestamp": stamp}


def captured_dialogue(job, records, scope="company", complete=False):
    messages = sorted(records.values(), key=lambda m: (m["timestamp"], m["id"]))
    if len(messages) < 2 or {m["role"] for m in messages} != {"client", "manager"}:
        raise CRMError("Пока нет сообщений обеих сторон. Откройте переписку Telegram в окне amoCRM и загрузите её историю")
    if len(messages) > 300:
        raise CRMError("Загружено больше 300 сообщений. Нужен выбор диапазона, импорт остановлен")
    source = {"type": "amocrm_browser_chat", "domain": job["domain"], "lead_id": job["lead_id"],
              "chat_id": job["chat_id"], "url": job["url"], "capture": "authenticated_browser_responses", "card_verified": False}
    d = {"title": (job["lead_name"] or f"Сделка #{job['lead_id']}")[:150] + " · чат / браузер",
         "scope": scope, "date": datetime.fromtimestamp(messages[0]["timestamp"], MOSCOW).date().isoformat(),
         "complete": complete, "messages": [{k: v for k, v in m.items() if k != "timestamp"} for m in messages],
         "context": "Источник: " + job["url"] + "\nПолучены ответы истории в авторизованном браузере amoCRM. "
                    + ("Проверяющий подтвердил полноту загруженной истории. " if complete else "Полнота истории не подтверждена; могла загрузиться только часть. ")
                    + "Согласования и результат в карточке не проверены. Ответственный сделки не использован как доказательство авторства. "
                    + "Для многодневного диалога проверьте применимость правил на даты отдельных сообщений.",
         "source": source}
    d["id"] = f"CHAT-{digest(d)[:24]}"
    return validate_dialogue(d)


class CRMBrowser:
    def __init__(self, profile_dir):
        self.profile_dir = Path(profile_dir)
        self.lock = threading.RLock()
        self.commands = queue.Queue()
        self.thread = None
        self.domain = None
        self.active_job = None
        self.jobs = {}
        self.phase = "closed"
        self.error = ""

    def status(self):
        with self.lock:
            return {"phase": self.phase, "error": self.error, "domain": self.domain,
                    "jobs": [{"id": key, **{k: job[k] for k in ("lead_id", "lead_name", "chat_id", "error")},
                              "message_count": len(job["records"]), "revision": digest(job["records"]),
                              "history_responses": job.get("history_responses", 0),
                              "last_http_status": job.get("last_http_status"),
                              "stage": job.get("stage", "waiting_response")}
                             for key, job in self.jobs.items()]}

    def start(self, domain):
        domain = domain_name(domain)
        with self.lock:
            if self.thread and self.thread.is_alive():
                if domain != self.domain:
                    raise CRMError("Сначала закройте окно чтения другого аккаунта")
                return self.status()
            try:
                import playwright.sync_api  # noqa: F401
            except ImportError:
                raise CRMError("Для чтения из браузера установите: python -m pip install -r requirements-browser.txt") from None
            self.domain, self.phase, self.error = domain, "starting", ""
            self.jobs.clear()
            self.active_job = None
            self.commands = queue.Queue()
            self.thread = threading.Thread(target=self._run, daemon=True, name="crm-browser-reader")
            self.thread.start()
        return self.status()

    def read(self, lead, talks, users):
        with self.lock:
            if self.phase != "open":
                raise CRMError("Сначала откройте браузер amoCRM и войдите в аккаунт")
            if not talks:
                raise CRMError("У сделки нет доступных бесед")
            for talk in talks:
                chat_id = talk.get("chat_id")
                if not isinstance(chat_id, str) or not chat_id:
                    continue
                key = digest([self.domain, lead["id"], chat_id])[:24]
                self.jobs[key] = {"domain": self.domain, "lead_id": lead["id"], "lead_name": lead.get("name", ""),
                                  "chat_id": chat_id, "url": f"https://{self.domain}/leads/detail/{lead['id']}",
                                  "users": copy.deepcopy(users), "records": {}, "error": "", "history_responses": 0,
                                  "stage": "waiting_response", "last_http_status": None}
            self.commands.put(("read", lead["id"]))
        return self.status()

    def import_dialogue(self, job_id, revision, scope, complete):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or revision != digest(job["records"]):
                raise CRMError("История изменилась. Обновите сведения о загрузке и повторите импорт")
            if job["error"]:
                raise CRMError(job["error"])
            return captured_dialogue(job, copy.deepcopy(job["records"]), scope, complete)

    def preview(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise CRMError("Сначала загрузите историю выбранной сделки")
            return {"revision": digest(job["records"]), "dialogue": captured_dialogue(job, copy.deepcopy(job["records"])),
                    "error": job["error"]}

    def stop(self):
        self.commands.put(("stop", None))

    def _response(self, response):
        chat_id = response_chat_id(response.url)
        with self.lock:
            jobs = [j for j in self.jobs.values() if (chat_id and j["chat_id"] == chat_id)
                    or timeline_lead_id(response.url, j["domain"]) == j["lead_id"]]
        if not jobs:
            return  # Never retain unrelated account responses, credentials or headers.
        with self.lock:
            for job in jobs:
                job["history_responses"] = job.get("history_responses", 0) + 1
                job["last_http_status"] = response.status
                job["stage"] = "received_response"
        if response.status != 200:
            with self.lock:
                for job in jobs:
                    job["stage"] = "request_error"
                    job["error"] = f"Ответ истории: HTTP {response.status}. Проверьте вход и доступ к сделке"
            return
        try:
            raw = response.body()
            if len(raw) > 8_000_000:
                raise CRMError("Ответ истории превышает допустимый размер")
            import json
            payload = json.loads(raw)
            for job in jobs:
                is_timeline = timeline_lead_id(response.url, job["domain"]) is not None
                batch = timeline_batch(payload, job) if is_timeline else message_batch(payload)
                normalized = [normalize_message(m, job["users"]) for m in batch]
                with self.lock:
                    for m in normalized:
                        job["records"][m["id"]] = m
                    job["stage"] = "messages_loaded" if job["records"] else "empty_response"
                    # An earlier unsupported batch cannot silently become complete later.
        except Exception as exc:
            reason = str(exc) if isinstance(exc, CRMError) else "Ответ истории не удалось обработать; сообщения не импортированы"
            with self.lock:
                for job in jobs:
                    job["error"] = reason
                    job["stage"] = "parse_error"

    def _run(self):
        try:
            from playwright.sync_api import sync_playwright
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(str(self.profile_dir), channel="chrome", headless=False)
                context.on("response", self._response)
                page = context.pages[0] if context.pages else context.new_page()
                page.goto("https://" + self.domain, wait_until="domcontentloaded", timeout=60000)
                with self.lock:
                    self.phase = "open"
                while context.pages:
                    try:
                        action, value = self.commands.get_nowait()
                    except queue.Empty:
                        page.wait_for_timeout(200)
                        continue
                    if action == "stop":
                        break
                    if action == "read":
                        page.goto(f"https://{self.domain}/leads/detail/{value}", wait_until="domcontentloaded", timeout=60000)
                context.close()
        except Exception:
            with self.lock:
                self.error = "Окно чтения закрыто или не открылось. Нужны установленный Chrome и Playwright. Вход выполняется вами в отдельном окне; сообщения пока не проверены"
        finally:
            with self.lock:
                self.phase = "closed"
