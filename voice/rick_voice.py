#!/usr/bin/env python3
"""Голос Рика: озвучка ответов. По умолчанию — системный `say` macOS, по желанию — ElevenLabs.

Команды:
  say "текст"      — озвучить (текст можно подать и на stdin)
  stop             — сразу замолчать (громкость системы — как была до речи)
  repeat           — повторить последнюю озвучку ElevenLabs
  louder / quieter — речь громче или тише (и сразу повторить)
  duck-more / duck-less — музыка глушится сильнее или слабее (и сразу повторить)
  bank             — личности и голоса, стрелкой отмечена включённая
  use <профиль>    — включить личность из profiles.json (манера ответа и, если сохранён, её голос)
  next             — следующая личность по кругу
  — только с ключом ElevenLabs —
  check            — подписка и сколько символов осталось
  design [профиль] — 3 пробных голоса по описанию личности
  save N "Имя" [профиль] — сохранить пробный голос N в аккаунт и включить его
  library [запрос] — поискать голос в общей библиотеке ElevenLabs
  voices           — голоса в аккаунте

Движок — `voice.engine` в config.json Рика: "say" (по умолчанию) или "elevenlabs".
ElevenLabs: ключ ELEVENLABS_API_KEY в voice/.env, настройки — voice/config.json (образец config.example.json).
Нет ключа, голоса или сети — говорит системный say (`voice.say_voice`, `voice.say_rate`), ответ звучит всё равно.

Живой разговор (live/): пока стоит флаг voice/cache/.live_front, say не говорит сам, а дописывает строку
в voice/otvety/spisok.txt — её зачитывает голосовая страница (иначе ответ звучит дважды).
"""

import base64
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

CODE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(CODE), "tools"))
import rick_config  # noqa: E402

DATA = os.path.join(rick_config.home(), "voice")
ENV_FILE = os.path.join(DATA, ".env")
CONFIG_FILE = os.path.join(DATA, "config.json")
CACHE_DIR = os.path.join(DATA, "cache")
SAY_PID = os.path.join(CACHE_DIR, "say.pid")
PREVIEW_DIR = os.path.join(CACHE_DIR, "previews")
ANSWERS_DIR = os.path.join(DATA, "otvety")
LIVE_FLAG = os.path.join(CACHE_DIR, ".live_front")
PROFILES_FILE = os.path.join(CODE, "profiles.json")
PLAY = os.path.join(CODE, "play.sh")
API = "https://api.elevenlabs.io/v1"


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_profiles():
    return read_json(PROFILES_FILE)


