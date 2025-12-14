# ============================================
# Section 5) Telegram Handlers & Commands (Tracks + Slots + 2% rule)
#  - كل الأوامر من حساب: Mohamad4992
#  - Email Gate: gate/off/gate open/gate close
#  - Pause/Reuse: pause / reuse
#  - Status:
#       • status        → حالة البوت + الصفقات + إحصائيات
#       • summary       → ملخّص الربح/الخسارة الإجمالي
#       • track         → ملخص كل المسارات
#       • track <n>     → تفاصيل مسار واحد
#       • verlauf       → سجل كامل لكل الصفقات (TRADES_FILE)
#  - Capacity:
#       • cycle slots         → عرض القيمة الحالية
#       • cycle slots <N>     → تغيير الحد الأقصى للصفقات المفتوحة
#       • slots               → عرض عدد الـ Slots المفتوحة/المتاحة
#  - Blacklist:
#       • Add <SYMBOL> / Remove <SYMBOL> / Status List
#  - Manual SELL:
#       • sell <index>        (من القائمة المرقّمة في status)
#       • sell <symbol>       (بيع أو إلغاء جميع الصفقات على هذا الرمز)
#  - Risk:
#       • risk ...            (يمرر النص إلى handle_risk_command إن وُجدت)
#  - Debug:
#       • debug funds on/off/<N>m
# ============================================

import os
import re
import json
import time
import asyncio
from datetime import datetime, timezone, date, timedelta
from typing import Any, Dict, List, Optional, Tuple, Set

# ===== Telethon events =====
try:
    from telethon import events
except Exception:
    events = None

# ===== ملفات أساسية =====
TRADES_FILE = globals().get("TRADES_FILE", "trades.json")
SUMMARY_FILE = globals().get("SUMMARY_FILE", "summary.json")
TERMINAL_LOG_FILE = globals().get("TERMINAL_LOG_FILE", "terminal_notices.json")

# ===== Telegram message splitter =====
TELEGRAM_MSG_LIMIT = 4000  # حد آمن لرسائل تيليجرام

# ===== console_echo alias =====
try:
    console_echo  # type: ignore[name-defined]
except Exception:
    def console_echo(msg: str) -> None:
        try:
            if bool(globals().get("ENABLE_CONSOLE_ECHO", False)):
                print(msg)
        except Exception:
            pass

_console_echo = console_echo

# ===== دوال إشعارات عامة (من Section 1) =====
send_notification = globals().get("send_notification")
send_notification_tc = globals().get("send_notification_tc")

# ===== Helpers عامة من الأقسام السابقة =====
get_trade_structure = globals().get("get_trade_structure")
save_trade_structure = globals().get("save_trade_structure")
normalize_symbol = globals().get("normalize_symbol") or (lambda s: (s or "").upper().replace('-', '').replace('/', ''))
fetch_current_price = globals().get("fetch_current_price")

is_email_gate_open = globals().get("is_email_gate_open")
set_email_gate = globals().get("set_email_gate")
should_accept_recommendations = globals().get("should_accept_recommendations")

is_bot_active = globals().get("is_bot_active")
set_bot_active = globals().get("set_bot_active")

add_to_blacklist = globals().get("add_to_blacklist")
remove_from_blacklist = globals().get("remove_from_blacklist")
list_blacklist = globals().get("list_blacklist")

enable_debug_funds = globals().get("enable_debug_funds")
disable_debug_funds = globals().get("disable_debug_funds")
is_debug_funds = globals().get("is_debug_funds")

classify_pnl = globals().get("classify_pnl")
register_trade_outcome = globals().get("register_trade_outcome")

handle_risk_command = globals().get("handle_risk_command")

# من Section 4 (يُستخدم مع البيع اليدوي)
quantize_down = globals().get("quantize_down")
get_symbol_meta = globals().get("get_symbol_meta")
get_trade_balance_usdt = globals().get("get_trade_balance_usdt")
place_market_order = globals().get("place_market_order")
get_order_deal_size = globals().get("get_order_deal_size")
is_simulation = globals().get("is_simulation")

# نفس الثوابت من Section 2/4
CYCLE_SLOTS_DEFAULT = int(globals().get("CYCLE_SLOTS", 10))
MAX_TRACKS = int(globals().get("MAX_TRACKS", 10))

# ===== الحساب الذي تُستقبل منه الأوامر =====
COMMAND_CHAT = globals().get("COMMAND_CHAT", "Mohamad4992")

# ========= دوال مساعدة للرسائل الطويلة =========
async def _send_long_message(text: str, part_title: str = None, limit: int = TELEGRAM_MSG_LIMIT):
    if text is None:
        return
    if len(text) <= limit:
        if callable(send_notification):
            await send_notification(text)
        _console_echo(text)
        return

    parts, chunk = [], ""
    for line in text.splitlines(True):
        if len(chunk) + len(line) > limit:
            parts.append(chunk.rstrip())
            chunk = line
        else:
            chunk += line
    if chunk:
        parts.append(chunk.rstrip())

    total = len(parts)
    title_prefix = (part_title + " — ") if part_title else ""
    for i, p in enumerate(parts, 1):
        header = f"{title_prefix}(Part {i}/{total})\n"
        msg = header + p
        if callable(send_notification):
            await send_notification(msg)
        _console_echo(msg)

# ===== Summary accumulation (PnL) =====
def accumulate_summary(profit_delta: float = 0.0, loss_delta: float = 0.0) -> None:
    try:
        data = {"total_profit": 0.0, "total_loss": 0.0, "net": 0.0}
        if os.path.exists(SUMMARY_FILE):
            try:
                with open(SUMMARY_FILE, "r") as f:
                    loaded = json.load(f)
                data["total_profit"] = float(loaded.get("total_profit", 0.0) or 0.0)
                data["total_loss"] = float(loaded.get("total_loss", 0.0) or 0.0)
            except Exception:
                pass

        if profit_delta and profit_delta > 0:
            data["total_profit"] += float(profit_delta)
        if loss_delta and loss_delta > 0:
            data["total_loss"] += float(loss_delta)

        data["net"] = data["total_profit"] - data["total_loss"]
        with open(SUMMARY_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"⚠️ accumulate_summary error: {e}")

async def show_trade_summary():
    summary = {"total_profit": 0.0, "total_loss": 0.0, "net": 0.0}
    try:
        if os.path.exists(SUMMARY_FILE):
            with open(SUMMARY_FILE, "r") as f:
                loaded = json.load(f)
            summary["total_profit"] = float(loaded.get("total_profit", 0.0) or 0.0)
            summary["total_loss"] = float(loaded.get("total_loss", 0.0) or 0.0)
        else:
            with open(SUMMARY_FILE, "w") as f:
                json.dump(summary, f, indent=2)
    except Exception as e:
        if callable(send_notification):
            await send_notification(f"⚠️ Summary read error: {e}")
    summary["net"] = summary["total_profit"] - summary["total_loss"]
    if callable(send_notification):
        await send_notification(
            "📊 Profit & Loss Summary:\n"
            f"💰 Total Profit: {summary['total_profit']:.2f} USDT\n"
            f"📉 Total Loss: {summary['total_loss']:.2f} USDT\n"
            f"📊 Net profit : {summary['net']:.2f} USDT"
        )

