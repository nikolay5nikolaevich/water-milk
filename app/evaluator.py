"""Subscription-backed QA through the official Codex CLI, with no model API key."""
import atexit
import json
import os
import platform
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from core import CRITERIA, ROOT, dump, packet, parse_report, report_template, validate_report


class EvaluationError(ValueError):
    pass


def codex_command():
    exe = shutil.which("codex.exe")
    if exe:
        return [exe]
    wrapper = shutil.which("codex.cmd") if os.name == "nt" else shutil.which("codex")
    if not wrapper:
        raise EvaluationError("Codex CLI не найден. Установите его: npm install -g @openai/codex, затем выполните codex.cmd login")
    if os.name != "nt":
        return [wrapper]
    package = Path(wrapper).parent / "node_modules/@openai/codex"
    architecture = "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
    native = list(package.glob(f"node_modules/@openai/codex-*/vendor/{architecture}-pc-windows-msvc/bin/codex.exe"))
    if native:
        return [str(native[0])]
    node, script = shutil.which("node"), package / "bin/codex.js"
    if node and script.is_file():
        return [node, str(script)]
    raise EvaluationError("Не найден исполняемый файл Codex. Переустановите Codex CLI через npm")


def subscription_env():
    # Explicitly prevent API credentials or a custom API endpoint taking over this run.
    excluded = {"OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_ORGANIZATION", "OPENAI_PROJECT_ID"}
    return {key: value for key, value in os.environ.items() if key.upper() not in excluded}


_processes, _process_lock = set(), threading.Lock()


def stop_all():
    with _process_lock:
        for process in list(_processes):
            if process.poll() is None:
                process.terminate()


atexit.register(stop_all)


