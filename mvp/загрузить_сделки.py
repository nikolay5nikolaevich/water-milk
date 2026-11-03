"""Загрузка учебных сценариев в amoCRM. Python 3, без зависимостей."""
import argparse
import getpass
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = 'https://ivanovnn.amocrm.ru'
SOURCE = Path(__file__).with_name('03_ТЕСТОВЫЕ_ДИАЛОГИ.md')
MANAGERS = ['менеджер алена', 'менеджер олег', 'менеджер мадина',
            'менеджер алена', 'менеджер олег']


def request(method, path, payload=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={'Authorization': 'Bearer ' + TOKEN,
                                          'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'amoCRM вернула HTTP {exc.code}: {exc.read().decode("utf-8", "replace")[:500]}') from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f'Ошибка соединения: {exc.reason}') from exc


def fetch_all(path, key):
    items = []
    for page in range(1, 1001):
        separator = '&' if '?' in path else '?'
        result = request('GET', f'{path}{separator}limit=250&page={page}')
        batch = result.get('_embedded', {}).get(key, [])
        items.extend(batch)
        if not result.get('_links', {}).get('next'):
            return items
    raise RuntimeError('Слишком много страниц; загрузка остановлена.')


def select(items, description):
    if not items:
        raise RuntimeError(f'Не найдено: {description}')
    print('\n' + description + ':')
    for i, item in enumerate(items, 1):
        print(f'{i}. {item["name"]} (ID {item["id"]})')
    try:
        index = int(input('Введите номер: ')) - 1
        if index < 0:
            raise ValueError
        return items[index]
    except (ValueError, IndexError):
        raise RuntimeError('Неверный номер.')


def main():
    global TOKEN
    parser = argparse.ArgumentParser(description='Учебные сделки D01–D05 для ivanovnn.amocrm.ru')
    parser.add_argument('--apply', action='store_true', help='создать сделки; без флага только просмотр')
    args = parser.parse_args()
    TOKEN = getpass.getpass('Вставьте действующий OAuth access token amoCRM (ввод скрыт): ').strip()
    if not TOKEN:
        raise RuntimeError('Токен не указан.')

    source = SOURCE.read_text(encoding='utf-8')
    chunks = re.split(r'(?m)^## (D0[1-5]) — ', source)
    if len(chunks) != 11:
        raise RuntimeError('Не удалось прочитать все пять сценариев.')
    cases = [(chunks[i], chunks[i + 1].strip()) for i in range(1, len(chunks), 2)]
    users = fetch_all('/api/v4/users', 'users')
    assigned = []
    for name in MANAGERS:
        matches = [u for u in users if u.get('name', '').casefold().replace('ё', 'е') == name]
        if len(matches) != 1:
            raise RuntimeError(f'Не удалось однозначно найти пользователя «{name}». Проверьте имя и доступ к списку пользователей.')
        assigned.append(matches[0])
    pipeline = select(fetch_all('/api/v4/leads/pipelines', 'pipelines'), 'Выберите ТЕСТОВУЮ воронку')
    statuses = fetch_all(f'/api/v4/leads/pipelines/{pipeline["id"]}/statuses', 'statuses')
    active = [s for s in statuses if s.get('id') not in (142, 143) and s.get('name', '').casefold() != 'неразобранное']
    status = select(active, 'Выберите начальный этап для всех пяти сделок')
    print('\nПредварительный просмотр:')
    plans = []
    for (case_id, body), manager in zip(cases, assigned):
        name = f'Учебный тест {case_id} — {body.splitlines()[0]}'
        existing = fetch_all('/api/v4/leads?query=' + urllib.parse.quote(name), 'leads')
        exact = [lead for lead in existing if lead.get('name') == name and lead.get('pipeline_id') == pipeline['id']]
        if len(exact) > 1:
            raise RuntimeError(f'Найдено несколько совпадений для {name}; проверьте их вручную.')
        print(f'{name} → {manager["name"]}; ' + ('уже существует, пропуск' if exact else 'будет создана'))
        plans.append((case_id, body, manager, name, exact))
    if not args.apply:
        print('\nРежим просмотра: ничего не создано. Для загрузки повторите запуск с --apply.')
        return
    if input('\nСоздать перечисленные сделки? Введите ДА: ').strip().upper() != 'ДА':
        print('Отменено.')
        return
    for case_id, body, manager, name, existing in plans:
        if existing:
            print(f'{case_id}: пропущена, ID {existing[0]["id"]}; проверьте наличие примечания вручную.')
            continue
        result = request('POST', '/api/v4/leads', [{'name': name, 'pipeline_id': pipeline['id'],
                           'status_id': status['id'], 'responsible_user_id': manager['id']}])
        lead_id = result['_embedded']['leads'][0]['id']
        print(f'{case_id}: сделка создана, ID {lead_id}', flush=True)
        note = ('УЧЕБНЫЙ СЦЕНАРИЙ. Вымышленные условия и сообщения; это не реальная переписка.\n'
                'Сотрудник С01 в сценарии соответствует ответственному сделки.\n'
                'Описанный в сценарии этап относится к учебному контексту, текущий этап сделки выбран при загрузке.\n\n'
                + case_id + ' — ' + body)
        try:
            request('POST', f'/api/v4/leads/{lead_id}/notes', [{'note_type': 'common', 'params': {'text': note}}])
            print(f'  Примечание добавлено: {BASE}/leads/detail/{lead_id}')
        except RuntimeError as exc:
            print(f'  ОШИБКА примечания: {exc}. Добавьте сценарий {case_id} вручную в сделку {lead_id}.', file=sys.stderr)
            raise


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, KeyError, OSError) as error:
        print(f'Ошибка: {error}', file=sys.stderr)
        sys.exit(1)