# ===== Berlin timezone helpers =====
def _berlin_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/Berlin")
    except Exception:
        return timezone.utc

def _dow_short(dt_local: datetime) -> str:
    return ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"][dt_local.weekday()]

def _fmt_berlin(ts: Optional[float]) -> str:
    if ts is None:
        return "—"
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(_berlin_tz())
        return f"{_dow_short(dt)} {dt.strftime('%d/%m--%H:%M')}"
    except Exception:
        return "—"

def _safe_ts_to_datestr(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).date().isoformat()
    except Exception:
        return ""

# ===== Email Gate status =====
async def show_gate_status():
    try:
        is_open = bool(is_email_gate_open()) if callable(is_email_gate_open) else True
    except Exception:
        is_open = True
    label = "OPEN ✅ (accepting recommendations)" if is_open else "CLOSED ⛔️ (paused; ignoring recommendations)"
    extra = "\nTrigger words via email: ‘buy crypto’ → OPEN, ‘sell crypto’ → CLOSE"
    if callable(send_notification):
        await send_notification(f"📧 Email Gate status: {label}{extra}")

# ========== STATUS INDEX MAP (index -> (symbol, track, slot)) ==========
_STATUS_INDEX_MAP: Dict[int, Tuple[str, int, str]] = {}
_STATUS_REV_INDEX_MAP: Dict[Tuple[str, int, str], int] = {}

def _load_trades_cache() -> List[Dict[str, Any]]:
    if not os.path.exists(TRADES_FILE):
        return []
    try:
        with open(TRADES_FILE, "r") as f:
            data = json.load(f) or {}
        return data.get("trades", []) or []
    except Exception:
        return []

def _find_latest_trade_for_slot(trades: List[Dict[str, Any]], sym: str, track_num: int, slot_id: str) -> Optional[Dict[str, Any]]:
    sym_norm = normalize_symbol(sym)
    latest = None
    latest_ts = -1.0
    for tr in trades:
        try:
            if normalize_symbol(tr.get("symbol")) != sym_norm:
                continue
            if int(tr.get("track_num", 0)) != int(track_num):
                continue
            if str(tr.get("slot_id")) != str(slot_id):
                continue
            ts = float(tr.get("opened_at", 0) or 0.0)
            if ts >= latest_ts:
                latest_ts = ts
                latest = tr
        except Exception:
            continue
    return latest

def _rebuild_status_index_map():
    global _STATUS_INDEX_MAP, _STATUS_REV_INDEX_MAP
    _STATUS_INDEX_MAP = {}
    _STATUS_REV_INDEX_MAP = {}

    if not callable(get_trade_structure):
        return

    structure = get_trade_structure()
    slots = structure.get("slots") or {}
    trades = _load_trades_cache()

    open_list: List[Tuple[str, int, str, float]] = []  # (sym, track, slot, opened_ts)
    buy_list: List[Tuple[str, int, str, float]] = []   # (sym, track, slot, opened_ts)

    for sid, cell in slots.items():
        if not cell:
            continue
        st = (cell.get("status") or "").lower()
        sym = normalize_symbol(cell.get("symbol"))
        if not sym:
            continue
        track_num = int(cell.get("track_num", 0) or 0)
        if track_num <= 0:
            continue

        tr = _find_latest_trade_for_slot(trades, sym, track_num, sid)
        opened_ts = float(tr.get("opened_at", time.time())) if tr else time.time()

        if st in ("open", "reserved"):
            open_list.append((sym, track_num, str(sid), opened_ts))
        elif st == "buy":
            buy_list.append((sym, track_num, str(sid), opened_ts))

    open_list_sorted = sorted(open_list, key=lambda x: (x[0], x[1], int(x[2])))
    buy_list_sorted = sorted(buy_list, key=lambda x: (x[0], x[1], int(x[2])))

    idx = 1
    for sym, track_num, sid, ts in open_list_sorted:
        _STATUS_INDEX_MAP[idx] = (sym, track_num, sid)
        _STATUS_REV_INDEX_MAP[(sym, track_num, sid)] = idx
        idx += 1
    for sym, track_num, sid, ts in buy_list_sorted:
        _STATUS_INDEX_MAP[idx] = (sym, track_num, sid)
        _STATUS_REV_INDEX_MAP[(sym, track_num, sid)] = idx
        idx += 1

