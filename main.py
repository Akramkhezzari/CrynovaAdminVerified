# ============================================================
# Crynova KYC Server - main.py
# خادم استقبال طلبات التحقق من الهوية (KYC) + إشعارات Telegram
# ============================================================

import os
import json
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Header, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator
import uvicorn

# Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, firestore

# ============================================================
# CONFIG
# ============================================================

API_KEY = os.getenv("CRYNOVA_API_KEY", "change-me-in-production")

FIREBASE_CREDENTIALS_PATH = os.getenv(
    "FIREBASE_CREDENTIALS_PATH",
    "/etc/secrets/serviceAccountKey.json"
)
FIREBASE_CREDENTIALS_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ADMIN_CHAT_IDS = os.getenv("TELEGRAM_ADMIN_CHAT_IDS", "")

KYC_COLLECTION = "kycRequests"
SUPPORT_URL = "https://t.me/Crynova_support"

ALLOWED_ORIGINS = [
    "https://telegram.org",
    "https://web.telegram.org",
    "*",
]

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("crynova.kyc")


# ============================================================
# FIREBASE ADMIN INIT
# ============================================================
def init_firebase():
    if firebase_admin._apps:
        logger.info("Firebase Admin already initialized")
        return

    try:
        if FIREBASE_CREDENTIALS_JSON:
            logger.info("Loading Firebase credentials from env variable...")
            cred_dict = json.loads(FIREBASE_CREDENTIALS_JSON)
            cred = credentials.Certificate(cred_dict)
            logger.info("✅ Firebase credentials loaded from env variable")
        elif os.path.exists(FIREBASE_CREDENTIALS_PATH):
            logger.info(f"Loading Firebase credentials from file: {FIREBASE_CREDENTIALS_PATH}")
            cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
            logger.info("✅ Firebase credentials loaded from file")
        else:
            raise RuntimeError(
                "❌ لم يتم العثور على بيانات اعتماد Firebase!\n"
                "الحل: أضف متغير البيئة FIREBASE_SERVICE_ACCOUNT_JSON في Render"
            )

        firebase_admin.initialize_app(cred)
        logger.info("✅ Firebase Admin initialized successfully")
    except json.JSONDecodeError as e:
        logger.error(f"❌ فشل تحليل JSON: {e}")
        raise
    except Exception as e:
        logger.error(f"❌ Failed to initialize Firebase: {e}")
        raise


init_firebase()
db = firestore.client()


# ============================================================
# PYDANTIC MODELS
# ============================================================
class KycSubmission(BaseModel):
    telegramId: str = Field(..., min_length=1, max_length=50)
    username: Optional[str] = Field("", max_length=100)
    displayName: Optional[str] = Field("", max_length=200)
    firstName: str = Field(..., min_length=2, max_length=50)
    lastName: str = Field(..., min_length=2, max_length=50)
    fullName: Optional[str] = Field("", max_length=100)
    birthDate: str = Field(...)
    frontUrl: str = Field(..., min_length=10)
    backUrl: str = Field(..., min_length=10)
    videoUrl: str = Field(..., min_length=10)
    submittedAt: Optional[str] = None
    source: Optional[str] = "telegram-webapp"
    platform: Optional[str] = "Crynova"

    @validator("telegramId")
    def validate_telegram_id(cls, v):
        if not v.strip():
            raise ValueError("telegramId لا يمكن أن يكون فارغاً")
        return v.strip()

    @validator("birthDate")
    def validate_birth_date(cls, v):
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("تاريخ الميلاد يجب أن يكون بصيغة YYYY-MM-DD")
        return v

    @validator("frontUrl", "backUrl", "videoUrl")
    def validate_urls(cls, v):
        if not v.startswith(("http://", "https://")):
            raise ValueError("الرابط يجب أن يبدأ بـ http:// أو https://")
        return v


class KycResponse(BaseModel):
    success: bool
    requestId: str
    message: str
    status: str = "pending"
    submittedAt: str


class KycReviewRequest(BaseModel):
    action: str = Field(...)
    rejectionReason: Optional[str] = Field("", max_length=500)


