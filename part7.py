# ============================================
# Section 7) Main Entrypoint (Tracks + Slots)
#  - تشغيل Telegram client
#  - ربط مستمع توصيات القناة
#  - تشغيل منبّه NTP + Drawdown (status_notifier)
#  - استئناف الصفقات المفتوحة (resume_open_trades)
#  - لا يوجد Email Gate هنا (يمكن إضافته لاحقًا إذا رغبت)
# ============================================

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict

# نقرأ الكائنات المعرّفة في الأقسام السابقة من globals
client = globals().get("client")  # تم تهيئته في Section 1
send_notification = globals().get("send_notification")
send_notification_both = globals().get("send_notification_both")

# من Section 6
status_notifier = globals().get("status_notifier")
resume_open_trades = globals().get("resume_open_trades")

# من Section 5
attach_channel_handler = globals().get("attach_channel_handler")

# من Section 2 (أو 1) لتحديد إن كان الوضع Simulation أو Live
is_simulation = globals().get("is_simulation")

# (اختياري) حالة البوت pause/reuse إن كانت موجودة
is_bot_active = globals().get("is_bot_active")
set_bot_active = globals().get("set_bot_active")


async def main():
    """
    نقطة الدخول الرئيسية:
      1) ربط مستمع توصيات القناة.
      2) تشغيل Telegram client.
      3) إرسال رسالة بدء إلى حساب التحكم (Mohamad4992).
      4) تشغيل:
           - status_notifier() في background (NTP + drawdown 4%).
           - resume_open_trades() لاستئناف الصفقات.
      5) الانتظار حتى ينقطع اتصال العميل (run_until_disconnected).
    """
    # 1) ربط مستمع القناة (channel handler) قبل start()
    try:
        if callable(attach_channel_handler):
            attach_channel_handler()
        else:
            print("[MAIN] attach_channel_handler not available; channel listener disabled.")
    except Exception as e:
        print(f"[MAIN] attach_channel_handler failed: {e}")

    # 2) تأكد أن client مهيأ
    if client is None:
        raise RuntimeError(
            "Telegram client (client) is not initialized. "
            "تأكد أن Section 1 تم تحميله وتنفيذه قبل Section 7."
        )

    # 3) بدء جلسة تلغرام
    await client.start()

    # 4) تحديد وضع التشغيل (Simulation أو Live)
    try:
        mode_label = "Simulation" if (callable(is_simulation) and is_simulation()) else "Live"
    except Exception:
        mode_label = "Live"

    # (اختياري) إذا عندك is_bot_active/set_bot_active تقدر تضمن تشغيله على True عند البدء
    try:
        if callable(set_bot_active):
            set_bot_active(True)
    except Exception:
        pass

    # 5) رسالة بدء إلى حساب التحكم (Mohamad4992) عبر send_notification/send_notification_both
    start_msg_lines = [
        f"✅ Bot started! ({mode_label})",
        "📡 Waiting for recommendations from channel…",
    ]
    # ممكن تضيف معلومات أخرى هنا مثل max_open_slots أو cycle_slots لو حاب
    start_msg = "\n".join(start_msg_lines)

    try:
        if callable(send_notification_both):
            # لو عندك مسارين للإشعارات (مثلاً حسابين أو شاتين)
            await send_notification_both(start_msg)
        elif callable(send_notification):
            # المسار الأساسي: يرسل للحساب الثاني (Mohamad4992) كما ضبطناه في Section 1
            await send_notification(start_msg)
        else:
            print(start_msg)
    except Exception as e:
        print(f"[MAIN] failed to send start notification: {e}")

    # 6) تشغيل منبّه الحالة (NTP + drawdown 4%) في background
    try:
        if callable(status_notifier):
            asyncio.create_task(status_notifier())
    except Exception as e:
        print(f"[MAIN] failed to start status_notifier: {e}")

    # 7) استئناف الصفقات المفتوحة (open/buy) باستخدام Slots
    try:
        if callable(resume_open_trades):
            await resume_open_trades()
    except Exception as e:
        print(f"[MAIN] resume_open_trades error: {e}")

    # 8) تشغيل حتى الانفصال
    try:
        await client.run_until_disconnected()
    except KeyboardInterrupt:
        print("🛑 Bot stopped manually.")
    except Exception as e:
        print(f"[MAIN] client.run_until_disconnected error: {e}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Bot stopped manually (KeyboardInterrupt).")
    except Exception as e:
        print(f"🛑 Bot crashed in main(): {e}")