# ============ STATUS (global) ============
async def show_bot_status():
    if not callable(get_trade_structure):
        if callable(send_notification):
            await send_notification("❌ Internal error: trade structure helpers not available.")
        return

    today = date.today().isoformat()
    structure = get_trade_structure()
    slots = structure.get("slots") or {}
    cycle_slots = int(structure.get("cycle_slots", CYCLE_SLOTS_DEFAULT))

    trades = _load_trades_cache()

    # ---- Counters من TRADES_FILE ----
    total_overall = len(trades)
    overall_tp       = sum(1 for tr in trades if (tr.get("status") or "").lower() == "closed")
    overall_sl       = sum(1 for tr in trades if (tr.get("status") or "").lower() == "stopped")
    overall_drawdown = sum(1 for tr in trades if (tr.get("status") or "").lower() == "drwn")
    overall_failed   = sum(1 for tr in trades if (tr.get("status") or "").lower() == "failed")

    latest_opened_date: Dict[str, str] = {}
    for tr in trades:
        sym = normalize_symbol(tr.get("symbol"))
        if not sym:
            continue
        d = _safe_ts_to_datestr(tr.get("opened_at"))
        if d:
            prev = latest_opened_date.get(sym)
            if (not prev) or (d > prev):
                latest_opened_date[sym] = d

    today_total = sum(1 for tr in trades if _safe_ts_to_datestr(tr.get("opened_at")) == today)

    # ---- open / buy slots ----
    open_slots: List[Tuple[str, int, str, float]] = []
    buy_slots: List[Tuple[str, int, str, float]] = []

    def _latest_open_ts_for(sym: str, track_num: int, slot_id: str) -> Optional[float]:
        tr = _find_latest_trade_for_slot(trades, sym, track_num, slot_id)
        return float(tr.get("opened_at", time.time())) if tr else None

    open_syms: List[str] = []
    buy_syms: List[str] = []

    for sid, cell in slots.items():
        if not cell:
            continue
        st = (cell.get("status") or "").lower()
        sym = normalize_symbol(cell.get("symbol"))
        if not sym:
            continue
        track_num = int(cell.get("track_num", 0) or 0)
        if track_num <= 0:
            continue

        if st in ("open", "reserved"):
            ts_open = _latest_open_ts_for(sym, track_num, str(sid)) or time.time()
            open_slots.append((sym, track_num, str(sid), ts_open))
            open_syms.append(sym)
        elif st == "buy":
            ts_buy = _latest_open_ts_for(sym, track_num, str(sid)) or time.time()
            buy_slots.append((sym, track_num, str(sid), ts_buy))
            buy_syms.append(sym)

    overall_open = len(open_slots)
    overall_buy  = len(buy_slots)

    today_open   = sum(1 for sym in set(open_syms) if latest_opened_date.get(sym) == today)
    today_buy    = sum(1 for sym in set(buy_syms)  if latest_opened_date.get(sym) == today)

    # ---- اليوم (realized) ----
    tp_today = sl_today = failed_today = drwn_today = 0
    tp_today_entries: List[str] = []
    sl_today_entries: List[str] = []
    failed_today_entries: List[str] = []
    drwn_today_entries: List[str] = []

    def _fmt_line(tr: Dict[str, Any]) -> str:
        sym = normalize_symbol(tr.get("symbol"))
        track_num = int(tr.get("track_num", 0) or 0)
        slot_id = str(tr.get("slot_id") or "?")
        return f"• {sym} — Track {track_num} | Slot {slot_id}"

    for tr in trades:
        st = (tr.get("status") or "").lower()
        closed_d = _safe_ts_to_datestr(tr.get("closed_at"))
        if closed_d != today:
            continue
        if st == "closed":
            tp_today += 1
            tp_today_entries.append(_fmt_line(tr))
        elif st == "stopped":
            sl_today += 1
            sl_today_entries.append(_fmt_line(tr))
        elif st == "failed":
            failed_today += 1
            failed_today_entries.append(_fmt_line(tr))
        elif st == "drwn":
            drwn_today += 1
            drwn_today_entries.append(_fmt_line(tr))

    used_now = overall_open + overall_buy
    free_now = max(0, cycle_slots - used_now)

    def _safe_pct(num: int, den: int) -> float:
        try:
            den = int(den)
            if den <= 0:
                return 0.0
            return (float(num) / float(den)) * 100.0
        except Exception:
            return 0.0

    realized_total = overall_tp + overall_sl + overall_drawdown
    tp_pct   = _safe_pct(overall_tp, realized_total)
    sl_pct   = _safe_pct(overall_sl, realized_total)
    drw_pct  = _safe_pct(overall_drawdown, realized_total)

    # ---- بنية index map للـ sell <index> ----
    open_sorted = sorted(open_slots, key=lambda x: (x[0], x[1], int(x[2])))
    buy_sorted  = sorted(buy_slots,  key=lambda x: (x[0], x[1], int(x[2])))

    global _STATUS_INDEX_MAP, _STATUS_REV_INDEX_MAP
    _STATUS_INDEX_MAP = {}
    _STATUS_REV_INDEX_MAP = {}

    idx = 1
    for sym, track_num, sid, ts in open_sorted:
        _STATUS_INDEX_MAP[idx] = (sym, track_num, sid)
        _STATUS_REV_INDEX_MAP[(sym, track_num, sid)] = idx
        idx += 1
    for sym, track_num, sid, ts in buy_sorted:
        _STATUS_INDEX_MAP[idx] = (sym, track_num, sid)
        _STATUS_REV_INDEX_MAP[(sym, track_num, sid)] = idx
        idx += 1

    # ---- Gate state ----
    try:
        gate_txt = "OPEN ✅" if callable(is_email_gate_open) and is_email_gate_open() else "CLOSED ⛔️"
    except Exception:
        gate_txt = "OPEN ✅"

    lines: List[str] = [
        "📊 Bot Status:",
        f"✅ Running: {'Yes' if (callable(is_bot_active) and is_bot_active()) else 'No'}",
        f"📧 Email Gate: {gate_txt}",
        f"📦 Slots: used {used_now} / limit {cycle_slots} → free {free_now}",
        "",
        f"📈 Today: {today_total} total signals",
        f" — open: {today_open} | buy: {today_buy} | 🏆 TP: {tp_today} | ❌ SL: {sl_today} | 📉 DRWDN: {drwn_today} | ⚠️ Failed: {failed_today}",
        "",
        f"📈 Overall trades: {total_overall}",
        (
            f" open: {overall_open} | buy: {overall_buy} | "
            f"🏆 TP: {overall_tp} ({tp_pct:.2f}%) | "
            f"❌ SL: {overall_sl} ({sl_pct:.2f}%) | "
            f"📉 DRWDN: {overall_drawdown} ({drw_pct:.2f}%) | "
            f"⚠️ Failed: {overall_failed}"
        ),
        "",
        "📜 Open Slots:",
    ]

    i = 1
    if open_sorted:
        for sym, track_num, sid, ts in open_sorted:
            ts_fmt = _fmt_berlin(ts)
            lines.append(f"• {i}. {ts_fmt} {sym} — Track {track_num} | Slot {sid}")
            i += 1
    else:
        lines.append("• (none)")

    lines.extend(["", "📜 Buy Slots:"])
    if buy_sorted:
        for sym, track_num, sid, ts in buy_sorted:
            ts_fmt = _fmt_berlin(ts)
            # حاول إظهار price / Δ%
            bought_price = None
            now_price = None
            pct_str = "—"
            try:
                # من TRADES_FILE
                tr = _find_latest_trade_for_slot(trades, sym, track_num, sid)
                if tr and tr.get("bought_price") is not None:
                    bought_price = float(tr["bought_price"])
                now_price = await fetch_current_price(sym) if callable(fetch_current_price) else None
                if bought_price and now_price:
                    pct = ((float(now_price) - bought_price) / bought_price) * 100.0
                    pct_str = f"{pct:+.2f}%"
            except Exception:
                pass
            bp_str = f"{bought_price:.6f}" if bought_price else "—"
            now_str = f"{now_price:.6f}" if now_price else "N/A"
            lines.append(f"• {i}. {ts_fmt} {sym} — Track {track_num} | Slot {sid}  {bp_str} → now {now_str} / Δ {pct_str}")
            i += 1
    else:
        lines.append("• (none)")

    # ---- اليوم realized ----
    lines.extend(["", "✅ TP (today):"])
    lines.extend(tp_today_entries or ["(none)"])

    lines.extend(["", "❌ SL (today):"])
    lines.extend(sl_today_entries or ["(none)"])

    lines.extend(["", "📉 DRWDN (today):"])
    lines.extend(drwn_today_entries or ["(none)"])

    lines.extend(["", "⚠️ Failed (today):"])
    lines.extend(failed_today_entries or ["(none)"])

    # ---- Terminal notices summary ----
    lines.extend(["", "🪵 Terminal Notices:"])
    if os.path.exists(TERMINAL_LOG_FILE):
        try:
            with open(TERMINAL_LOG_FILE, "r") as f:
                notif_log = json.load(f) or {}
            if notif_log:
                items = sorted(notif_log.items(), key=lambda kv: kv[1].get("count", 0), reverse=True)
                notif_summary = "\n".join([f"• {msg} (x{info['count']})" for msg, info in items])
            else:
                notif_summary = "(none)"
        except Exception:
            notif_summary = "(none)"
    else:
        notif_summary = "(none)"
    lines.append(notif_summary)

    await _send_long_message("\n".join(lines), part_title="📊 Bot Status")

