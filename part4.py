# ============================================
# Section 4) Execution & Monitoring (NEW: Tracks + Slots + 2% rule)
#   - execute_trade(): استلام التوصية وفتح Slot جديد
#   - monitor_and_execute(): شراء/إدارة TP + Trailing + SL إشعار فقط
#   - الحد الأقصى للصفقات المفتوحة = cycle_slots من الهيكل
#   - البيع حصراً بعد TP1 (Trailing 1%)، لا بيع على SL
#   - تصنيف النتيجة بالاعتماد على classify_pnl من Section 3
#   - تسجيل كل شيء في TRADES_FILE مع trade_id
# ============================================

from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple
import asyncio
import os
import json
import time

# ====== استيراد ثوابت ومسارات من الأقسام السابقة ======
TRADES_FILE = globals().get("TRADES_FILE", "trades.json")

# من Section 1/2:
kucoin = globals().get("kucoin")
INITIAL_TRADE_AMOUNT = float(globals().get("INITIAL_TRADE_AMOUNT", 50.0))
TRADE_INCREMENT_PERCENT = float(globals().get("TRADE_INCREMENT_PERCENT", 2.0))
MAX_TRACKS = int(globals().get("MAX_TRACKS", 10))
CYCLE_SLOTS_DEFAULT = int(globals().get("CYCLE_SLOTS", 10))

# من Section 2:
get_trade_structure = globals().get("get_trade_structure")
save_trade_structure = globals().get("save_trade_structure")
track_base_amount = globals().get("track_base_amount")

# من Section 1 (أو 2):
send_notification = globals().get("send_notification")
send_notification_tc = globals().get("send_notification_tc")
log_terminal_notification = globals().get("log_terminal_notification")

normalize_symbol = globals().get("normalize_symbol") or (lambda s: (s or "").upper().replace('-', '').replace('/', ''))
format_symbol = globals().get("format_symbol") or (lambda s: (s or "").upper().replace('/', '-'))

# من Section 2: دوال التعامل مع KuCoin (نفترض أنها موجودة)
quantize_down = globals().get("quantize_down")
get_symbol_meta = globals().get("get_symbol_meta")
get_trade_balance_usdt = globals().get("get_trade_balance_usdt")
place_market_order = globals().get("place_market_order")
get_order_deal_size = globals().get("get_order_deal_size")

# من Section 2: Email Gate + blacklist
_email_gate_allows = globals().get("_email_gate_allows")
should_accept_recommendations = globals().get("should_accept_recommendations")
_is_blocked_symbol = globals().get("_is_blocked_symbol")

# من Section 3:
is_simulation = globals().get("is_simulation")
get_latest_candle = globals().get("get_latest_candle")
_interval_to_ms = globals().get("_interval_to_ms")
classify_pnl = globals().get("classify_pnl")
register_trade_outcome = globals().get("register_trade_outcome")

# من Section 5 (لاحقاً) – اختياري
accumulate_summary = globals().get("accumulate_summary")

# صغيرة للـ console
try:
    console_echo  # type: ignore[name-defined]
except NameError:  # pragma: no cover
    def console_echo(msg: str) -> None:
        print(msg)

_console_echo = console_echo

# ===== إعدادات التريلينغ / SL =====
RETRACE_PERCENT = 1.0   # هبوط 1% من القمّة
EPS = 1e-9              # هامش صغير للتحاشي من مساواة رقمية
PRICE_TIMEOUT_SEC = 600  # 10 دقائق بدون سعر → إلغاء الصفقة

_FINAL_STATES = {"closed", "stopped", "drwn", "failed"}


# ============================================
# Helpers: Email Gate / capacity / slots / TRADES_FILE
# ============================================

def _email_gate_ok() -> bool:
    """هل البوابة تسمح بفتح توصيات الآن؟"""
    try:
        if callable(_email_gate_allows):
            return bool(_email_gate_allows())
        if callable(should_accept_recommendations):
            return bool(should_accept_recommendations())
    except Exception:
        pass
    return True


def _get_cycle_slots_limit(structure: Dict[str, Any]) -> int:
    """قراءة الحد الأقصى للصفقات المفتوحة في نفس الوقت."""
    try:
        return int(structure.get("cycle_slots", CYCLE_SLOTS_DEFAULT))
    except Exception:
        return CYCLE_SLOTS_DEFAULT


def _count_open_slots(structure: Dict[str, Any]) -> int:
    """عدد الخانات (slots) المفتوحة حالياً (open/reserved/buy)."""
    slots = structure.get("slots") or {}
    cnt = 0
    for cell in slots.values():
        if not cell:
            continue
        st = (cell.get("status") or "").lower()
        if st in ("open", "reserved", "buy"):
            cnt += 1
    return cnt


