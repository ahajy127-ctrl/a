# 🛠️ Technical Review Guide & System Architecture (v25)

## 📋 CHANGELOG — Bug-fix pass (این نسخه)

بعد از یه بررسی چندمرحله‌ای کد، موارد زیر پیدا و اصلاح شدن:

**بحرانی (پول لایو):**
- لوریج واقعی صرافی حالا با لوریج محاسبات ریسک/SL/TP یکسانه (`nb_open_position` دیگه مستقل حساب نمی‌کنه)
- لیکویید شدن/بسته‌شدن دستی پوزیشن حالا رکورد معامله ثبت می‌کنه (`reconcile_live_positions` از `finalize_close` استفاده می‌کنه)
- آستانهٔ عمق اردربوک حالا همه‌جا یکسانه (`LIVE_MAX_BOOK_SHARE`)

**امنیتی:**
- کوکی جلسه و توکن CSRF از یه سکرت مستقل (`session_key`) میان، نه از هش رمز
- هش رمز داشبورد حالا PBKDF2 با salt تصادفی per-install (با migration خودکار از فرمت قدیمی)
- مقایسه‌های رمز/توکن با `hmac.compare_digest`
- توکن API دیگه در `state.json` به‌صورت plaintext ذخیره نمی‌شه (فقط فلگ بولی)
- فایل بکاپ `manage.sh` حالا `chmod 600` می‌گیره

**درستی داده/یادگیری:**
- جهت یادگیری فیلتر `falling_knife` در `evaluate_shadows` هم اصلاح شد (قبلاً فقط در `brain_learn_from_trade` بود)
- هشدار وین‌ریت/PF چک‌پوینت حالا روی ۶۰ معاملهٔ اخیر حساب می‌شه، نه کل تاریخچه
- Migration از v24 سرمایه رو به لجر درست (لایو/مجازی) map می‌کنه

**معماری (جزئی):**
- قفل `state_lock` دور مهم‌ترین نقطهٔ نوشتن سرمایه (`engine_exit_leg`, extension fee) اضافه شد

### ⚠️ باگ باز مونده (عمداً دست نخورده)
**بک‌تست و لایو در تولید سیگنال ورود کاملاً یکسان نیستن** (`_bt_entry_signal` در برابر `analyze_coin`/`confirm_signal`) — بک‌تست از RSI ثابت به‌جای `get_genome()` داینامیک استفاده می‌کنه و وزن‌دهی تطبیقی (`weighted()`) رو اعمال نمی‌کنه. این عمداً در این پاس دست نخورد چون نیاز به یه بازنویسی بزرگ‌تر و تست جداگانه داره (ریسک بهم‌زدن بک‌تست/بهینه‌سازی بدون تست کافی). **قبل از تکیه کردن به نتایج `auto_optimize()` برای تنظیم پارامترهای لایو، این رو در نظر داشته باشید.**

---