# --- Single track details ---
async def show_single_track_status(track_index: int):
    if not callable(get_trade_structure):
        if callable(send_notification):
            await send_notification("❌ Internal error: trade structure helpers not available.")
        return
    try:
        structure = get_trade_structure()
        tracks = structure.get("tracks") or {}
        tdata = tracks.get(str(track_index))
        if not tdata:
            if callable(send_notification):
                await send_notification(f"⚠️ Track {track_index} not found.")
            return
        amount = float(tdata.get("amount", 0) or 0)
        slots = structure.get("slots") or {}
        trades = _load_trades_cache()

        lines: List[str] = [f"🔎 Track {track_index} / {amount:.2f} USDT — details"]
        open_entries: List[str] = []
        buy_entries: List[str] = []
        tp_entries: List[str] = []
        sl_entries: List[str] = []
        drw_entries: List[str] = []

        def _pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
            try:
                if a is None or b is None:
                    return None
                a = float(a)
                b = float(b)
                if a == 0.0:
                    return None
                return ((b - a) / a) * 100.0
            except Exception:
                return None

        for sid, cell in slots.items():
            if not cell:
                continue
            if int(cell.get("track_num", 0) or 0) != int(track_index):
                continue
            st = (cell.get("status") or "").lower()
            sym = normalize_symbol(cell.get("symbol"))
            if not sym:
                continue

            tr = _find_latest_trade_for_slot(trades, sym, track_index, str(sid))
            opened_ts = float(tr.get("opened_at")) if tr and tr.get("opened_at") is not None else None

            if st in ("open", "reserved"):
                entry = float(cell.get("entry", 0) or 0)
                sl = float(cell.get("sl", 0) or 0)
                tps = cell.get("targets") or []
                tp1 = float(tps[0]) if tps else 0.0
                open_entries.append(
                    f"{_fmt_berlin(opened_ts)} {sym} — Track {track_index} | Slot {sid} / "
                    f"Entry≤{entry:.6f} / TP1≥{tp1:.6f} / SL≤{sl:.6f}"
                )
            elif st == "buy":
                now_price: Optional[float] = None
                bought_price = None
                try:
                    if tr and tr.get("bought_price") is not None:
                        bought_price = float(tr["bought_price"])
                    now_price = await fetch_current_price(sym) if callable(fetch_current_price) else None
                except Exception:
                    pass
                pct_val = _pct(bought_price, now_price) if (bought_price and now_price) else None
                pct_str = f"{pct_val:+.2f}%" if pct_val is not None else "—"
                bp_str = f"{bought_price:.6f}" if bought_price else "—"
                now_str = f"{now_price:.6f}" if now_price else "N/A"
                buy_entries.append(
                    f"{_fmt_berlin(opened_ts)} {sym} — Track {track_index} | Slot {sid} / "
                    f"buy {bp_str} → now {now_str} / Δ {pct_str}"
                )

        # realized (من TRADES_FILE)
        for tr in trades:
            if int(tr.get("track_num", 0) or 0) != int(track_index):
                continue
            st = (tr.get("status") or "").lower()
            if st not in ("closed", "stopped", "drwn"):
                continue
            sym = normalize_symbol(tr.get("symbol"))
            slot_id = str(tr.get("slot_id") or "?")
            open_ts = tr.get("opened_at")
            close_ts = tr.get("closed_at")
            bought_exec = tr.get("bought_price")
            sell_exec = tr.get("sell_price")
            pct = None
            try:
                if bought_exec is not None and sell_exec is not None and float(bought_exec) != 0.0:
                    pct = ((float(sell_exec) - float(bought_exec)) / float(bought_exec)) * 100.0
            except Exception:
                pct = None
            pct_str = f"{pct:+.2f}%" if pct is not None else "—"
            tag = "TP" if st == "closed" else ("SL" if st == "stopped" else "DRWDN")
            linestr = (
                f"{_fmt_berlin(close_ts)} {sym} — Track {track_index} | Slot {slot_id} / "
                f"{tag} / Δ {pct_str}  {_fmt_berlin(open_ts)}"
            )
            if st == "closed":
                tp_entries.append(linestr)
            elif st == "stopped":
                sl_entries.append(linestr)
            else:
                drw_entries.append(linestr)

        c_open = len(open_entries)
        c_buy = len(buy_entries)
        c_tp = len(tp_entries)
        c_sl = len(sl_entries)
        c_drw = len(drw_entries)
        lines.append(f"open: {c_open} | buy: {c_buy} | TP: {c_tp} | SL: {c_sl} | DRWDN: {c_drw}\n")

        if open_entries:
            lines.append("📜 Open:")
            lines.extend(sorted(open_entries))
            lines.append("")
        if buy_entries:
            lines.append("📜 Buy:")
            lines.extend(sorted(buy_entries))
            lines.append("")
        if tp_entries:
            lines.append("✅ TP (realized):")
            lines.extend(tp_entries)
            lines.append("")
        if sl_entries:
            lines.append("🛑 SL (realized):")
            lines.extend(sl_entries)
            lines.append("")
        if drw_entries:
            lines.append("📉 DRWDN (realized):")
            lines.extend(drw_entries)
            lines.append("")

        msg = "\n".join(lines).rstrip()
        await _send_long_message(msg, part_title=f"Track {track_index} details")
    except Exception as e:
        if callable(send_notification):
            await send_notification(f"⚠️ Error showing track {track_index}: {e}")

