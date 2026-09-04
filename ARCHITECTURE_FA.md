# معماری نسخه نهایی V1 — Hyperliquid Only

## انتخاب پایه
هسته این نسخه از `hyperliquid-bot-windows` گرفته شده است.

## اجزای ترکیبی
- Hyperliquid Windows: execution، reconciliation، position management، dashboard و deployment
- V25: ایده‌های risk/learning/regime/kelly که مستقل از Nobitex هستند
- Trading Bot: research، Monte Carlo و A/B isolation به شکل offline
- V28: Titan/Ultimate و تست‌های مفید

## قوانین قطعی
1. فقط Hyperliquid Perpetuals.
2. Master private key وارد برنامه نمی‌شود؛ فقط Agent Wallet.
3. Live به صورت پیش‌فرض خاموش است.
4. بدون Stop-Loss تاییدشده روی Exchange، Live نباید ادامه پیدا کند.
5. State و fills صرافی مرجع reconciliation هستند.
6. Retry سفارش باید idempotent باشد و قبل از ارسال مجدد وضعیت سفارش/پوزیشن بررسی شود.
7. Paper/Shadow/Live از نظر داده و ledger از هم جدا می‌مانند.
8. یادگیری آنلاین پارامترهای production ممنوع است؛ تغییرات باید از مسیر research → walk-forward → shadow → approval عبور کنند.
