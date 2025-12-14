# ============================================
# Section 2) Helpers, Storage, Notifications, KuCoin wrappers
#   - بنية البيانات الجديدة (Tracks + Slots فقط، بدون دورات)
#   - cycle_slots = الحد الأقصى لعدد الصفقات المفتوحة في نفس الوقت
#   - مسارات ثابتة بمبالغ تصاعدية 2% لكل مسار
#   - Email Gate + bot active flags
#   - إشعارات إلى حساب المالك OWNER_CHAT (Mohamad4992)
#   - KuCoin helpers (meta / price / balance / orders)
#   - Blacklist + Terminal notices
#   - Debug funds toggles
# ============================================

import os
import json
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, date
from typing import Any, Dict, List, Optional, Tuple, Set

# --------- Console echo (قادم من القسم الأول، مع fallback) ---------
try:
    console_echo  # type: ignore[name-defined]
except NameError:  # pragma: no cover
    def console_echo(msg: str) -> None:
        try:
            if bool(globals().get("ENABLE_CONSOLE_ECHO", True)):
                print(msg)
        except Exception:
            print(msg)

_console_echo = console_echo

# --------- استيراد/استخدام بعض ثوابت القسم الأول ---------
TRADES_FILE = globals().get("TRADES_FILE", "trades.json")
SUMMARY_FILE = globals().get("SUMMARY_FILE", "summary.json")
TERMINAL_LOG_FILE = globals().get("TERMINAL_LOG_FILE", "terminal_notices.json")
EMAIL_STATE_FILE = globals().get("EMAIL_STATE_FILE", "email_gate_state.json")
BLACKLIST_FILE = globals().get("BLACKLIST_FILE", "blacklist.json")
STRUCTURE_FILE = globals().get("STRUCTURE_FILE", "trade_structure.json")

OWNER_CHAT = globals().get("OWNER_CHAT", "me")
SECONDARY_CHAT = globals().get("SECONDARY_CHAT")

DEFAULT_TRACK_COUNT = int(globals().get("DEFAULT_TRACK_COUNT", 10))
DEFAULT_CYCLE_SLOTS = int(globals().get("DEFAULT_CYCLE_SLOTS", 10))
INITIAL_TRADE_AMOUNT = float(globals().get("INITIAL_TRADE_AMOUNT", 50.0))
INCREMENT_PCT = float(globals().get("INCREMENT_PCT", 0.02))

# KuCoin client و وضع المحاكاة من القسم الأول
kucoin = globals().get("kucoin")
IS_SIMULATION = bool(globals().get("IS_SIMULATION", False))

# Telegram client من القسم الأول
client = globals().get("client")

# --------- Helpers عامة ---------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def utc_ts() -> float:
    return utc_now().timestamp()

def normalize_symbol(sym: str) -> str:
    """تحويل الرمز لصيغة موحّدة: بدون شرطة أو سلاش وبأحرف كبيرة."""
    return (sym or "").upper().replace("-", "").replace("/", "")

def format_symbol(sym: str) -> str:
    """
    تحويل الرمز لصيغة KuCoin القياسية: BTCUSDT → BTC-USDT.
    إذا كان يحتوي على '-' نفترض أنه جاهز.
    """
    s = (sym or "").upper()
    if "-" in s:
        return s
    s_norm = normalize_symbol(s)
    QUOTES = ("USDT", "BTC", "ETH", "EUR", "KCS")
    for q in QUOTES:
        if s_norm.endswith(q):
            base = s_norm[:-len(q)]
            return f"{base}-{q}"
    return s_norm

def track_base_amount(track_index: int) -> float:
    """
    مبلغ المسار: المسار 1 = INITIAL_TRADE_AMOUNT،
    كل مسار بعده يزيد 2% (INCREMENT_PCT) عن السابق (تراكمياً).
    """
    try:
        idx = int(track_index)
        if idx <= 1:
            return float(INITIAL_TRADE_AMOUNT)
        amt = float(INITIAL_TRADE_AMOUNT)
        for _ in range(2, idx + 1):
            amt *= (1.0 + INCREMENT_PCT)
        return float(round(amt, 2))
    except Exception:
        return float(INITIAL_TRADE_AMOUNT)

