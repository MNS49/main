# ============================================
# Section 6) NTP, Hourly Drawdown & Resume Open Trades (Tracks + Slots)
#  - فحص انحراف الوقت (NTP)
#  - تنبيه ساعي عند الهبوط ≥ 4% لصفقات BUY
#  - استئناف مراقبة الصفقات المفتوحة عند تشغيل البوت
#  - احترام بنية Tracks + Slots + cycle_slots الجديدة
#  - احترام الحالات النهائية من TRADES_FILE (closed/stopped/drwn/failed)
# ============================================

import time
import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# ---------- Console echo ----------
try:
    console_echo  # provided by previous sections
except NameError:  # safe no-op fallback
    def console_echo(msg: str) -> None:
        try:
            if bool(globals().get("ENABLE_CONSOLE_ECHO", False)):
                print(msg)
        except Exception:
            pass

_console_echo = console_echo

# ---------- Basic globals ----------
TRADES_FILE = globals().get("TRADES_FILE", "trades.json")

send_notification = globals().get("send_notification")
send_notification_both = globals().get("send_notification_both")
send_to_second_account = globals().get("send_to_second_account")

get_trade_structure = globals().get("get_trade_structure")
save_trade_structure = globals().get("save_trade_structure")

fetch_current_price = globals().get("fetch_current_price")
normalize_symbol = globals().get("normalize_symbol") or (lambda s: (s or "").upper().replace('-', '').replace('/', '))

monitor_and_execute = globals().get("monitor_and_execute")

register_trade_outcome = globals().get("register_trade_outcome")
accumulate_summary = globals().get("accumulate_summary")

# من Section 5 (لو موجودة)، نعيد استخدامها إن كانت معرفة
_load_trades_cache = globals().get("_load_trades_cache")
if _load_trades_cache is None:
    def _load_trades_cache() -> List[Dict[str, Any]]:
        if not os.path.exists(TRADES_FILE):
            return []
        try:
            with open(TRADES_FILE, "r") as f:
                tdata = json.load(f) or {}
            return tdata.get("trades", []) or []
        except Exception:
            return []

# ---------- Berlin timezone helpers (إعادة استخدام) ----------
def _berlin_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/Berlin")
    except Exception:
        return timezone.utc

def _fmt_berlin(ts: Optional[float]) -> str:
    if ts is None:
        return "—"
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(_berlin_tz())
        return dt.strftime("%d.%m %H:%M:%S")
    except Exception:
        return "—"

# ---------- NTP (time sync) ----------
NTP_MAX_DIFF_SEC = 2.0       # KuCoin غالبًا يرفض > 2 ثواني فرق توقيت
NTP_ALERT_COOLDOWN = 3600    # تنبيه واحد كل ساعة
_last_ntp_alert_ts = 0.0

def check_system_time(max_allowed_diff_sec: float = NTP_MAX_DIFF_SEC) -> float:
    """
    قياس انحراف الوقت (ثواني) مقارنةً بـ pool.ntp.org.
    يرجّع:
      - قيمة موجبة = الانحراف (ثواني)
      - -1.0 عند الفشل (عدم وجود ntplib أو مشاكل شبكة)
    يطبع فقط للترمينال.
    """
    try:
        try:
            import ntplib
        except ImportError:
            print("ℹ️ ntplib غير مُثبت؛ نفّذ: pip install ntplib")
            return -1.0

        client_ntp = ntplib.NTPClient()
        diffs: List[float] = []
        for _ in range(3):
            try:
                resp = client_ntp.request("pool.ntp.org", version=3, timeout=2)
                diffs.append(abs(time.time() - resp.tx_time))
            except Exception:
                pass

        if not diffs:
            print("⚠️ Unable to reach NTP.")
            return -1.0

        best = min(diffs)
        if best > max_allowed_diff_sec:
            print(f"⚠️ Large time skew: ~{best:.2f}s — may cause KuCoin signature errors.")
        else:
            print(f"✅ Time in sync (~{best:.2f}s).")
        return best

    except Exception as e:
        print(f"⚠️ NTP check failed: {e}")
        return -1.0

async def _maybe_warn_ntp_diff():
    """
    تُشغَّل من منبّه دوري، وترسل إشعار إذا كان انحراف الوقت كبير.
    """
    global _last_ntp_alert_ts
    now = time.time()
    diff = check_system_time(NTP_MAX_DIFF_SEC)

    # ntplib غير متوفر أو لا يوجد اتصال
    if diff == -1.0:
        if send_notification and (now - _last_ntp_alert_ts > NTP_ALERT_COOLDOWN):
            _last_ntp_alert_ts = now
            await send_notification("ℹ️ NTP skew not measured (ntplib missing or no network).")
        return

    # الانحراف أكبر من الحد المسموح → تنبيه
    if diff > NTP_MAX_DIFF_SEC and (now - _last_ntp_alert_ts > NTP_ALERT_COOLDOWN):
        _last_ntp_alert_ts = now
        if send_notification:
            await send_notification(
                f"⚠️ System time skew is ~{diff:.2f}s. KuCoin may reject requests.\n"
                f"🔧 Use chrony (preferred) or ntpdate to sync."
            )

# ---------- TRADES_FILE helpers للحالات النهائية ----------
_FINAL_STATES = {"closed", "stopped", "drwn", "failed"}

def _latest_trade_for_slot(
    trades: List[Dict[str, Any]],
    sym_norm: str,
    track_num: int,
    slot_id: str
) -> Optional[Dict[str, Any]]:
    latest = None
    latest_ts = -1.0
    for tr in trades:
        try:
            if normalize_symbol(tr.get("symbol")) != sym_norm:
                continue
            if int(tr.get("track_num", 0) or 0) != int(track_num):
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

def _latest_state_for_slot(
    trades: List[Dict[str, Any]],
    sym_norm: str,
    track_num: int,
    slot_id: str
) -> Optional[str]:
    tr = _latest_trade_for_slot(trades, sym_norm, track_num, slot_id)
    return (tr.get("status") or "").lower() if tr else None

def _is_final_in_trades_slot(
    trades: List[Dict[str, Any]],
    sym_norm: str,
    track_num: int,
    slot_id: str
) -> bool:
    st = _latest_state_for_slot(trades, sym_norm, track_num, slot_id)
    return (st in _FINAL_STATES) if st else False

# ---------- Hourly 4% drawdown aggregation ----------
async def _hourly_drawdown_check_and_notify():
    """
    كل ساعة: يجمع كل الـ Slots بحالة BUY التي هبطت أسعارها ≥ 4% عن سعر الشراء الفعلي.
    - يعتمد على structure["slots"] + TRADES_FILE للحالات النهائية.
    - يستخدم خريطة الترقيم _STATUS_REV_INDEX_MAP (من Section 5) لو متوفرة.
    - لا يرسل تنبيه لصفقة منتهية (closed/stopped/drwn/failed) حتى لو بقيت الخانة بحالة buy بالهيكل.
    """
    try:
        # أعِد بناء خريطة الترقيم لتكون متوافقة مع آخر status
        try:
            rebuild_fn = globals().get("_rebuild_status_index_map")
            if callable(rebuild_fn):
                rebuild_fn()
        except Exception:
            pass

        if not callable(get_trade_structure):
            return

        structure = get_trade_structure()
        slots: Dict[str, Any] = structure.get("slots") or {}
        trades = _load_trades_cache()

        affected_lines: List[str] = []

        for sid, cell in slots.items():
            if not cell:
                continue
            st = (cell.get("status") or "").lower()
            if st != "buy":
                continue

            sym = normalize_symbol(cell.get("symbol"))
            if not sym:
                continue

            track_num = int(cell.get("track_num", 0) or 0)
            if track_num <= 0:
                continue

            bought_price = float(cell.get("bought_price", 0) or 0)
            if bought_price <= 0:
                continue

            # لا تنبيه إذا الصفقة منتهية نهائياً في TRADES_FILE
            if _is_final_in_trades_slot(trades, sym, track_num, str(sid)):
                continue

            if not callable(fetch_current_price):
                continue

            price = await fetch_current_price(sym)
            if price is None or price <= 0:
                continue

            drop_pct = ((bought_price - float(price)) / max(bought_price, 1e-12)) * 100.0
            if drop_pct >= 4.0:
                # رقم index من خريطة Section 5 (لو متوفرة)
                idx_map: Dict[Tuple[str, int, str], int] = globals().get("_STATUS_REV_INDEX_MAP", {}) or {}
                idx = idx_map.get((sym, int(track_num), str(sid)))
                idx_prefix = f"{idx} " if idx is not None else ""

                affected_lines.append(
                    f"•  {idx_prefix}{sym} — Track {track_num} | Slot {sid} | "
                    f"Buy {bought_price:.6f} → Now {float(price):.6f}  (−{drop_pct:.2f}%)"
                )

        if affected_lines:
            msg = "📉 Hourly drawdown alert (≥ 4%):\n" + "\n".join(sorted(affected_lines))
            # أرسل رسالة واحدة للحسابات المتاحة
            if callable(send_notification_both):
                await send_notification_both(msg)
            else:
                if callable(send_notification):
                    await send_notification(msg)
                if callable(send_to_second_account):
                    try:
                        await send_to_second_account(msg)
                    except Exception:
                        pass

    except Exception as e:
        print(f"⚠️ hourly drawdown aggregation error: {e}")

# ---------- Resume open trades on startup ----------
async def resume_open_trades():
    """
    عند تشغيل البوت:
      - أي Slot بحالة open/reserved → يعاد تشغيل monitor_and_execute عليها من جديد.
      - أي Slot بحالة buy          → يعاد تشغيل monitor_and_execute (سيكتشف من الهيكل أنها BUY ويكمل TP/Trailing/SL logic).
      - لا يتم استئناف أي Slot إذا كانت الصفقة منتهية نهائيًا في TRADES_FILE (closed/stopped/drwn/failed)،
        ويتم تنظيف الخانة (slot = None) في هذه الحالة.
    في النهاية: يُرسل تلخيص بعدد الـ Slots التي تم استئناف مراقبتها وعدد الخانات التي تم تنظيفها.
    """
    open_resumed = 0
    buy_resumed = 0
    cleaned_slots: List[Tuple[str, int, str]] = []  # (symbol, track_num, slot_id)

    if not callable(get_trade_structure) or not callable(save_trade_structure) or not callable(monitor_and_execute):
        if callable(send_notification):
            await send_notification("⚠️ resume_open_trades: required helpers not available.")
        return

    structure = get_trade_structure()
    slots: Dict[str, Any] = structure.get("slots") or {}
    trades = _load_trades_cache()

    dirty = False

    for sid, cell in list(slots.items()):
        if not cell:
            continue
        try:
            status = (cell.get("status") or "").lower()
            symbol = normalize_symbol(cell.get("symbol"))
            if not symbol:
                continue

            track_num = int(cell.get("track_num", 0) or 0)
            if track_num <= 0:
                continue

            entry = float(cell.get("entry", 0) or 0)
            sl = float(cell.get("sl", 0) or 0)
            targets = list(cell.get("targets") or [])
            amount = float(cell.get("amount", 0) or 0)

            # تخطّي أي خانة بلا Targets أو مبلغ
            if not targets or amount <= 0:
                continue

            # إذا الصفقة نهائية في TRADES_FILE → حرّر الخانة ولا تستأنف مراقبتها
            if _is_final_in_trades_slot(trades, symbol, track_num, str(sid)):
                if status in ("open", "buy", "reserved"):
                    slots[str(sid)] = None
                    dirty = True
                    cleaned_slots.append((symbol, track_num, str(sid)))
                continue

            # استئناف المراقبة
            if status in ("open", "reserved", "buy"):
                asyncio.create_task(
                    monitor_and_execute(
                        symbol,
                        entry,
                        sl,
                        targets,
                        amount,
                        track_num,
                        str(sid),
                    )
                )
                if status in ("open", "reserved"):
                    open_resumed += 1
                else:
                    buy_resumed += 1

        except Exception as e:
            sym_dbg = cell.get("symbol") if isinstance(cell, dict) else None
            if sym_dbg:
                print(f"resume error on Slot {sid} for {sym_dbg}: {e}")
            else:
                print(f"resume error on Slot {sid}: {e}")

    if dirty:
        try:
            structure["slots"] = slots
            save_trade_structure(structure)
        except Exception as e:
            print(f"⚠️ resume cleanup save error: {e}")

    # ملخص الاستئناف
    if open_resumed or buy_resumed or cleaned_slots:
        lines = [
            "🔄 Resume summary:",
            f"• Open/Reserved monitors restarted: {open_resumed}",
            f"• Buy monitors restarted        : {buy_resumed}",
        ]
        if cleaned_slots:
            preview = "\n".join(
                f"   - {s} — Track {t} | Slot {c}" for s, t, c in cleaned_slots[:12]
            )
            more = " …" if len(cleaned_slots) > 12 else ""
            lines.append("• Cleaned finalized slots (freed):")
            lines.append(preview + more)

        if callable(send_notification):
            await send_notification("\n".join(lines))

# ---------- Status notifier (NTP + drawdown) ----------
async def status_notifier():
    """
    منبّه دوري:
      - كل ساعة: فحص NTP + تجميع تنبيه هبوط 4%+ لكل المراكز المشتراة (BUY).
    """
    while True:
        try:
            await _maybe_warn_ntp_diff()
            await _hourly_drawdown_check_and_notify()
            await asyncio.sleep(3600)
        except Exception as e:
            print(f"⚠️ status_notifier error: {e}")
            await asyncio.sleep(300)