class HealthResponse(BaseModel):
    status: str
    timestamp: str
    service: str


# ============================================================
# FASTAPI APP
# ============================================================
app = FastAPI(
    title="Crynova KYC Server",
    description="خادم استقبال ومراجعة طلبات التحقق من الهوية + إشعارات Telegram",
    version="1.2.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


# ============================================================
# TELEGRAM NOTIFICATIONS
# ============================================================
def get_admin_chat_ids():
    if not TELEGRAM_ADMIN_CHAT_IDS:
        return []
    return [cid.strip() for cid in TELEGRAM_ADMIN_CHAT_IDS.split(",") if cid.strip()]


async def send_telegram_message(chat_id: str, text: str, reply_markup: dict = None):
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("⚠️ TELEGRAM_BOT_TOKEN غير مضبوط")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=payload)
            if response.status_code == 200:
                logger.info(f"✅ تم إرسال الإشعار إلى {chat_id}")
                return True
            else:
                logger.error(f"❌ فشل إرسال الإشعار إلى {chat_id}: {response.text}")
                return False
    except Exception as e:
        logger.error(f"❌ خطأ في إرسال Telegram: {e}")
        return False


async def notify_admins_new_kyc(kyc_data: dict):
    admin_ids = get_admin_chat_ids()
    logger.info(f"👥 عدد المشرفين للإشعار: {len(admin_ids)} - {admin_ids}")

    if not admin_ids:
        logger.warning("⚠️ لا يوجد TELEGRAM_ADMIN_CHAT_IDS محدد")
        return

    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN غير مضبوط")
        return

    telegram_id = kyc_data.get("telegramId", "—")
    username = kyc_data.get("username", "")
    display_name = kyc_data.get("displayName", "")
    full_name = kyc_data.get("fullName", "")
    birth_date = kyc_data.get("birthDate", "")
    front_url = kyc_data.get("frontUrl", "")
    back_url = kyc_data.get("backUrl", "")
    video_url = kyc_data.get("videoUrl", "")
    request_id = kyc_data.get("requestId", "")

    message = (
        f"🔔 <b>طلب توثيق هوية جديد</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 <b>الاسم الكامل:</b> {full_name}\n"
        f"📛 <b>الاسم:</b> {kyc_data.get('firstName', '')}\n"
        f"📛 <b>اللقب:</b> {kyc_data.get('lastName', '')}\n"
        f"🎂 <b>تاريخ الميلاد:</b> {birth_date}\n\n"
        f"🆔 <b>Telegram ID:</b> <code>{telegram_id}</code>\n"
        f"📱 <b>Username:</b> @{username if username else '—'}\n"
        f"💬 <b>Display Name:</b> {display_name or '—'}\n\n"
        f"🔖 <b>Request ID:</b> <code>{request_id}</code>\n"
        f"⏰ <b>تم الإرسال:</b> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📎 <b>المرفقات:</b>\n"
        f"• <a href='{front_url}'>صورة البطاقة (أمامي)</a>\n"
        f"• <a href='{back_url}'>صورة البطاقة (خلفي)</a>\n"
        f"• <a href='{video_url}'>مقطع الفيديو</a>\n\n"
        f"<i>لاستعراض البروفايل:</i> <a href='tg://user?id={telegram_id}'>فتح حساب المستخدم</a>"
    )

    for admin_id in admin_ids:
        logger.info(f"📤 إرسال إلى chat_id: {admin_id}")
        result = await send_telegram_message(admin_id, message)
        if result:
            logger.info(f"✅ نجح الإرسال إلى {admin_id}")
        else:
            logger.error(f"❌ فشل الإرسال إلى {admin_id}")


async def notify_user_kyc_reviewed(telegram_id: str, status: str, reason: str = ""):
    if status == "approved":
        message = (
            f"✅ <b>تم توثيق هويتك بنجاح!</b>\n\n"
            f"مرحباً بك في منصة Crynova. تمت الموافقة على طلب التحقق من هويتك.\n\n"
            f"🎉 شكراً لثقتك بنا."
        )
    elif status == "rejected":
        message = (
            f"❌ <b>تم رفض طلب التحقق من هويتك</b>\n\n"
            f"<b>السبب:</b> {reason or 'يرجى إعادة الإرسال ببيانات صحيحة'}\n\n"
            f"للاستفسار: {SUPPORT_URL}"
        )
    else:
        return

    await send_telegram_message(telegram_id, message)


