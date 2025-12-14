# ============================================
# Section 1) Config & Clients (KuCoin + Telegram)
# ============================================

import os
from telethon import TelegramClient, events
from kucoin.client import Client as KucoinClient

# -------- KuCoin API --------
KUCOIN_API_KEY = ''          # <-- ضع KuCoin API Key
KUCOIN_API_SECRET = ''       # <-- ضع KuCoin API Secret
KUCOIN_API_PASSPHRASE = ''   # <-- ضع KuCoin API Passphrase (نصيّة كما هي في V2)

# إصدار مفاتيح KuCoin: V2
KUCOIN_API_KEY_VERSION = 2

# (اختياري) مفاتيح Partner/Broker من البيئة
KUCOIN_PARTNER = os.getenv("KUCOIN_PARTNER", "")
KUCOIN_PARTNER_KEY = os.getenv("KUCOIN_PARTNER_KEY", "")
KUCOIN_PARTNER_SECRET = os.getenv("KUCOIN_PARTNER_SECRET", "")

# اختيار بيئة KuCoin
KUCOIN_SANDBOX = os.getenv("KUCOIN_SANDBOX", "false").lower() == "true"

# -------- Telegram API --------
TG_API_ID = ""          # Telegram API ID (اكتبه كرقم لكن بين "" لأنه سترينغ)
TG_API_HASH = ""        # Telegram API HASH
YOUR_TELEGRAM_ID = ""   # اختياري لو بدك تستخدمه بقسم آخر
CHANNEL_USERNAME = ""   # قناة التوصيات، مثلاً: "@MySignalsChannel"

# حساب الإشعارات الأساسي (أنت)
OWNER_CHAT = "Mohamad4992"   # هنا غيّرته يرسل الإشعارات لهذا الحساب
# حساب ثانوي لو حاب (تقدر تخليه فاضي)
SECONDARY_CHAT = None        # أو مثلاً: "AnotherUser"

# -------- Simulation / Live switch --------
# 0 = SIM ، 1 = LIVE 
IS_SIMULATION = bool(int(os.getenv("BOT_SIMULATION", "0")))

# -------- KuCoin Client --------
if KUCOIN_API_KEY and KUCOIN_API_SECRET and KUCOIN_API_PASSPHRASE:
    try:
        kucoin = KucoinClient(
            KUCOIN_API_KEY,
            KUCOIN_API_SECRET,
            KUCOIN_API_PASSPHRASE,
            sandbox=KUCOIN_SANDBOX,
            api_key_version=KUCOIN_API_KEY_VERSION,
        )

        # لو عندك مفاتيح Partner:
        if KUCOIN_PARTNER and KUCOIN_PARTNER_KEY and KUCOIN_PARTNER_SECRET:
            kucoin._requests_params = kucoin._requests_params or {}
            kucoin._requests_params.update({
                "KC-API-PARTNER": KUCOIN_PARTNER,
                "KC-API-PARTNER-KEY": KUCOIN_PARTNER_KEY,
                "KC-API-PARTNER-SIGN": KUCOIN_PARTNER_SECRET,
            })
    except Exception as e:
        print(f"⚠️ KuCoin client init failed: {e}")
        kucoin = None
else:
    kucoin = None
    print("ℹ️ KuCoin API keys are empty. Running in price-only / simulation mode.")

# -------- Telegram client --------
client: TelegramClient | None = None

def _init_telegram_client():
    global client
    if client is not None:
        return
    if not TG_API_ID or not TG_API_HASH:
        print("⚠️ TG_API_ID أو TG_API_HASH فارغة. لن يتم تهيئة Telegram client.")
        client = None
        return
    try:
        api_id_int = int(TG_API_ID)
    except ValueError:
        print("⚠️ TG_API_ID يجب أن يكون رقم (حتى لو مكتوب كنص).")
        client = None
        return

    # اسم جلسة التخزين (ملف .session يتخزن في نفس المجلد)
    session_name = "kucoin_bot_session"

    client = TelegramClient(session_name, api_id_int, TG_API_HASH)
    print("✅ Telegram client initialized (ready to start in Section 7).")

# نهيّئه مباشرة عند استيراد الملف
try:
    _init_telegram_client()
except Exception as e:
    print(f"⚠️ Telegram client init failed: {e}")
    client = None
