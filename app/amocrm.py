"""Read-only amoCRM connector and explicit-role transcript extraction."""
import base64
import ctypes
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from core import digest, parse_transcript, validate_dialogue

MOSCOW = timezone(timedelta(hours=3))
_rate_lock = threading.Lock()
_last_request = 0.0


class CRMError(ValueError):
    pass


def domain_name(value):
    value = str(value).strip().lower()
    if not value.startswith("https://"):
        value = "https://" + value
    parts = urlsplit(value)
    if (parts.scheme != "https" or parts.username or parts.password or parts.port
            or parts.path not in ("", "/") or parts.query or parts.fragment
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.(?:amocrm\.ru|amocrm\.com|kommo\.com)", parts.netloc)):
        raise CRMError("Укажите адрес аккаунта, например ivanovnn.amocrm.ru, без пути и порта")
    return parts.netloc


def protect_token(token, decrypt=False):
    """Windows DPAPI: credentials stay on this computer, under this OS user."""
    if os.name != "nt":
        raise CRMError("Сохранение токена поддерживается в Windows. Используйте подключение на время запуска")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    raw = base64.b64decode(token) if decrypt else token.encode("utf-8")
    buf = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source, target = Blob(len(raw), buf), Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
    function.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise CRMError("Защита токена Windows недоступна в текущем профиле. Подключение можно использовать без сохранения")
    try:
        result = ctypes.string_at(target.data, target.size)
        return result.decode("utf-8") if decrypt else base64.b64encode(result).decode("ascii")
    finally:
        kernel.LocalFree(target.data)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CRMError("amoCRM перенаправляет запрос. Проверьте точный адрес аккаунта")


class AmoClient:
    def __init__(self, domain, token):
        self.domain = domain_name(domain)
        if not isinstance(token, str) or not token.strip() or any(c.isspace() for c in token.strip()):
            raise CRMError("Введите действующий токен доступа amoCRM")
        self.token = token.strip()
        self.opener = build_opener(NoRedirect())

    def get(self, path, params=None):
        global _last_request
        if not path.startswith("/api/v4/") or "?" in path or ".." in path:
            raise CRMError("Недопустимый метод amoCRM")
        url = "https://" + self.domain + path
        if params:
            url += "?" + urlencode(params)
        for attempt in range(3):
            with _rate_lock:
                wait = .26 - (time.monotonic() - _last_request)
                if wait > 0:
                    time.sleep(wait)
                _last_request = time.monotonic()
            request = Request(url, headers={"Authorization": "Bearer " + self.token,
                                           "Accept": "application/json"}, method="GET")
            try:
                with self.opener.open(request, timeout=20) as response:
                    raw = response.read(8_000_001)
                    if len(raw) > 8_000_000:
                        raise CRMError("Слишком большой ответ amoCRM")
                    data = json.loads(raw) if raw else {}
                    if not isinstance(data, dict):
                        raise CRMError("Неожиданный формат ответа amoCRM")
                    return data
            except HTTPError as exc:
                code = exc.code
                exc.close()
                if code == 429 and attempt < 2:
                    time.sleep(attempt + 1)
                    continue
                messages = {401: "Токен amoCRM недействителен или истёк. Подключите аккаунт заново",
                            403: "У интеграции недостаточно прав для чтения этих данных",
                            402: "amoCRM сообщает об ограничении тарифа аккаунта",
                            404: "Запись amoCRM не найдена или недоступна",
                            429: "Лимит запросов amoCRM. Повторите через несколько секунд"}
                raise CRMError(messages.get(code, f"amoCRM вернула HTTP {code}. Повторите загрузку")) from None
            except (URLError, TimeoutError, OSError):
                raise CRMError("Не удалось соединиться с amoCRM. Проверьте адрес аккаунта и интернет") from None
            except (json.JSONDecodeError, UnicodeError):
                raise CRMError("amoCRM вернула ответ, который не удалось прочитать") from None

    def collection(self, path, key, params=None, max_pages=20):
        result = []
        for page in range(1, max_pages + 1):
            data = self.get(path, {**(params or {}), "limit": 250, "page": page})
            batch = data.get("_embedded", {}).get(key, [])
            if not isinstance(batch, list):
                raise CRMError("Неожиданный список записей amoCRM")
            result.extend(batch)
            if not data.get("_links", {}).get("next"):
                return result
        raise CRMError("Слишком много страниц. Загрузка остановлена без импорта неполных данных")

    def metadata(self):
        warnings = []
        account = self.get("/api/v4/account")
        # Users may be hidden by account permissions; leads remain readable.
        try:
            users = self.collection("/api/v4/users", "users", {"with": "amojo_id"})
        except CRMError as exc:
            users = []
            warnings.append(str(exc))
        pipelines = self.collection("/api/v4/leads/pipelines", "pipelines")
        return {"account": {"id": account.get("id"), "name": account.get("name", self.domain)},
                "users": [{"id": u["id"], "name": u.get("name", str(u["id"])), "amojo_id": u.get("amojo_id")} for u in users],
                "pipelines": [{"id": p["id"], "name": p.get("name", ""),
                               "statuses": [{"id": s["id"], "name": s.get("name", "")} for s in p.get("_embedded", {}).get("statuses", [])]} for p in pipelines],
                "warnings": warnings}

    def leads(self, query="", page=1):
        page = int(page)
        if not 1 <= page <= 10000 or len(query) > 200:
            raise CRMError("Неверные параметры поиска")
        params = {"limit": 50, "page": page, "order[updated_at]": "desc"}
        if query:
            params["query"] = query
        data = self.get("/api/v4/leads", params)
        return {"leads": [{k: lead.get(k) for k in ("id", "name", "responsible_user_id", "pipeline_id", "status_id", "updated_at", "created_at", "closed_at")}
                          for lead in data.get("_embedded", {}).get("leads", [])],
                "has_more": bool(data.get("_links", {}).get("next")), "page": page}

    def dialogues(self, lead_id, metadata):
        lead_id = positive_id(lead_id)
        lead = self.get(f"/api/v4/leads/{lead_id}")
        notes = self.collection(f"/api/v4/leads/{lead_id}/notes", "notes", {"filter[note_type]": "common"})
        candidates, other_notes = [], []
        for note in notes:
            candidate = note_dialogue(note, lead, self.domain, metadata)
            if candidate:
                candidates.append(candidate)
            else:
                body = note.get("params", {}).get("text", "")
                if body:
                    other_notes.append({"id": note["id"], "text": body[:1500], "truncated": len(body) > 1500})
        warnings = []
        try:
            talks = self.collection("/api/v4/talks", "talks", {"filter[entity_type]": "lead", "filter[entity_id]": lead_id})
            if talks:
                warnings.append(f"В сделке есть беседы: {len(talks)}. Их сообщения не входят в примечания; для истории чатов нужен отдельный доступ канала/провайдера")
        except CRMError as exc:
            warnings.append("Проверка доступности бесед: " + str(exc))
        if not candidates:
            warnings.append("В примечаниях не найдена переписка с явными ролями клиента и менеджера. Обычные заметки не выдаются за диалог")
        return {"lead_id": lead_id, "lead_name": lead.get("name", ""), "candidates": candidates,
                "other_notes": other_notes, "warnings": warnings}


def positive_id(value):
    if isinstance(value, bool) or not str(value).isdigit() or not 0 < int(value) < 10**15:
        raise CRMError("Неверный ID сделки/примечания")
    return int(value)


def note_dialogue(note, lead, domain, metadata):
    body = note.get("params", {}).get("text", "")
    if note.get("note_type") != "common" or not isinstance(body, str):
        return None
    matches = re.findall(r"(?m)^-\s+(M\d+),\s*(\d{2}:\d{2}),\s*([^:\n]+):\s*«(.+)»\.?\s*$", body)
    messages = []
    if matches:
        for mid, clock, author, message in matches:
            author = author.strip()
            if author.casefold() == "клиент":
                role = "client"
            elif re.fullmatch(r"[СC]\d+", author) or author.casefold().startswith("менеджер"):
                role = "manager"
            else:
                return None  # Ambiguous authorship must not produce employee findings.
            messages.append({"id": mid, "time": clock, "role": role, "author": author, "text": message})
        expected_lines = re.findall(r"(?m)^-\s+M\d+[,]", body)
        if len(expected_lines) != len(matches):
            return None
    else:
        try:
            messages = parse_transcript(body)
        except ValueError:
            return None
    if {m["role"] for m in messages} != {"client", "manager"}:
        return None
    users = {u["id"]: u["name"] for u in metadata.get("users", [])}
    manager = users.get(lead.get("responsible_user_id"), f"Сотрудник #{lead.get('responsible_user_id', '?')}")
    demo = "УЧЕБНЫЙ СЦЕНАРИЙ. Вымышленные условия и сообщения" in body
    # Only this explicit mapping in the author's upload script permits C01 -> assignee.
    if demo and "Сотрудник С01 в сценарии соответствует ответственному сделки" in body:
        for message in messages:
            if message["role"] == "manager" and message["author"] == "С01":
                message["author"] = manager + " (С01 в учебном сценарии)"
    date_match = re.search(r"Дата(?: видимого фрагмента)?:\s*(\d{2})\.(\d{2})\.(\d{4})", body)
    created_at = note.get("created_at") or lead.get("created_at")
    if date_match:
        day, month, year = date_match.groups()
        day_string = f"{year}-{month}-{day}"
    elif created_at:
        day_string = datetime.fromtimestamp(int(created_at), MOSCOW).date().isoformat()
    else:
        return None
    complete = bool(re.search(r"История полная", body, re.I))
    source = {"type": "amocrm_note", "domain": domain, "lead_id": positive_id(lead["id"]),
              "note_id": positive_id(note["id"]), "note_updated_at": note.get("updated_at"),
              "url": f"https://{domain}/leads/detail/{lead['id']}"}
    context_part = body.split("- M", 1)[0].strip() if matches else "Полнота истории и результат обращения не подтверждены. Источник — примечание CRM, не выгрузка чата."
    # CRM assignee is context, never evidence that they authored arbitrary messages.
    context = (f"Источник: {source['url']}; примечание #{note['id']}.\n"
               f"Ответственный сделки: {manager}; pipeline_id={lead.get('pipeline_id')}; status_id={lead.get('status_id')}.\n"
               "Ответственный сделки может отличаться от автора сообщения.\n" + context_part)
    d = {"title": str(lead.get("name") or f"Сделка #{lead['id']}")[:160] + f" · примечание {note['id']}",
         "scope": "demo" if demo else "company", "date": day_string, "complete": complete,
         "context": context, "messages": messages, "source": source}
    # Immutable revisions preserve the evidence behind already saved results.
    d["id"] = f"AMO-{digest(domain)[:8]}-{lead['id']}-{note['id']}-{digest(d)[:12]}"
    try:
        return validate_dialogue(d)
    except (ValueError, KeyError, TypeError):
        return None
