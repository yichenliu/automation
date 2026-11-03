#!/bin/sh
# ./start.sh            start the platform in the background (http://localhost:5050)
# ./start.sh stop       stop it
# ./start.sh status     is it running?
# ./start.sh backup     make a backup now (data/backups)
# ./start.sh install    start it automatically when you log in to this Mac (and restart it if it stops)
# ./start.sh uninstall  stop starting it automatically
cd "$(dirname "$0")"
mkdir -p data/logs
PYTHON=.venv/bin/python
[ -x "$PYTHON" ] || PYTHON=python3
PLIST="$HOME/Library/LaunchAgents/com.keel.erp.plist"
case "$1" in
  stop)
    if [ -f "$PLIST" ]; then launchctl unload "$PLIST" 2>/dev/null; fi
    [ -f data/server.pid ] && kill "$(cat data/server.pid)" 2>/dev/null && rm -f data/server.pid && echo "Stopped." || echo "Not running."
    exit 0 ;;
  status)
    if [ -f data/server.pid ] && kill -0 "$(cat data/server.pid)" 2>/dev/null; then echo "Running (pid $(cat data/server.pid))."; else echo "Not running."; fi
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
if [ -f data/server.pid ] && kill -0 "$(cat data/server.pid)" 2>/dev/null; then echo "Already running: http://localhost:${PORT:-5050}"; exit 0; fi
nohup "$PYTHON" run.py >> data/logs/server.out 2>&1 &
echo $! > data/server.pid
echo "Starting… open http://localhost:${PORT:-5050} (logs: data/logs)"
