import os
import io
import logging
import tempfile
import subprocess
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from PIL import Image
import firebase_admin
from firebase_admin import credentials, firestore

# ============================================================
# ⚙️ الإعدادات
# ============================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
FIREBASE_CREDENTIALS_PATH = os.getenv("FIREBASE_CREDENTIALS", "/etc/secrets/firebase-key.json")
MAX_FILE_SIZE_MB = 15
MAX_VIDEO_DURATION = 35  # ثانية

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ============================================================
# 🔥 Firebase Admin Init
# ============================================================
try:
    cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    logger.info("✅ Firebase Admin initialized")
except Exception as e:
    logger.error(f"❌ Firebase init failed: {e}")
    db = None

# ============================================================
# 🚀 FastAPI + Telegram App
# ============================================================
app = FastAPI()
telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()

# تخزين مؤقت لجلسات KYC: {user_id: {...}}
kyc_sessions = {}


# ============================================================
# 🛠️ دوال مساعدة
# ============================================================
def compress_image_bytes(data: bytes, max_dim: int = 900, quality: int = 65) -> bytes:
    """ضغط صورة → JPEG بأقصى حجم 900px وجودة 65%"""
    try:
        img = Image.open(io.BytesIO(data))
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=quality, optimize=True)
        return out.getvalue()
    except Exception as e:
        logger.error(f"compress_image failed: {e}")
        return data


def compress_video(input_path: str, output_path: str) -> bool:
    """ضغط فيديو باستخدام ffmpeg → 480p, 15fps, ~250kbps"""
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", input_path,
                "-vf", "scale=480:-2",
                "-r", "15",
                "-b:v", "250k",
                "-b:a", "32k",
                "-preset", "fast",
                "-movflags", "+faststart",
                output_path,
            ],
            capture_output=True,
            timeout=120,
        )
        if result.returncode != 0:
            logger.error(f"ffmpeg error: {result.stderr.decode()[:500]}")
            return False
        return True
    except Exception as e:
        logger.error(f"compress_video failed: {e}")
        return False


def validate_account_id(account_id: str) -> bool:
    """تحقق من صيغة رقم الحساب: 8-20 رقم"""
    return account_id.isdigit() and 8 <= len(account_id) <= 20


async def notify_admin_error(text: str):
    """إرسال رسالة خطأ للمشرف"""
    try:
        await telegram_app.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"⚠️ {text}")
    except Exception as e:
        logger.error(f"notify_admin_error failed: {e}")


