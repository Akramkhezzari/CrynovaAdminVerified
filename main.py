# ============================================================
# Crynova KYC Server - main.py
# خادم استقبال طلبات التحقق من الهوية (KYC)
# ============================================================

import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional, List
from enum import Enum

from fastapi import FastAPI, HTTPException, Header, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator
import uvicorn

# Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, firestore

# ============================================================
# CONFIG - الإعدادات
# ============================================================

# مفتاح API للتحقق من الطلبات (اختياري لكنه موصى به بشدة)
API_KEY = os.getenv("CRYNOVA_API_KEY", "change-me-in-production")

# مسار ملف Firebase Service Account
# يمكن أن يكون:
#   1. مسار ملف محلي: /etc/secrets/serviceAccount.json
#   2. متغير بيئة JSON: FIREBASE_SERVICE_ACCOUNT_JSON
FIREBASE_CREDENTIALS_PATH = os.getenv(
    "FIREBASE_CREDENTIALS_PATH",
    "/etc/secrets/serviceAccountKey.json"
)
FIREBASE_CREDENTIALS_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "")

# اسم مجموعة Firestore لحفظ طلبات KYC
KYC_COLLECTION = "kycRequests"

# رابط الدعم لعرضه في الأخطاء
SUPPORT_URL = "https://t.me/Crynova_support"

# نطاقات CORS المسموح بها (Telegram + أي واجهة)
ALLOWED_ORIGINS = [
    "https://telegram.org",
    "https://web.telegram.org",
    "https://*.telegram.org",
    "*",  # يمكن تضييقها لاحقاً في الإنتاج
]

# ============================================================
# LOGGING - إعداد السجلات
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
    """تهيئة Firebase Admin SDK"""
    if firebase_admin._apps:
        logger.info("Firebase Admin already initialized")
        return

    try:
        if FIREBASE_CREDENTIALS_JSON:
            # من متغير بيئة (مفيد في Render)
            cred_dict = json.loads(FIREBASE_CREDENTIALS_JSON)
            cred = credentials.Certificate(cred_dict)
            logger.info("Firebase credentials loaded from env variable")
        elif os.path.exists(FIREBASE_CREDENTIALS_PATH):
            # من ملف محلي
            cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
            logger.info(f"Firebase credentials loaded from {FIREBASE_CREDENTIALS_PATH}")
        else:
            # استخدام Application Default Credentials
            cred = credentials.ApplicationDefault()
            logger.info("Firebase using Application Default Credentials")

        firebase_admin.initialize_app(cred)
        logger.info("✅ Firebase Admin initialized successfully")
    except Exception as e:
        logger.error(f"❌ Failed to initialize Firebase: {e}")
        raise


# تهيئة Firebase عند بدء التطبيق
init_firebase()
db = firestore.client()


# ============================================================
# PYDANTIC MODELS - نماذج البيانات
# ============================================================
class KycSubmission(BaseModel):
    """نموذج طلب KYC الوارد من التطبيق"""
    telegramId: str = Field(..., min_length=1, max_length=50, description="معرف Telegram للمستخدم")
    username: Optional[str] = Field("", max_length=100)
    displayName: Optional[str] = Field("", max_length=200)
    firstName: str = Field(..., min_length=2, max_length=50)
    lastName: str = Field(..., min_length=2, max_length=50)
    fullName: Optional[str] = Field("", max_length=100)
    birthDate: str = Field(..., description="تاريخ الميلاد YYYY-MM-DD")
    frontUrl: str = Field(..., min_length=10, description="رابط صورة البطاقة الأمامية")
    backUrl: str = Field(..., min_length=10, description="رابط صورة البطاقة الخلفية")
    videoUrl: str = Field(..., min_length=10, description="رابط الفيديو")
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
    """نموذج الرد عند استقبال طلب KYC"""
    success: bool
    requestId: str
    message: str
    status: str = "pending"
    submittedAt: str


class KycReviewRequest(BaseModel):
    """نموذج مراجعة الطلب (للمشرفين)"""
    action: str = Field(..., description="approve أو reject")
    rejectionReason: Optional[str] = Field("", max_length=500)


