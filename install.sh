#!/usr/bin/env bash
# ============================================================
#  نصب خودکار ربات هایپرلیکوئید (Hyperliquid Bot) — Ubuntu/Debian/CentOS
# ============================================================
set -e
cd "$(dirname "$0")"

echo "=============================================================="
echo "  🤖 نصب ربات هایپرلیکوئید v26"
echo "=============================================================="

# 1) Python3
if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ python3 پیدا نشد. نصب می‌کنم..."
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update -y && sudo apt-get install -y python3 python3-pip
    elif command -v yum >/dev/null 2>&1; then
        sudo yum install -y python3 python3-pip
    else
        echo "❌ لطفا python3 را دستی نصب کنید و دوباره اجرا کنید."
        exit 1
    fi
fi

# 2) pip packages
echo "[1/3] نصب کتابخانه‌های پایتون..."
python3 -m pip install --upgrade pip -q
python3 -m pip install -r requirements.txt -q
echo "     ✅ کتابخانه‌ها نصب شد"

# 3) .env file
if [ ! -f .env ]; then
    echo "[2/3] ساخت فایل .env..."
    if [ -f .env.example ]; then
        cp .env.example .env
        chmod 600 .env
        echo "     ✅ فایل .env ساخته شد (از .env.example)"
    else
        touch .env
        chmod 600 .env
        echo "     ✅ فایل .env خالی ساخته شد"
    fi
else
    echo "[2/3] فایل .env از قبل وجود دارد - دست نمی‌زنم"
fi

# 4) self test
echo "[3/3] اجرای تست سلامت (Self-Test)..."
python3 hyperliquid_bot.py --selftest || true

# 5) systemd service (24/7)
echo "ثبت سرویس ۲۴ ساعته (systemd)..."
SERVICE=/etc/systemd/system/hyperliquid-bot.service
if [ -w /etc/systemd/system ]; then
    cat > "$SERVICE" <<EOF
[Unit]
Description=Hyperliquid Trading Bot v26
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PWD
ExecStart=/usr/bin/env python3 $PWD/hyperliquid_bot.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable hyperliquid-bot.service >/dev/null 2>&1 || true
    echo "     ✅ سرویس ثبت شد: hyperliquid-bot.service"
    echo "     برای روشن کردن:  sudo systemctl start hyperliquid-bot"
else
    echo "     ⚠️  دسترسی root نیست - سرویس را دستی ثبت کنید یا از manage.sh استفاده کنید"
fi

echo ""
echo "=============================================================="
echo "  ✅ نصب کامل شد!"
echo "  مرحله بعد:  python3 setup_hyperliquid.py"
echo "  سپس:        ./manage.sh start"
echo "  داشبورد:    http://IP-SERVER:8080"
echo "=============================================================="
