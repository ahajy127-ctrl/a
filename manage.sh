#!/usr/bin/env bash
# ==============================================================================
# پنل مدیریت سریع ربات نوبیتکس (برای کاربران بدون نیاز به کدنویسی)
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

function show_help() {
    echo -e "${CYAN}--------------------------------------------------------------${NC}"
    echo -e "${GREEN} 🤖 پنل مدیریت آسان ربات معامله‌گر نوبیتکس v25 ${NC}"
    echo -e "${CYAN}--------------------------------------------------------------${NC}"
    echo -e "راهنمای استفاده از دستورات:"
    echo -e "  ${YELLOW}./manage.sh start${NC}    -> 🟢 روشن کردن ربات (در پس‌زمینه سرور)"
    echo -e "  ${YELLOW}./manage.sh stop${NC}     -> 🔴 خاموش کردن ربات"
    echo -e "  ${YELLOW}./manage.sh restart${NC}  -> 🔄 راه‌اندازی مجدد (ریستارت)"
    echo -e "  ${YELLOW}./manage.sh status${NC}   -> 📊 نمایش وضعیت کارکرد و آدرس داشبورد"
    echo -e "  ${YELLOW}./manage.sh log${NC}      -> 📜 مشاهده زنده لاگ‌ها و معاملات ربات"
    echo -e "  ${YELLOW}./manage.sh test${NC}     -> 🧪 اجرای تست سلامت کدها (Self-Test)"
    echo -e "  ${YELLOW}./manage.sh backup${NC}   -> 💾 گرفتن پشتیبان (بکاپ) از فایل معاملات"
    echo -e "${CYAN}--------------------------------------------------------------${NC}"
}

function start_bot() {
    echo -e "${YELLOW}در حال روشن کردن ربات نوبیتکس...${NC}"
    if command -v systemctl &> /dev/null && systemctl list-unit-files nobitex-bot.service &> /dev/null; then
        sudo systemctl start nobitex-bot.service
        echo -e "${GREEN}  ✓ ربات با سرویس سیستم (Systemd) روشن شد و ۲۴ ساعته فعال است.${NC}"
    else
        # اگر systemd نبود، با nohup در پس‌زمینه اجرا شود
        if pgrep -f "nobitex_bot.py" > /dev/null; then
            echo -e "${YELLOW}  ! ربات از قبل در حال اجراست.${NC}"
        else
            nohup python3 nobitex_bot.py > /dev/null 2>&1 &
            echo -e "${GREEN}  ✓ ربات در پس‌زمینه سرور اجرا شد.${NC}"
        fi
    fi
    show_status
}

function stop_bot() {
    echo -e "${YELLOW}در حال خاموش کردن ربات...${NC}"
    if command -v systemctl &> /dev/null && systemctl is-active --quiet nobitex-bot.service 2>/dev/null; then
        sudo systemctl stop nobitex-bot.service
        echo -e "${RED}  ✓ ربات (سرویس systemd) متوقف شد.${NC}"
    fi
    if pgrep -f "nobitex_bot.py" > /dev/null; then
        pkill -f "nobitex_bot.py" || true
        echo -e "${RED}  ✓ پردازش‌های ربات بسته شدند.${NC}"
    else
        echo -e "${YELLOW}  * ربات خاموش است.${NC}"
    fi
}

function show_status() {
    echo -e "\n${CYAN}--- وضعیت ربات نوبیتکس ---${NC}"
    PID=$(pgrep -f "nobitex_bot.py" | head -n 1)
    if [ -n "$PID" ]; then
        echo -e "وضعیت: ${GREEN}🟢 روشن و فعال${NC} (PID: ${PID})"
        # دریافت IP سرور برای نمایش لینک داشبورد
        IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
        echo -e "لینک داشبورد وب: ${CYAN}http://${IP}:8080${NC}  (یا http://localhost:8080)"
        echo -e "آدرس بخش مجازی (۱۰۰$): ${CYAN}http://${IP}:8080/paper${NC}"
        if [ -f app.log ]; then
            echo -e "\n${YELLOW}آخرین پیام ثبت شده در لاگ ربات:${NC}"
            tail -n 3 app.log | sed 's/^/  /'
        fi
    else
        echo -e "وضعیت: ${RED}🔴 خاموش${NC}"
        echo -e "برای روشن کردن بزنید: ${YELLOW}./manage.sh start${NC}"
    fi
    echo ""
}

function show_logs() {
    if [ ! -f app.log ]; then
        echo -e "${YELLOW}هنوز فایل لاگ (app.log) ایجاد نشده است. ابتدا ربات را روشن کنید.${NC}"
        exit 1
    fi
    echo -e "${CYAN}نمایش زنده لاگ‌ها (برای خروج کلیدهای Ctrl + C را همزمان فشار دهید):${NC}\n"
    tail -f app.log
}

function run_test() {
    echo -e "${YELLOW}در حال تست کامل عملکرد ۳۰ بخش استراتژی و محاسبات ربات...${NC}"
    python3 nobitex_bot.py --selftest
}

function do_backup() {
    mkdir -p backups
    chmod 700 backups   # FIX: backup dir previously had default (often world-readable) perms
    TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
    BACKUP_FILE="backups/nobitex_backup_${TIMESTAMP}.tar.gz"
    echo -e "${YELLOW}در حال ایجاد فایل پشتیبان از معاملات و تنظیمات...${NC}"
    tar -czf "$BACKUP_FILE" state.json .env app.log 2>/dev/null || tar -czf "$BACKUP_FILE" state.json app.log 2>/dev/null || true
    if [ -f "$BACKUP_FILE" ]; then
        chmod 600 "$BACKUP_FILE"   # FIX: this archive contains .env (API/Telegram tokens
                                    # in plaintext) and state.json - lock it down the same
                                    # way install.sh locks down .env itself.
        echo -e "${GREEN}  ✓ فایل پشتیبان با موفقیت ساخته شد:${NC} ${CYAN}${BACKUP_FILE}${NC}"
        echo -e "    می‌توانید این فایل را دانلود کرده و در جای امن نگهداری کنید."
    else
        echo -e "${RED}  ✗ خطا در ایجاد بکاپ. بررسی کنید که آیا فایل state.json وجود دارد.${NC}"
    fi
}

case "$1" in
    start)
        start_bot
        ;;
    stop)
        stop_bot
        ;;
    restart)
        stop_bot
        sleep 2
        start_bot
        ;;
    status)
        show_status
        ;;
    log|logs)
        show_logs
        ;;
    test)
        run_test
        ;;
    backup)
        do_backup
        ;;
    *)
        show_help
        ;;
esac