# ============================================================
# 🎬 معالجات البوت
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عند فتح البوت: /start <telegram_id>"""
    user = update.effective_user
    args = context.args

    # إذا كانت الجلسة موجودة، تابع
    if user.id in kyc_sessions:
        session = kyc_sessions[user.id]
        await update.message.reply_text(
            f"👋 مرحباً بك مرة أخرى\n\n"
            f"أنت في الخطوة: *{session.get('step', 'البداية')}*\n"
            f"تابع الإرسال حسب التعليمات.",
            parse_mode="Markdown",
        )
        return

    # بداية جديدة
    tg_id_from_link = args[0] if args else None

    kyc_sessions[user.id] = {
        "telegram_id": tg_id_from_link or str(user.id),
        "username": user.username or "",
        "full_name": user.full_name,
        "step": "waiting_name",
        "files": {"front": None, "back": None, "video": None},
    }

    await update.message.reply_text(
        "🔐 *مرحباً بك في نظام التحقق من الهوية (Crynova)*\n\n"
        "لتأكيد حسابك، نحتاج منك:\n"
        "1️⃣ الاسم الكامل (كما في البطاقة)\n"
        "2️⃣ رقم حسابك في Crynova\n"
        "3️⃣ صورة البطاقة (الوجه الأمامي)\n"
        "4️⃣ صورة البطاقة (الوجه الخلفي)\n"
        "5️⃣ فيديو قصير (20-30 ثانية) وأنت تحمل البطاقة\n\n"
        "⏱️ المدة المتوقعة: 3 دقائق\n"
        "📌 ابدأ بإرسال *اسمك الكامل* الآن.",
        parse_mode="Markdown",
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال نص: الاسم أو رقم الحساب"""
    user = update.effective_user
    text = update.message.text.strip()

    if user.id not in kyc_sessions:
        await update.message.reply_text("⚠️ ابدأ من جديد بأمر /start")
        return

    session = kyc_sessions[user.id]
    step = session["step"]

    if step == "waiting_name":
        if len(text) < 3 or len(text) > 80:
            await update.message.reply_text("❌ الاسم قصير جداً أو طويل. أعد الإرسال.")
            return
        session["full_name"] = text
        session["step"] = "waiting_account"
        await update.message.reply_text(
            "✅ تم حفظ الاسم.\n\nالآن أرسل *رقم حسابك في Crynova* (8-20 رقم فقط).",
            parse_mode="Markdown",
        )

    elif step == "waiting_account":
        clean = text.replace(" ", "")
        if not validate_account_id(clean):
            await update.message.reply_text("❌ رقم غير صحيح. أرسل أرقاماً فقط (8-20 رقم).")
            return
        session["account_id"] = clean
        session["step"] = "waiting_front"
        await update.message.reply_text(
            "✅ تم حفظ رقم الحساب.\n\n"
            "📸 الآن أرسل *صورة البطاقة (الوجه الأمامي)*\n"
            "أرسلها كصورة، ليس كملف.",
            parse_mode="Markdown",
        )

    else:
        await update.message.reply_text("⚠️ في هذه المرحلة، أرسل الملفات المطلوبة فقط.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال صورة: أمامية أو خلفية"""
    user = update.effective_user
    if user.id not in kyc_sessions:
        await update.message.reply_text("⚠️ ابدأ من جديد بأمر /start")
        return

    session = kyc_sessions[user.id]
    step = session["step"]

    if step not in ("waiting_front", "waiting_back"):
        await update.message.reply_text("⚠️ لا نتوقع صورة الآن.")
        return

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)

    # تحميل وضغط
    buf = io.BytesIO()
    await file.download_to_memory(buf)
    compressed = compress_image_bytes(buf.getvalue())

    if step == "waiting_front":
        session["files"]["front"] = compressed
        session["step"] = "waiting_back"
        await update.message.reply_text(
            f"✅ تم استلام الصورة الأمامية ({len(compressed)//1024} KB)\n\n"
            "📸 الآن أرسل *صورة البطاقة (الوجه الخلفي)*.",
            parse_mode="Markdown",
        )
    else:
        session["files"]["back"] = compressed
        session["step"] = "waiting_video"
        await update.message.reply_text(
            f"✅ تم استلام الصورة الخلفية ({len(compressed)//1024} KB)\n\n"
            "🎥 الآن أرسل *فيديو قصير (20-30 ثانية)*\n"
            "يجب أن تظهر فيه أنت وبطاقتك.",
            parse_mode="Markdown",
        )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال الفيديو"""
    user = update.effective_user
    if user.id not in kyc_sessions:
        await update.message.reply_text("⚠️ ابدأ من جديد بأمر /start")
        return

    session = kyc_sessions[user.id]
    if session["step"] != "waiting_video":
        await update.message.reply_text("⚠️ لا نتوقع فيديو الآن.")
        return

    video = update.message.video or update.message.video_note
    if not video:
        await update.message.reply_text("❌ أرسل فيديو صحيح.")
        return

    if video.file_size and video.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await update.message.reply_text(f"❌ حجم الفيديو كبير جداً. الحد: {MAX_FILE_SIZE_MB} MB")
        return

    duration = video.duration or 0
    if duration < 15 or duration > MAX_VIDEO_DURATION:
        await update.message.reply_text(
            f"❌ مدة الفيديو {duration} ثانية.\n"
            f"يجب أن تكون بين 15 و {MAX_VIDEO_DURATION} ثانية."
        )
        return

    await update.message.reply_text("⏳ جاري ضغط الفيديو...")

    file = await context.bot.get_file(video.file_id)

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, "in.mp4")
        output_path = os.path.join(tmpdir, "out.mp4")

        await file.download_to_drive(input_path)

        ok = compress_video(input_path, output_path)
        if not ok or not os.path.exists(output_path):
            await update.message.reply_text("❌ فشل ضغط الفيديو. حاول مرة أخرى.")
            return

        with open(output_path, "rb") as f:
            compressed = f.read()

    session["files"]["video"] = compressed
    session["step"] = "ready"

    size_kb = len(compressed) // 1024
    await update.message.reply_text(
        f"✅ تم استلام الفيديو وضغطه ({size_kb} KB)\n\n"
        "📤 جاري إرسال طلبك للمراجعة...",
    )

    await send_to_admin(update, context, session)


async def send_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, session: dict):
    """إرسال الطلب للمشرف مع أزرار الموافقة/الرفض"""
    user_id = update.effective_user.id
    tg_id = session["telegram_id"]
    full_name = session["full_name"]
    account_id = session["account_id"]
    username = session.get("username", "")

    header = (
        f"📩 *طلب توثيق جديد*\n\n"
        f"👤 الاسم: {full_name}\n"
        f"🆔 حساب Crynova: `{account_id}`\n"
        f"📱 Telegram: @{username if username else 'لا يوجد'}\n"
        f"🔑 Telegram ID: `{user_id}`\n"
        f"🌐 Referral ID: `{tg_id}`\n\n"
        f"⏳ بانتظار قرارك:"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ موافقة", callback_data=f"approve:{user_id}"),
            InlineKeyboardButton("❌ رفض", callback_data=f"reject:{user_id}"),
        ]
    ])

    try:
        # نص المعلومات
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=header,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

        # الصور
        await context.bot.send_photo(
            chat_id=ADMIN_CHAT_ID,
            photo=io.BytesIO(session["files"]["front"]),
            caption="📸 الوجه الأمامي",
        )
        await context.bot.send_photo(
            chat_id=ADMIN_CHAT_ID,
            photo=io.BytesIO(session["files"]["back"]),
            caption="📸 الوجه الخلفي",
        )

        # الفيديو
        await context.bot.send_video(
            chat_id=ADMIN_CHAT_ID,
            video=io.BytesIO(session["files"]["video"]),
            caption="🎥 فيديو التحقق",
            supports_streaming=True,
        )

        await update.message.reply_text(
            "✅ تم إرسال طلبك بنجاح!\n"
            "⏳ سيتم مراجعته خلال 24 ساعة.\n"
            "ستصلك النتيجة هنا.",
        )

    except Exception as e:
        logger.error(f"send_to_admin failed: {e}")
        await update.message.reply_text("❌ حدث خطأ في إرسال الطلب. حاول لاحقاً.")


# ============================================================
# 🔘 أزرار الموافقة/الرفض
# ============================================================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عندما يضغط المشرف على ✅ أو ❌"""
    query = update.callback_query
    await query.answer()

    # تحقق أن المستخدم هو المشرف
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.edit_message_text("⛔ غير مصرح لك.")
        return

    action, user_id_str = query.data.split(":")
    user_id = int(user_id_str)
    session = kyc_sessions.get(user_id)

    if not session:
        await query.edit_message_text("⚠️ الجلسة منتهية أو لم تُعد موجودة.")
        return

    tg_id = session["telegram_id"]
    full_name = session["full_name"]
    account_id = session["account_id"]

    if action == "approve":
        # تحديث Firestore
        if db:
            try:
                db.collection("users").doc(tg_id).update({
                    "kycStatus": "approved",
                    "kyc.status": "approved",
                    "kyc.reviewedAt": firestore.SERVER_TIMESTAMP,
                    "kyc.fullName": full_name,
                    "kyc.accountId": account_id,
                    "kyc.telegramId": tg_id,
                    "verified": True,
                })
                logger.info(f"✅ Firestore updated: {tg_id} → approved")
            except Exception as e:
                logger.error(f"Firestore approve failed: {e}")
                await query.edit_message_text(f"⚠️ تم القبول لكن فشل تحديث Firestore: {e}")
                return

        await query.edit_message_text(
            f"✅ *تم القبول*\n\n"
            f"👤 {full_name}\n"
            f"🆔 {account_id}\n"
            f"🌐 `{tg_id}`",
            parse_mode="Markdown",
        )

        # إبلاغ المستخدم
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="🎉 *تم توثيق حسابك بنجاح!*\n\nيمكنك الآن استخدام كل مزايا Crynova.",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Notify user failed: {e}")

    else:  # reject
        if db:
            try:
                db.collection("users").doc(tg_id).update({
                    "kycStatus": "rejected",
                    "kyc.status": "rejected",
                    "kyc.reviewedAt": firestore.SERVER_TIMESTAMP,
                })
            except Exception as e:
                logger.error(f"Firestore reject failed: {e}")

        await query.edit_message_text(
            f"❌ *تم الرفض*\n\n👤 {full_name}\n🆔 {account_id}",
            parse_mode="Markdown",
        )

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ *تم رفض طلب التوثيق*\n\nللمزيد من المعلومات، تواصل مع الدعم.",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Notify user failed: {e}")

    # تنظيف الجلسة
    kyc_sessions.pop(user_id, None)


