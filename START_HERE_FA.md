# شروع امن Hyperliquid Final V1.4

این نسخه فقط برای Hyperliquid Perpetuals است.

## ترتیب اجرا

1. محیط مجازی و وابستگی‌ها را نصب کنید.
2. `python setup_hyperliquid.py` را اجرا کنید.
3. Agent Wallet ساخته‌شده را در Hyperliquid تأیید کنید.
4. `python preflight.py` را اجرا کنید.
5. ابتدا با `MODE=paper` کار کنید.
6. سپس Shadow/Testnet را بررسی کنید.
7. برای Live، چک‌لیست `PRODUCTION_CHECKLIST_FA.md` را کامل کنید.

## Live

Live عمداً با یک تأیید صریح process-level محافظت شده است:

`LIVE_CONFIRM=I_UNDERSTAND_LIVE_TRADING`

این مقدار را در `.env` ذخیره نکنید. کلید Master/Seed هرگز نباید وارد Bot شود؛ فقط Agent Wallet.

## نکته

این بسته «آماده شروع تست و استقرار» است، اما بدون اجرای سناریوهای واقعی روی Testnet نباید سرمایه واقعی وارد شود.
