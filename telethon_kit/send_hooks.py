"""Проверки перед отправкой в Telegram.

pre_send_check(...) -> (ok, warnings). ok=False — НЕ отправлять.
Ловит: запрещённых получателей, несовпадение ID и имени (по team_config.json, если он есть),
локальные пути в тексте, сырой markdown, служебные префиксы («Вот сообщение:»).

Свои данные — только в team_config.json (в .gitignore, образец team_config.example.json):
  team        {ключ: {full_name, telegram_id}} — сверка «id ↔ человек»
  aliases     {"короткое имя": id} — получатели для send_to.py и read_from.py
  never_send  {"id": "почему"} — кому писать нельзя никогда
"""
import json
import logging
import os
import re

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "team_config.json")  # необязательный, см. выше

log = logging.getLogger("send_hooks")

_CONTACTS_CACHE = None



def _config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("send_hooks: team_config.json не прочитан: %s", e)
        return {}


def _ids(section):
    """{ключ: значение} раздела team_config.json с id числами; кривые строки пропускаются."""
    out = {}
    for k, v in (_config().get(section) or {}).items():
        try:
            out.update({int(k): str(v)} if section == "never_send" else {str(k).strip().lower(): int(v)})
        except (TypeError, ValueError):
            continue
    return out


# Кому писать нельзя никогда: {telegram_id: "почему"} — из team_config.json, раздел never_send.
NEVER_SEND_TELEGRAM_IDS = _ids("never_send")
# Свои люди и группы: {"имя": id} — из team_config.json, раздел aliases.
ALIASES = _ids("aliases")

_MARKDOWN_PATTERNS = [
    (r'^#{1,3}\s', "Markdown заголовок (## ...)"),
    (r'```', "Блок кода (```)"),
    (r'\|.*\|.*\|', "Markdown таблица (|...|...|)"),
    (r'^---+$', "Горизонтальная линия (---)"),
    (r'\*\*\*', "Triple asterisks (***)"),
]

_WRAPPER_PREFIXES = [
    "Here's the message:", "Here is the message:", "Вот сообщение:", "Вот текст:", "Вот драфт:",
    "Here's the draft:", "Message:", "Draft:", "Сообщение:", "Текст сообщения:", "Done:", "Готово:",
]

_LOCAL_PATH_RE = re.compile(r'(/Users/\w+/|/home/\w+/|[A-Z]:\\Users\\)')


def _load_contacts():
    global _CONTACTS_CACHE
    if _CONTACTS_CACHE is not None:
        return
    _CONTACTS_CACHE = {}
    for member in (_config().get("team") or {}).values():
        try:
            if member.get("telegram_id") and member.get("full_name"):
                _CONTACTS_CACHE[int(member["telegram_id"])] = member["full_name"]
        except (AttributeError, TypeError, ValueError):
            continue


def resolve_name_by_id(platform, recipient_id):
    """Имя по ID из team_config.json или None."""
    _load_contacts()
    if platform != "telegram":
        return None
    try:
        return _CONTACTS_CACHE.get(int(recipient_id))
    except (ValueError, TypeError):
        return None


def pre_send_check(platform, recipient_id, recipient_name, text):
    """Вернуть (ok, warnings). ok=False — не отправлять; ok=True с warnings — отправить, но глянуть."""
    warnings = []

    if platform == "telegram" and recipient_id:
        try:
            forbidden_reason = NEVER_SEND_TELEGRAM_IDS.get(int(recipient_id))
        except (TypeError, ValueError):
            forbidden_reason = None
        if forbidden_reason:
            return False, [f"FORBIDDEN RECIPIENT {recipient_id}: {forbidden_reason}"]

        known_name = resolve_name_by_id("telegram", recipient_id)
        if known_name and recipient_name:
            known_lower = known_name.lower().split()[0]
            given_lower = recipient_name.lower().split()[0]
            if known_lower != given_lower and given_lower not in known_name.lower():
                warnings.append(f"RECIPIENT MISMATCH: ID {recipient_id} = '{known_name}', "
                                f"but you specified '{recipient_name}'")

    if _LOCAL_PATH_RE.search(text):
        warnings.append(f"LOCAL PATHS in message: {_LOCAL_PATH_RE.findall(text)}")

    if platform == "telegram":
        for pattern, desc in _MARKDOWN_PATTERNS:
            if re.search(pattern, text, re.MULTILINE):
                warnings.append(f"RAW MARKDOWN: {desc}")

    text_stripped = text.strip()
    for prefix in _WRAPPER_PREFIXES:
        if text_stripped.startswith(prefix):
            warnings.append(f"WRAPPER PREFIX: starts with '{prefix}'")
            break

    ok = not any("MISMATCH" in w for w in warnings)
    return ok, warnings


def clean_wrapper_text(text):
    """Срезать служебный префикс («Вот сообщение:») в начале текста."""
    text = text.strip()
    for prefix in _WRAPPER_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    return text


def format_send_confirmation(platform, recipient_id, text):
    """Строка «Отправляю -> Имя (id): начало текста…» для показа перед отправкой."""
    name = resolve_name_by_id(platform, recipient_id)
    display_name = f"{name} ({recipient_id})" if name else str(recipient_id)
    preview = text[:100] + "..." if len(text) > 100 else text
    return f"Отправляю -> {display_name} в {platform}: {preview}"
