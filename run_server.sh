#!/usr/bin/env bash
# 내부망 서버에서 UI를 켜고 끄는 스크립트.
#
#   ./run_server.sh start    백그라운드로 띄운다 (SSH를 끊어도 유지된다)
#   ./run_server.sh stop     내린다
#   ./run_server.sh status   상태와 접속 주소를 보여준다
#   ./run_server.sh log      최근 로그를 본다
#
# 상시 서비스로 두려면 DEPLOY.md의 systemd 설정을 참고할 것.

set -euo pipefail
cd "$(dirname "$0")"

ENV_NAME=project-archive-rag
PORT=8501
PIDFILE=.streamlit_app.pid
LOGFILE=.streamlit_app.log

# conda activate는 비대화형 셸에서 동작하지 않으므로 환경의 실행 파일을 직접 쓴다
CONDA_BASE=$(conda info --base 2>/dev/null || echo "$HOME/miniconda3")
STREAMLIT="$CONDA_BASE/envs/$ENV_NAME/bin/streamlit"

running() {
  [[ -f $PIDFILE ]] && kill -0 "$(cat $PIDFILE)" 2>/dev/null
}

case "${1:-status}" in
  start)
    if running; then
      echo "이미 실행 중이다 (PID $(cat $PIDFILE))"
      exit 0
    fi
    [[ -x $STREAMLIT ]] || { echo "streamlit을 찾을 수 없다: $STREAMLIT"; exit 1; }

    nohup "$STREAMLIT" run app.py \
      --server.address 0.0.0.0 --server.port $PORT --server.headless true \
      > "$LOGFILE" 2>&1 &
    echo $! > $PIDFILE
    sleep 3

    if running; then
      IP=$(hostname -I | awk '{print $1}')
      echo "실행됨 (PID $(cat $PIDFILE))"
      echo "접속: http://$IP:$PORT"
      echo "종료: ./run_server.sh stop"
    else
      echo "기동에 실패했다. 로그를 확인할 것:"
      tail -20 "$LOGFILE"
      rm -f $PIDFILE
      exit 1
    fi
    ;;

  stop)
    if running; then
      kill "$(cat $PIDFILE)"
      rm -f $PIDFILE
      echo "종료했다"
    else
      echo "실행 중이 아니다"
      rm -f $PIDFILE
    fi
    ;;

  status)
    if running; then
      IP=$(hostname -I | awk '{print $1}')
      echo "실행 중 (PID $(cat $PIDFILE))  →  http://$IP:$PORT"
    else
      echo "중지 상태"
    fi
    ;;

  log)
    tail -n "${2:-40}" "$LOGFILE"
    ;;

  *)
    echo "사용법: $0 {start|stop|status|log}"
    exit 1
    ;;
esac
