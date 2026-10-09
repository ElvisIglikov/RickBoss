# Рик — паспорт для разработчиков

Рик — главный чат над проектами хозяина на голом Claude Code: характер в `CLAUDE.md`, форма ответа — стиль
`.claude/output-styles/rick.md` (его скелет `mem_hook.py` берёт оттуда же), скиллы в `.claude/skills/`, память и
почта — хуками, чаты проектов — tmux. Как пользоваться — `README.md`.

## Устройство

- `tools/rick_config.py` — общий модуль: `home()` (корень — `$RICK_HOME` или папка над `tools/`) и `load()`
  (`config.json` поверх умолчаний; без файла — умолчания). Новые настройки — только сюда и в `config.example.json`.
- `tools/rick_config.py status` — что подключено, словами, без значений ключей; `tools/remind.py` — напоминание
  в «Напоминания» Мака на точное время.
- `tools/mem_hook.py` (`start | prompt | batch | stop`) и `tools/rick_bus.py wait` — хуки из `.claude/settings.json`;
  хук никогда не ломает ход, ошибки — в `state/hook-errors.log`.
- `tools/rick_bus.py` — почта (`tell`, `note`, `claim`, `state`, `pending`, `wait`); `tools/mem_guard.py` — размеры
  памяти.
- `tools/term.py` — чаты проектов в tmux (`up`, `send`, `read`, `stop`, `list`, `watch`, `projects`);
  `docs/terminals.md`; чату проекта — памятка `docs/project-chat.md` и форма `docs/status-format.md`.
- `tools/tasks.py` — задачи #N (`memory/tasks.jsonl`); `tools/notify.py` — баннер и звук Мака; `tools/digest.py` —
  «что у нас» по status.md проектов (`--html` — по `templates/digest.html`); `tools/statusline.py` — строка
  состояния (`statusLine` в `.claude/settings.json`).
- `.claude/agents/critic.md`, `checker.md` — субагенты workflow; скилл `revizor` — ревизия проекта.
- `voice/`, `live/`, `telethon_kit/`, `tools/bot_send.py` — по желанию, без ключей выключены: `docs/voice-live.md`,
  `docs/telegram.md`.
- Память пользователя (`memory/*.md`, `memory/keywords.json`, `memory/tasks.jsonl`, `memory/projects/*.md`,
  `journal/*.md`, `archive/`) и рабочее (`state/`, `inbox/`, `live/log/`, `voice/cache/`) — в `.gitignore`; в git только шаблоны
  (`memory/*.example.md`, `install.sh` копирует их в рабочие).

## Правила

- Только macOS: tmux, `say`, `afplay`. Python 3.9+, только стандартная библиотека (кроме `telethon_kit/`).
- Никаких абсолютных путей: корень — `rick_config.home()`, хуки — `python3 "$CLAUDE_PROJECT_DIR/tools/…"`.
- Никаких личных данных: ни имён, ни телефонов, ни ключей, ни путей `/Users/<кто-то>`. Хозяин в текстах —
  «хозяин» или «ты», в коде — `load()["owner"]`. Тестовые данные — выдуманные.
- Ключи — только в `.env` (в `.gitignore`), никогда в выводе, журнале и коммитах.
- Внешнее (сообщения, публикация, деньги) — только по «да» хозяина; новый внешний канал ставь за тем же правилом.
- Правка — минимальная и точечная: без «заодно», новых слоёв и флагов.

## Проверки перед коммитом

```bash
python3 -m unittest discover -s tools/tests
python3 -m unittest discover -s live/tests
python3 tools/check_public.py
```

`check_public.py` ищет ключи, телефоны, почту, IP, чужие пути и суммы; выход 1 — есть находки или не
проверено ни одного файла («Проверено файлов: 0» — не «чисто»), выход 2 — `--deny` без файла. Законное совпадение
помечай в конце строки словом `public-ok`.