# --- All tracks overview ---
async def show_tracks_status():
    if not callable(get_trade_structure):
        if callable(send_notification):
            await send_notification("❌ Internal error: trade structure helpers not available.")
        return
    try:
        structure = get_trade_structure()
        tracks = structure.get("tracks") or {}
        slots = structure.get("slots") or {}
        trades = _load_trades_cache()

        def _format_duration(open_ts: Any, close_ts: Any) -> str:
            try:
                if open_ts is None or close_ts is None:
                    return ""
                t1 = datetime.fromtimestamp(float(open_ts), tz=timezone.utc)
                t2 = datetime.fromtimestamp(float(close_ts), tz=timezone.utc)
                if t2 < t1:
                    return ""
                delta = t2 - t1
                d = delta.days
                h = delta.seconds // 3600
                m = (delta.seconds % 3600) // 60
                return f"{d}d / {h}h / {m}m"
            except Exception:
                return ""

        lines: List[str] = []
        for tkey in sorted(tracks.keys(), key=lambda x: int(x)):
            tdata = tracks[tkey]
            amount = float(tdata.get("amount", 0) or 0)
            track_num = int(tkey)

            open_cnt = 0
            buy_cnt = 0
            tp_cnt = 0
            sl_cnt = 0
            drw_cnt = 0

            open_entries: List[str] = []
            buy_entries: List[str] = []
            tp_entries: List[str] = []
            sl_entries: List[str] = []
            drw_entries: List[str] = []

            # من structure (open/buy)
            for sid, cell in slots.items():
                if not cell:
                    continue
                if int(cell.get("track_num", 0) or 0) != track_num:
                    continue
                st = (cell.get("status") or "").lower()
                sym = normalize_symbol(cell.get("symbol"))
                if not sym:
                    continue
                if st in ("open", "reserved"):
                    open_cnt += 1
                    open_entries.append(f"{sym} — Slot {sid} / open")
                elif st == "buy":
                    buy_cnt += 1
                    buy_entries.append(f"{sym} — Slot {sid} / buy")

            # من TRADES_FILE (realized)
            for tr in trades:
                if int(tr.get("track_num", 0) or 0) != track_num:
                    continue
                st = (tr.get("status") or "").lower()
                if st not in ("closed", "stopped", "drwn"):
                    continue
                sym = normalize_symbol(tr.get("symbol"))
                slot_id = str(tr.get("slot_id") or "?")
                dur = _format_duration(tr.get("opened_at"), tr.get("closed_at"))
                if st == "closed":
                    tp_cnt += 1
                    tp_entries.append(f"{sym} — Slot {slot_id} / TP / {dur}")
                elif st == "stopped":
                    sl_cnt += 1
                    sl_entries.append(f"{sym} — Slot {slot_id} / SL / {dur}")
                else:
                    drw_cnt += 1
                    drw_entries.append(f"{sym} — Slot {slot_id} / DRWDN / {dur}")

            total_cycles = open_cnt + buy_cnt + tp_cnt + sl_cnt + drw_cnt
            lines.append(f"Track {track_num} : base {amount:.2f} USDT / {total_cycles} trades")
            lines.append(f"open: {open_cnt} | buy: {buy_cnt} | TP: {tp_cnt} | SL: {sl_cnt} | DRWDN: {drw_cnt}")
            if open_entries:
                lines.extend(sorted(open_entries))
            if buy_entries:
                lines.extend(sorted(buy_entries))
            if tp_entries:
                lines.extend(tp_entries)
            if sl_entries:
                lines.extend(sl_entries)
            if drw_entries:
                lines.extend(drw_entries)
            lines.append("")

        if not lines:
            if callable(send_notification):
                await send_notification("ℹ️ No tracks currently.")
        else:
            await _send_long_message("\n".join(lines).rstrip(), part_title="Tracks status")
    except Exception as e:
        if callable(send_notification):
            await send_notification(f"⚠️ Error showing tracks: {e}")

# --- cycle slots (capacity) ---
async def apply_cycle_slots(new_slots: int):
    if not callable(get_trade_structure) or not callable(save_trade_structure):
        if callable(send_notification):
            await send_notification("❌ Internal error: trade structure helpers not available.")
        return
    try:
        structure = get_trade_structure()
        old_slots = int(structure.get("cycle_slots", CYCLE_SLOTS_DEFAULT))
        new_slots = max(1, int(new_slots))
        structure["cycle_slots"] = new_slots
        save_trade_structure(structure)
        if callable(send_notification):
            await send_notification(
                f"✅ cycle_slots changed: {old_slots} → {new_slots}\n"
                f"🔒 Max open trades at same time = {new_slots}"
            )
    except Exception as e:
        if callable(send_notification):
            await send_notification(f"❌ cycle slots error: {e}")

# --- slots command (عرض عدد المستخدم والمتاح) ---
async def cmd_list_slots():
    if not callable(get_trade_structure):
        if callable(send_notification):
            await send_notification("❌ Internal error: trade structure helpers not available.")
        return
    try:
        structure = get_trade_structure()
        slots = structure.get("slots") or {}
        cap = int(structure.get("cycle_slots", CYCLE_SLOTS_DEFAULT))
        used = 0
        used_ids: List[str] = []
        for sid, cell in slots.items():
            if not cell:
                continue
            st = (cell.get("status") or "").lower()
            if st in ("open", "reserved", "buy"):
                used += 1
                used_ids.append(str(sid))
        free = max(0, cap - used)
        lines = [
            "🧩 Slots overview:",
            f"• Used : {used}",
            f"• Free : {free}",
            f"• Limit: {cap}",
        ]
        if used_ids:
            lines.append("")
            lines.append("Active Slot IDs:")
            lines.extend(f"• {sid}" for sid in sorted(used_ids, key=lambda x: int(x)))
        await _send_long_message("\n".join(lines), part_title="slots")
    except Exception as e:
        if callable(send_notification):
            await send_notification(f"⚠️ slots error: {e}")

# --- Verlauf: full timeline from TRADES_FILE ---
def _fmt_dt(ts: Optional[float]) -> str:
    if ts is None:
        return "—"
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(_berlin_tz())
        return dt.strftime("%d.%m %H:%M:%S")
    except Exception:
        return "—"

async def show_verlauf():
    trades = _load_trades_cache()
    if not trades:
        if callable(send_notification):
            await send_notification("ℹ️ No trades yet.")
        return

    trades = sorted(trades, key=lambda tr: float(tr.get("opened_at", 0) or 0.0))

    lines: List[str] = ["📜 Verlauf — Full trade history"]
    for tr in trades:
        try:
            sym = normalize_symbol(tr.get("symbol"))
            track_num = int(tr.get("track_num", 0) or 0)
            slot_id = str(tr.get("slot_id") or "?")
            opened_at = tr.get("opened_at")
            bought_at = tr.get("bought_at")
            closed_at = tr.get("closed_at")
            amount = float(tr.get("amount", 0) or 0)
            bought_price = tr.get("bought_price")
            sell_price = tr.get("sell_price")
            qty = tr.get("sell_qty")
            status = (tr.get("status") or "").lower()
            pnl_usdt = tr.get("pnl_usdt")
            pnl_pct = tr.get("pnl_pct")

            lines.append(f"\n— {sym}")
            lines.append(
                f"📥 Signal @ {_fmt_dt(opened_at)} → Track {track_num}, Slot {slot_id} | Amount {amount:.2f} USDT"
            )

            if bought_price is not None:
                buy_ts_show = bought_at if bought_at is not None else opened_at
                qty_show = f"{float(qty):.6f}" if qty is not None else "—"
                usd_spent = (float(bought_price) * float(qty)) if (qty and bought_price) else amount
                lines.append(
                    f"✅ Buy   @ {_fmt_dt(buy_ts_show)} → price {float(bought_price):.6f} | qty {qty_show} | ~USDT {usd_spent:.4f}"
                )

            if status in ("closed", "stopped", "drwn", "failed"):
                ts_sell = closed_at
                pnl_str = "—"
                if pnl_usdt is not None:
                    sign = "+" if float(pnl_usdt) >= 0 else "-"
                    pnl_str = f"{sign}{abs(float(pnl_usdt)):.4f} USDT"
                pct_str = f"{float(pnl_pct):+.2f}%" if pnl_pct is not None else "—"
                tag = status.upper()
                lines.append(
                    f"🏁 {tag} @ {_fmt_dt(ts_sell)} → sell {float(sell_price) if sell_price is not None else 0.0:.6f} | "
                    f"PnL {pnl_str} ({pct_str})"
                )

        except Exception as e:
            lines.append(f"(parse error on one trade: {e})")

    await _send_long_message("\n".join(lines), part_title="verlauf")