## خلاصه اجرایی و معماری فنی ویژه مهندس/برنامه‌نویس بررسی‌کننده
**Project:** Nobitex Algorithmic Trading Bot v25 (`nobitex_bot.py`)  
**Target Exchange:** Nobitex (Iran's leading crypto exchange — Margin/Tahodi API & Spot API)  
**Execution Environment:** Python 3.10+ (Ubuntu 24.04 / Debian / CentOS Linux VPS)  
**Dependencies:** Pure Standard Library + `requests>=2.28.0`, `numpy>=1.22.0`  

---

## 1. Executive Summary & Design Principles
This bot is an autonomous algorithmic trading engine engineered with **1:1 Parity across three execution environments**:
1. **Live Engine (`live_leg_exec`)**: Executes real leveraged orders on Nobitex Margin API (`/margin/orders/add`, `/positions/{id}/close`, `/positions/{id}/edit-collateral`).
2. **Paper Engine (`paper_leg_exec`)**: Runs concurrently with a virtual $100 ledger (`PAPER_CAPITAL`), generating out-of-sample statistical evidence.
3. **Backtest / Lab Engine (`lab_backtest` & `walk_forward_learning`)**: Uses the **exact same position management logic (`manage_engine_pos`)** on historical OHLCV data—guaranteeing zero backtest-to-live divergence.

### Security & Wallet Separation Principle
* **Margin Wallet (`type=margin`)**: Used EXCLUSIVELY for automated long/short trading.
* **Spot Wallet (`type=spot`)**: Strictly isolated for Hold/DCA assets. Ordinary margin trade entries (`nb_open_position`) **never** initiate automated Spot-to-Margin transfers. Spot balance is accessed only during extreme liquidation danger (`>= 80%` towards liquidation price) as a capped collateral spare tire (`emergency_collateral_rescue`), with a hard settle (`kill_switch`) at `75%` to prevent spot wallet drain.

---

## 2. Quantitative Engine & 6-Layer Technical Architecture

### Layer 1: Signal & Scoring Engine (`analyze`, `VSA`)
* **Whale Flow (`whale_flow`, +2 score)**: Detects volume > 3x mean with candle body spread < 0.8% (VSA silent accumulation/distribution).
* **Volume Climax (`volume_climax`, +1 score)**: Detects exhaustion volume > 4x mean in overbought/oversold RSI zones.
* **Falling Knife Guard (`falling_knife`, -3 score)**: Blocks entries on 3 sequential red/green dumps/pumps (>0.7% per candle).
* **Dead-Zone SMA Filter**: Requires `(SMA7 - SMA20) / SMA20 > 0.15%` separation to validate trend signals.
* **Orderbook Spoofing Filter**: Ignores orderbook imbalance if total orderbook depth value < $2,000 (`tot_val < 2000`).

### Layer 2: Capital Management, Sizing & Risk Engine (`open_engine_position`, `kelly_risk`)
* **Dynamic Half-Kelly Sizing**: Computes optimal risk fraction `k = (wr - (1-wr)/payoff) * 0.5` bounded within `[5%, 15%]` of equity from the last 30 trades.
* **Score & Regime Multiplier**:
  * Score `>= threshold + 2` $\rightarrow$ **1.3x** size boost; Score `< threshold + 1` $\rightarrow$ **0.7x** reduction.
  * High volatility regime $\rightarrow$ **0.7x** size; Storm regime $\rightarrow$ **0.5x** size.
* **Max Total Portfolio Risk Cap (`MAX_TOTAL_RISK = 0.25`)**: Total combined margin used across all open trades cannot exceed 25% of total equity (retaining 75% free equity cushion).
* **1:1 Parity Leverage Cap (`MAX_LEV = 5.0`)**: Automatically scales leverage down to **3.0x** during high-volatility regimes (`eff_leverage`).
* **Consecutive Loss Drawdown Brake**: After 2 consecutive losses, position size reduces to **0.8x** (-20%); after 3+ losses, reduces to **0.6x** (-40%).
* **Orderbook Clamping**: Automatically clamps trade margin down to 15% of orderbook depth (`book_val * 0.15`) if liquidity is thin.
* **Execution & Slippage Protection**:
  * Protects Market orders using `price * 1.025` for buys and `price * 0.975` for sells.
  * High-Priority Close Retry Queue: Retries failed close orders (`nb_close_order`) immediately up to 3 times (3-second intervals) before backoff.
  * Daily Extension Fee Accounting: Reconciles `0.1%/day` (`LIVE_EXT_FEE_DAILY`) liability fees in `sync_live_capital`.

### Layer 3: Safety Guards & Watchdogs (`crash_guard`, `checkpoint_guard`, `watchdog`)
* **Dual-Horizon Crash Guard**: Triggers if BTC drops `< -7%` in 7 days OR `< -5%` in 24 hours, locking out new entries while managing open positions.
* **Max Drawdown Checkpoint 3**: Triggers if Max Drawdown exceeds **12% (`dd >= 12.0`)** after trade 10.
* **Checkpoint Auto-Recovery**: Automatically unlocks a paused ledger if recent 10 trades achieve Win Rate `>= 40%` and Profit Factor `>= 1.30`.
* **Stale Feed Watchdog**: Alerts and flags system health if price feed is stale for `> 900s` (15 min).

### Layer 4: Unified 5-Tier Smart Exit Engine (`manage_engine_pos`)
All engines share `manage_engine_pos(lg, pos, price, ts, leg_exec, quiet)`:
1. **Half-Cash (`be_trig` @ 40% TP distance)**: Cashes out **50%** of initial position.
2. **Partial TP (`partial_trig` @ 50% TP distance)**: Cashes out **40%** of remaining position.
3. **Breakeven Floor Lock**: Moves Stop-Loss to Breakeven (`1.002` / `0.998`) as soon as gain reaches `+1.2%` (`gain >= 0.012`), ensuring zero capital loss.
4. **Wider Trailing Breathing Room**: Maintains a wider `1.2%` trailing buffer (`cur_trail_gap = 0.012`) before reaching `+2.5%` gain to prevent premature shakeouts on minor dips, and tightens to `0.4%` (`cur_trail_gap = 0.004`) once gain reaches `+2.5%`.
5. **Runner Mode (`take_profit`)**: Cashes out **60%**, leaves **40%** as a runner with `RUNNER_TRAIL = 0.5%`.
6. **Time-Decay Breakeven Ratchet**: Automatically moves Stop-Loss to breakeven (`1.002`/`0.998`) if a trade stays open `> 12h` in profit without breaking out.
7. **Parabolic Spike Lock**: Tightens runner trail to **0.2%** during extreme parabolic spikes (`gain >= +4%`).

### Layer 5: Strategy Lab & Walk-Forward Learning (`auto_optimize`, `update_coin_filter`)
* **70/30 In-Sample/Out-of-Sample Walk-Forward Optimizer**: Evaluates 18 combinations of SL, TP, and threshold across 30 days of hourly candles for BTC, SOL, and DOGE. Only adopts parameter sets that pass both In-Sample and Out-of-Sample validation (`oos_return > 0.5%`, `trades >= 2`).
* **Daily Coin Filter**: Evaluates 30-day historical returns and Profit Factor (`PF >= 0.75`) for all whitelisted coins every 24 hours, benching underperforming altcoins.
* **Adaptive Threshold Scaling**: Automatically raises entry threshold (`+1 score`) if recent 6 paper trades show win rate `< 33%`.

### Layer 6: Spot DCA & Hold Analyzer (`analyze_hold`, `dca_engine`)
* **200-Day Macro Hold Analysis**: Compares USDT/Toman rate against 20d/90d MAs and 200d percentiles.
* **Virtual Spot DCA Simulator**: Simulates multi-tranche DCA buys on weekly dip signals (`<= -5%`) while leaving real Spot wallet balance 100% untouched.

---

## 3. How to Verify & Audit in 60 Seconds
To verify all calculations, unit conversions, fee deductions, and exit logic:
```bash
python3 nobitex_bot.py --selftest
```
Expected output:
```text
SELFTEST: 32 passed, 0 failed
```

### Key Endpoints & Telemetry
* **Web Dashboard**: Available on port `8080`.
* **Health Page**: Visit `/diagnostics` for real-time 6-System red/green health indicators, API verification, and latency metrics.
* **CLI Manager**: `./manage.sh status` or `./manage.sh test`.

---

## 4. Peer-Review Audit Resolution (August 11, 2026)
Following a quantitative engineering code audit, the following 5 real-market safety optimizations were implemented and verified (`30/30 selftests passing`):
1. **`live_preflight` Dashboard Password Check Fix**: Updated from `state.get('dash_pass')` to `dash_secret()`, properly recognizing hashed passwords stored in `dash_pass_hash`.
2. **Emergency Collateral Rescue Systemic Risk Control**: Added `ENABLE_COLLATERAL_RESCUE` environment toggle (defaulting to **OFF / False**), eliminating automated average-down systemic risk while preserving normal stop-loss discipline.
3. **Conservative Capital & Risk Scaling**: Halved default real-money exposure: `RISK_PER_TRADE = 8%` (from 15%), Kelly risk ceiling = **15%** (from 25%), and total portfolio margin cap `MAX_TOTAL_RISK = 25%` (from 48%).
4. **API Token Plaintext Security**: Added `nb_token()` supporting `NOBITEX_TOKEN` environment precedence (`.env`), allowing zero-plaintext API token storage in `state.json` with strict `0600` permissions.
5. **Anti-Overfitting OOS Gate**: Strengthened Walk-Forward Out-of-Sample validation in `auto_optimize()` to require `t_tr >= 5` unseen trades and `t_ret > 1.0%` return.

---

## 5. Live VPS Telemetry Audit Resolution (August 11, 2026)
Following live VPS dashboard analysis (`Scan #175`, `UNI +$0.2843 banked`), 3 architectural refinements were deployed:
1. **Regime Reversal Breakeven Protection (`manage_engine_pos`)**: Open trades in an opposite macro regime (`long` in `trend_down` or `short` in `trend_up`) automatically activate trailing stop and raise Stop-Loss to Breakeven (`entry * 1.002` / `0.998`) once gain reaches `+0.8%`.
2. **Chunked Bulk Price Request + Individual Fallback (`get_all_prices_light`)**: Batched active coin price queries into chunks of 8 coins and added individual fallback queries (`r_ind`), preventing URL/query truncation and curing TON/SHIB false bench loops.
3. **Falling Knife Dead-Code Fix (`analyze`)**: Corrected candle history request from `count=4` to `count=5` when `drop_forming=True`, ensuring 4 closed candles remain to evaluate 3 consecutive dumps (`range(1, 4)`).

---

## 6. Airtight Macro Holding Pool & Slot Bundling (August 12, 2026)
To extend stagnant trades up to 4 days (`96h`) without causing slot paralysis or over-leveraging, 4 quantitative rules govern `eff_positions_count`:
1. **Directional Clamping (`max 3 longs, max 3 shorts`)**: Even when old trades free up a numeric slot, total directional exposure across old + new trades is capped at 3 per side.
2. **Hard Physical Position Cap (`max 6 trades`)**: Regardless of how many trades enter the >24h holding pool, total open trades (`len(lg['positions'])`) can never exceed 6.
3. **Dynamic Pool Margin Sizing (`overtime_count >= 2`)**: When 2 or more >24h trades are in the holding pool, new breakout entries automatically use **75% size** (`margin *= 0.75`).
4. **72h Stagnant Drift Kill-Switch**: Stagnant trades below breakeven (`cur_gain_now < 0.0`) are automatically closed at 72 hours; only breakeven/positive trades extend to 96h.

---

## 7. Global Market Sessions — Teachable Liquidity Filter (August 17, 2026)
Crypto is 24/7 but TradFi liquidity still drives it. A teachable `session` factor was added:

**Sessions (Tehran time):** `asia 03:30-11:30 🌙` → `europe 11:30-17:30 🇪🇺` → `overlap 17:30-21:30 🔥` (Europe+US golden) → `us 21:30-01:30 🇺🇸` → `quiet 01:30-03:30 😴`

**How it learns:** New `FACTOR_KEYS='session'` with `weighted()` and `brain_summary_fa()` entry `جلسه معاملاتی 🌍`. In `confirm_signal` → `apply_regime` → `apply_session`:
- `quiet` (-1) — almost no global flow, stricter
- `asia` (-0.5 if score<5) — low volume
- `europe/us` (+0.5 via `weighted`) — real money
- `overlap` (+1 if score>=5 else +0.5 via `weighted`) — peak liquidity

Every trade stores `snapshot['session']` and `factors` may include `'session'`, so `brain_learn_from_trade` and `evaluate_shadows` (both now correctly invert `falling_knife`) also learn `session` weights adaptively (`0.5-1.5`). Dashboard `regime_card` now shows live `جلسه جهانی` label+hour. Backtest intentionally omits session (no historical hour attribution) — documented as data limitation.

**Result:** Bot now knows *when* a signal fired, not just *what* fired, and will gradually up-weight the 17:30-21:30 overlap that produced `PF=0.66→` short outperformance in your 27-trade CSV, and down-weight low-liquidity Asia nights.

---

## 8. Tiny Bug Hunt — Final Polish (August 17, 2026)
Micro-fixes after full 4627-line audit, all verified `32/32`:
- `state_lock` → `RLock()` to prevent deadlock when `save_state()` (which snapshots `json.dumps(state)` inside `state_lock`) is called from dashboard handlers already holding the lock.
- `apply_session` now `weighted()` for **all** sessions (including `quiet`/`asia` penalties) and always records `'session'` factor so `brain_learn`/`shadow` can actually learn session quality (previously quiet/asia penalty was untracked).
- `session_info()` uses single `fa_now()` snapshot to avoid 00:59→01:00 race.
- `install.sh` message corrected `30` → `32` tests.
- `open_engine_position` snapshot now includes `session` for post-trade analytics.