class HealthResponse(BaseModel):
    """رد فحص الصحة"""
    status: str
    timestamp: str
    service: str


# ============================================================
# FASTAPI APP
# ============================================================
app = FastAPI(
    title="Crynova KYC Server",
    description="خادم استقبال ومراجعة طلبات التحقق من الهوية لمنصة Crynova",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


# ============================================================
# AUTHENTICATION HELPER
# ============================================================
def verify_api_key(x_api_key: Optional[str] = Header(None)):
    """التحقق من مفتاح API"""
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="مفتاح API مفقود (X-API-Key)"
        )
    if x_api_key != API_KEY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="مفتاح API غير صالح"
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
    """الصفحة الرئيسية - فحص سريع"""
    return HealthResponse(
        status="online",
        timestamp=datetime.now(timezone.utc).isoformat(),
        service="Crynova KYC Server"
    )


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """فحص الصحة - يستخدمه Render لمراقبة التطبيق"""
    try:
        # اختبار اتصال Firestore
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
    """
    استقبال طلب KYC من تطبيق Telegram WebApp
    """
    # التحقق من API Key (اختياري - يمكن تعطيله في التطوير)
    if API_KEY and API_KEY != "change-me-in-production":
        if not x_api_key or x_api_key != API_KEY:
            logger.warning(f"Invalid or missing API key from {submission.telegramId}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="مفتاح API غير صالح أو مفقود"
            )

    logger.info(f"📥 استلام طلب KYC جديد من المستخدم: {submission.telegramId}")

    try:
        # التحقق من عدم وجود طلب قيد المراجعة سابقاً
        existing_ref = db.collection(KYC_COLLECTION).document(submission.telegramId)
        existing_doc = existing_ref.get()

        if existing_doc.exists:
            existing_data = existing_doc.to_dict()
            existing_status = existing_data.get("status")

            if existing_status == "pending":
                logger.warning(f"⚠️ المستخدم {submission.telegramId} لديه طلب قيد المراجعة بالفعل")
                return KycResponse(
                    success=False,
                    requestId=existing_data.get("requestId", ""),
                    message="لديك طلب قيد المراجعة بالفعل",
                    status="pending",
                    submittedAt=existing_data.get("submittedAt", {}).isoformat()
                    if isinstance(existing_data.get("submittedAt"), datetime)
                    else str(existing_data.get("submittedAt", ""))
                )

            if existing_status == "approved":
                logger.info(f"✅ المستخدم {submission.telegramId} موثق بالفعل")
                return KycResponse(
                    success=False,
                    requestId=existing_data.get("requestId", ""),
                    message="هويتك موثقة بالفعل",
                    status="approved",
                    submittedAt=existing_data.get("submittedAt", {}).isoformat()
                    if isinstance(existing_data.get("submittedAt"), datetime)
                    else str(existing_data.get("submittedAt", ""))
                )

        # إنشاء معرّف طلب فريد
        now = datetime.now(timezone.utc)
        request_id = f"KYC_{submission.telegramId}_{int(now.timestamp())}"

        # تجهيز البيانات للحفظ
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
            "serverIp": None,
            "lastUpdated": now,
            "history": [
                {
                    "action": "submitted",
                    "timestamp": now,
                    "by": "system",
                    "note": "تم استلام الطلب من التطبيق"
                }
            ]
        }

        # حفظ في Firestore
        existing_ref.set(kyc_document, merge=True)
        logger.info(f"✅ تم حفظ طلب KYC بنجاح - Request ID: {request_id}")

        # تحديث وثيقة المستخدم أيضاً (اختياري)
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
            logger.info(f"✅ تم تحديث حالة المستخدم {submission.telegramId}")
        except Exception as user_err:
            logger.warning(f"⚠️ لم يتم تحديث وثيقة المستخدم: {user_err}")

        # إرسال إشعار للمشرفين (اختياري - يمكن إضافته لاحقاً)
        # await notify_admins_new_kyc(kyc_document)

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
    """الحصول على حالة طلب KYC لمستخدم معين"""
    if API_KEY and API_KEY != "change-me-in-production":
        if not x_api_key or x_api_key != API_KEY:
            raise HTTPException(status_code=401, detail="مفتاح API غير صالح")

    try:
        doc_ref = db.collection(KYC_COLLECTION).document(telegram_id)
        doc = doc_ref.get()

        if not doc.exists:
            return {
                "success": True,
                "found": False,
                "message": "لا يوجد طلب KYC لهذا المستخدم"
            }

        data = doc.to_dict()
        return {
            "success": True,
            "found": True,
            "status": data.get("status"),
            "requestId": data.get("requestId"),
            "submittedAt": data.get("submittedAt").isoformat()
            if isinstance(data.get("submittedAt"), datetime)
            else str(data.get("submittedAt")),
            "reviewedAt": data.get("reviewedAt").isoformat()
            if isinstance(data.get("reviewedAt"), datetime) and data.get("reviewedAt")
            else None,
            "rejectionReason": data.get("rejectionReason")
        }

    except Exception as e:
        logger.error(f"فشل جلب حالة KYC: {e}")
        raise HTTPException(status_code=500, detail="فشل جلب الحالة")


