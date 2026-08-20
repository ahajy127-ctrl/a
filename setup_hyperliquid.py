#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
راه‌اندازی یک‌کلیکه ربات هایپرلیکوئید (Agent Wallet)
=====================================================
کاری که این اسکریپت انجام می‌دهد:
  1) یک کیف پول Agent (کلید خصوصی جدید) برای ربات می‌سازد
  2) آدرس Agent و راهنمای گام‌به‌گام فارسی را نشان می‌دهد
     (اتصال Rabby به هایپرلیکوئید، واریز، تایید Agent)
  3) فایل .env را با کلیدها و تنظیمات تکمیل می‌کند
  4) در صورت فراهم بودن، اتصال را تست می‌کند

مفهوم امنیتی مهم:
  - کلید Master (کیف پول Rabby شما) هرگز در ربات ذخیره نمی‌شود.
  - ربات فقط با کلید Agent کار می‌کند که حق معامله دارد و هرگز نمی‌تواند برداشت کند.
"""

import os, sys, json, time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, '.env')

# ---------- helpers ----------

def load_env():
    env = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, _, v = line.partition('=')
                    env[k.strip()] = v.strip()
    return env

def save_env(env):
    order = ['MODE', 'HL_TESTNET', 'HL_ACCOUNT_ADDRESS', 'HL_AGENT_PRIVATE_KEY',
             'NOBITEX_TOKEN', 'TG_TOKEN', 'TG_CHAT', 'TG_PROXY', 'DASH_PASS',
             'HL_USE_TRIGGER_SL']
    lines = ["# ================================================================",
             "# تنظیمات ربات هایپرلیکوئید v26 (Hyperliquid Bot Env Config)",
             "# ================================================================",
             "# نکته امنیتی: این فایل حاوی کلید خصوصی Agent است.",
             "# آن را به هیچ کس ندهید و فایل .env را عمومی نکنید.",
             ""]
    for k in order:
        v = env.get(k)
        if v is None:
            continue
        lines.append(f"{k}={v}")
    for k, v in env.items():
        if k not in order:
            lines.append(f"{k}={v}")
    lines.append("")
    with open(ENV_FILE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    try:
        os.chmod(ENV_FILE, 0o600)
    except Exception:
        pass
    print(f"✅ فایل .env ذخیره شد (دسترسی فقط برای کاربر فعلی: chmod 600)")

def ask(prompt, default=None):
    d = f" [پیش‌فرض: {default}]" if default is not None else ""
    v = input(f"{prompt}{d}: ").strip()
    return v if v else (default or '')

# ---------- main ----------

def banner():
    print("""
==============================================================
  🤖 راه‌اندازی ربات هایپرلیکوئید - Agent Wallet
==============================================================
""")

def generate_agent():
    """Generate a fresh EVM keypair for the agent wallet."""
    try:
        import eth_account
        from eth_account import Account
        acc = Account.create()
        return acc.address, acc.key.hex()  # 0x-prefixed address, 0x key
    except Exception:
        print("❌ کتابخانه eth-account نصب نیست. در حال نصب...")
        os.system(f'"{sys.executable}" -m pip install eth-account -q')
        from eth_account import Account
        acc = Account.create()
        return acc.address, acc.key.hex()

def read_master_address():
    print("""
────────────────────────────────────────────────────────────
  قدم ۱: آدرس کیف پول اصلی (Master) شما
────────────────────────────────────────────────────────────
  این آدرس، آدرس کیف پول Rabby شماست (همان کیف پولی که
  سرمایه را از آن به هایپرلیکوئید واریز می‌کنید).

  نحوه پیدا کردن آدرس در Rabby:
    - برنامه Rabby را باز کنید
    - روی دکمه «Copy Address» (کپی آدرس) بالای صفحه بزنید
    - آدرس به شکل 0x... است
""")
    while True:
        addr = ask("آدرس کیف پول Master را وارد کنید (0x...)", '')
        addr = addr.strip()
        if addr.startswith('0x') and len(addr) == 42:
            return addr
        print("❌ آدرس معتبر نیست. باید با 0x شروع شود و ۴۲ کاراکتر باشد. دوباره تلاش کنید.")

def main():
    banner()
    env = load_env()

    # 0) network choice
    net = ask("شبکه معاملاتی؟ (1=واقعی Mainnet ، 2=آزمایشی Testnet)", '1')
    env['HL_TESTNET'] = 'false' if net == '1' else 'true'
    if net == '2':
        print("🧪 حالت آزمایشی (Testnet) انتخاب شد - برای تست بدون ریسک")

    # 1) master address
    existing = env.get('HL_ACCOUNT_ADDRESS', '')
    if existing:
        use = ask(f"آدرس Master قبلاً ثبت شده: {existing[:10]}...\n   همان را نگه دارم؟ (y/n)", 'y')
        if use.lower() == 'n':
            env['HL_ACCOUNT_ADDRESS'] = read_master_address()
    else:
        env['HL_ACCOUNT_ADDRESS'] = read_master_address()

    # 2) agent keypair
    existing_key = env.get('HL_AGENT_PRIVATE_KEY', '')
    if existing_key:
        print(f"ℹ️  کلید Agent قبلاً ساخته شده — آن را حفظ می‌کنم.")
        import eth_account
        w = eth_account.Account.from_key(existing_key)
        agent_addr = w.address
    else:
        agent_addr, agent_key = generate_agent()
        env['HL_AGENT_PRIVATE_KEY'] = agent_key
        print(f"""