def quantize_down(value: float, step: float) -> float:
    """تقريب value للأسفل لأقرب مضاعف من step."""
    try:
        step = float(step)
        if step <= 0:
            return float(value)
        return float((int(value / step)) * step)
    except Exception:
        return float(value)

# --------- Debug FUNDS toggles ---------
_DEBUG_FUNDS_STATE: Dict[str, Any] = {
    "enabled": False,
    "expires_at": 0.0,
}

def enable_debug_funds(minutes: int = 0) -> None:
    """
    تفعيل وضع DEBUG_FUNDS:
      minutes = 0  → بدون انتهاء.
      minutes > 0  → ينتهي بعد N دقيقة.
    """
    try:
        global _DEBUG_FUNDS_STATE
        _DEBUG_FUNDS_STATE["enabled"] = True
        if minutes and minutes > 0:
            _DEBUG_FUNDS_STATE["expires_at"] = time.time() + (minutes * 60.0)
        else:
            _DEBUG_FUNDS_STATE["expires_at"] = 0.0
        console_echo(f"[DEBUG_FUNDS] enabled for {minutes} minute(s)" if minutes else "[DEBUG_FUNDS] enabled (no expiry)")
    except Exception as e:
        console_echo(f"[DEBUG_FUNDS] enable error: {e}")

def disable_debug_funds() -> None:
    try:
        global _DEBUG_FUNDS_STATE
        _DEBUG_FUNDS_STATE["enabled"] = False
        _DEBUG_FUNDS_STATE["expires_at"] = 0.0
        console_echo("[DEBUG_FUNDS] disabled")
    except Exception as e:
        console_echo(f"[DEBUG_FUNDS] disable error: {e}")

def is_debug_funds() -> bool:
    try:
        st = _DEBUG_FUNDS_STATE
        if not st.get("enabled"):
            return False
        exp = float(st.get("expires_at") or 0.0)
        if exp and time.time() > exp:
            # انتهى الوقت
            disable_debug_funds()
            return False
        return True
    except Exception:
        return False

# --------- Email Gate + Bot Active ---------
# بوابة الإيميل تتحكم فقط باستقبال توصيات القناة
# (لا تؤثر على إدارة الصفقات المفتوحة).
_BOT_ACTIVE = True  # pause / reuse

def is_bot_active() -> bool:
    return bool(_BOT_ACTIVE)

def set_bot_active(value: bool) -> None:
    global _BOT_ACTIVE
    _BOT_ACTIVE = bool(value)
    console_echo(f"[BOT] active = {value}")

def _load_email_state() -> Dict[str, Any]:
    try:
        if os.path.exists(EMAIL_STATE_FILE):
            with open(EMAIL_STATE_FILE, "r") as f:
                data = json.load(f) or {}
            return data
    except Exception as e:
        console_echo(f"[GATE] load state error: {e}")
    return {"gate_open": True}

