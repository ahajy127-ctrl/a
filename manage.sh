#!/usr/bin/env bash
# ============================================================
#  پنل مدیریت ربات هایپرلیکوئید — کاملا فارسی
#  دستورها: start | stop | restart | status | log | test | setup | backup
# ============================================================
cd "$(dirname "$0")"
BOT=hyperliquid_bot.py
LOG=run.log
PORT=8080

case "${1:-}" in
  start)
    if pgrep -f "$BOT" >/dev/null 2>&1; then
      echo "🟢 ربات از قبل در حال اجراست."
    else
      nohup python3 "$BOT" > "$LOG" 2>&1 &
      sleep 3
      echo "🟢 ربات روشن شد (PID $(pgrep -f "$BOT" | head -1))"
      echo "   داشبورد: http://$(hostname -I | awk '{print $1}'):$PORT"
    fi
    ;;
  stop)
    pkill -f "$BOT" && echo "🔴 ربات متوقف شد" || echo "ℹ️ ربات در حال اجرا نبود"
    ;;
  restart)
    pkill -f "$BOT" 2>/dev/null; sleep 2
    nohup python3 "$BOT" > "$LOG" 2>&1 &
    sleep 3
    echo "🔄 ربات ری‌استارت شد"
    ;;
  status)
    if pgrep -f "$BOT" >/dev/null 2>&1; then
      echo "🟢 ربات در حال اجراست (PID $(pgrep -f "$BOT" | head -1))"
    else
      echo "🔴 ربات روشن نیست"
    fi
    echo "📊 داشبورد: http://$(hostname -I | awk '{print $1}'):$PORT"
    echo "🧪 آخرین لاگ:"
    tail -5 app.log 2>/dev/null || echo "   (لاگی نیست)"
    ;;
  log)
    tail -f app.log
    ;;
  test)
    python3 "$BOT" --selftest
    ;;
  setup)
    python3 setup_hyperliquid.py
    ;;
  backup)
    mkdir -p backups
    BK="backups/backup-$(date +%Y%m%d-%H%M).tar.gz"
    tar czf "$BK" state.json .env app.log 2>/dev/null
    echo "💾 بکاپ گرفته شد: $BK"
    ;;
  *)
    echo "پنل مدیریت ربات هایپرلیکوئید"
    echo "  ./manage.sh start    روشن کردن"
    echo "  ./manage.sh stop     خاموش کردن"
    echo "  ./manage.sh restart  ری‌استارت"
    echo "  ./manage.sh status   وضعیت و آدرس داشبورد"
    echo "  ./manage.sh log      مشاهده زنده لاگ‌ها"
    echo "  ./manage.sh test     تست سلامت"
    echo "  ./manage.sh setup    راه‌اندازی Agent Wallet (قدم بعدی!)"
    echo "  ./manage.sh backup   بکاپ گرفتن"
    ;;
esac
