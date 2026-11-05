#!/bin/sh
# ./start.sh            start the platform in the background (http://localhost:5050), and its PostgreSQL: the web
#                       (WORKER=0) and, beside it, the worker that does the background work (python -m app.worker)
# ./start.sh setup      make the platform's PostgreSQL (with pgvector) the first time: data/postgres, port 5433
# ./start.sh stop       stop it (the web, the worker and its PostgreSQL)
# ./start.sh restart    stop the web and the worker and start them again (PostgreSQL keeps running)
# ./start.sh status     is it running?
# ./start.sh backup     make a backup now (data/backups)
# ./start.sh install    start it automatically when you log in to this Mac (and restart it if it stops): one process,
#                       the worker running inside the web (WORKER=1)
# ./start.sh uninstall  stop starting it automatically
cd "$(dirname "$0")"
mkdir -p data/logs
PYTHON=.venv/bin/python
[ -x "$PYTHON" ] || PYTHON=python3
PLIST="$HOME/Library/LaunchAgents/com.keel.erp.plist"

alive() {  # alive data/server.pid: whether the process it names is running
  [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null
}

stop_one() {  # stop_one data/worker.pid "The worker": stop it, and wait until it has (up to 30 seconds: the worker
  if alive "$1"; then  # finishes what it's doing first, and what it can't finish is picked up when it starts again)
    pid="$(cat "$1")"
    kill "$pid" 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
      kill -0 "$pid" 2>/dev/null || break; sleep 1
    done
    rm -f "$1"
    echo "$2 stopped."
  else
    rm -f "$1"
    echo "$2 wasn't running."
  fi
}

stop_both() {
  stop_one data/worker.pid "The worker"
  stop_one data/server.pid "The web"
}

start_both() {
  if alive data/server.pid && alive data/worker.pid; then echo "Already running: http://localhost:${PORT:-5050}"; return 0; fi
  if [ ! -f data/postgres/PG_VERSION ]; then "$PYTHON" -m app.database setup || exit 1; else "$PYTHON" -m app.database start || exit 1; fi
  if ! alive data/server.pid; then
    WORKER=0 nohup "$PYTHON" run.py >> data/logs/server.out 2>&1 &
    echo $! > data/server.pid
  fi
  if ! alive data/worker.pid; then
    nohup "$PYTHON" -m app.worker >> data/logs/worker.out 2>&1 &
    echo $! > data/worker.pid
  fi
  echo "Starting… open http://localhost:${PORT:-5050} (logs: data/logs)"
}

case "$1" in
  setup)
    "$PYTHON" -m app.database setup
    exit $? ;;
  stop)
    if [ -f "$PLIST" ]; then launchctl unload "$PLIST" 2>/dev/null; fi
    stop_both
    "$PYTHON" -m app.database stop
    exit 0 ;;
  restart)
    stop_both
    start_both
    exit 0 ;;
  status)
    if alive data/server.pid; then echo "The web is running (pid $(cat data/server.pid))."; else echo "The web isn't running."; fi
    if alive data/worker.pid; then echo "The worker is running (pid $(cat data/worker.pid))."; else echo "The worker isn't running."; fi
    exit 0 ;;
  backup)
    "$PYTHON" -c "from app import create_app; from app import backup; a = create_app({'WORKER': False}); a.app_context().push(); print('Backup:', backup.make())"
    exit $? ;;
  install)
    cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.keel.erp</string>
  <key>ProgramArguments</key><array><string>$(pwd)/$PYTHON</string><string>$(pwd)/run.py</string></array>
  <key>WorkingDirectory</key><string>$(pwd)</string>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$(pwd)/data/logs/server.out</string>
  <key>StandardErrorPath</key><string>$(pwd)/data/logs/server.out</string>
</dict></plist>
PLISTEOF
    launchctl unload "$PLIST" 2>/dev/null; launchctl load "$PLIST" && echo "Installed: it starts when you log in. Open http://localhost:${PORT:-5050}"
    exit 0 ;;
  uninstall)
    launchctl unload "$PLIST" 2>/dev/null; rm -f "$PLIST"; echo "It no longer starts automatically."
    exit 0 ;;
esac
start_both