def _select_track_for_new_trade(structure: Dict[str, Any]) -> int:
    """
    اختيار رقم المسار للصفقة الجديدة:
      - يستخدم next_track_index من الهيكل.
      - إذا غير موجود → 1
      - إذا تخطّى MAX_TRACKS → يثبت على MAX_TRACKS
    """
    try:
        next_idx = int(structure.get("next_track_index", 1))
    except Exception:
        next_idx = 1

    max_tracks = int(structure.get("max_tracks", MAX_TRACKS))
    if next_idx < 1:
        next_idx = 1
    if next_idx > max_tracks:
        next_idx = max_tracks
    return next_idx


def _allocate_new_slot_id(structure: Dict[str, Any]) -> str:
    """
    تخصيص Slot ID جديد:
      - يعيد استخدام الخانات النهائية (closed/stopped/drwn/failed) إذا وُجدت.
      - وإلا ينشئ رقم جديد بالاعتماد على next_slot_id.
    """
    slots: Dict[str, Any] = structure.setdefault("slots", {})
    next_id = int(structure.get("next_slot_id", 1))

    # حاول العثور على خانة نهائية يمكن إعادة استخدامها
    for sid, cell in slots.items():
        if not cell:
            return str(sid)
        st = (cell.get("status") or "").lower()
        if st in _FINAL_STATES:
            slots[sid] = None
            return str(sid)

    # لا يوجد شيء يعاد استخدامه → أنشئ ID جديد
    sid = str(next_id)
    structure["next_slot_id"] = next_id + 1
    return sid