def load_key():
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if key or not os.path.exists(ENV_FILE):
        return key
    with open(ENV_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("ELEVENLABS_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def load_config():
    cfg = {
        "voice_id": "",
        "voice_name": "",
        "model_id": "eleven_multilingual_v2",
        "language_code": "ru",
        "output_format": "mp3_44100_128",
        "voice_settings": {"stability": 0.35, "similarity_boost": 0.75, "style": 0.55,
                           "use_speaker_boost": True, "speed": 0.9},
    }
    if os.path.exists(CONFIG_FILE):
        cfg.update(read_json(CONFIG_FILE))
    return cfg


def save_config(cfg):
    os.makedirs(DATA, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


def request(path, key, method="GET", body=None, raw=False):
    url = path if path.startswith("http") else API + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"xi-api-key": key}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=90) as resp:
        payload = resp.read()
    return payload if raw else json.loads(payload.decode("utf-8"))


def clean(text):
    """Убрать разметку и значки — вслух они звучат мусором."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"(?:https?://|www\.)\S+", " ", text)
    text = re.sub(r"\[?#\d+\]?", " ", text)  # номера задач: #1234, [#1234]
    # Пути и файлы: /abs, ~/x, ./x, voice/cache, rick_voice.py — «слэш», «точка пай» вслух не нужны.
    text = re.sub(r"(?<![\w/])[~.]*/[\w.~/-]+|[A-Za-z_.~][\w.~-]*(?:/[\w.~-]+)+/?", " ", text)
    text = re.sub(r"[\w.-]*\.(?:py|json|md|sh|html|txt|mp3|js|ts|env|command|log)\b", " ", text)
    text = re.sub(r"[*_#>|]+", " ", text)
    text = re.sub(r"[^\w\s.,!?;:()«»\"'—-]", " ", text, flags=re.U)
    # Абзацы (пустая строка) оставляем — между ними пауза paragraph_pause.
    paras = (re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text))
    return "\n\n".join(p for p in paras if re.search(r"\w", p))


def play(path, cfg):
    subprocess.Popen([PLAY, path, str(cfg.get("duck_to", 100)), str(cfg.get("fade_steps", 8)),
                      str(cfg.get("gain", 1.0))],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def say_system(text):
    v = rick_config.load()["voice"]
    proc = subprocess.Popen(["say", "-v", str(v["say_voice"]), "-r", str(v["say_rate"]), text],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:   # stop глушит свой say по pid, а не все say с этим голосом
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(SAY_PID, "w") as f:
            f.write(str(proc.pid))
    except (OSError, AttributeError, TypeError):
        pass


def append_answer(label, text):
    """Строка «метка<tab> <tab>текст» в spisok.txt — её читает live/server.py."""
    os.makedirs(ANSWERS_DIR, exist_ok=True)
    with open(os.path.join(ANSWERS_DIR, "spisok.txt"), "a", encoding="utf-8") as f:
        f.write(f"{label}\t \t{text}\n")


def archive(src, text):
    """Пронумерованная копия озвучки: можно переслушать по порядку."""
    os.makedirs(ANSWERS_DIR, exist_ok=True)
    nums = [int(f[:4]) for f in os.listdir(ANSWERS_DIR) if f[:4].isdigit()]
    n = max(nums, default=0) + 1
    shutil.copy(src, os.path.join(ANSWERS_DIR, f"{n:04d}.mp3"))
    append_answer(f"{n:04d}", text)


def loudify(path):
    """Поднять громкость озвучки, чтобы речь пробивалась сквозь музыку; нет ffmpeg — играем как есть."""
    tmp = path + ".loud.mp3"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-af",
             "highpass=f=130,equalizer=f=2600:t=q:w=1.4:g=7,equalizer=f=4200:t=q:w=1.6:g=4,"
             "acompressor=threshold=-26dB:ratio=10:attack=4:release=90:makeup=4,"
             "volume=18dB,alimiter=limit=0.98:attack=1:release=40",
             "-b:a", "160k", tmp],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=True)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)


def cmd_say(text):
    cfg = load_config()
    cur = cfg.get("voices", {}).get(cfg.get("current"))
    if cur:
        cfg["voice_id"] = cur["voice_id"]
    text = clean(text)
    if not text:
        return
    paras = text.split("\n\n")
    if os.path.exists(LIVE_FLAG):
        append_answer("live", " ".join(paras))
        return
    pause = cfg.get("paragraph_pause", 1.2)
    said = f" [[slnc {int(pause * 1000)}]] ".join(paras)  # пауза для системного say
    text = f' <break time="{pause}s" /> '.join(paras)  # пауза тегом ElevenLabs
    key = load_key()
    if rick_config.load()["voice"].get("engine") != "elevenlabs" or not key or not cfg["voice_id"]:
        say_system(said)
        return
    os.makedirs(CACHE_DIR, exist_ok=True)
    stamp = hashlib.sha1((cfg["voice_id"] + cfg["model_id"] + json.dumps(cfg["voice_settings"], sort_keys=True)
                          + text).encode()).hexdigest()[:16]
    path = os.path.join(CACHE_DIR, stamp + ".mp3")
    if not os.path.exists(path):
        try:
            audio = request(f"/text-to-speech/{cfg['voice_id']}?output_format={cfg['output_format']}", key,
                            method="POST", raw=True,
                            body={"text": text, "model_id": cfg["model_id"], "language_code": cfg["language_code"],
                                  "voice_settings": cfg["voice_settings"]})
            with open(path, "wb") as f:
                f.write(audio)
            loudify(path)
        except Exception as exc:  # сеть, лимит, битый ключ — ответ всё равно должен прозвучать
            print(f"ElevenLabs не ответил ({exc}); говорю системным голосом", file=sys.stderr)
            say_system(said)
            return
    archive(path, " ".join(paras))
    play(path, cfg)


def cmd_stop():
    """Заглушить сразу: сперва play.sh (чтобы не запустил следующий файл), потом afplay и say.
    play.sh убит посреди речи — громкость возвращаем сами из его замка."""
    ere = lambda t: re.sub(r"([][.*+?^${}()|\\])", r"\\\1", t)  # pkill -f — регулярка: путь и голос буквально
    subprocess.run(["pkill", "-f", ere(PLAY)])
    subprocess.run(["pkill", "-f", "afplay .*" + ere(DATA)])
    try:   # свой say — по pid из say_system; чужой «say -v …» в соседнем терминале не трогаем
        with open(SAY_PID) as f:
            pid = int(f.read().strip())
        os.remove(SAY_PID)
        comm = subprocess.run(["ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True).stdout
        if os.path.basename(str(comm).strip()) == "say":
            os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError, TypeError):
        pass
    lock = os.path.join(CACHE_DIR, ".volume")
    if os.path.exists(lock):
        with open(lock) as f:
            vol = f.read().strip()
        if vol.isdigit():
            subprocess.run(["osascript", "-e", f"set volume output volume {vol}"])
        os.remove(lock)
    print("Замолчал")


def cmd_repeat():
    files = sorted(f for f in os.listdir(ANSWERS_DIR) if f.endswith(".mp3")) if os.path.isdir(ANSWERS_DIR) else []
    if not files:
        sys.exit("Озвучек ElevenLabs пока нет")
    play(os.path.join(ANSWERS_DIR, files[-1]), load_config())
    print(f"Повторяю {files[-1]}")


def cmd_tune(what, step):
    cfg = load_config()
    lo, hi = {"gain": (0.5, 8.0), "duck_to": (5, 100)}[what]
    cfg[what] = max(lo, min(hi, round(cfg.get(what, 1.0 if what == "gain" else 100) + step, 2)))
    save_config(cfg)
    print(f"{ {'gain': 'громкость речи', 'duck_to': 'музыка приглушается до'}[what] }: {cfg[what]}")


def cmd_bank():
    cfg = load_config()
    bank = cfg.get("voices", {})
    for alias, p in load_profiles().items():
        mark = "→" if alias == cfg.get("current") else " "
        voice = f" · голос «{bank[alias]['name']}»" if alias in bank else ""
        print(f"{mark} {alias:10} {p['name']}{voice}")


def cmd_use(alias):
    profiles = load_profiles()
    if alias not in profiles:
        sys.exit(f"Личности «{alias}» нет. Что есть — команда bank")
    cfg = load_config()
    cfg["current"] = alias
    voice = cfg.get("voices", {}).get(alias)
    if voice:
        cfg["voice_id"], cfg["voice_name"] = voice["voice_id"], voice["name"]
    save_config(cfg)
    print(f"Включил «{profiles[alias]['name']}». Манера: {profiles[alias].get('manner', 'обычная')}")


def cmd_next():
    ring = list(load_profiles())
    cur = load_config().get("current")
    i = ring.index(cur) if cur in ring else -1
    cmd_use(ring[(i + 1) % len(ring)])


def cmd_check(key):
    me = request("/user/subscription", key)
    used, limit = me.get("character_count", 0), me.get("character_limit", 0)
    print(f"Подписка: {me.get('tier', '?')}")
    print(f"Символы: использовано {used} из {limit}, осталось {max(limit - used, 0)}")
    cfg = load_config()
    print(f"Голос: {cfg['voice_name'] or '(не выбран)'} {cfg['voice_id']}")


def cmd_design(key, profile):
    prof = load_profiles()[profile]
    out = os.path.join(PREVIEW_DIR, profile)
    os.makedirs(out, exist_ok=True)
    res = request("/text-to-voice/design", key, method="POST",
                  body={"model_id": "eleven_multilingual_ttv_v2", "voice_description": prof["prompt"],
                        "text": prof["text"], "auto_generate_text": False, "guidance_scale": 25, "loudness": 0.6})
    state = []
    for i, p in enumerate(res.get("previews", []), 1):
        path = os.path.join(out, f"rick_{i}.mp3")
        with open(path, "wb") as f:
            f.write(base64.b64decode(p["audio_base_64"]))
        state.append({"n": i, "generated_voice_id": p["generated_voice_id"], "file": path})
        print(f"Вариант {i}: {path}")
    with open(os.path.join(out, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    for item in state:
        print(f"\n>>> играю вариант {item['n']}")
        subprocess.run(["afplay", item["file"]])
    print(f"\nПонравился — запусти: rick_voice.py save N \"Имя\" {profile}")


def cmd_save(key, n, name, profile):
    prof = load_profiles()[profile]
    state = read_json(os.path.join(PREVIEW_DIR, profile, "state.json"))
    item = next(x for x in state if x["n"] == int(n))
    voice = request("/text-to-voice", key, method="POST",
                    body={"voice_name": name, "voice_description": prof["prompt"],
                          "generated_voice_id": item["generated_voice_id"]})
    cfg = load_config()
    cfg.setdefault("voices", {})[profile] = {"voice_id": voice["voice_id"], "name": name}
    save_config(cfg)
    cmd_use(profile)


def cmd_library(key, query):
    res = request(f"/shared-voices?page_size=20&search={urllib.parse.quote(query)}&language=ru", key)
    for v in res.get("voices", []):
        labels = ", ".join(v["labels"].values()) if isinstance(v.get("labels"), dict) else ""
        print(f"{v.get('voice_id')}  {v.get('name')}  [{v.get('language')}] {labels} — {v.get('preview_url', '')}")


def cmd_voices(key):
    for v in request("/voices", key).get("voices", []):
        print(f"{v['voice_id']}  {v['name']}  ({v.get('category')})")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd, rest = args[0], args[1:]
    if cmd in ("use", "save") and not rest:   # значение обязательно — без него был бы IndexError
        sys.exit("rick_voice.py %s: нужно значение — %s" % (cmd, "профиль" if cmd == "use" else "voice_id [имя] [профиль]"))
    local = {
        "say": lambda: cmd_say(" ".join(rest) if rest else sys.stdin.read()),
        "stop": cmd_stop,
        "repeat": cmd_repeat,
        "louder": lambda: (cmd_tune("gain", 0.7), cmd_repeat()),
        "quieter": lambda: (cmd_tune("gain", -0.7), cmd_repeat()),
        "duck-more": lambda: (cmd_tune("duck_to", -10), cmd_repeat()),
        "duck-less": lambda: (cmd_tune("duck_to", 10), cmd_repeat()),
        "bank": cmd_bank,
        "use": lambda: cmd_use(rest[0]),
        "next": cmd_next,
    }
    if cmd in local:
        local[cmd]()
        return
    key = load_key()
    if not key:
        sys.exit("Нет ключа: впиши ELEVENLABS_API_KEY в voice/.env")
    if cmd == "check":
        cmd_check(key)
    elif cmd == "design":
        cmd_design(key, rest[0] if rest else "rick")
    elif cmd == "save":
        cmd_save(key, rest[0], rest[1] if len(rest) > 1 else "Рик", rest[2] if len(rest) > 2 else "rick")
    elif cmd == "library":
        cmd_library(key, " ".join(rest) or "raspy old man")
    elif cmd == "voices":
        cmd_voices(key)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
