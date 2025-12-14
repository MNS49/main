# ============================================
# Section 3) Tracks & Slots Management (UPDATED)
#  - مسارات فقط (No dynamic cycle routing)
#  - لكل مسار خانة واحدة فقط: A{track_num}
#  - عدد الخانات الفعّالة = max_open_slots (cycle slots)
#  - لا تحريك بين المسارات بعد الإغلاق (المسار ثابت)
#  - update_active_trades: مسؤول عن العدّادات فقط
#  - update_trade_status: يحدّث TRADES_FILE فقط (بدون عدّادات)
# ============================================

from datetime import date, datetime, timezone
from typing import Dict, Any, Optional, Tuple, List
import json
import os
import re

# ====== Globals من Section 1/2 ======
TRACK_FILE   = globals().get("TRACK_FILE",   "tracks.json")
TRADES_FILE  = globals().get("TRADES_FILE",  "trades.json")
DEFAULT_CYCLE_COUNT = globals().get("DEFAULT_CYCLE_COUNT", 1)

INITIAL_TRADE_AMOUNT    = float(globals().get("INITIAL_TRADE_AMOUNT", 50.0))
TRADE_INCREMENT_PERCENT = float(globals().get("TRADE_INCREMENT_PERCENT", 2.0))

get_trade_structure     = globals().get("get_trade_structure")
get_effective_max_open  = globals().get("get_effective_max_open")
log_terminal_notification = globals().get("log_terminal_notification", lambda msg, tag=None: None)


# ========== أدوات حفظ/تحميل الهيكل ==========
def save_trade_structure(structure: Dict[str, Any]) -> None:
    """حفظ ملف المسارات/الخانات (TRACK_FILE) بأمان."""
    try:
        with open(TRACK_FILE, "w") as f:
            json.dump(structure, f, indent=2)
    except Exception as e:
        print(f"⚠️ save_trade_structure error: {e}")


# ========== حجم الصفقة لكل مسار ==========
def track_base_amount(track_num: int) -> float:
    """
    حساب حجم الصفقة لمسار معيّن:
      المسار 1 = INITIAL_TRADE_AMOUNT
      المسار 2 = INITIAL_TRADE_AMOUNT * (1 + p%)^1
      المسار 3 = INITIAL_TRADE_AMOUNT * (1 + p%)^2
      ...
    حيث p = TRADE_INCREMENT_PERCENT
    """
    try:
        mult = (1.0 + (TRADE_INCREMENT_PERCENT / 100.0)) ** max(0, int(track_num) - 1)
        return round(INITIAL_TRADE_AMOUNT * mult, 2)
    except Exception:
        return float(INITIAL_TRADE_AMOUNT)


# ========== إنشاء مسار جديد ==========
def create_new_track(track_num: int, base_amount: float) -> Dict[str, Any]:
    """
    إنشاء مسار جديد:
      - لكل مسار خانة واحدة فقط (slot واحد) باسم: A{track_num}
      - amount = base_amount
    """
    cell_key = f"A{int(track_num)}"
    return {
        "cycles": {
            cell_key: None   # لا يوجد شيء في الخانة مبدئياً
        },
        "amount": float(base_amount),
    }


def _ensure_track_exists(structure: Dict[str, Any], track_idx: int) -> None:
    """
    تأكّد من وجود مسار معيّن داخل structure، وإن لم يكن موجودًا يتم إنشاؤه.
    لكل مسار خانة واحدة فقط باسم A{track_idx}.
    """
    try:
        tracks = structure.setdefault("tracks", {})
        tkey = str(track_idx)
        if tkey not in tracks:
            tracks[tkey] = create_new_track(track_idx, track_base_amount(track_idx))
        else:
            # تأكّد أنّ له خانة واحدة على الأقل (A{track_idx})
            cycles = tracks[tkey].get("cycles") or {}
            cell_key = f"A{track_idx}"
            if cell_key not in cycles:
                cycles[cell_key] = None
                tracks[tkey]["cycles"] = cycles
        save_trade_structure(structure)
    except Exception as e:
        print(f"⚠️ _ensure_track_exists error: {e}")


