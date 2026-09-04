# چک‌لیست قبل از Live

- [ ] Agent Wallet جدا از Master Wallet ساخته شده است.
- [ ] Private key فقط در `.env`/Secret Store است و در log/state ذخیره نمی‌شود.
- [ ] `DASH_PASS` قوی و غیرپیش‌فرض است.
- [ ] حداقل 100 معامله Paper با منطق فعلی ثبت شده است.
- [ ] Backtest و Walk-forward انجام شده است.
- [ ] Monte Carlo drawdown بررسی شده است.
- [ ] Shadow Live حداقل چند روز بدون orphan/duplicate order اجرا شده است.
- [ ] قطع اینترنت، restart، timeout و partial fill تست شده است.
- [ ] Stop روی Exchange ایجاد و از Exchange قابل مشاهده/تایید است.
- [ ] اگر Stop نصب نشود، position وارد حالت emergency می‌شود و ورود جدید متوقف می‌شود.
- [ ] Reconciliation بعد از restart تست شده است.
- [ ] Kill switch با تایید وضعیت واقعی Exchange تست شده است.
- [ ] Live با سرمایه کم شروع می‌شود.

این پروژه «ابزار معاملاتی» است، نه تضمین سود. هیچ تنظیمی به معنی تضمین عملکرد یا ایمنی مالی نیست.


## Gateهای اجباری V1.1
- [ ] Agent approval با موفقیت Verify شده باشد؛ حالت Unknown مجاز نیست.
- [ ] بعد از هر ورود Live، Position و Entry واقعی از Exchange تأیید شود.
- [ ] Stop Trigger با frontendOpenOrders دیده و Verify شود.
- [ ] قبل از لغو Stop قدیمی، Stop جدید Verify شده باشد.
- [ ] Close فقط بعد از Verify شدن تغییر Position روی Exchange نهایی شود.
- [ ] در Close failure هیچ‌وقت Position محلی به‌صورت مصنوعی بسته نشود.
- [ ] state.json فاقد TG token، Dashboard password و Private Key باشد.
- [ ] Testnet: Restart / timeout / duplicate retry / partial fill / stop trigger / kill switch تست شده باشد.
- [ ] Shadow Live بدون ارسال سفارش واقعی حداقل یک دوره کامل اجرا شده باشد.