# ============================================================
# AUTHENTICATION
# ============================================================
def verify_api_key(x_api_key: Optional[str] = Header(None)):
    if API_KEY and API_KEY != "change-me-in-production":
        if not x_api_key or x_api_key != API_KEY:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="مفتاح API غير صالح أو مفقود"
            )
    return True


# ============================================================
# GLOBAL EXCEPTION HANDLER
# ============================================================
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"خطأ غير متوقع: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "message": "حدث خطأ في السيرفر، يرجى المحاولة لاحقاً",
            "support": SUPPORT_URL
        }
    )


# ============================================================
# ENDPOINTS
# ============================================================

@app.get("/", response_model=HealthResponse)
async def root():
    return HealthResponse(
        status="online",
        timestamp=datetime.now(timezone.utc).isoformat(),
        service="Crynova KYC Server"
    )


@app.api_route("/health", methods=["GET", "HEAD"], response_model=HealthResponse)
async def health_check():
    try:
        db.collection("_health").document("ping").set({
            "lastPing": datetime.now(timezone.utc),
            "service": "crynova-kyc"
        }, merge=True)
        return HealthResponse(
            status="healthy",
            timestamp=datetime.now(timezone.utc).isoformat(),
            service="Crynova KYC Server"
        )
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(status_code=503, detail="Service unhealthy")


@app.post("/api/kyc/submit", response_model=KycResponse)
async def submit_kyc(
    submission: KycSubmission,
    x_api_key: Optional[str] = Header(None)
):
    verify_api_key(x_api_key)
    logger.info(f"📥 استلام طلب KYC من: {submission.telegramId}")

    try:
        existing_ref = db.collection(KYC_COLLECTION).document(submission.telegramId)
        existing_doc = existing_ref.get()

        if existing_doc.exists:
            existing_data = existing_doc.to_dict()
            existing_status = existing_data.get("status")

            if existing_status == "pending":
                logger.warning(f"⚠️ طلب قيد المراجعة: {submission.telegramId}")
                return KycResponse(
                    success=False,
                    requestId=existing_data.get("requestId", ""),
                    message="لديك طلب قيد المراجعة بالفعل",
                    status="pending",
                    submittedAt=str(existing_data.get("submittedAt", ""))
                )

            if existing_status == "approved":
                logger.info(f"✅ موثق بالفعل: {submission.telegramId}")
                return KycResponse(
                    success=False,
                    requestId=existing_data.get("requestId", ""),
                    message="هويتك موثقة بالفعل",
                    status="approved",
                    submittedAt=str(existing_data.get("submittedAt", ""))
                )

        now = datetime.now(timezone.utc)
        request_id = f"KYC_{submission.telegramId}_{int(now.timestamp())}"

        kyc_document = {
            "requestId": request_id,
            "telegramId": submission.telegramId,
            "username": submission.username or "",
            "displayName": submission.displayName or "",
            "firstName": submission.firstName,
            "lastName": submission.lastName,
            "fullName": submission.fullName or f"{submission.firstName} {submission.lastName}",
            "birthDate": submission.birthDate,
            "frontUrl": submission.frontUrl,
            "backUrl": submission.backUrl,
            "videoUrl": submission.videoUrl,
            "status": "pending",
            "source": submission.source or "telegram-webapp",
            "platform": submission.platform or "Crynova",
            "submittedAt": now,
            "reviewedAt": None,
            "reviewedBy": None,
            "rejectionReason": None,
            "lastUpdated": now,
            "notifiedAt": None,
            "history": [
                {
                    "action": "submitted",
                    "timestamp": now,
                    "by": "system",
                    "note": "تم استلام الطلب من التطبيق"
                }
            ]
        }

        existing_ref.set(kyc_document, merge=True)
        logger.info(f"✅ تم حفظ الطلب: {request_id}")

        try:
            user_ref = db.collection("users").document(submission.telegramId)
            user_ref.update({
                "kyc.status": "pending",
                "kyc.requestId": request_id,
                "kyc.submittedAt": now,
                "kyc.renderSyncedAt": now,
                "kyc.renderSyncStatus": "synced",
                "kycStatus": "pending"
            })
        except Exception as user_err:
            logger.warning(f"⚠️ لم يتم تحديث وثيقة المستخدم: {user_err}")

        try:
            logger.info(f"📤 إرسال إشعار KYC إلى {len(get_admin_chat_ids())} مشرف...")
            await notify_admins_new_kyc(kyc_document)
            logger.info("✅ تم إرسال الإشعارات بنجاح")
            try:
                existing_ref.update({"notifiedAt": now})
            except Exception:
                pass
        except Exception as notify_err:
            logger.error(f"❌ فشل إرسال الإشعارات: {notify_err}", exc_info=True)

        return KycResponse(
            success=True,
            requestId=request_id,
            message="تم استلام طلبك بنجاح، وهو الآن قيد المراجعة",
            status="pending",
            submittedAt=now.isoformat()
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ فشل استقبال طلب KYC: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="فشل استقبال الطلب، يرجى المحاولة لاحقاً"
        )