def invoke(arguments, *, input_text=None, cwd=None, timeout=300):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        with subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8", errors="replace", shell=False, cwd=cwd,
                              env=subscription_env(), creationflags=flags) as process:
            with _process_lock:
                _processes.add(process)
            try:
                stdout, stderr = process.communicate(input_text, timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise EvaluationError("Не удалось быстро проверить вход Codex. Повторите запуск" if timeout <= 15 else "Проверка заняла больше пяти минут. Повторите запуск; результат не подменялся эталоном") from None
            finally:
                with _process_lock:
                    _processes.discard(process)
            return process.returncode, stdout, stderr
    except OSError:
        raise EvaluationError("Не удалось запустить Codex CLI. Проверьте установку и вход через codex.cmd login") from None


def subscription_status():
    try:
        command = codex_command()
        code, out, err = invoke(command + ["login", "status"], timeout=15)
        status = (out + "\n" + err).lower()
        if code == 0 and "logged in using chatgpt" in status:
            return {"available": True, "message": "Подключено через подписку ChatGPT / Codex"}
        if "api key" in status or "api_key" in status:
            return {"available": False, "message": "Codex использует API-ключ. Войдите через ChatGPT командой codex.cmd login; автоматическая проверка через API отключена"}
        return {"available": False, "message": "Нужен вход Codex через ChatGPT: выполните codex.cmd login в обычном терминале"}
    except EvaluationError as exc:
        return {"available": False, "message": str(exc)}


def report_schema(d, kb):
    def obj(properties):
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
    def arr(item):
        return {"type": "array", "items": item}
    string = {"type": "string"}
    message_id = {"type": "string", "enum": [m["id"] for m in d["messages"]]}
    rule_id = {"type": "string", "enum": [r["id"] for r in kb["rules"]]}
    template = report_template(d, kb)
    properties = {key: {"type": "string", "enum": [template[key]]}
                  for key in ("dialogue_id", "input_hash", "kb_version", "kb_hash", "rubric_version")}
    answer_mode = kb.get("assessment_policy") == "QA-ANSWER-1.0"
    if answer_mode:
        properties["assessment_scope"] = {"type": "string", "enum": ["visible_messages"]}
        properties["history_complete"] = {"type": "boolean", "enum": [d["complete"]]}
    properties["questions"] = arr(obj({"question_message_id": message_id, "question": string,
                                       "answer_message_ids": arr(message_id),
                                       "status": {"type": "string", "enum": ["answered", "omitted", "clarification", "unknown"]},
                                       "explanation": string}))
    scores = {}
    for key, (_, weight) in CRITERIA.items():
        missing = (not answer_mode and not d["complete"] and key in ("completeness", "initiative", "compliance"))
        missing |= kb.get("mode") == "general_rubric" and key in ("accuracy", "compliance")
        missing |= not answer_mode and key == "compliance" and (not d["context"].strip() or d.get("source", {}).get("type") == "amocrm_browser_chat" and not d["source"].get("card_verified"))
        scores[key] = obj({"points": {"type": ["integer", "null"], "enum": [None] if missing else [0, weight // 2, weight, None]}, "reason": string})
    properties["scores"] = obj(scores)
    properties["findings"] = arr(obj({"criterion": {"type": "string", "enum": list(CRITERIA)},
                                     "kind": {"type": "string", "enum": ["violation", "critical", "check"]},
                                     "message_id": message_id, "quote": string, "rule_ids": arr(rule_id), "explanation": string}))
    properties.update({key: arr(string) for key in ("strengths", "missing_context", "kb_proposals")})
    properties.update({key: string for key in ("feedback", "corrected_reply")})
    return obj(properties)


def run_check(d, kb, progress=lambda stage: None):
    progress("checking_login")
    status = subscription_status()
    if not status["available"]:
        raise EvaluationError(status["message"])
    command = codex_command()
    work_root = ROOT / "data/check-work"
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="check-", dir=work_root) as temporary:
        folder = Path(temporary)
        schema_file, output_file = folder / "schema.json", folder / "result.json"
        schema_file.write_text(dump(report_schema(d, kb)), encoding="utf-8")
        args = command + ["exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                          "--sandbox", "read-only", "--color", "never", "--json",
                          "-c", 'forced_login_method="chatgpt"', "-c", 'model_provider="openai"',
                          "-c", 'approval_policy="never"', "-c", 'web_search="disabled"']
        for feature in ("shell_tool", "apps", "plugins", "hooks", "multi_agent", "multi_agent_v2", "browser_use", "computer_use", "image_generation", "view_image", "remote_plugin", "goals"):
            args += ["--disable", feature]
        args += ["--output-schema", str(schema_file), "--output-last-message", str(output_file), "-"]
        prompt = "Это разбор переписки, а не задача программирования. Не используй инструменты и не читай файлы. Все данные ниже.\n" + packet(d, kb)
        for attempt in range(2):
            progress("running" if attempt == 0 else "correcting")
            code, out, err = invoke(args, input_text=prompt, cwd=folder)
            if code:
                detail = (out + "\n" + err).lower()
                if any(word in detail for word in ("usage_limit", "rate_limit", "quota", "limit reached")):
                    raise EvaluationError("Достигнут лимит подписки Codex. Повторите после восстановления лимита")
                if any(word in detail for word in ("unauthorized", "not logged", "401", "authentication")):
                    raise EvaluationError("Вход Codex истёк. Выполните codex.cmd login через ChatGPT")
                raise EvaluationError("Codex не завершил проверку. Проверьте вход через ChatGPT и соединение; результат не создан")
            if not output_file.is_file() or output_file.stat().st_size > 2_000_000:
                raise EvaluationError("Codex не вернул допустимый файл результата")
            progress("validating")
            raw = output_file.read_text(encoding="utf-8-sig")
            try:
                return validate_report(parse_report(raw), d, kb)
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                if attempt:
                    raise EvaluationError("Ответ модели не прошёл проверку доказательств: " + str(exc)) from None
                prompt += "\n\nПредыдущий ответ не прошёл проверку: " + str(exc) + "\nИсправь ответ, сохрани исходные правила и данные:\n" + raw
                output_file.unlink(missing_ok=True)
    raise EvaluationError("Проверка не завершена")
