#!/usr/bin/env bash
# ==============================================================================
# اسکریپت نصب و راه‌اندازی خودکار ربات معامله‌گر نوبیتکس (Nobitex Trading Bot v25)
# مناسب برای سرورهای مجازی (VPS) اوبونتو، دبیان و سنت‌اواس
# ==============================================================================

set -e

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${CYAN}==============================================================${NC}"
echo -e "${GREEN} 🚀 شروع نصب و راه‌اندازی ربات معامله‌گر نوبیتکس نسخه ۲۵ ${NC}"
echo -e "${CYAN}==============================================================${NC}"

# 1. بررسی دایرکتوری پروژه
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 2. نصب پیش‌نیازهای پایتون
echo -e "\n${YELLOW}[۱/۵] بررسی پیش‌نیازهای پایتون و ابزارهای سرور...${NC}"
if command -v apt-get &> /dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3 python3-pip python3-venv python3-requests python3-numpy curl
elif command -v yum &> /dev/null; then
    sudo yum install -y -q python3 python3-pip curl
fi

# 3. نصب کتابخانه‌ها (سازگار با همه نسخه‌های اوبونتو و PEP-668)
echo -e "${YELLOW}[۲/۵] نصب کتابخانه‌های مورد نیاز (requests, numpy)...${NC}"
python3 -m pip install -r requirements.txt --break-system-packages -q 2>/dev/null || python3 -m pip install -r requirements.txt -q
echo -e "${GREEN}  ✓ کتابخانه‌ها با موفقیت نصب شدند.${NC}"

# 4. ایجاد فایل تنظیمات .env در صورت عدم وجود
echo -e "${YELLOW}[۳/۵] بررسی فایل تنظیمات محیطی (.env)...${NC}"
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo -e "${GREEN}  ✓ فایل .env از روی الگو ایجاد شد (با دسترسی امن 600).${NC}"
    echo -e "${CYAN}  * نکته: می‌توانید فایل .env را ویرایش کنید و توکن نوبیتکس و تلگرام خود را وارد کنید.${NC}"
else
    echo -e "${GREEN}  ✓ فایل .env از قبل موجود است.${NC}"
fi

# 5. اجرای تست خودکار ربات
echo -e "${YELLOW}[۴/۵] اجرای تست خودکار کدها (Self-Test)...${NC}"
python3 nobitex_bot.py --selftest
echo -e "${GREEN}  ✓ تمام ۳۲ تست کدها با موفقیت پاس شد!${NC}"

# 6. تنظیم سرویس Systemd برای اجرای ۲۴ ساعته در پس‌زمینه (در صورت دسترسی روت یا sudo)
echo -e "${YELLOW}[۵/۵] تنظیم سرویس خودکار سرور (Systemd)...${NC}"
SERVICE_FILE="/etc/systemd/system/nobitex-bot.service"

if [ -w "/etc/systemd/system" ] || sudo -n true 2>/dev/null; then
    SERVICE_CONTENT="[Unit]
Description=Nobitex Trading Bot v25 Service
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=${SCRIPT_DIR}
ExecStart=$(which python3) ${SCRIPT_DIR}/nobitex_bot.py
Restart=always
RestartSec=10
EnvironmentFile=-${SCRIPT_DIR}/.env

[Install]
WantedBy=multi-user.target"

    echo "$SERVICE_CONTENT" | sudo tee "$SERVICE_FILE" > /dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable nobitex-bot.service > /dev/null 2>&1 || true
    echo -e "${GREEN}  ✓ سرویس nobitex-bot در سیستم‌عامل ثبت و خودکار شد.${NC}"
    echo -e "${CYAN}  * برای شروع سرویس دستور زیر را بزنید:${NC}"
    echo -e "    ${GREEN}sudo systemctl start nobitex-bot${NC}"
else
    echo -e "${YELLOW}  ! دسترسی روت برای ثبت systemd وجود ندارد (عادی برای محیط‌های غیر روت).${NC}"
    echo -e "${CYAN}  * ربات را می‌توانید با دستور ./manage.sh start اجرا کنید.${NC}"
fi

# 7. اعطای دسترسی اجرایی به اسکریپت‌ها
chmod +x nobitex_bot.py manage.sh install.sh

echo -e "\n${GREEN}==============================================================${NC}"
echo -e "${GREEN} 🎉 نصب ربات با موفقیت به اتمام رسید! ${NC}"
echo -e "${CYAN}  👉 برای مدیریت ساده ربات از اسکریپت فارسی manage.sh استفاده کنید:${NC}"
echo -e "     ${YELLOW}./manage.sh start${NC}   -> روشن کردن ربات"
echo -e "     ${YELLOW}./manage.sh status${NC}  -> دیدن وضعیت کارکرد"
echo -e "     ${YELLOW}./manage.sh log${NC}     -> مشاهده زنده لاگ‌ها و معاملات"
echo -e "     ${YELLOW}./manage.sh stop${NC}    -> خاموش کردن ربات"
echo -e "${GREEN}==============================================================${NC}"