@app.get("/api/kyc/status/{telegram_id}")
async def get_kyc_status(
    telegram_id: str,
    x_api_key: Optional[str] = Header(None)
):
    verify_api_key(x_api_key)
    try:
        doc_ref = db.collection(KYC_COLLECTION).document(telegram_id)
        doc = doc_ref.get()
        if not doc.exists:
            return {"success": True, "found": False, "message": "لا يوجد طلب KYC"}
        data = doc.to_dict()
        return {
            "success": True,
            "found": True,
            "status": data.get("status"),
            "requestId": data.get("requestId"),
            "submittedAt": data.get("submittedAt").isoformat()
            if isinstance(data.get("submittedAt"), datetime) else str(data.get("submittedAt")),
            "reviewedAt": data.get("reviewedAt").isoformat()
            if isinstance(data.get("reviewedAt"), datetime) and data.get("reviewedAt") else None,
            "rejectionReason": data.get("rejectionReason")
        }
    except Exception as e:
        logger.error(f"فشل جلب حالة KYC: {e}")
        raise HTTPException(status_code=500, detail="فشل جلب الحالة")


# ============================================================
# ADMIN ENDPOINTS
# ============================================================

@app.get("/api/admin/kyc/list")
async def list_kyc_requests(
    status_filter: Optional[str] = None,
    limit: int = 50,
    x_api_key: Optional[str] = Header(None)
):
    verify_api_key(x_api_key)
    try:
        query = db.collection(KYC_COLLECTION)
        if status_filter:
            query = query.where("status", "==", status_filter)
        query = query.order_by("submittedAt", direction=firestore.Query.DESCENDING).limit(limit)

        docs = query.stream()
        results = []
        for doc in docs:
            data = doc.to_dict()
            data["telegramId"] = doc.id
            for key in ["submittedAt", "reviewedAt", "lastUpdated", "notifiedAt"]:
                if isinstance(data.get(key), datetime):
                    data[key] = data[key].isoformat()
            results.append(data)

        return {"success": True, "count": len(results), "requests": results}
    except Exception as e:
        logger.error(f"فشل جلب قائمة KYC: {e}")
        raise HTTPException(status_code=500, detail="فشل جلب القائمة")