def _save_email_state(data: Dict[str, Any]) -> None:
    try:
        with open(EMAIL_STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        console_echo(f"[GATE] save state error: {e}")

def is_email_gate_open() -> bool:
    """قراءة حالة بوابة الإيميل من ملف الحالة (افتراضي: مفتوحة)."""
    try:
        data = _load_email_state()
        return bool(data.get("gate_open", True))
    except Exception:
        return True

def set_email_gate(open_flag: bool) -> None:
    """تغيير حالة بوابة الإيميل وتخزينها في EMAIL_STATE_FILE."""
    try:
        data = _load_email_state()
        data["gate_open"] = bool(open_flag)
        _save_email_state(data)
        console_echo(f"[GATE] email gate → {'OPEN' if open_flag else 'CLOSED'}")
    except Exception as e:
        console_echo(f"[GATE] set_email_gate error: {e}")

def should_accept_recommendations() -> bool:
    """
    المصدر الموحد لمعرفة هل نستقبل توصيات القناة:
      - البوت مفعّل (pause / reuse)
      - بوابة الإيميل مفتوحة
    """
    try:
        return is_bot_active() and is_email_gate_open()
    except Exception:
        return True

# --------- Trade Structure (Tracks + Slots) ---------
# بنية جديدة:
# {
#   "tracks": {
#       "1": {"amount": 50.0, "slot": {...} أو None},
#       "2": {"amount": 51.0, "slot": {...} أو None},
#       ...
#   },
#   "cycle_slots": 10,   # الحد الأقصى للصفقات المفتوحة بنفس الوقت
#   "cycle_count": 10,   # alias لـ cycle_slots (لأوامر cycl)
#   "total_trades": 0,
#   "total_successful_trades": 0,
#   "total_failed_trades": 0,
#   "total_drawdown_trades": 0,
#   "daily_successful_trades": {"yyyy-mm-dd": n}
# }

def _new_empty_structure() -> Dict[str, Any]:
    tracks: Dict[str, Any] = {}
    for i in range(1, DEFAULT_TRACK_COUNT + 1):
        tracks[str(i)] = {
            "amount": track_base_amount(i),
            "slot": None,
        }
    return {
        "tracks": tracks,
        "cycle_slots": int(DEFAULT_CYCLE_SLOTS),
        "cycle_count": int(DEFAULT_CYCLE_SLOTS),  # alias
        "total_trades": 0,
        "total_successful_trades": 0,
        "total_failed_trades": 0,
        "total_drawdown_trades": 0,
        "daily_successful_trades": {},
    }

def get_trade_structure() -> Dict[str, Any]:
    """
    تحميل بنية التداول من STRUCTURE_FILE أو إنشاؤها لأول مرة.
    يتأكد أن عدد المسارات = DEFAULT_TRACK_COUNT على الأقل.
    """
    data: Dict[str, Any]
    if os.path.exists(STRUCTURE_FILE):
        try:
            with open(STRUCTURE_FILE, "r") as f:
                data = json.load(f) or {}
        except Exception as e:
            console_echo(f"[STRUCTURE] read error: {e}")
            data = _new_empty_structure()
    else:
        data = _new_empty_structure()

    # تأكد من وجود الحقول الأساسية
    if "tracks" not in data or not isinstance(data["tracks"], dict):
        data = _new_empty_structure()
    tracks = data["tracks"]

    # تأكد أن عدد المسارات >= DEFAULT_TRACK_COUNT
    for i in range(1, DEFAULT_TRACK_COUNT + 1):
        key = str(i)
        if key not in tracks or not isinstance(tracks[key], dict):
            tracks[key] = {"amount": track_base_amount(i), "slot": None}
        else:
            # تأكد من وجود amount/slot
            if "amount" not in tracks[key]:
                tracks[key]["amount"] = track_base_amount(i)
            if "slot" not in tracks[key]:
                tracks[key]["slot"] = None

    data["tracks"] = tracks
    if "cycle_slots" not in data:
        data["cycle_slots"] = int(DEFAULT_CYCLE_SLOTS)
    if "cycle_count" not in data:
        data["cycle_count"] = int(data.get("cycle_slots", DEFAULT_CYCLE_SLOTS))

    if "total_trades" not in data:
        data["total_trades"] = 0
    if "total_successful_trades" not in data:
        data["total_successful_trades"] = 0
    if "total_failed_trades" not in data:
        data["total_failed_trades"] = 0
    if "total_drawdown_trades" not in data:
        data["total_drawdown_trades"] = 0
    if "daily_successful_trades" not in data:
        data["daily_successful_trades"] = {}

    return data

def save_trade_structure(structure: Dict[str, Any]) -> None:
    try:
        with open(STRUCTURE_FILE, "w") as f:
            json.dump(structure, f, indent=2)
    except Exception as e:
        console_echo(f"[STRUCTURE] save error: {e}")

def get_effective_max_open(structure: Optional[Dict[str, Any]] = None) -> int:
    """
    الحد الأقصى للصفقات المفتوحة في نفس الوقت = cycle_slots
    (مع سقف عدد المسارات المتاحة فعلياً).
    """
    if structure is None:
        structure = get_trade_structure()
    try:
        max_slots = int(structure.get("cycle_slots", DEFAULT_CYCLE_SLOTS))
    except Exception:
        max_slots = DEFAULT_CYCLE_SLOTS
    tracks = structure.get("tracks", {}) or {}
    return max(1, min(max_slots, len(tracks)))

def count_open_positions(structure: Optional[Dict[str, Any]] = None) -> int:
    """
    حساب عدد الصفقات المفتوحة فعلياً (status in open/buy/reserved).
    """
    if structure is None:
        structure = get_trade_structure()
    tracks = structure.get("tracks", {}) or {}
    cnt = 0
    for tdata in tracks.values():
        cell = tdata.get("slot")
        if not isinstance(cell, dict):
            continue
        st = (cell.get("status") or "").lower()
        if st in ("open", "buy", "reserved"):
            cnt += 1
    return cnt

def find_available_track(structure: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """
    إيجاد أول مسار متاح (slot فارغ أو ليس عليه حالة open/buy/reserved).
    لا يتحقق من الحد الأقصى cycle_slots هنا.
    """
    if structure is None:
        structure = get_trade_structure()
    tracks = structure.get("tracks", {}) or {}
    for tkey in sorted(tracks.keys(), key=lambda x: int(x)):
        cell = tracks[tkey].get("slot")
        if not isinstance(cell, dict) or not cell.get("status"):
            return str(tkey)
        st = (cell.get("status") or "").lower()
        if st not in ("open", "buy", "reserved"):
            return str(tkey)
    return None

def update_slot(structure: Dict[str, Any], track_num: str, cell: Optional[Dict[str, Any]]) -> None:
    """
    تحديث خانة مسار معيّن (slot) بخانة جديدة أو تفريغها.
    """
    tkey = str(track_num)
    if "tracks" not in structure:
        structure["tracks"] = {}
    if tkey not in structure["tracks"]:
        structure["tracks"][tkey] = {"amount": track_base_amount(int(tkey)), "slot": None}
    structure["tracks"][tkey]["slot"] = cell

# --------- TRADES_FILE helpers ---------
def _ensure_trades_file() -> Dict[str, Any]:
    """تحميل TRADES_FILE (مع ضمان الشكل الأساسي)."""
    data: Dict[str, Any] = {"trades": []}
    try:
        if os.path.exists(TRADES_FILE):
            with open(TRADES_FILE, "r") as f:
                loaded = json.load(f) or {}
            if isinstance(loaded.get("trades"), list):
                data = loaded
    except Exception as e:
        console_echo(f"[TRADES] read error: {e}")
    return data

def _save_trades_file(data: Dict[str, Any]) -> None:
    try:
        with open(TRADES_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        console_echo(f"[TRADES] save error: {e}")

def append_trade_record(record: Dict[str, Any]) -> None:
    """
    إضافة سجل صفقة جديد إلى TRADES_FILE.
    يُفترض أن يحتوي على:
      symbol / entry / sl / targets / track_num / amount / status / opened_at ...
    """
    data = _ensure_trades_file()
    trades = data.get("trades", [])
    trades.append(record)
    data["trades"] = trades
    _save_trades_file(data)

def update_trade_status(symbol: str, status: str, track_num: Optional[str] = None) -> None:
    """
    تحديث حالة صفقة في TRADES_FILE (آخر صفقة لهذا الرمز/المسار).
    """
    try:
        data = _ensure_trades_file()
        trades = data.get("trades", [])
        sym_norm = normalize_symbol(symbol)
        last_idx = None
        for i, tr in enumerate(trades):
            if normalize_symbol(tr.get("symbol", "")) != sym_norm:
                continue
            if track_num is not None and str(tr.get("track_num")) != str(track_num):
                continue
            last_idx = i
        if last_idx is None:
            return
        trades[last_idx]["status"] = status
        if status in ("closed", "stopped", "failed", "drwn"):
            trades[last_idx]["closed_at"] = utc_ts()
        data["trades"] = trades
        _save_trades_file(data)
    except Exception as e:
        console_echo(f"[TRADES] update_trade_status error: {e}")

def _update_trade_exec_fields(
    symbol: str,
    track_num: str,
    bought_price: Optional[float] = None,
    sell_price: Optional[float] = None,
    sell_qty: Optional[float] = None,
) -> None:
    """
    تحديث حقول التنفيذ (سعر الشراء/البيع والكمية) في TRADES_FILE.
    """
    try:
        data = _ensure_trades_file()
        trades = data.get("trades", [])
        sym_norm = normalize_symbol(symbol)
        last_idx = None
        for i, tr in enumerate(trades):
            if normalize_symbol(tr.get("symbol", "")) != sym_norm:
                continue
            if str(tr.get("track_num")) != str(track_num):
                continue
            last_idx = i
        if last_idx is None:
            return
        tr = trades[last_idx]
        if bought_price is not None:
            tr["bought_price"] = float(bought_price)
            tr.setdefault("bought_at", utc_ts())
        if sell_price is not None:
            tr["sell_price"] = float(sell_price)
            tr.setdefault("sold_at", utc_ts())
        if sell_qty is not None:
            tr["sell_qty"] = float(sell_qty)
        trades[last_idx] = tr
        data["trades"] = trades
        _save_trades_file(data)
    except Exception as e:
        console_echo(f"[TRADES] _update_trade_exec_fields error: {e}")

# --------- Summary (PnL) ---------
def accumulate_summary(profit_delta: float = 0.0, loss_delta: float = 0.0) -> None:
    """
    تحديث SUMMARY_FILE بمجموع الربح/الخسارة المحققة.
    """
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
        console_echo(f"[SUMMARY] accumulate_summary error: {e}")

# --------- Terminal Notices ---------
def log_terminal_notification(msg: str, tag: Optional[str] = None) -> None:
    """
    تسجيل إشعار بسيط في TERMINAL_LOG_FILE على شكل:
      { "msg": {"count": N, "last_ts": ...}, ... }
    """
    try:
        data: Dict[str, Any] = {}
        if os.path.exists(TERMINAL_LOG_FILE):
            with open(TERMINAL_LOG_FILE, "r") as f:
                data = json.load(f) or {}
        key = msg if tag is None else tag
        if key not in data:
            data[key] = {"count": 0, "last_ts": 0}
        data[key]["count"] = int(data[key].get("count", 0)) + 1
        data[key]["last_ts"] = utc_ts()
        with open(TERMINAL_LOG_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        console_echo(f"[TERMINAL_LOG] error: {e}")

# --------- Blacklist ---------
def _load_blacklist() -> Set[str]:
    try:
        if os.path.exists(BLACKLIST_FILE):
            with open(BLACKLIST_FILE, "r") as f:
                arr = json.load(f) or []
            return set(normalize_symbol(s) for s in arr if s)
    except Exception as e:
        console_echo(f"[BLACKLIST] read error: {e}")
    return set()

def _save_blacklist(symbols: Set[str]) -> None:
    try:
        with open(BLACKLIST_FILE, "w") as f:
            json.dump(sorted(list(symbols)), f, indent=2)
    except Exception as e:
        console_echo(f"[BLACKLIST] save error: {e}")

def add_to_blacklist(symbol: str) -> bool:
    s_norm = normalize_symbol(symbol)
    bl = _load_blacklist()
    if s_norm in bl:
        return False
    bl.add(s_norm)
    _save_blacklist(bl)
    return True

def remove_from_blacklist(symbol: str) -> bool:
    s_norm = normalize_symbol(symbol)
    bl = _load_blacklist()
    if s_norm not in bl:
        return False
    bl.remove(s_norm)
    _save_blacklist(bl)
    return True

def list_blacklist() -> List[str]:
    return sorted(list(_load_blacklist()))

def _is_blocked_symbol(symbol: str) -> bool:
    return normalize_symbol(symbol) in _load_blacklist()

# --------- KuCoin Meta / Price / Balance / Orders ---------
_SYMBOL_META_CACHE: Dict[str, Dict[str, Any]] = {}

def get_symbol_meta(pair: str) -> Optional[Dict[str, Any]]:
    """
    جلب معلومات الزوج من KuCoin (baseMinSize / baseIncrement / quoteIncrement).
    يستخدم كاش داخلي لتجنب التكرار.
    """
    try:
        global _SYMBOL_META_CACHE
        if pair in _SYMBOL_META_CACHE:
            return _SYMBOL_META_CACHE[pair]

        if kucoin is None:
            # fallback بسيط في وضع عدم توفر الكلينت
            meta = {
                "symbol": pair,
                "baseMinSize": 0.0001,
                "baseIncrement": 0.000001,
                "quoteIncrement": 0.0001,
            }
            _SYMBOL_META_CACHE[pair] = meta
            return meta

        # بعض نسخ مكتبة KuCoin تستعمل get_symbol_list فقط
        symbols = kucoin.get_symbol_list()
        for s in symbols:
            if (s.get("symbol") or "").upper() == pair.upper():
                base_min = float(s.get("baseMinSize", 0) or 0.0)
                base_inc = float(s.get("baseIncrement", base_min or 0) or 0.0)
                quote_inc = float(s.get("quoteIncrement", 0) or 0.0)
                meta = {
                    "symbol": pair,
                    "baseMinSize": base_min,
                    "baseIncrement": base_inc if base_inc > 0 else base_min,
                    "quoteIncrement": quote_inc if quote_inc > 0 else 0.0001,
                }
                _SYMBOL_META_CACHE[pair] = meta
                return meta

        return None
    except Exception as e:
        console_echo(f"[META] get_symbol_meta error for {pair}: {e}")
        return None

async def fetch_current_price(symbol: str) -> Optional[float]:
    """
    جلب آخر سعر من KuCoin.
    يسجل price_fetch_fail_<SYMBOL> في Terminal Notices عند الفشل.
    """
    pair = format_symbol(symbol)
    sym_norm = normalize_symbol(symbol)
    try:
        if kucoin is None:
            return None
        ticker = kucoin.get_ticker(symbol=pair)
        price_str = (ticker or {}).get("price")
        if price_str is None:
            log_terminal_notification(f"price_fetch_fail_{sym_norm}", tag=f"price_fetch_fail_{sym_norm}")
            return None
        return float(price_str)
    except Exception as e:
        console_echo(f"[PRICE] fetch error for {pair}: {e}")
        log_terminal_notification(f"price_fetch_fail_{sym_norm}", tag=f"price_fetch_fail_{sym_norm}")
        return None

def get_trade_balance_usdt(sim_override: bool = False) -> float:
    """
    قراءة الرصيد المتاح من USDT في حساب التداول.
    في وضع المحاكاة أو عدم توفر الكلينت نرجّع قيمة كبيرة.
    """
    try:
        if sim_override or IS_SIMULATION or kucoin is None:
            return float(os.getenv("SIM_USDT_BALANCE", "999999"))
        accts = kucoin.get_accounts()
        avail = 0.0
        for a in accts:
            if (a.get("currency") or "").upper() != "USDT":
                continue
            if (a.get("type") or "").lower() not in ("trade", "trading"):
                continue
            try:
                avail += float(a.get("available", 0) or 0.0)
            except Exception:
                continue
        return float(avail)
    except Exception as e:
        console_echo(f"[BALANCE] error: {e}")
        return 0.0

# --- أوامر السوق (حقيقي/محاكاة) ---
_SIM_ORDERS: Dict[str, Tuple[float, float]] = {}  # orderId → (filled_qty, deal_funds)

def _sim_place_order(
    pair: str,
    side: str,
    size: Optional[str] = None,
    funds: Optional[str] = None,
    symbol_hint: Optional[str] = None,
) -> Dict[str, Any]:
    """
    تنفيذ وهمي لأمر سوق في وضع المحاكاة.
    نحسب الكمية/القيمة على آخر سعر متاح من KuCoin (إن وجد).
    """
    sym = symbol_hint or pair
    price = None
    try:
        # محاولة جلب السعر بشكل متزامن (استدعاء بسيط على kucoin)
        if kucoin is not None:
            ticker = kucoin.get_ticker(symbol=pair)
            price_str = (ticker or {}).get("price")
            if price_str:
                price = float(price_str)
    except Exception:
        price = None

    if price is None:
        # fallback لسعر افتراضي
        price = 1.0

    qty = 0.0
    deal_funds = 0.0
    if funds is not None:
        deal_funds = float(funds)
        qty = deal_funds / max(price, 1e-12)
    elif size is not None:
        qty = float(size)
        deal_funds = qty * price
    else:
        raise ValueError("size or funds required in sim order")

    order_id = f"SIM-{int(time.time() * 1000)}"
    _SIM_ORDERS[order_id] = (qty, deal_funds)
    return {"orderId": order_id, "symbol": pair, "side": side, "price": price}

def place_market_order(
    pair: str,
    side: str,
    size: Optional[str] = None,
    funds: Optional[str] = None,
    symbol_hint: Optional[str] = None,
    sim_override: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    أمر سوق موحّد:
      - في الوضع الحقيقي → يرسل إلى KuCoin.
      - في وضع المحاكاة → يستخدم _sim_place_order.
    """
    try:
        if sim_override or IS_SIMULATION or kucoin is None:
            return _sim_place_order(pair, side, size=size, funds=funds, symbol_hint=symbol_hint)

        args: Dict[str, Any] = {"symbol": pair, "side": side}
        if size is not None:
            args["size"] = size
        if funds is not None:
            args["funds"] = funds

        # واجهة مكتبة KuCoin الرسمية: create_market_order(symbol, side, size=None, funds=None)
        order = kucoin.create_market_order(**args)
        console_echo(f"[ORDER] market {side} {pair} → {order}")
        return order
    except Exception as e:
        console_echo(f"[ORDER] place_market_order error: {e}")
        return None

async def get_order_deal_size(
    order_id: str,
    symbol: Optional[str] = None,
    sim_override: bool = False,
) -> Tuple[float, float]:
    """
    إرجاع (filled_qty, deal_funds) لأمر محدد.
    في وضع المحاكاة نقرأ من _SIM_ORDERS.
    """
    if not order_id:
        return 0.0, 0.0
    try:
        if sim_override or IS_SIMULATION or kucoin is None or order_id.startswith("SIM-"):
            if order_id in _SIM_ORDERS:
                return _SIM_ORDERS[order_id]
            return 0.0, 0.0

        info = kucoin.get_order(order_id)
        filled_size = float(info.get("dealSize", 0) or 0.0)
        deal_funds = float(info.get("dealFunds", 0) or 0.0)
        return filled_size, deal_funds
    except Exception as e:
        console_echo(f"[ORDER] get_order_deal_size error: {e}")
        return 0.0, 0.0

# --------- إشعارات تلغرام ---------
async def send_notification(message: str, to_telegram: bool = True) -> None:
    """
    إرسال رسالة بسيطة إلى حساب المالك OWNER_CHAT (Mohamad4992).
    يمكن توسيعها لاحقاً للإرسال إلى SECONDARY_CHAT أيضاً.
    """
    if not message:
        return
    try:
        _console_echo(f"[TG] {message}")
        if not to_telegram:
            return
        if client is None:
            return
        await client.send_message(OWNER_CHAT, message)
    except Exception as e:
        _console_echo(f"[TG] send_notification error: {e}")

async def send_notification_both(message: str) -> None:
    """
    إرسال نفس الرسالة إلى OWNER_CHAT ثم SECONDARY_CHAT (إن كان معرفاً).
    """
    await send_notification(message, to_telegram=True)
    if SECONDARY_CHAT:
        try:
            if client is not None:
                await client.send_message(SECONDARY_CHAT, message)
        except Exception as e:
            _console_echo(f"[TG] send_notification_both error: {e}")

async def send_notification_tc(
    message: str,
    symbol: Optional[str] = None,
    track_num: Optional[str] = None,
    style: str = "default",
) -> None:
    """
    مغلّف بسيط لإضافة تاغ الرمز/المسار في آخر الرسالة:
      "… for BTCUSDT — T 3"
    """
    try:
        tag = ""
        if symbol:
            tag_sym = normalize_symbol(symbol)
            if track_num is not None:
                tag = f" for {tag_sym} — T {track_num}"
            else:
                tag = f" for {tag_sym}"
        full = f"{message}{tag}"
        await send_notification(full, to_telegram=True)
    except Exception as e:
        _console_echo(f"[TG] send_notification_tc error: {e}")
