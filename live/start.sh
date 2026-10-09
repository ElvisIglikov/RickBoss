#!/bin/zsh
# Живой голос Рика: поднять локальный сервер и открыть разговор в Google Chrome.
# Ключ — OPENAI_API_KEY в voice/.env; memory/NOW.md уходит в OpenAI (docs/voice-live.md).
#   live/start.sh            — GPT-Live-1 (основной)
#   live/start.sh realtime   — gpt-realtime-2.1 (запасной)
#   live/start.sh stop       — погасить сервер
cd "${0:A:h}" || exit 1
PORT=${LIVE_PORT:-8787}
URL="http://127.0.0.1:$PORT"

# Свой сервер — по pid из log/server.pid (пишет запуск ниже) или по буквальному пути этого server.py;
# серверы других копий Рика и чужие процессы с «live/server.py» в строке не трогаем.
PIDFILE=log/server.pid
SERVER="$PWD/server.py"
own_pids() {
  local pid esc
  pid=$(cat "$PIDFILE" 2>/dev/null)
  if [[ "$pid" == <-> ]] && ps -p "$pid" -o command= 2>/dev/null | grep -qF -- "$SERVER"; then
    echo "$pid"; return
  fi
  esc=$(printf '%s' "$SERVER" | sed 's/[][\\.*^$()+?{}|]/\\&/g')
  pgrep -f "[Pp]ython[0-9.]* +$esc\$"
}

if [[ "$1" == "stop" ]]; then
  rm -f ../voice/cache/.live_front
  PIDS=($(own_pids))
  if (( ${#PIDS} )) && kill "${PIDS[@]}" 2>/dev/null; then
    for i in {1..15}; do   # до 3 с: «погашен» — когда процесса и правда нет
      ps -p "${(j:,:)PIDS}" >/dev/null 2>&1 || break
      sleep 0.2
    done
    echo "Сервер погашен"
  else
    echo "Сервер и не работал"
  fi
  rm -f "$PIDFILE"
  exit 0
fi

# На порту — сервер именно этой копии: /api/health отдаёт путь своего server.py. Иначе страница чужой копии Рика
# слала бы фразы в её почту.
mine() {
  curl -s --max-time 2 "$URL/api/health" 2>/dev/null | python3 -c \
    'import json, sys; sys.exit(0 if json.load(sys.stdin).get("server") == sys.argv[1] else 1)' "$SERVER" 2>/dev/null
}

mkdir -p log
if ! mine; then
  PIDS=($(own_pids))   # свой сервер старой версии (без «server» в health) — перезапустить
  (( ${#PIDS} )) && kill "${PIDS[@]}" 2>/dev/null
  # убитый сервер отпускает порт не сразу: ждём до 3 с, и только потом «занят чужим»
  TO=2; (( ${#PIDS} )) && TO=0.5
  for i in {1..15}; do
    curl -s -o /dev/null --max-time $TO "$URL/"
    FREE=$?   # 7 — соединение отклонено: порт свободен
    (( FREE == 7 || ! ${#PIDS} )) && break
    sleep 0.2
  done
  if (( FREE != 7 )); then
    echo "Порт ${PORT} занят другой копией Рика или чужой программой — задай LIVE_PORT: LIVE_PORT=8788 live/start.sh"
    exit 1
  fi
  nohup python3 "$SERVER" >> log/server.log 2>&1 &
  echo $! > "$PIDFILE"
  for i in {1..25}; do
    mine && break
    sleep 0.2
  done
fi
if ! mine; then
  echo "Сервер не поднялся — смотри live/log/server.log"
  exit 1
fi

# Пока идёт разговор, rick_voice.py say не говорит сам, а отдаёт строку голосу (иначе ответ звучит дважды).
mkdir -p ../voice/cache && touch ../voice/cache/.live_front
ENGINE=${1:-live}
open -a "Google Chrome" "$URL/?engine=$ENGINE&auto=1"
echo "Открыл $URL в Chrome ($ENGINE). Погасить: live/start.sh stop"