@app.post("/api/admin/kyc/{telegram_id}/review")
async def review_kyc(
    telegram_id: str,
    review: KycReviewRequest,
    x_api_key: Optional[str] = Header(None)
):
    verify_api_key(x_api_key)

    if review.action not in ["approve", "reject"]:
        raise HTTPException(status_code=400, detail="action يجب أن يكون approve أو reject")

    try:
        doc_ref = db.collection(KYC_COLLECTION).document(telegram_id)
        doc = doc_ref.get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail="الطلب غير موجود")

        now = datetime.now(timezone.utc)
        new_status = "approved" if review.action == "approve" else "rejected"

        update_data = {
            "status": new_status,
            "reviewedAt": now,
            "lastUpdated": now,
            "rejectionReason": review.rejectionReason if review.action == "reject" else None
        }

        history_entry = {
            "action": review.action,
            "timestamp": now,
            "by": "admin",
            "note": review.rejectionReason or ""
        }

        doc_ref.update({
            **update_data,
            "history": firestore.ArrayUnion([history_entry])
        })

        try:
            user_ref = db.collection("users").document(telegram_id)
            user_update = {
                "kyc.status": new_status,
                "kyc.reviewedAt": now,
                "kyc.rejectionReason": review.rejectionReason if review.action == "reject" else None,
                "kycStatus": new_status
            }
            if review.action == "approve":
                user_update["verified"] = True
                user_update["verificationExpiry"] = None
            user_ref.update(user_update)
        except Exception as user_err:
            logger.warning(f"لم يتم تحديث وثيقة المستخدم: {user_err}")

        asyncio.create_task(
            notify_user_kyc_reviewed(telegram_id, new_status, review.rejectionReason or "")
        )

        logger.info(f"✅ تمت مراجعة الطلب {telegram_id}: {new_status}")

        return {
            "success": True,
            "message": f"تم {'قبول' if review.action == 'approve' else 'رفض'} الطلب بنجاح",
            "status": new_status,
            "reviewedAt": now.isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"فشل مراجعة KYC: {e}")
        raise HTTPException(status_code=500, detail="فشل المراجعة")


# ============================================================
# TEST ENDPOINT
# ============================================================
@app.post("/api/test/notify")
async def test_notify(x_api_key: Optional[str] = Header(None)):
    verify_api_key(x_api_key)

    if not TELEGRAM_BOT_TOKEN:
        return {"success": False, "message": "TELEGRAM_BOT_TOKEN غير مضبوط"}

    admin_ids = get_admin_chat_ids()
    if not admin_ids:
        return {"success": False, "message": "TELEGRAM_ADMIN_CHAT_IDS غير مضبوط"}

    results = []
    for admin_id in admin_ids:
        ok = await send_telegram_message(
            admin_id,
            "🧪 <b>اختبار إشعار Crynova KYC</b>\n\nإذا وصلتك هذه الرسالة، فالإشعارات تعمل بشكل صحيح ✅"
        )
        results.append({"chat_id": admin_id, "success": ok})

    return {"success": True, "results": results}


# ============================================================
# DEBUG ENDPOINTS
# ============================================================
@app.get("/api/debug/config")
async def debug_config(x_api_key: Optional[str] = Header(None)):
    verify_api_key(x_api_key)

    bot_token_status = "Set ✅" if TELEGRAM_BOT_TOKEN else "Missing ❌"
    bot_token_preview = (
        f"{TELEGRAM_BOT_TOKEN[:10]}...{TELEGRAM_BOT_TOKEN[-5:]}"
        if TELEGRAM_BOT_TOKEN and len(TELEGRAM_BOT_TOKEN) > 20
        else "N/A"
    )

    admin_ids = get_admin_chat_ids()

    return {
        "api_key_configured": bool(API_KEY and API_KEY != "change-me-in-production"),
        "telegram_bot_token": bot_token_status,
        "telegram_bot_token_preview": bot_token_preview,
        "admin_chat_ids": admin_ids,
        "admin_count": len(admin_ids),
        "firebase": "Connected" if firebase_admin._apps else "Not initialized",
        "kyc_collection": KYC_COLLECTION
    }


