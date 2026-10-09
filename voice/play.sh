#!/bin/bash
# Проиграть озвучку: сигнал (системный звук macOS), речь; при приглушении — плавно убрать музыку и вернуть.
# Вызов: play.sh <файл.mp3> [до_скольких_процентов (100 — не приглушать)] [шагов] [усиление_afplay]
# Сигнал выключить — NO_SIGNAL=1; свой звук — RICK_SIGNAL=<файл>.
set -u
FILE="$1"
DUCK="${2:-100}"
STEPS="${3:-8}"
GAIN="${4:-1}"
DIR="$(cd "$(dirname "$0")" && pwd)"
CACHE="${RICK_HOME:-$(dirname "$DIR")}/voice/cache"
LOCK="$CACHE/.volume"
SIGNAL="${RICK_SIGNAL:-/System/Library/Sounds/Tink.aiff}"
mkdir -p "$CACHE"

# Очередь: две озвучки разом не говорят — следующая ждёт, пока договорит предыдущая.
QLOCK="$CACHE/.speaking"
for _ in $(seq 1 600); do
  mkdir "$QLOCK" 2>/dev/null && break
  # замок старше 3 минут остался от убитой озвучки
  [ -n "$(find "$QLOCK" -maxdepth 0 -mmin +3 2>/dev/null)" ] && rmdir "$QLOCK" 2>/dev/null
  sleep 0.3
done
trap 'rmdir "$QLOCK" 2>/dev/null' EXIT
trap 'exit 143' TERM INT

signal() { [ -f "$SIGNAL" ] && [ "${NO_SIGNAL:-0}" != "1" ] && afplay "$SIGNAL"; }

if [ "$DUCK" -ge 100 ]; then
  signal
  afplay -v "$GAIN" "$FILE"
  exit 0
fi

vol() { osascript -e "set volume output volume $1" 2>/dev/null; }
cur() { osascript -e "output volume of (get volume settings)" 2>/dev/null; }

START="$(cur)"
[ -z "$START" ] && START=70
# Параллельная озвучка уже приглушила музыку — не запоминаем приглушённый уровень как исходный.
if [ -f "$LOCK" ]; then
  START="$(cat "$LOCK")"
else
  echo "$START" > "$LOCK"
fi

fade() { # fade <из> <в>
  local from=$1 to=$2 i
  for ((i = 1; i <= STEPS; i++)); do
    vol $(( from + (to - from) * i / STEPS ))
    sleep 0.04
  done
}

fade "$START" "$DUCK"
signal
afplay -v "$GAIN" "$FILE"
fade "$DUCK" "$START"
rm -f "$LOCK"