def _append_trade_record(
    symbol: str,
    track_num: int,
    slot_id: str,
    entry: float,
    sl: float,
    targets: List[float],
    amount: float,
    sim_flag: bool
) -> int:
    """
    إضافة سجل صفقة جديد إلى TRADES_FILE.
    يرجّع trade_id (int) ويخزّنه لاحقاً في الخانة.
    """
    data = {"trades": []}
    if os.path.exists(TRADES_FILE):
        try:
            with open(TRADES_FILE, "r") as f:
                loaded = json.load(f) or {}
            data["trades"] = loaded.get("trades", []) or []
        except Exception:
            pass

    trades = data["trades"]
    if trades:
        try:
            last_id = max(int(tr.get("id", 0)) for tr in trades)
        except Exception:
            last_id = len(trades)
    else:
        last_id = 0
    new_id = last_id + 1

    rec = {
        "id": new_id,
        "symbol": normalize_symbol(symbol),
        "track_num": int(track_num),
        "slot_id": str(slot_id),
        "entry": float(entry),
        "sl": float(sl),
        "targets": [float(x) for x in targets],
        "amount": float(amount),
        "status": "open",
        "opened_at": datetime.now(timezone.utc).timestamp(),
        "simulated": bool(sim_flag),
        "bought_at": None,
        "closed_at": None,
        "bought_price": None,
        "sell_price": None,
        "sell_qty": None,
        "pnl_usdt": None,
        "pnl_pct": None,
    }
    trades.append(rec)

    try:
        with open(TRADES_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        _console_echo(f"[TRADES] append error: {e}")

    return new_id


def _update_trade_on_buy(trade_id: int, bought_price: float, qty: float) -> None:
    """تحديث سجل TRADES_FILE بعد تنفيذ أمر الشراء."""
    if not os.path.exists(TRADES_FILE):
        return
    try:
        with open(TRADES_FILE, "r") as f:
            data = json.load(f) or {}
        trades = data.get("trades", []) or []
    except Exception:
        return

    changed = False
    for tr in trades:
        if int(tr.get("id", 0)) == int(trade_id):
            tr["status"] = "buy"
            tr["bought_price"] = float(bought_price)
            tr["sell_qty"] = float(qty)
            tr["bought_at"] = datetime.now(timezone.utc).timestamp()
            changed = True
            break

    if changed:
        try:
            with open(TRADES_FILE, "w") as f:
                json.dump({"trades": trades}, f, indent=2)
        except Exception as e:
            _console_echo(f"[TRADES] buy update error: {e}")


def _finalize_trade_record(
    trade_id: int,
    status: str,
    sell_price: float,
    sell_qty: float,
    pnl_usdt: float,
    pnl_pct: float
) -> None:
    """تحديث سجل TRADES_FILE عند إغلاق الصفقة (closed/drwn/failed/...)."""
    if not os.path.exists(TRADES_FILE):
        return
    try:
        with open(TRADES_FILE, "r") as f:
            data = json.load(f) or {}
        trades = data.get("trades", []) or []
    except Exception:
        return

    changed = False
    for tr in trades:
        if int(tr.get("id", 0)) == int(trade_id):
            tr["status"] = status
            tr["sell_price"] = float(sell_price)
            tr["sell_qty"] = float(sell_qty)
            tr["pnl_usdt"] = float(pnl_usdt)
            tr["pnl_pct"] = float(pnl_pct)
            tr["closed_at"] = datetime.now(timezone.utc).timestamp()
            changed = True
            break

    if changed:
        try:
            with open(TRADES_FILE, "w") as f:
                json.dump({"trades": trades}, f, indent=2)
        except Exception as e:
            _console_echo(f"[TRADES] finalize error: {e}")


def _update_track_pointer_on_result(status: str) -> None:
    """
    تحديث next_track_index حسب نتيجة الصفقة:
      - إذا status == "closed" (ربح ≥ 2%) → نزيد المؤشر +1 حتى لا يتخطى MAX_TRACKS.
      - أي حالة أخرى → لا تغيير (إعادة المحاولة على نفس المسار المنطقي).
    """
    if get_trade_structure is None or save_trade_structure is None:
        return
    try:
        s = get_trade_structure()
        max_tracks = int(s.get("max_tracks", MAX_TRACKS))
        try:
            cur = int(s.get("next_track_index", 1))
        except Exception:
            cur = 1
        if (status or "").lower() == "closed":
            cur = min(max_tracks, cur + 1)
            s["next_track_index"] = cur
            save_trade_structure(s)
    except Exception as e:
        _console_echo(f"[TRACK PTR] update error: {e}")


# ============================================
# fetch_current_price (Async)
# ============================================

async def fetch_current_price(symbol: str) -> Optional[float]:
    """
    جلب السعر الحالي من KuCoin.
    - يستخدم kucoin.get_ticker(pair)
    - عند الفشل: يسجّل في Terminal Notices (price_fetch_fail_SYMBOL)
    """
    if kucoin is None:
        return None

    try:
        pair = format_symbol(symbol)
    except Exception:
        pair = symbol

    sym_norm = normalize_symbol(symbol)

    try:
        # نستخدم to_thread حتى لا نحجب event loop
        ticker = await asyncio.to_thread(kucoin.get_ticker, pair)
        if not ticker:
            raise RuntimeError("empty ticker")

        price_str = (
            ticker.get("price")
            or ticker.get("bestBid")
            or ticker.get("bestAsk")
        )
        if not price_str:
            raise RuntimeError("no price field")

        return float(price_str)
    except Exception as e:
        try:
            if callable(log_terminal_notification):
                log_terminal_notification(
                    f"price_fetch_fail_{sym_norm}",
                    tag=f"price_fetch_fail_{sym_norm}"
                )
        except Exception:
            pass
        _console_echo(f"[PRICE] fetch error for {sym_norm}: {e}")
        return None


# ============================================
# execute_trade  (استقبال التوصية وفتح Slot)
# ============================================

async def execute_trade(symbol: str, entry_price: float, sl_price: float, targets: List[float]):
    """
    استلام توصية جديدة من القناة (أو يدويًا) وفتح Slot جديد:

      - يتحقق من Email Gate.
      - يتحقق من blacklist.
      - يتحقق من عدد الصفقات المفتوحة <= cycle_slots.
      - يختار رقم المسار (track_num) حسب next_track_index.
      - يخصص Slot ID جديد.
      - يسجّل في trade_structure + TRADES_FILE.
      - يطلق monitor_and_execute كـ task.
    """
    sym_norm = normalize_symbol(symbol)

    # ===== 1) Email Gate =====
    try:
        if not _email_gate_ok():
            if callable(send_notification_tc):
                await send_notification_tc(
                    "⛔️ Recommendation ignored — Email gate is CLOSED.",
                    symbol=sym_norm
                )
            else:
                _console_echo(f"[GATE] CLOSED → ignore {sym_norm}")
            return
    except Exception:
        pass  # لو فشل الفحص، نعتبر البوابة مفتوحة (fail-open)

    # ===== 2) Blacklist =====
    try:
        if callable(_is_blocked_symbol) and _is_blocked_symbol(sym_norm):
            if callable(send_notification_tc):
                await send_notification_tc(
                    "🚫 Ignored: symbol is in blacklist.",
                    symbol=sym_norm
                )
            return
    except Exception:
        pass

    # ===== 3) Targets =====
    try:
        targets = [float(x) for x in (targets or []) if x is not None]
    except Exception:
        targets = []
    if not targets:
        if callable(send_notification_tc):
            await send_notification_tc(
                "⚠️ No targets provided. Cancel trade.",
                symbol=sym_norm
            )
        return
    targets = sorted(targets)
    tp1 = float(targets[0])

    # ===== 4) قراءة الهيكل و التحقق من السعة =====
    if get_trade_structure is None or save_trade_structure is None:
        if callable(send_notification_tc):
            await send_notification_tc(
                "❌ Internal error: trade structure helpers not available.",
                symbol=sym_norm
            )
        return

    structure = get_trade_structure()
    cap = _get_cycle_slots_limit(structure)
    open_cnt = _count_open_slots(structure)

    if open_cnt >= cap:
        if callable(send_notification_tc):
            await send_notification_tc(
                f"⚠️ Cannot open new trade. Capacity reached {open_cnt}/{cap}.",
                symbol=sym_norm
            )
        return

    # ===== 5) اختيار المسار وحجم الصفقة =====
    track_num = _select_track_for_new_trade(structure)
    tracks_def = structure.get("tracks") or {}
    tinfo = tracks_def.get(str(track_num)) or {}

    try:
        amount = float(tinfo.get("amount", 0) or 0.0)
    except Exception:
        amount = 0.0

    if amount <= 0.0:
        try:
            if callable(track_base_amount):
                amount = float(track_base_amount(track_num))
            else:
                amount = float(INITIAL_TRADE_AMOUNT * ((1 + TRADE_INCREMENT_PERCENT / 100.0) ** (track_num - 1)))
        except Exception:
            amount = float(INITIAL_TRADE_AMOUNT)

    # ===== 6) تخصيص Slot جديد =====
    slot_id = _allocate_new_slot_id(structure)
    sim_flag = bool(is_simulation()) if callable(is_simulation) else False

    cell = {
        "symbol": sym_norm,
        "entry": float(entry_price),
        "sl": float(sl_price),
        "targets": targets,
        "status": "open",
        "amount": float(amount),
        "track_num": int(track_num),
        "slot_id": str(slot_id),
        "start_time": None,
        "filled_qty": None,
        "bought_price": None,
        "simulated": bool(sim_flag),
        "trade_id": None,  # نملأه بعد append
    }

    structure.setdefault("slots", {})[str(slot_id)] = cell
    save_trade_structure(structure)

    # ===== 7) سجل في TRADES_FILE =====
    trade_id = _append_trade_record(
        symbol=sym_norm,
        track_num=track_num,
        slot_id=str(slot_id),
        entry=float(entry_price),
        sl=float(sl_price),
        targets=targets,
        amount=float(amount),
        sim_flag=sim_flag,
    )
    # خزّن trade_id في الخانة
    structure = get_trade_structure()
    structure["slots"][str(slot_id)]["trade_id"] = int(trade_id)
    save_trade_structure(structure)

    # ===== 8) إشعار استقبال التوصية =====
    if callable(send_notification_tc):
        await send_notification_tc(
            (
                "📥 New recommendation:\n"
                f"🎯 Entry ≤ {float(entry_price):.6f}, TP1 ≥ {tp1:.6f}, SL ≤ {float(sl_price):.6f}\n"
                f"💵 Amount: {amount:.2f} USDT\n"
                f"🔢 Track {track_num} | Slot {slot_id}"
            ),
            symbol=sym_norm
        )

    # ===== 9) إطلاق المراقب =====
    asyncio.create_task(
        monitor_and_execute(
            symbol=sym_norm,
            entry_price=float(entry_price),
            sl_price=float(sl_price),
            targets=targets,
            amount=float(amount),
            track_num=int(track_num),
            slot_id=str(slot_id),
            trade_id=int(trade_id),
        )
    )


# ============================================
# monitor_and_execute
# ============================================

async def monitor_and_execute(
    symbol: str,
    entry_price: float,
    sl_price: float,
    targets: List[float],
    amount: float,
    track_num: int,
    slot_id: str,
    trade_id: int,
):
    """
    منطق التنفيذ والمراقبة:

      1) شراء Market عند وصول السعر ≤ entry.
      2) لا بيع مباشرة على أي TP:
         - نستخدم TP ladder + Trailing 1% بعد لمس TP1.
         - أرضية floor = آخر TP مُلامس، ولا نبيع تحتها.
      3) SL:
         - إشعار فقط عند إغلاق شمعة 1h ≤ SL (لا بيع، نستمر في ملاحقة الأهداف).
      4) إلغاء الصفقة إذا فشل جلب السعر لـ 10 دقائق متواصلة.
      5) تصنيف نتيجة الصفقة:
         - نستخدم classify_pnl (من Section 3) → "closed" إذا الربح ≥ 2%، وإلا "drwn".
      6) تسجيل النتيجة في:
         - TRADES_FILE via _finalize_trade_record
         - register_trade_outcome (Counters)
         - تحديث next_track_index على "closed" فقط.
    """
    sym_norm = normalize_symbol(symbol)
    sim_flag = bool(is_simulation()) if callable(is_simulation) else False

    try:
        pair = format_symbol(symbol)
    except Exception:
        pair = symbol

    # ===== meta من KuCoin =====
    meta = None
    try:
        if callable(get_symbol_meta):
            meta = get_symbol_meta(pair)
    except Exception as e:
        _console_echo(f"[META] get_symbol_meta error for {sym_norm}: {e}")

    if not meta:
        if callable(send_notification_tc):
            await send_notification_tc(
                "❌ Meta fetch failed. Cancel trade.",
                symbol=sym_norm
            )
        # فشل كامل → نعتبرها failed
        register_trade_outcome(str(track_num), "failed") if callable(register_trade_outcome) else None
        _finalize_trade_record(trade_id, "failed", 0.0, 0.0, 0.0, 0.0)
        # حرّر الخانة
        if get_trade_structure and save_trade_structure:
            s = get_trade_structure()
            slot_cell = (s.get("slots") or {}).get(str(slot_id))
            if slot_cell:
                s["slots"][str(slot_id)] = None
                save_trade_structure(s)
        return

    quote_inc = float(meta["quoteIncrement"])
    base_inc = float(meta["baseIncrement"])
    min_base = float(meta["baseMinSize"])

    # ===== أهداف مرتبَة =====
    try:
        targets = [float(x) for x in (targets or []) if x is not None]
    except Exception:
        targets = []
    if not targets:
        targets = [float(entry_price * 1.01)]  # احتياط
    targets = sorted(targets)
    tp1_val = float(targets[0])

    # ===== حالة الصفقة =====
    bought_price: Optional[float] = None
    qty: float = 0.0
    start_time: Optional[datetime] = None

    highest_idx = -1  # أعلى TP مُلامس
    trailing_armed = False
    max_after_touch: Optional[float] = None
    last_tp_floor: Optional[float] = None

    last_price_ok_ts = time.time()
    sl_alerted = False

    # ===== Helper: finalize trade (بيع) =====
    async def _do_market_sell(exec_price_hint: Optional[float]) -> Tuple[float, float, float, float]:
        """
        تنفيذ أمر بيع Market:
          - يعيد: (sell_price, sell_qty, pnl_usdt, pnl_pct)
        """
        nonlocal qty, bought_price

        adj_qty = quantize_down(qty * 0.9998, base_inc) if callable(quantize_down) else qty * 0.9998
        if adj_qty < min_base or adj_qty <= 0.0:
            raise RuntimeError("adjusted qty below min_base")

        order = place_market_order(
            pair, "sell",
            size=str(adj_qty),
            symbol_hint=sym_norm,
            sim_override=bool(sim_flag)
        ) if callable(place_market_order) else None

        await asyncio.sleep(1)

        if order and isinstance(order, dict):
            order_id = order.get("orderId")
        else:
            order_id = None

        if order_id and callable(get_order_deal_size):
            filled_qty, deal_funds = await get_order_deal_size(
                order_id, symbol=sym_norm, sim_override=bool(sim_flag)
            )
            if filled_qty <= 0.0:
                raise RuntimeError("sell order filled_qty = 0")
            sell_price = float(deal_funds) / float(filled_qty)
            sell_qty = float(filled_qty)
        else:
            # fallback تقريبي
            sell_price = float(exec_price_hint or bought_price or entry_price)
            sell_qty = float(adj_qty)

        bp = float(bought_price or entry_price)
        pnl_usdt = (sell_price - bp) * sell_qty
        pct = ((sell_price - bp) / max(bp, 1e-12)) * 100.0

        return sell_price, sell_qty, pnl_usdt, pct

    async def _finalize_and_cleanup(
        final_status: str,
        sell_price: float,
        sell_qty: float,
        pnl_usdt: float,
        pnl_pct: float,
        tag: str
    ):
        """تحديث الملفات + counters + pointer + إشعار نهائي + تحرير الـ Slot."""
        # 1) summary (اختياري)
        try:
            if callable(accumulate_summary):
                if pnl_usdt >= 0:
                    accumulate_summary(profit_delta=float(pnl_usdt))
                else:
                    accumulate_summary(loss_delta=float(-pnl_usdt))
        except Exception:
            pass

        # 2) TRADES_FILE
        try:
            _finalize_trade_record(trade_id, final_status, sell_price, sell_qty, pnl_usdt, pnl_pct)
        except Exception:
            pass

        # 3) Counters (structure)
        try:
            if callable(register_trade_outcome):
                register_trade_outcome(str(track_num), final_status)
        except Exception:
            pass

        # 4) مسار next_track_index (يتقدّم فقط عند closed ≥ 2%)
        try:
            _update_track_pointer_on_result(final_status)
        except Exception:
            pass

        # 5) تحرير الـ Slot
        if get_trade_structure and save_trade_structure:
            s = get_trade_structure()
            slots = s.get("slots") or {}
            cell = slots.get(str(slot_id))
            if cell:
                slots[str(slot_id)] = None
                s["slots"] = slots
                save_trade_structure(s)

        # 6) إشعار نهائي
        dur_str = ""
        try:
            if start_time:
                delta = datetime.now(timezone.utc) - start_time
                dur_str = f"{delta.days}d / {delta.seconds // 3600}h / {(delta.seconds % 3600)//60}m"
        except Exception:
            pass

        if callable(send_notification_tc):
            emoji = "🟢" if final_status == "closed" else "🔴"
            await send_notification_tc(
                (
                    f"{emoji} Auto SELL — {tag}\n"
                    f"💰 Buy: {float(bought_price or entry_price):.6f} → Sell: {sell_price:.6f}\n"
                    f"📦 Qty: {sell_qty:.6f} | 💵 Amount: {amount:.2f} USDT\n"
                    f"💵 PnL: {pnl_usdt:.4f} USDT  ({pnl_pct:+.2f}%)\n"
                    f"{('⏱️ ' + dur_str) if dur_str else ''}"
                ),
                symbol=sym_norm
            )

    # ========== الحلقة الرئيسية ==========
    try:
        while True:
            # ---- حارس مبكر: إذا تم مسح الخانة أو تغيير الحالة لشيء نهائي، أوقف المراقبة ----
            try:
                if get_trade_structure:
                    s_now = get_trade_structure()
                    cell_now = (s_now.get("slots") or {}).get(str(slot_id))
                    if not cell_now:
                        return
                    st_now = (cell_now.get("status") or "").lower()
                    if st_now not in ("open", "reserved", "buy"):
                        return
            except Exception:
                pass

            # ---- جلب السعر الحالي ----
            price = await fetch_current_price(sym_norm)
            if price is None:
                if (time.time() - last_price_ok_ts) >= PRICE_TIMEOUT_SEC and bought_price is None:
                    # فشل 10 دقائق قبل الدخول → نعتبر الصفقة failed ونحرر الـSlot
                    if callable(send_notification_tc):
                        await send_notification_tc(
                            "⛔️ Canceled: لم يتم الحصول على سعر لمدة 10 دقائق. تم إلغاء الصفقة.",
                            symbol=sym_norm
                        )
                    try:
                        _finalize_trade_record(trade_id, "failed", 0.0, 0.0, 0.0, 0.0)
                        if callable(register_trade_outcome):
                            register_trade_outcome(str(track_num), "failed")
                    except Exception:
                        pass
                    if get_trade_structure and save_trade_structure:
                        s = get_trade_structure()
                        slots = s.get("slots") or {}
                        if str(slot_id) in slots:
                            slots[str(slot_id)] = None
                            s["slots"] = slots
                            save_trade_structure(s)
                    return
                await asyncio.sleep(60)
                continue
            else:
                last_price_ok_ts = time.time()

            # =================== تنفيذ الشراء ===================
            if bought_price is None and price <= float(entry_price) + EPS:
                try:
                    # حجم USDT المخطّط
                    funds_planned = quantize_down(amount, meta["quoteIncrement"]) if callable(quantize_down) else amount
                    if funds_planned <= 0:
                        if callable(send_notification_tc):
                            await send_notification_tc("⚠️ Funds too small.", symbol=sym_norm)
                        _finalize_trade_record(trade_id, "failed", 0.0, 0.0, 0.0, 0.0)
                        if callable(register_trade_outcome):
                            register_trade_outcome(str(track_num), "failed")
                        return

                    # رصيد USDT في حساب التداول
                    available_usdt = get_trade_balance_usdt(sim_override=sim_flag) if callable(get_trade_balance_usdt) else funds_planned
                    if available_usdt <= 0:
                        if callable(send_notification_tc):
                            await send_notification_tc("❌ Buy failed: USDT balance is 0.", symbol=sym_norm)
                        _finalize_trade_record(trade_id, "failed", 0.0, 0.0, 0.0, 0.0)
                        if callable(register_trade_outcome):
                            register_trade_outcome(str(track_num), "failed")
                        return

                    funds = min(funds_planned, available_usdt)
                    funds = quantize_down(funds, meta["quoteIncrement"]) if callable(quantize_down) else funds
                    if funds <= 0:
                        if callable(send_notification_tc):
                            await send_notification_tc(
                                "❌ Buy failed: not enough USDT after quantization.",
                                symbol=sym_norm
                            )
                        _finalize_trade_record(trade_id, "failed", 0.0, 0.0, 0.0, 0.0)
                        if callable(register_trade_outcome):
                            register_trade_outcome(str(track_num), "failed")
                        return

                    est_qty = funds / max(price, 1e-12)
                    est_qty_q = quantize_down(est_qty, base_inc) if callable(quantize_down) else est_qty
                    if est_qty_q < min_base:
                        min_needed = min_base * price
                        if callable(send_notification_tc):
                            await send_notification_tc(
                                (
                                    "❌ Buy blocked: amount too small for pair min size.\n"
                                    f"• est_qty={est_qty_q:.8f} < baseMinSize={min_base}\n"
                                    f"• Approx min USDT needed: {min_needed:.4f}"
                                ),
                                symbol=sym_norm
                            )
                        _finalize_trade_record(trade_id, "failed", 0.0, 0.0, 0.0, 0.0)
                        if callable(register_trade_outcome):
                            register_trade_outcome(str(track_num), "failed")
                        return

                    order = place_market_order(
                        pair, "buy",
                        funds=str(funds),
                        symbol_hint=sym_norm,
                        sim_override=bool(sim_flag)
                    ) if callable(place_market_order) else None

                    if not order or not isinstance(order, dict) or not order.get("orderId"):
                        if callable(send_notification_tc):
                            await send_notification_tc("❌ Buy error: no orderId returned.", symbol=sym_norm)
                        _finalize_trade_record(trade_id, "failed", 0.0, 0.0, 0.0, 0.0)
                        if callable(register_trade_outcome):
                            register_trade_outcome(str(track_num), "failed")
                        return

                    order_id = order["orderId"]
                    await asyncio.sleep(1)

                    if callable(get_order_deal_size):
                        filled_qty, deal_funds = await get_order_deal_size(
                            order_id, symbol=sym_norm, sim_override=bool(sim_flag)
                        )
                    else:
                        filled_qty, deal_funds = est_qty_q, est_qty_q * price

                    if filled_qty <= 0.0:
                        if callable(send_notification_tc):
                            await send_notification_tc(
                                "❌ Buy issue: order executed but filled size = 0.",
                                symbol=sym_norm
                            )
                        _finalize_trade_record(trade_id, "failed", 0.0, 0.0, 0.0, 0.0)
                        if callable(register_trade_outcome):
                            register_trade_outcome(str(track_num), "failed")
                        return

                    qty = float(filled_qty)
                    bought_price = float(deal_funds) / float(filled_qty)
                    start_time = datetime.now(timezone.utc)

                    # تحديث الخانة في structure
                    if get_trade_structure and save_trade_structure:
                        s = get_trade_structure()
                        slots = s.get("slots") or {}
                        cell = slots.get(str(slot_id)) or {}
                        cell["status"] = "buy"
                        cell["start_time"] = start_time.isoformat()
                        cell["filled_qty"] = qty
                        cell["bought_price"] = bought_price
                        slots[str(slot_id)] = cell
                        s["slots"] = slots
                        save_trade_structure(s)

                    # تحديث TRADES_FILE للشراء
                    _update_trade_on_buy(trade_id, bought_price, qty)

                    sim_tag = " (SIM)" if sim_flag else ""
                    if callable(send_notification_tc):
                        await send_notification_tc(
                            (
                                f"✅ Bought{sim_tag}\n"
                                f"💰 Price: {bought_price:.6f}\n"
                                f"📦 Qty: {qty:.6f}\n"
                                f"💵 Amount: {amount:.2f} USDT\n"
                                f"🔢 Track {track_num} | Slot {slot_id}"
                            ),
                            symbol=sym_norm
                        )

                except Exception as e:
                    _console_echo(f"[BUY] error on {sym_norm}: {e}")
                    if callable(send_notification_tc):
                        await send_notification_tc(f"❌ Buy execution error: {e}", symbol=sym_norm)
                    _finalize_trade_record(trade_id, "failed", 0.0, 0.0, 0.0, 0.0)
                    if callable(register_trade_outcome):
                        register_trade_outcome(str(track_num), "failed")
                    return

            # =================== بعد الشراء: إدارة الخروج ===================
            poll_sec = 60

            if bought_price is not None:
                # كمية للبيع (مع هامش صغير) + التحقق من min_base
                adj_qty = quantize_down(qty * 0.9998, base_inc) if callable(quantize_down) else qty * 0.9998
                if adj_qty < min_base or adj_qty <= 0.0:
                    if callable(send_notification_tc):
                        await send_notification_tc(
                            "⚠️ Adjusted qty < min size. Cancel sell logic.",
                            symbol=sym_norm
                        )
                    # نعتبرها failed تقنياً، لكن نترك الخانة للتدخّل اليدوي
                    _finalize_trade_record(trade_id, "failed", 0.0, 0.0, 0.0, 0.0)
                    if callable(register_trade_outcome):
                        register_trade_outcome(str(track_num), "failed")
                    return

                # -------- TP ladder (بدون بيع على الملامسة) --------
                progressed = False
                while (highest_idx + 1) < len(targets) and price >= float(targets[highest_idx + 1]) - EPS:
                    highest_idx += 1
                    progressed = True
                    last_tp_floor = float(targets[highest_idx])

                if progressed:
                    if not trailing_armed and price >= tp1_val - EPS:
                        # تفعيل التريلينغ عند لمس TP1
                        trailing_armed = True
                        max_after_touch = price
                        last_tp_floor = max(last_tp_floor or 0.0, tp1_val)
                        if callable(send_notification_tc):
                            await send_notification_tc(
                                (
                                    "🟢 Trailing-1% ARMED (on TP1 touch).\n"
                                    f"• TP1: {tp1_val:.6f} | Price: {price:.6f}\n"
                                    "• Floor ≥ last TP touched"
                                ),
                                symbol=sym_norm
                            )
                    else:
                        if trailing_armed:
                            if max_after_touch is None or price > max_after_touch:
                                max_after_touch = price
                            last_tp_floor = max(last_tp_floor or 0.0, float(targets[highest_idx]))

                    next_label = (
                        f"TP{highest_idx + 2}"
                        if (highest_idx + 1) < len(targets)
                        else "TRAILING-ONLY"
                    )
                    if callable(send_notification_tc):
                        await send_notification_tc(
                            f"➡️ {sym_norm} — Track {track_num} | Slot {slot_id} — touched TP{highest_idx+1} "
                            f"({float(targets[highest_idx]):.6f}); moving to {next_label}.",
                            symbol=sym_norm
                        )

                # -------- Trailing logic --------
                if trailing_armed:
                    poll_sec = 10  # بعد التفعيل نراقب بسرعة أعلى

                    # تحديث القمّة
                    if max_after_touch is None or price > max_after_touch:
                        max_after_touch = price

                    enforced_floor = max(float(last_tp_floor or 0.0), tp1_val)
                    raw_trigger = (max_after_touch or price) * (1.0 - RETRACE_PERCENT / 100.0)

                    try:
                        # (A) كسر الأرضية → بيع فوري
                        if price < enforced_floor - EPS:
                            sell_price, sell_qty, pnl_usdt, pnl_pct = await _do_market_sell(exec_price_hint=price)
                            res = classify_pnl(float(bought_price), float(sell_price)) if callable(classify_pnl) else {"status": "drwn", "pct": pnl_pct}
                            final_status = (res.get("status") or "drwn").lower()
                            await _finalize_and_cleanup(final_status, sell_price, sell_qty, pnl_usdt, res.get("pct", pnl_pct))
                            break

                        # (B) هبوط ≥1% من القمّة مع البقاء فوق الأرضية
                        elif price <= raw_trigger + EPS and price >= enforced_floor - EPS:
                            sell_price, sell_qty, pnl_usdt, pnl_pct = await _do_market_sell(exec_price_hint=price)
                            res = classify_pnl(float(bought_price), float(sell_price)) if callable(classify_pnl) else {"status": "drwn", "pct": pnl_pct}
                            final_status = (res.get("status") or "drwn").lower()
                            await _finalize_and_cleanup(final_status, sell_price, sell_qty, pnl_usdt, res.get("pct", pnl_pct))
                            break
                    except Exception as e:
                        _console_echo(f"[SELL] trailing error on {sym_norm}: {e}")
                        if callable(send_notification_tc):
                            await send_notification_tc(
                                f"❌ Sell (trail) failed: {e}",
                                symbol=sym_norm
                            )
                        # نعتبرها failed
                        _finalize_trade_record(trade_id, "failed", 0.0, 0.0, 0.0, 0.0)
                        if callable(register_trade_outcome):
                            register_trade_outcome(str(track_num), "failed")
                        return

                # -------- SL: إشعار فقط بدون بيع --------
                if not sl_alerted and start_time is not None and callable(get_latest_candle) and callable(_interval_to_ms):
                    candle = get_latest_candle(sym_norm, interval="1hour")
                    now_ms = datetime.now(timezone.utc).timestamp() * 1000.0
                    if candle:
                        interval_ms = _interval_to_ms("1hour")
                        candle_start_ms = float(candle["timestamp"])
                        candle_end_ms = candle_start_ms + interval_ms
                        trade_start_ms = start_time.timestamp() * 1000.0
                        if (
                            candle_end_ms <= now_ms
                            and candle_end_ms > trade_start_ms
                            and candle["close"] <= float(sl_price) + EPS
                        ):
                            sl_alerted = True
                            if callable(send_notification_tc):
                                await send_notification_tc(
                                    (
                                        "🛑 SL touched (no sell).\n"
                                        "➡️ Continuing to monitor for TP1/targets."
                                    ),
                                    symbol=sym_norm
                                )

            await asyncio.sleep(poll_sec)

    except Exception as e:
        _console_echo(f"[MONITOR] error on {sym_norm}: {e}")
        if callable(send_notification_tc):
            await send_notification_tc(
                f"⚠️ Monitor failed: {e}",
                symbol=sym_norm
            )
        # في أي انهيار غير متوقَّع نعتبر الصفقة failed (مع ترك slot للتدخل اليدوي إذا لزم)
        _finalize_trade_record(trade_id, "failed", 0.0, 0.0, 0.0, 0.0)
        if callable(register_trade_outcome):
            register_trade_outcome(str(track_num), "failed")
