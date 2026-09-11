# Hyperliquid Bot — V2 (Research Core)

ربات معاملاتی Hyperliquid Perpetuals با لایه اجرای امن + هسته تحقیق و بک‌تست مستقل.

## وضعیت فعلی (۲۰۲۶-۰۹-۰۴)

| بخش | وضعیت |
|---|---|
| لایه اجرا (`hyperliquid_bot.py`) | ✅ پایدار — Agent Wallet، backstop اجباری، paper-default، reconciliation |
| استراتژی قدیمی (RSI 38/62 روی 1H) | ❌ **رد شده** — روی ۳.۷ سال داده از ورود تصادفی بدتر است ([گزارش](docs/EVALUATION_REPORT_FA.md)) |
| **پرتفوی v4** (Donchian48/ADX22 + EMA15/40 لانگ + پله در 2.5R، BTC/ETH/SOL) | 🟢 **قفل شده، در پیپر** — CAGR 61٪ / DD 11.7٪ @1٪ ریسک، t 3.8 ([v4](docs/IMPROVEMENT_V4_FA.md)، [راهنمای پیپر](docs/PAPER_GUIDE_FA.md)) |
| اتصال استراتژی جدید به ربات | ⏳ مرحله بعد |
| Live | 🚫 مجاز نیست |

## ساختار

```
hyperliquid_bot.py      لایه اجرا: paper/live، مدیریت پوزیشن، dashboard، telegram  (دست نخورده)
risk_guard.py           گیت‌های سخت ریسک (آفلاین تست‌پذیر)
preflight.py            چک قبل از اجرا
setup_hyperliquid.py    ساخت Agent Wallet
titan.py / ultimate.py  ماژول‌های advisory که ربات import می‌کند (فعلاً لازم‌اند)

v2/                     هسته تحقیق جدید — مستقل از ربات، بدون کلید، بدون شبکه به جز دانلود داده
  data.py               دانلود/کش داده OKX (۳.۷ سال) و Hyperliquid (۲۰۸ روز)، resample
  indicators.py         اندیکاتورهای بدون look-ahead (RSI Wilder، ATR، ADX واقعی، Donchian، HTF trend)
  engine.py             موتور بک‌تست: ورود open بعدی، stop-first، gap، trailing، BE، ریسک‌محور، Sharpe روزانه
  strategies.py         رجیستری سیگنال‌ها (rsi_mr، donchian، ema_*، random)
  research.py           جدول مقایسه، benchmark تصادفی، سالانه، walk-forward، Monte Carlo
  stability.py          آزمون پایداری کاندید (سطح پارامتر، WF، MC، cost)
  tests.py              ۱۵ تست آفلاین موتور

research_data/          CSV داده (کش) — okx_{COIN}_1h.csv
docs/                   معماری، چک‌لیست production، گزارش‌های ارزیابی
legacy/                 بک‌تست قدیمی A/B/C و اسکریپت‌های ویندوز — فقط برای مرجع، استفاده نکن
```

## اجرا

```bash
pip install -r requirements.txt
python3 v2/tests.py                 # باید 0 failure (۲۲ تست، شامل برابری live==backtest)
python3 v2/report.py okx v4         # گزارش کامل پرتفوی v4
./run_paper.sh                      # پیپر تریدینگ v4 روی کندل‌های زنده‌ی Hyperliquid (بدون کلید)
python3 v2/research.py --tf 4h      # مقایسه استراتژی‌ها روی BTC/ETH/SOL
python3 v2/stability.py             # آزمون‌های پایداری کاندید Donchian
python3 test_risk_guard.py          # تست گیت‌های ریسک
python3 hyperliquid_bot.py --selftest
python3 hyperliquid_bot.py          # MODE=paper پیش‌فرض
```

## قوانین (تغییر نکرده)
1. فقط Hyperliquid Perpetuals. فقط Agent Wallet.
2. Live پیش‌فرض خاموش؛ نیاز به `LIVE_CONFIRM=I_UNDERSTAND_LIVE_TRADING` در process env.
3. بدون Stop-Loss تأییدشده روی exchange، Live ادامه پیدا نمی‌کند.
4. هیچ پارامتری بدون مسیر research → walk-forward → paper → approval وارد production نمی‌شود.
5. **هر بک‌تست باید کنار خودش benchmark تصادفی داشته باشد.** اگر از میانه تصادفی بهتر نیستی، لبه نداری.

## نقشه راه
- [x] ارزیابی سیستم قبلی روی داده واقعی
- [x] موتور بک‌تست مستقل و تست‌شده
- [x] ۳.۷ سال داده، ۸ خانواده استراتژی، walk-forward، MC
- [ ] تحقیق فیلتر رژیم BTC + کوین چهارم/پنجم (هدف: t-stat > 2)
- [x] `v2/live_engine.py` + `v2/paper.py` — همان موتور بک‌تست روی کندل‌های زنده (parity تست‌شده)
- [ ] ≥۶۰ روز پیپر با تطابق کامل `--expected` / `--status`
- [ ] جایگزینی `_score_signal` در ربات با سیگنال V2 + تست پریتی واقعی (بک‌تست == لایو روی همان کندل‌ها)
- [ ] Paper trading ≥ ۶۰ روز، مقایسه با بک‌تست همان بازه
- [ ] تصمیم Live فقط بعد از تکمیل `docs/PRODUCTION_CHECKLIST_FA.md`
