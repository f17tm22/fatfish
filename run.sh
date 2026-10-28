#!/usr/bin/env bash
# 大肥鱼快捷入口：./run.sh [start|stop|status|usage|gui|say "话"]
set -euo pipefail
cd "$(dirname "$0")"
# 多机器人：./run.sh --profile claude start   → 用 profiles/claude 这套配置/人格/账本
PROFILE=""
if [ "${1:-}" = "--profile" ]; then
  PROFILE="${2:?用法: ./run.sh --profile <名字> <命令>}"
  shift 2
fi
cmd="${1:-status}"
shift || true
if [ -n "$PROFILE" ]; then
  export FATFISH_ROOT="$(cd "$(dirname "$0")" && pwd)/profiles/$PROFILE"
  export PYTHONPATH="$(cd "$(dirname "$0")" && pwd)${PYTHONPATH:+:$PYTHONPATH}"
  [ -d "$FATFISH_ROOT" ] || { echo "没有这个 profile: $PROFILE（先 ./run.sh --profile $PROFILE init）"; exit 1; }
fi

case "$cmd" in
  start)  exec python3 -m fatfish.cli start ;;
  stop)   exec python3 -m fatfish.cli stop ;;
  status) exec python3 -m fatfish.cli status ;;
  usage)  exec python3 -m fatfish.cli usage "$@" ;;
  persona) exec python3 -m fatfish.cli persona ;;
  say)    exec python3 -m fatfish.cli say "$@" ;;
  gui)    exec python3 gui/tuner.py ;;
  _autostart)
    PLIST="$HOME/Library/LaunchAgents/ai.fatfish.plist"
    case "${1:-status}" in
      install)
        mkdir -p "$HOME/Library/LaunchAgents"
        bash "$0" stop >/dev/null 2>&1
        cp deploy/ai.fatfish.plist "$PLIST"
        launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1
        launchctl bootstrap "gui/$(id -u)" "$PLIST" && echo "已装自启（开机/登录后自动拉起，崩溃 30 秒后重试）"
        launchctl kickstart -k "gui/$(id -u)/ai.fatfish" >/dev/null 2>&1
        ;;
      remove)
        launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1
        rm -f "$PLIST"
        echo "已取消自启"
        ;;
      *)
        if [ -f "$PLIST" ]; then
          echo "自启：已安装 $PLIST"
          launchctl print "gui/$(id -u)/ai.fatfish" 2>/dev/null | grep -E "state|pid" | head -3
        else
          echo "自启：未安装（./run.sh autostart install 装，remove 卸）"
        fi
        ;;
    esac
    ;;
  logs)   exec tail -n 80 -f logs/fatfish.log ;;
  check)  exec python3 scripts/selfcheck.py ;;
  sim)    exec python3 scripts/e2e_group_sim.py ;;
  verify) python3 scripts/selfcheck.py | tail -1; python3 scripts/e2e_group_sim.py | tail -1; python3 -m fatfish.cli diagnose | tail -6; exec python3 -m fatfish.cli status ;;
  diag)   exec python3 -m fatfish.cli diagnose ;;
  groups) exec python3 -m fatfish.cli groups ;;
  master) exec python3 -m fatfish.cli master "$@" ;;
  events) exec python3 -m fatfish.cli events "$@" ;;
  probe)  shift; exec python3 -m fatfish.cli probe "$@" ;;
  cron)   shift_args=("$@"); exec python3 -m fatfish.cli cron "${shift_args[@]}" ;;
  wechat-persona) exec python3 scripts/wechat_persona.py "$@" ;;
  autostart) exec bash "$0" _autostart "$@" ;;
  init)
    NAME="${1:?用法: ./run.sh init <profile 名> --app-id <AppID> --secret <Secret>}"
    shift || true
    exec python3 scripts/new_profile.py "$NAME" "$@"
    ;;
  *) echo "用法: ./run.sh [--profile 名] [start|stop|status|usage|persona|say 话|gui|logs|check|sim|verify|diag|groups|master|events|probe <openid>|init 名字 --app-id X --secret Y|wechat-persona --list|--on|--off|autostart ...|cron ...]"; exit 1 ;;
esac