────────────────────────────────────────────────────────────
  🗝️  کیف پول Agent ربات ساخته شد
────────────────────────────────────────────────────────────
  آدرس Agent (ربات):  {agent_addr}

  این آدرس را در مرحله بعد (تایید در هایپرلیکوئید) پیست کنید.
  کلید خصوصی Agent در فایل .env ذخیره شد.
""")

    # 3) mode
    mode = ask("حالت اجرا؟ (1=فقط مجازی Paper برای هفته اول ✅ ، 2=لایو+مجازی)", '1')
    env['MODE'] = 'paper' if mode == '1' else 'live'

    # 4) telegram
    if not env.get('TG_TOKEN') or not env.get('TG_CHAT'):
        print("""
────────────────────────────────────────────────────────────
  قدم ۲: ربات تلگرام (اختیاری ولی پیشنهادی)
────────────────────────────────────────────────────────────
  - در تلگرام با @BotFather یک ربات بسازید و توکن بگیرید
  - Chat ID خود را با /start به @userinfobot بدهید
""")
        tg_token = ask("توکن تلگرام (خالی = رد شدن)", '')
        if tg_token:
            env['TG_TOKEN'] = tg_token
            tg_chat = ask("Chat ID تلگرام", '')
            env['TG_CHAT'] = tg_chat

    # 5) dashboard password
    if not env.get('DASH_PASS'):
        dp = ask("رمز داشبورد وب (حداقل ۴ کاراکتر)", '')
        if len(dp) >= 4:
            env['DASH_PASS'] = dp
        else:
            print("⚠️  رمز معتبر وارد نشد - داشبورد بدون رمز می‌ماند (فقط برای سرور خصوصی)")

    save_env(env)

    # 6) instructions
    print("""
==============================================================
  📋 مراحل تکمیل (یک بار انجام می‌دهید):
==============================================================

  گام ۱: کیف پول Rabby
    - اگر Rabby ندارید: rabby.io → دانلود اکستنشن مرورگر
    - کیف پول بسازید یا با عبارت بازیابی (Seed) وارد شوید
    - از عبارت بازیابی بکاپ بگیرید!

  گام ۲: واریز به هایپرلیکوئید
    - به app.hyperliquid.xyz بروید
    - Connect Wallet → Rabby را انتخاب و تایید کنید
    - دکمه Deposit → USDC واریز کنید (از صرافی/بریج)

  گام ۳: تایید Agent Wallet (مهم)
    - در هایپرلیکوئید: Settings → API Wallets → Add API wallet
    - آدرس Agent زیر را پیست کنید و Approve را بزنید:
""")
    print(f"      🗝️  آدرس Agent:  {agent_addr}")
    print("""
    - تایید با کیف پول Rabby شما امضا می‌شود (فقط یک بار)

  گام ۴: روشن کردن ربات
    ./install.sh      (نصب + تست سلامت)
    ./manage.sh start (روشن کردن ربات)
""")

    # 7) optional verification
    verify = ask("آیا Agent را الان تایید کرده‌اید؟ می‌خواهید اتصال را تست کنم؟ (y/n)", 'n')
    if verify.lower() == 'y':
        try:
            from hyperliquid.info import Info
            from hyperliquid.utils import constants
            info = Info(constants.TESTNET_API_URL if env.get('HL_TESTNET') == 'true' else constants.MAINNET_API_URL,
                        skip_ws=True)
            agents = info.extra_agents(env['HL_ACCOUNT_ADDRESS'])
            print("✅ پاسخ سرور:", agents)
            print("ℹ️  اگر آدرس Agent در لیست 'extraAgents' بود، تایید موفق است.")
        except Exception as e:
            print(f"⚠️  تست ناموفق بود (ممکن است هنوز تایید نکرده باشید): {e}")

    print("""
✅ راه‌اندازی کامل شد!
برای شروع:  ./manage.sh start
داشبورد:    http://آی-پی-سرور:8080
""")

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\nلغو شد.")