# --- helpers للبحث عن Slots حسب الرمز ---
def _find_active_slots_by_symbol(symbol_norm: str):
    out = []
    if not callable(get_trade_structure):
        return out
    try:
        structure = get_trade_structure()
        slots = structure.get("slots") or {}
        for sid, cell in slots.items():
            if not cell:
                continue
            st = (cell.get("status") or "").lower()
            sym = normalize_symbol(cell.get("symbol"))
            if sym == symbol_norm and st in ("open", "buy", "reserved"):
                out.append((str(sid), cell))
    except Exception as e:
        print(f"_find_active_slots_by_symbol error: {e}")
    return out

# --- Manual SELL helpers ---
async def _manual_sell_slot(sym_norm: str, track_num: int, slot_id: str, cell: Dict[str, Any]):
    """
    يبيع Slot واحد بحالة BUY (Market)، ويحدّث:
      - TRADES_FILE (status + pnl)
      - register_trade_outcome
      - summary
      - تحرير الـ Slot
    """
    try:
        pair = globals().get("format_symbol", lambda s: s)(sym_norm)
    except Exception:
        pair = sym_norm

    sim_flag = bool(is_simulation()) if callable(is_simulation) else False

    meta = get_symbol_meta(pair) if callable(get_symbol_meta) else None
    if not meta:
        if callable(send_notification_tc):
            await send_notification_tc("❌ Sell meta fetch failed.", symbol=sym_norm)
        return

    qty = float(cell.get("filled_qty", 0) or 0)
    bought_price = float(cell.get("bought_price", 0) or 0)
    amount = float(cell.get("amount", 0) or 0)

    if qty <= 0 or bought_price <= 0:
        if callable(send_notification_tc):
            await send_notification_tc(
                "⚠️ Sell aborted: missing execution data (qty/price).",
                symbol=sym_norm
            )
        return

    base_inc = float(meta["baseIncrement"])
    min_base = float(meta["baseMinSize"])

    adj_qty = quantize_down(qty * 0.9998, base_inc) if callable(quantize_down) else qty * 0.9998
    if adj_qty < min_base or adj_qty <= 0.0:
        if callable(send_notification_tc):
            await send_notification_tc(
                "⚠️ Sell aborted: adjusted qty < min size.",
                symbol=sym_norm
            )
        return

    # --- نفّذ أمر البيع Market ---
    order = place_market_order(
        pair, "sell", size=str(adj_qty),
        symbol_hint=sym_norm,
        sim_override=sim_flag
    ) if callable(place_market_order) else None

    await asyncio.sleep(1)

    if order and isinstance(order, dict):
        order_id = order.get("orderId")
    else:
        order_id = None

    if order_id and callable(get_order_deal_size):
        filled_qty, deal_funds = await get_order_deal_size(order_id, symbol=sym_norm, sim_override=sim_flag)
        if filled_qty <= 0.0:
            if callable(send_notification_tc):
                await send_notification_tc(
                    f"❌ Sell issue: order executed but filled size = 0.\n🆔 orderId: {order_id}",
                    symbol=sym_norm
                )
            return
        sell_price = float(deal_funds) / float(filled_qty)
        sell_qty = float(filled_qty)
    else:
        # fallback: نستخدم آخر سعر
        last_price = await fetch_current_price(sym_norm) if callable(fetch_current_price) else bought_price
        sell_price = float(last_price or bought_price)
        sell_qty = float(adj_qty)

    pnl_usdt = (sell_price - bought_price) * sell_qty
    pnl_pct = ((sell_price - bought_price) / max(bought_price, 1e-12)) * 100.0

    # --- تحديث TRADES_FILE ---
    trades = _load_trades_cache()
    changed = False
    trade_id_for_slot = None
    for tr in trades:
        if normalize_symbol(tr.get("symbol")) != sym_norm:
            continue
        if int(tr.get("track_num", 0) or 0) != int(track_num):
            continue
        if str(tr.get("slot_id")) != str(slot_id):
            continue
        # أحدث سجل هو الهدف
        trade_id_for_slot = int(tr.get("id", 0))
    if trade_id_for_slot is not None:
        # استخدم _finalize_trade_record من Section 4 إن وجد
        finalize_fn = globals().get("_finalize_trade_record")
        if callable(finalize_fn):
            # استنتاج status عبر classify_pnl
            res = classify_pnl(bought_price, sell_price) if callable(classify_pnl) else {"status": "drwn", "pct": pnl_pct}
            status = (res.get("status") or "drwn").lower()
            finalize_fn(trade_id_for_slot, status, sell_price, sell_qty, pnl_usdt, res.get("pct", pnl_pct))
        else:
            # fallback: تعديل يدوي
            for tr in trades:
                if int(tr.get("id", 0)) == trade_id_for_slot:
                    res = classify_pnl(bought_price, sell_price) if callable(classify_pnl) else {"status": "drwn", "pct": pnl_pct}
                    status = (res.get("status") or "drwn").lower()
                    tr["status"] = status
                    tr["sell_price"] = float(sell_price)
                    tr["sell_qty"] = float(sell_qty)
                    tr["pnl_usdt"] = float(pnl_usdt)
                    tr["pnl_pct"] = float(res.get("pct", pnl_pct))
                    tr["closed_at"] = datetime.now(timezone.utc).timestamp()
                    changed = True
                    break
            if changed:
                try:
                    with open(TRADES_FILE, "w") as f:
                        json.dump({"trades": trades}, f, indent=2)
                except Exception:
                    pass
        final_status = res.get("status") or "drwn"
    else:
        # لم نجد trade id → نستخدم fallback بسيط
        res = classify_pnl(bought_price, sell_price) if callable(classify_pnl) else {"status": "drwn", "pct": pnl_pct}
        final_status = res.get("status") or "drwn"

    # --- summary + counters ---
    try:
        if pnl_usdt >= 0:
            accumulate_summary(profit_delta=float(pnl_usdt))
        else:
            accumulate_summary(loss_delta=float(-pnl_usdt))
    except Exception:
        pass

    try:
        if callable(register_trade_outcome):
            register_trade_outcome(str(track_num), final_status)
    except Exception:
        pass

    # --- تحرير الـ Slot ---
    if callable(get_trade_structure) and callable(save_trade_structure):
        s = get_trade_structure()
        slots = s.get("slots") or {}
        if str(slot_id) in slots:
            slots[str(slot_id)] = None
            s["slots"] = slots
            save_trade_structure(s)

    # --- إشعار ---
    dur_str = ""
    try:
        st_iso = cell.get("start_time")
        if st_iso:
            dt = datetime.fromisoformat(st_iso)
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            delta = datetime.now(timezone.utc) - dt
            dur_str = f"{delta.days}d / {delta.seconds // 3600}h / {(delta.seconds % 3600)//60}m"
    except Exception:
        pass

    if callable(send_notification_tc):
        await send_notification_tc(
            (
                "🧾 Manual SELL\n"
                f"💰 Buy: {bought_price:.6f} → Sell: {sell_price:.6f}\n"
                f"📦 Qty: {sell_qty:.6f} | 💵 Amount: {amount:.2f} USDT\n"
                f"💵 PnL: {pnl_usdt:.4f} USDT  ({pnl_pct:+.2f}%)\n"
                f"🏷️ Final status: {final_status.upper()}\n"
                f"{('⏱️ ' + dur_str) if dur_str else ''}"
            ),
            symbol=sym_norm
        )