# ============================================================
# 🔗 تسجيل المعالجات
# ============================================================
telegram_app.add_handler(CommandHandler("start", cmd_start))
telegram_app.add_handler(CallbackQueryHandler(handle_callback))
telegram_app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
telegram_app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE, handle_video))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))


# ============================================================
# 🌐 Webhook + Health check
# ============================================================
@app.post("/webhook")
async def webhook(request: Request):
    """استقبال تحديثات تيليجرام"""
    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"webhook error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
async def health():
    """نقطة الصحة — Render يستخدمها للتحقق + UptimeRobot"""
    return {
        "status": "ok",
        "bot": telegram_app.bot.username if telegram_app.bot else None,
        "sessions": len(kyc_sessions),
    }


@app.on_event("startup")
async def on_startup():
    """عند بدء التشغيل: تسجيل Webhook"""
    await telegram_app.initialize()
    await telegram_app.start()

    if WEBHOOK_URL:
        webhook_endpoint = f"{WEBHOOK_URL}/webhook"
        try:
            await telegram_app.bot.set_webhook(
                url=webhook_endpoint,
                drop_pending_updates=True,
                allowed_updates=["message", "callback_query"],
            )
            logger.info(f"✅ Webhook set: {webhook_endpoint}")
        except Exception as e:
            logger.error(f"❌ Webhook failed: {e}")
    else:
        logger.warning("⚠️ WEBHOOK_URL not set — bot will not receive updates")


@app.on_event("shutdown")
async def on_shutdown():
    """عند الإيقاف"""
    await telegram_app.stop()
    await telegram_app.shutdown()
    logger.info("🛑 Bot stopped")


# ============================================================
# 🏁 نقطة البداية (Render + Local)
# ============================================================
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