@app.get("/api/debug/telegram")
async def debug_telegram(x_api_key: Optional[str] = Header(None)):
    verify_api_key(x_api_key)

    debug_info = {
        "step_1_token_check": None,
        "step_2_getMe": None,
        "step_3_webhook_info": None,
        "step_4_admin_ids": None,
        "step_5_send_test": None,
        "errors": []
    }

    if not TELEGRAM_BOT_TOKEN:
        debug_info["step_1_token_check"] = "❌ التوكن غير موجود"
        debug_info["errors"].append("TELEGRAM_BOT_TOKEN غير مضبوط في Render")
        return debug_info
    debug_info["step_1_token_check"] = "✅ التوكن موجود"

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe")
            data = r.json()
            if data.get("ok"):
                bot = data["result"]
                debug_info["step_2_getMe"] = {
                    "status": "✅ صحيح",
                    "bot_id": bot.get("id"),
                    "bot_username": bot.get("username"),
                    "bot_name": bot.get("first_name")
                }
            else:
                debug_info["step_2_getMe"] = f"❌ فشل: {data}"
                debug_info["errors"].append(f"getMe failed: {data}")
        except Exception as e:
            debug_info["step_2_getMe"] = f"❌ خطأ: {str(e)}"
            debug_info["errors"].append(f"getMe exception: {e}")

        try:
            r = await client.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getWebhookInfo")
            data = r.json()
            if data.get("ok"):
                wh = data["result"]
                webhook_url = wh.get("url", "")
                if webhook_url:
                    debug_info["step_3_webhook_info"] = {
                        "status": "⚠️ يوجد webhook قديم يجب حذفه",
                        "url": webhook_url,
                        "pending_updates": wh.get("pending_update_count", 0)
                    }
                    debug_info["errors"].append(f"Webhook قديم مضبوط: {webhook_url} - احذفه")
                else:
                    debug_info["step_3_webhook_info"] = "✅ لا يوجد webhook (جيد)"
            else:
                debug_info["step_3_webhook_info"] = f"❌ فشل: {data}"
        except Exception as e:
            debug_info["step_3_webhook_info"] = f"❌ خطأ: {str(e)}"

        admin_ids = get_admin_chat_ids()
        debug_info["step_4_admin_ids"] = {
            "count": len(admin_ids),
            "ids": admin_ids
        }

        if not admin_ids:
            debug_info["errors"].append("لا يوجد admin chat IDs محدد")
            return debug_info

        test_results = []
        for admin_id in admin_ids:
            try:
                msg = (
                    "🧪 <b>اختبار تشخيصي</b>\n\n"
                    "إذا وصلتك هذه الرسالة، فالإشعارات تعمل بشكل صحيح ✅\n"
                    f"chat_id: <code>{admin_id}</code>"
                )
                r = await client.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": admin_id,
                        "text": msg,
                        "parse_mode": "HTML"
                    }
                )
                data = r.json()
                if data.get("ok"):
                    test_results.append({
                        "chat_id": admin_id,
                        "status": "✅ نجح الإرسال"
                    })
                else:
                    error_desc = data.get("description", "unknown")
                    test_results.append({
                        "chat_id": admin_id,
                        "status": f"❌ فشل: {error_desc}"
                    })
                    debug_info["errors"].append(f"فشل الإرسال إلى {admin_id}: {error_desc}")
            except Exception as e:
                test_results.append({
                    "chat_id": admin_id,
                    "status": f"❌ خطأ: {str(e)}"
                })
                debug_info["errors"].append(f"exception لـ {admin_id}: {e}")

        debug_info["step_5_send_test"] = test_results

    return debug_info


# ============================================================
# STARTUP / SHUTDOWN
# ============================================================
@app.on_event("startup")
async def startup_event():
    logger.info("=" * 60)
    logger.info("🚀 Crynova KYC Server starting...")
    logger.info(f"📌 KYC Collection: {KYC_COLLECTION}")
    logger.info(f"🔐 API Key configured: {'Yes' if API_KEY and API_KEY != 'change-me-in-production' else 'No'}")
    logger.info(f"📨 Telegram Bot Token: {'Set ✅' if TELEGRAM_BOT_TOKEN else 'Missing ❌'}")
    logger.info(f"👥 Admin Chat IDs: {len(get_admin_chat_ids())} admin(s)")
    logger.info(f"🔥 Firebase: Connected")
    logger.info("=" * 60)


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("🛑 Crynova KYC Server shutting down...")


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False, log_level="info")
