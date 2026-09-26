#!/usr/bin/env bash
# cline-router 控制脚本（launchd 感知：装了开机自启后由 launchd 托管）
#   ./cline-router.sh start | stop | restart | status | log | models | test | ui
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$DIR/router.pid"
LOGFILE="$DIR/router.log"
PY="${PYTHON:-/usr/bin/python3}"
LABEL="com.wangcheng.cline-router"
DOMAIN="gui/$(id -u)"
PORT="$("$PY" -c "import json;print(json.load(open('$DIR/models.json')).get('port',4000))" 2>/dev/null || echo 4000)"

agent_loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }
healthy() { curl -s -m 3 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; }

case "${1:-status}" in
  start)
    if agent_loaded; then
      launchctl kickstart "$DOMAIN/$LABEL" >/dev/null 2>&1
      echo "已由 launchd（开机自启）拉起"
    else
      cd "$DIR" || exit 1
      nohup "$PY" router.py >>"$LOGFILE" 2>&1 &
      echo $! >"$PIDFILE"
      echo "已启动（未装开机自启，pid $(cat "$PIDFILE")）"
    fi
    sleep 1
    if healthy; then echo "健康检查通过：http://127.0.0.1:$PORT/ui"; else echo "健康检查未通过，日志："; tail -n 15 "$LOGFILE"; fi
    ;;
  stop)
    if agent_loaded; then
      launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 && echo "已停止，并取消开机自启（再执行 start 会重新装回）"
    elif [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      kill "$(cat "$PIDFILE")" && rm -f "$PIDFILE" && echo "已停止"
    else
      echo "未在运行"
    fi
    ;;
  restart)
    if agent_loaded; then
      launchctl kickstart -k "$DOMAIN/$LABEL" >/dev/null 2>&1
      for _ in 1 2 3 4 5 6 7 8 9 10; do healthy && break; sleep 0.5; done
      if healthy; then echo "已重启：http://127.0.0.1:$PORT/ui"; else echo "重启后健康检查未通过，日志："; tail -n 15 "$LOGFILE"; fi
    else
      "$0" stop
      sleep 1
      "$0" start
    fi
    ;;
  status)
    if agent_loaded; then echo "launchd：已加载（开机自启生效）"; else echo "launchd：未加载"; fi
    if healthy; then echo "服务：运行中  http://127.0.0.1:$PORT/v1"; else echo "服务：未响应"; fi
    curl -s -m 3 "http://127.0.0.1:$PORT/health" || true
    echo
    ;;
  log)
    tail -n "${2:-40}" "$LOGFILE"
    ;;
  models)
    curl -s -m 5 "http://127.0.0.1:$PORT/v1/models" \
      | "$PY" -c "import json,sys;[print(m['id']) for m in json.load(sys.stdin)['data']]" 2>/dev/null \
      || echo "router 未运行"
    ;;
  test)
    "$PY" "$DIR/selftest.py"
    ;;
  ui)
    open "http://127.0.0.1:$PORT/ui"
    ;;
  *)
    echo "用法: $0 {start|stop|restart|status|log|models|test|ui}"
    exit 1
    ;;
esac