# ========== البحث عن خانة متاحة (slot) ==========
def find_available_slot(structure: Optional[Dict[str, Any]] = None) -> Tuple[Optional[str], Optional[str], Optional[float]]:
    """
    اختيار خانة متاحة لصفقة جديدة:

      - نستخدم max_open_slots كحدّ أعلى لعدد الصفقات المفتوحة في آن واحد.
      - نعتبر أنّ عدد المسارات الفعّال لا يتجاوز هذا الحدّ.
      - لكل مسار خانة واحدة فقط A{track_num}.

    يُرجع:
      (track_num: str, cycle_key: str (مثل 'A5'), amount: float)
      أو (None, None, None) إذا لا توجد خانة متاحة حاليًا.
    """
    try:
        if structure is None or not isinstance(structure, dict):
            structure = get_trade_structure() if callable(get_trade_structure) else {}

        tracks = structure.setdefault("tracks", {})
        cap = get_effective_max_open(structure) if callable(get_effective_max_open) else len(tracks) or 0

        # تأكد من وجود المسارات حتى cap
        for idx in range(1, max(cap, 1) + 1):
            _ensure_track_exists(structure, idx)

        # ابحث عن خانة فارغة في المسارات من 1..cap
        for idx in range(1, max(cap, 1) + 1):
            tkey = str(idx)
            tdata = tracks.get(tkey) or {}
            cycles = tdata.get("cycles") or {}
            cell_key = f"A{idx}"

            if cell_key not in cycles:
                cycles[cell_key] = None
                tdata["cycles"] = cycles
                tracks[tkey] = tdata
                save_trade_structure(structure)

            cell = cycles.get(cell_key)
            if cell is None:
                amount = float(tdata.get("amount", track_base_amount(idx)))
                return tkey, cell_key, amount

        # لا خانة فارغة حاليًا (رغم أنّ open_count يجب أن يكون قد تحقّق قبلًا في execute_trade)
        return None, None, None

    except Exception as e:
        print(f"⚠️ find_available_slot error: {e}")
        return None, None, None


# ========== مساعدة: إرجاع الخانات الفارغة (لأوامر slots) ==========
def get_empty_slots(structure: Optional[Dict[str, Any]] = None, include_out_of_range: bool = False) -> Dict[str, List[str]]:
    """
    إرجاع جميع الخانات الفارغة بشكل مبسّط:
      { 'track_num': [cycle_key, ...], ... }

    في هذا التصميم:
      - نعتبر فقط الخانة A{track_num} لكل مسار.
      - include_out_of_range لا يغيّر الكثير هنا، لكنه موجود للتوافق.
    """
    out: Dict[str, List[str]] = {}
    try:
        if structure is None:
            structure = get_trade_structure() if callable(get_trade_structure) else {}

        tracks = structure.get("tracks", {}) or {}
        cap = get_effective_max_open(structure) if callable(get_effective_max_open) else len(tracks) or 0

        for idx in range(1, max(cap, 1) + 1):
            tkey = str(idx)
            if tkey not in tracks:
                continue
            cycles = (tracks[tkey].get("cycles") or {})
            cell_key = f"A{idx}"
            cell = cycles.get(cell_key)
            if cell is None:
                out.setdefault(tkey, []).append(cell_key)
    except Exception as e:
        print(f"⚠️ get_empty_slots error: {e}")
    return out


def predict_next_slot(structure: Optional[Dict[str, Any]] = None) -> Tuple[Optional[str], Optional[str], Optional[float]]:
    """
    واجهة بسيطة لأمر nextslots القديم:
      - تعتمد الآن مباشرة على find_available_slot.
    """
    return find_available_slot(structure)


