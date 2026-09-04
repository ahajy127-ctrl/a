# Hyperliquid Final V1

نسخه ترکیبی چهار پروژه با یک Execution Layer واحد برای **Hyperliquid Perpetuals**.

## اجرا
1. Python 3.10+ نصب کنید.
2. محیط مجازی بسازید و `pip install -r requirements.txt` بزنید.
3. `.env.example` را به `.env` کپی کنید.
4. پیش‌فرض `MODE=paper` است.
5. برای تست‌های آفلاین: `python test_risk_guard.py`
6. برای اجرای ربات: `python hyperliquid_bot.py`
7. برای self-test داخلی: `python hyperliquid_bot.py --selftest`

## Live
Live فقط بعد از تکمیل `PRODUCTION_CHECKLIST_FA.md` توصیه می‌شود. از Agent Wallet استفاده کنید، نه Master private key.

## Research
`research_lab.py` یک Monte Carlo مستقل از Exchange روی CSV معاملات اجرا می‌کند:
`python research_lab.py trades.csv --runs 5000`

### نکته
این نسخه با هدف «پایه مهندسی بهتر» ساخته شده است. قبل از پول واقعی باید روی Testnet/Shadow و سپس سرمایه بسیار کم، رفتار واقعی سفارش‌ها و stopها به‌صورت عملی تایید شود.
