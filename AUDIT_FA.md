# گزارش اصلاحات V1.2 -> V1.3

این بسته برای Hyperliquid Perpetuals ساخته شده و به‌صورت پیش‌فرض PAPER است.

## اصلاحات این نسخه

- `risk_guard.require_verified_protection` به مسیر واقعی Live SL متصل شد؛ دیگر کد امنیتی مرده نیست.
- احراز هویت داشبورد از hash ثابت مشتق‌شده از password به session token تصادفی، کوتاه‌عمر و فقط در RAM منتقل شد.
- کوکی داشبورد `HttpOnly` و `SameSite=Strict` است و در صورت فعال بودن HTTPS، `Secure` می‌شود.
- مسیر GET دیگر با وجود یک cookie نامعتبر، احراز هویت را دور نمی‌زند.
- تغییر password فقط با session معتبر یا password فعلی مجاز است و حداقل طول password جدید ۱۲ کاراکتر است.
- password در `state.json` ذخیره نمی‌شود و `.env` پس از تغییر password در سیستم‌های پشتیبانی‌شده با permission محدود ذخیره می‌شود.
- XSS داشبورد برای لاگ و داده‌های Position با escaping/DOM text nodes بسته شد.
- Dashboard به‌صورت پیش‌فرض localhost است؛ bind عمومی بدون `DASH_ALLOW_PUBLIC=true` رد می‌شود.
- هدرهای امنیتی اضافی فعال شدند.
- launcherهای Linux/macOS و systemd unit اضافه شدند.
- artefactهای runtime (`__pycache__`, `state.json`, logs, DB) از release پاک شدند.
- self-test و unit-testهای موجود دوباره اجرا شدند.

## وضعیت تست

- Python compile: PASS
- `test_risk_guard.py`: PASS
- `hyperliquid_bot.py --selftest`: 25/25 PASS

## محدودیت مهم

این تست‌ها Offline هستند و موفقیت آن‌ها به معنی تأیید Live Trading نیست. قبل از سرمایه واقعی باید Testnet/Shadow و سناریوهای failure مانند timeout، partial fill، stop trigger، restart و reconciliation با API واقعی Hyperliquid اجرا شوند.

## V1.3.1 Hotfix
- Fixed dashboard partial-close buttons by HTML-attribute-escaping JSON-encoded coin arguments before inserting them into inline handlers.