# ====== COMMAND HANDLER (from Mohamad4992) ======
client = globals().get("client")

if client is not None and events is not None:
    @client.on(events.NewMessage(chats=COMMAND_CHAT))
    async def command_handler(event):
        text = (event.raw_text or "").strip()
        cmd = text.lower()

        _console_echo(f"[CMD] {text}")

        # ===== Blacklist commands =====
        if cmd.startswith("add "):
            sym = normalize_symbol(text.split(maxsplit=1)[1])
            try:
                if callable(add_to_blacklist):
                    added = add_to_blacklist(sym)
                    if added and callable(send_notification):
                        await send_notification(f"✅ Added {sym} to blacklist. Future signals will be ignored.")
                    elif callable(send_notification):
                        await send_notification(f"ℹ️ {sym} is already in the blacklist.")
                else:
                    if callable(send_notification):
                        await send_notification("⚠️ blacklist is not enabled in this setup.")
            except Exception as e:
                if callable(send_notification):
                    await send_notification(f"❌ Failed to add {sym} to blacklist: {e}")
            return

        if cmd.startswith("remove "):
            sym = normalize_symbol(text.split(maxsplit=1)[1])
            try:
                if callable(remove_from_blacklist):
                    removed = remove_from_blacklist(sym)
                    if removed and callable(send_notification):
                        await send_notification(f"✅ Removed {sym} from blacklist.")
                    elif callable(send_notification):
                        await send_notification(f"ℹ️ {sym} was not in the blacklist.")
                else:
                    if callable(send_notification):
                        await send_notification("⚠️ blacklist is not enabled in this setup.")
            except Exception as e:
                if callable(send_notification):
                    await send_notification(f"❌ Failed to remove {sym} from blacklist: {e}")
            return

        if cmd == "status list":
            try:
                if callable(list_blacklist):
                    bl = list_blacklist()
                    if bl and callable(send_notification):
                        await send_notification("🚫 Blacklist symbols:\n" + "\n".join(f"• {s}" for s in bl))
                    elif callable(send_notification):
                        await send_notification("🚫 Blacklist is empty.")
                else:
                    if callable(send_notification):
                        await send_notification("⚠️ blacklist is not enabled in this setup.")
            except Exception as e:
                if callable(send_notification):
                    await send_notification(f"❌ Failed to read blacklist: {e}")
            return

        # ===== Email Gate commands =====
        if cmd in ("off", "gate"):
            await show_gate_status()
            return

        if cmd in ("gate close", "gate off"):
            try:
                if callable(set_email_gate):
                    set_email_gate(False)
                if callable(send_notification):
                    await send_notification("📧 Email gate changed → CLOSED ⛔️ (blocking new recommendations)")
            except Exception as e:
                if callable(send_notification):
                    await send_notification(f"❌ Failed to close Email gate: {e}")
            return

        if cmd in ("gate open", "gate on"):
            try:
                if callable(set_email_gate):
                    set_email_gate(True)
                if callable(send_notification):
                    await send_notification("📧 Email gate changed → OPEN ✅ (accepting channel recommendations)")
            except Exception as e:
                if callable(send_notification):
                    await send_notification(f"❌ Failed to open Email gate: {e}")
            return

        # ===== Debug funds toggles =====
        if cmd.startswith("debug funds"):
            parts = cmd.split()
            try:
                if len(parts) == 3 and parts[2] == "on":
                    if callable(enable_debug_funds):
                        enable_debug_funds(0)
                    if callable(send_notification):
                        await send_notification("🟢 DEBUG_FUNDS enabled (no expiry).")
                    return
                if len(parts) == 3 and parts[2] == "off":
                    if callable(disable_debug_funds):
                        disable_debug_funds()
                    if callable(send_notification):
                        await send_notification("🔴 DEBUG_FUNDS disabled.")
                    return
                if len(parts) == 3 and parts[2].endswith("m"):
                    n = int(parts[2][:-1])
                    if callable(enable_debug_funds):
                        enable_debug_funds(n)
                    if callable(send_notification):
                        await send_notification(f"🟢 DEBUG_FUNDS enabled for {n} minute(s).")
                    return
                if len(parts) == 3 and parts[2].isdigit():
                    n = int(parts[2])
                    if callable(enable_debug_funds):
                        enable_debug_funds(n)
                    if callable(send_notification):
                        await send_notification(f"🟢 DEBUG_FUNDS enabled for {n} minute(s).")
                    return
                if callable(send_notification):
                    await send_notification("ℹ️ Usage: debug funds on | debug funds off | debug funds <N>m")
            except Exception as e:
                if callable(send_notification):
                    await send_notification(f"⚠️ debug funds error: {e}")
            return

        # ===== Slots / cycle slots =====
        if cmd == "slots":
            await cmd_list_slots()
            return

        if cmd.startswith("cycle slots"):
            parts = cmd.split()
            if len(parts) == 2:
                # فقط عرض القيمة الحالية
                if callable(get_trade_structure) and callable(send_notification):
                    s = get_trade_structure()
                    cur = int(s.get("cycle_slots", CYCLE_SLOTS_DEFAULT))
                    await send_notification(
                        f"ℹ️ Current cycle_slots = {cur}\nUsage: cycle slots <N> (example: cycle slots 10)"
                    )
            else:
                try:
                    n = int(parts[2])
                    await apply_cycle_slots(n)
                except Exception:
                    if callable(send_notification):
                        await send_notification("⚠️ Usage: cycle slots <N>  (example: cycle slots 10)")
            return

        # ===== Risk command =====
        if cmd.startswith("risk"):
            if callable(handle_risk_command):
                await handle_risk_command(text)
            else:
                if callable(send_notification):
                    await send_notification("⚠️ risk command is not enabled in this setup.")
            return

        # ===== Manual SELL: sell <index> أو sell <symbol> =====
        if cmd.startswith("sell"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                if callable(send_notification):
                    await send_notification(
                        "⚠️ Usage: sell <index>  or  sell <symbol>\n"
                        "Example: sell 3  or  sell ALGO"
                    )
                return

            arg = parts[1].strip()
            is_index = arg.isdigit()

            # --- sell <index> ---
            if is_index:
                idx = int(arg)
                if idx not in _STATUS_INDEX_MAP:
                    try:
                        _rebuild_status_index_map()
                    except Exception:
                        pass
                    if idx not in _STATUS_INDEX_MAP:
                        if callable(send_notification):
                            await send_notification(f"⚠️ sell {idx}: index not found in current status.")
                        return
                sym_norm, track_num, slot_id = _STATUS_INDEX_MAP[idx]

                if not callable(get_trade_structure) or not callable(save_trade_structure):
                    if callable(send_notification_tc):
                        await send_notification_tc(
                            "❌ Internal error: trade structure helpers not available.",
                            symbol=sym_norm
                        )
                    return

                structure = get_trade_structure()
                slots = structure.get("slots") or {}
                cell = slots.get(str(slot_id))
                if not cell:
                    if callable(send_notification_tc):
                        await send_notification_tc(
                            "ℹ️ No active trade on this slot.",
                            symbol=sym_norm
                        )
                    return

                st = (cell.get("status") or "").lower()

                # إذا كانت open → إلغاء الصفقة
                if st in ("open", "reserved"):
                    if callable(send_notification_tc):
                        await send_notification_tc(
                            "🚫 Cancelled pending buy.",
                            symbol=sym_norm
                        )
                    # تحديث TRADES_FILE كـ failed
                    trades = _load_trades_cache()
                    changed = False
                    for tr in trades:
                        if normalize_symbol(tr.get("symbol")) == sym_norm \
                           and int(tr.get("track_num", 0) or 0) == int(track_num) \
                           and str(tr.get("slot_id")) == str(slot_id):
                            tr["status"] = "failed"
                            tr["closed_at"] = datetime.now(timezone.utc).timestamp()
                            changed = True
                    if changed:
                        try:
                            with open(TRADES_FILE, "w") as f:
                                json.dump({"trades": trades}, f, indent=2)
                        except Exception:
                            pass
                    if callable(register_trade_outcome):
                        register_trade_outcome(str(track_num), "failed")
                    # حرّر الـ Slot
                    slots[str(slot_id)] = None
                    structure["slots"] = slots
                    save_trade_structure(structure)
                    try:
                        _rebuild_status_index_map()
                    except Exception:
                        pass
                    return

                # إذا كانت BUY → بيع يدوي
                if st == "buy":
                    await _manual_sell_slot(sym_norm, track_num, str(slot_id), cell)
                    try:
                        _rebuild_status_index_map()
                    except Exception:
                        pass
                return

            # --- sell <symbol> ---
            symbol_in = arg.strip()
            symbol_norm = normalize_symbol(symbol_in)
            active = _find_active_slots_by_symbol(symbol_norm)
            if not active:
                if callable(send_notification):
                    await send_notification(f"ℹ️ No active trades for {symbol_norm}.")
                return

            if not callable(get_trade_structure) or not callable(save_trade_structure):
                if callable(send_notification_tc):
                    await send_notification_tc(
                        "❌ Internal error: trade structure helpers not available.",
                        symbol=symbol_norm
                    )
                return

            structure = get_trade_structure()
            slots = structure.get("slots") or {}

            for sid, cell in active:
                st = (cell.get("status") or "").lower()
                track_num = int(cell.get("track_num", 0) or 0)

                if st in ("open", "reserved"):
                    if callable(send_notification_tc):
                        await send_notification_tc(
                            "🚫 Cancelled pending buy.",
                            symbol=symbol_norm
                        )
                    # TRADES_FILE → failed
                    trades = _load_trades_cache()
                    changed = False
                    for tr in trades:
                        if normalize_symbol(tr.get("symbol")) == symbol_norm \
                           and int(tr.get("track_num", 0) or 0) == int(track_num) \
                           and str(tr.get("slot_id")) == str(sid):
                            tr["status"] = "failed"
                            tr["closed_at"] = datetime.now(timezone.utc).timestamp()
                            changed = True
                    if changed:
                        try:
                            with open(TRADES_FILE, "w") as f:
                                json.dump({"trades": trades}, f, indent=2)
                        except Exception:
                            pass
                    if callable(register_trade_outcome):
                        register_trade_outcome(str(track_num), "failed")
                    slots[str(sid)] = None
                    continue

                if st == "buy":
                    await _manual_sell_slot(symbol_norm, track_num, str(sid), cell)

            structure["slots"] = slots
            save_trade_structure(structure)
            try:
                _rebuild_status_index_map()
            except Exception:
                pass
            return

        # ===== Track commands =====
        if cmd.startswith("track "):
            parts = text.split()
            if len(parts) >= 2:
                try:
                    tn = int(parts[1])
                    await show_single_track_status(tn)
                except Exception:
                    if callable(send_notification):
                        await send_notification("⚠️ Usage: track <n>  (example: track 1)")
            else:
                if callable(send_notification):
                    await send_notification("⚠️ Usage: track <n>  (example: track 1)")
            return

        if cmd == "track":
            await show_tracks_status()
            return

        # ===== Pause / Reuse =====
        if cmd == "pause":
            if callable(set_bot_active):
                set_bot_active(False)
            if callable(send_notification):
                await send_notification("⏸️ Bot paused (will ignore new recommendations).")
            return

        if cmd == "reuse":
            if callable(set_bot_active):
                set_bot_active(True)
            if callable(send_notification):
                await send_notification("▶️ Bot resumed.")
            return

        # ===== Global status / summary =====
        if cmd == "status":
            await show_bot_status()
            return

        if cmd == "summary":
            await show_trade_summary()
            return

        if cmd == "verlauf":
            await show_verlauf()
            return

        # ===== Help =====
        if cmd == "help":
            if callable(send_notification):
                await send_notification(
                    "🆘 Commands (from Mohamad4992):\n"
                    "• gate (or off) – Show Email Gate status\n"
                    "• gate open / gate close – Manually OPEN/CLOSE Email Gate\n"
                    "• pause – Pause recommendations\n"
                    "• reuse – Resume recommendations\n"
                    "• status – Show bot status (with numbering for SELL)\n"
                    "• summary – Profit/Loss summary\n"
                    "• track – Show tracks status (all)\n"
                    "• track <n> – Show only track n details\n"
                    "• cycle slots – Show current max open trades\n"
                    "• cycle slots <N> – Change max open trades\n"
                    "• slots – Show used/free slots\n"
                    "• sell <index> – Exit/cancel by index from status (e.g., sell 3)\n"
                    "• sell <symbol> – Market-exit or cancel pending by symbol (e.g., sell ALGO)\n"
                    "• verlauf – Full trade timeline\n"
                    "• debug funds on/off/<N>m – Toggle detailed balance logging\n"
                    "• Add <symbol> / Remove <symbol> / Status List – manage blacklist\n"
                    "• risk ... – Market quality report (if enabled)"
                )
            return

        # أوامر أخرى غير معروفة → تجاهل
        return
else:
    _console_echo("⚠️ Telethon client or events not available; Section 5 commands disabled.")