# ============================================================
# ADMIN ENDPOINTS (للمشرفين فقط)
# ============================================================

@app.get("/api/admin/kyc/list")
async def list_kyc_requests(
    status_filter: Optional[str] = None,
    limit: int = 50,
    x_api_key: Optional[str] = Header(None)
):
    """
    قائمة طلبات KYC (للمشرفين)
    status_filter: pending, approved, rejected
    """
    if API_KEY and API_KEY != "change-me-in-production":
        if not x_api_key or x_api_key != API_KEY:
            raise HTTPException(status_code=401, detail="مفتاح API غير صالح")

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
            # تحويل التواريخ
            for key in ["submittedAt", "reviewedAt", "lastUpdated"]:
                if isinstance(data.get(key), datetime):
                    data[key] = data[key].isoformat()
            results.append(data)

        return {
            "success": True,
            "count": len(results),
            "requests": results
        }

    except Exception as e:
        logger.error(f"فشل جلب قائمة KYC: {e}")
        raise HTTPException(status_code=500, detail="فشل جلب القائمة")


@app.post("/api/admin/kyc/{telegram_id}/review")
async def review_kyc(
    telegram_id: str,
    review: KycReviewRequest,
    x_api_key: Optional[str] = Header(None)
):
    """
    مراجعة طلب KYC (موافقة أو رفض)
    """
    if API_KEY and API_KEY != "change-me-in-production":
        if not x_api_key or x_api_key != API_KEY:
            raise HTTPException(status_code=401, detail="مفتاح API غير صالح")

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

        # إضافة للسجل
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

        # تحديث وثيقة المستخدم
        try:
            user_ref = db.collection("users").document(telegram_id)
            user_ref.update({
                "kyc.status": new_status,
                "kyc.reviewedAt": now,
                "kyc.rejectionReason": review.rejectionReason if review.action == "reject" else None,
                "kycStatus": new_status
            })

            # إذا تمت الموافقة، تفعيل التحقق
            if review.action == "approve":
                user_ref.update({
                    "verified": True,
                    "verificationExpiry": None  # دائم حتى يقرر المشرف
                })
        except Exception as user_err:
            logger.warning(f"لم يتم تحديث وثيقة المستخدم: {user_err}")

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
# STARTUP / SHUTDOWN EVENTS
# ============================================================
@app.on_event("startup")
async def startup_event():
    logger.info("=" * 60)
    logger.info("🚀 Crynova KYC Server starting...")
    logger.info(f"📌 KYC Collection: {KYC_COLLECTION}")
    logger.info(f"🔐 API Key configured: {'Yes' if API_KEY else 'No'}")
    logger.info(f"🔥 Firebase: Connected")
    logger.info("=" * 60)


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("🛑 Crynova KYC Server shutting down...")


# ============================================================
# MAIN ENTRY POINT
# ============================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info"
    )