# ========== تحديث العدّادات وتفريغ الخانة بعد الإغلاق ==========
async def update_active_trades(slot_pos: Tuple[str, str], cell_data: Dict[str, Any], final_status: str) -> None:
    """
    تحديث عدّادات الصفقات وحالة الخانة بعد الإغلاق:

      - لا يوجد أي تحريك لمسارات بعد الآن (المسار ثابت).
      - final_status ∈ { 'closed', 'stopped', 'failed', 'drwn' }.

      - العدّادات في structure:
          • total_trades           += 1
          • closed  → total_successful_trades += 1
                      + daily_successful_trades[اليوم] += 1
          • stopped → total_lost_trades       += 1
          • failed  → total_failed_trades     += 1
          • drwn    → total_drawdown_trades   += 1

      - TRADES_FILE يتم تحديثه عبر update_trade_status() (لا نلمسه هنا).
    """
    track_num, cycle_num = slot_pos

    # --- إخلاء الخانة ---
    try:
        structure = get_trade_structure() if callable(get_trade_structure) else {}
        tracks = structure.get("tracks", {}) or {}
        tdata = tracks.get(str(track_num))
        if tdata:
            cycles = tdata.get("cycles") or {}
            if str(cycle_num) in cycles:
                cycles[str(cycle_num)] = None
                tdata["cycles"] = cycles
                tracks[str(track_num)] = tdata
                structure["tracks"] = tracks
        # حتى لو فشل أي شيء، نحاول إكمال العدّادات
    except Exception as e:
        sym_dbg = (cell_data or {}).get("symbol", "?")
        print(f"⚠️ update_active_trades clear-slot error for {sym_dbg} at {track_num}-{cycle_num}: {e}")
        structure = get_trade_structure() if callable(get_trade_structure) else {}

    # --- تحديث العدّادات ---
    try:
        structure["total_trades"] = int(structure.get("total_trades", 0) or 0) + 1
        today_str = date.today().isoformat()

        if final_status == "closed":
            structure["total_successful_trades"] = int(structure.get("total_successful_trades", 0) or 0) + 1
            daily = structure.get("daily_successful_trades", {})
            daily[today_str] = int(daily.get(today_str, 0) or 0) + 1
            structure["daily_successful_trades"] = daily
            log_terminal_notification(
                f"Trade closed in profit ({(cell_data or {}).get('symbol','?')})",
                tag="trade_closed"
            )
        elif final_status == "stopped":
            structure["total_lost_trades"] = int(structure.get("total_lost_trades", 0) or 0) + 1
            log_terminal_notification(
                f"Trade stopped at SL ({(cell_data or {}).get('symbol','?')})",
                tag="trade_stopped"
            )
        elif final_status == "failed":
            structure["total_failed_trades"] = int(structure.get("total_failed_trades", 0) or 0) + 1
            log_terminal_notification(
                f"Trade failed ({(cell_data or {}).get('symbol','?')})",
                tag="trade_failed"
            )
        elif final_status == "drwn":
            structure["total_drawdown_trades"] = int(structure.get("total_drawdown_trades", 0) or 0) + 1
            log_terminal_notification(
                f"Trade closed in manual drawdown ({(cell_data or {}).get('symbol','?')})",
                tag="trade_drawdown"
            )
    except Exception as e:
        print(f"⚠️ update_active_trades counters error: {e}")

    save_trade_structure(structure)


# ========== TRADES_FILE: تحديث حالة الصفقة ==========
def _normalize_symbol(sym: str) -> str:
    return (sym or "").upper().replace("-", "").replace("/", "")


def update_trade_status(
    symbol: str,
    new_status: str,
    track_num: Optional[str] = None,
    cycle_num: Optional[str] = None
) -> None:
    """
    تحديث حالة صفقة في TRADES_FILE فقط (بدون تعديل العدّادات في structure):

      - نبحث عن أحدث صفقة لنفس (symbol, track_num, cycle_num) عبر opened_at.
      - نحدّث الحقل status.
      - إذا new_status ∈ {closed, stopped, drwn, failed} نضيف closed_at (إن لم يكن موجوداً).

    ملاحظة:
      - العدّادات تُدار حصراً في update_active_trades.
      - هذا يسمح بإلغاء صفقات معلّقة (open/reserved) بدون احتسابها في الإحصائيات.
    """
    try:
        if not os.path.exists(TRADES_FILE):
            return

        with open(TRADES_FILE, "r") as f:
            data = json.load(f) or {}
        trades = data.get("trades", []) or []

        sym_norm = _normalize_symbol(symbol)
        best_i = None
        best_ts = -1.0

        for i, tr in enumerate(trades):
            try:
                if _normalize_symbol(tr.get("symbol", "")) != sym_norm:
                    continue
                if track_num is not None and str(tr.get("track_num")) != str(track_num):
                    continue
                if cycle_num is not None and str(tr.get("cycle_num")) != str(cycle_num):
                    continue
                ts = float(tr.get("opened_at", 0) or 0)
                if ts >= best_ts:
                    best_ts = ts
                    best_i = i
            except Exception:
                continue

        if best_i is None:
            return  # لا صفقة مطابقة

        tr = trades[best_i]
        tr["status"] = new_status
        # إضافة ختم زمني للإغلاق عند الحالات النهائية
        if new_status.lower() in ("closed", "stopped", "drwn", "failed"):
            if not tr.get("closed_at"):
                tr["closed_at"] = datetime.now(timezone.utc).timestamp()

        trades[best_i] = tr
        data["trades"] = trades

        with open(TRADES_FILE, "w") as f:
            json.dump(data, f, indent=2)

    except Exception as e:
        print(f"⚠️ update_trade_status error: {e}")
