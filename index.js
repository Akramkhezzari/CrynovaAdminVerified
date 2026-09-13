require('dotenv').config();
const express = require('express');
const multer = require('multer');
const path = require('path');
const fs = require('fs');
const admin = require('firebase-admin');

// ============================================================
// Firebase Init
// ============================================================
let serviceAccount;
if (process.env.FIREBASE_SERVICE_ACCOUNT) {
    serviceAccount = JSON.parse(process.env.FIREBASE_SERVICE_ACCOUNT);
} else {
    serviceAccount = require('./serviceAccountKey.json');
}
admin.initializeApp({ credential: admin.credential.cert(serviceAccount) });
const db = admin.firestore();

// ============================================================
// Express + Multer
// ============================================================
const app = express();
app.use(express.json({ limit: '50mb' }));

const UPLOADS_DIR = path.join(__dirname, 'uploads');
if (!fs.existsSync(UPLOADS_DIR)) fs.mkdirSync(UPLOADS_DIR);

const storage = multer.diskStorage({
    destination: UPLOADS_DIR,
    filename: (req, file, cb) => {
        const userId = (req.body.userId || 'unknown').replace(/[^0-9a-zA-Z_-]/g, '');
        const ts = Date.now();
        const ext = file.fieldname === 'video' ? '.mp4' : '.jpg';
        cb(null, `${userId}_${ts}_${file.fieldname}${ext}`);
    }
});
const upload = multer({
    storage,
    limits: { fileSize: 50 * 1024 * 1024 } // 50 MB
});

const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD || 'admin123';

// ============================================================
// Auth middleware
// ============================================================
function requireAuth(req, res, next) {
    const token = req.headers['x-admin-token'] || req.query.token;
    if (token !== ADMIN_PASSWORD) return res.status(401).json({ error: 'غير مصرح' });
    next();
}

// ============================================================
// POST /kyc  — استقبال طلب من التطبيق
// ============================================================
app.post(
    '/kyc',
    upload.fields([
        { name: 'front', maxCount: 1 },
        { name: 'back',  maxCount: 1 },
        { name: 'video', maxCount: 1 }
    ]),
    async (req, res) => {
        try {
            const { userId, firstName, lastName, birthDate, username, displayName } = req.body;
            const files = req.files || {};

            if (!userId) return res.status(400).json({ error: 'userId مطلوب' });
            if (!files.front || !files.back || !files.video) {
                return res.status(400).json({ error: 'يجب إرسال الصور والفيديو' });
            }

            const kycData = {
                userId,
                firstName: firstName || '',
                lastName: lastName || '',
                fullName: `${firstName || ''} ${lastName || ''}`.trim(),
                birthDate: birthDate || '',
                username: username || '',
                displayName: displayName || '',
                frontFile: files.front[0].filename,
                backFile:  files.back[0].filename,
                videoFile: files.video[0].filename,
                submittedAt: new Date(),
                status: 'pending'
            };

            // حفظ في مجموعة مؤقتة
            await db.collection('kyc_pending').doc(userId).set(kycData);

            // تحديث حالة المستخدم
            await db.collection('users').doc(userId).update({
                kycStatus: 'pending',
                kycSubmittedAt: new Date(),
                kycRejectionReason: null,
                kycFullName: kycData.fullName,
                kycBirthDate: birthDate || ''
            });

            console.log(`✅ KYC received for ${userId}`);
            res.json({ success: true });
        } catch (e) {
            console.error('❌ KYC error:', e);
            res.status(500).json({ error: e.message });
        }
    }
);

// ============================================================
// Admin Login
// ============================================================
app.post('/admin/login', (req, res) => {
    const { password } = req.body || {};
    if (password === ADMIN_PASSWORD) return res.json({ success: true, token: ADMIN_PASSWORD });
    res.status(401).json({ error: 'كلمة المرور خاطئة' });
});

// ============================================================
// GET /admin/api/requests  — قائمة الطلبات
// ============================================================
app.get('/admin/api/requests', requireAuth, async (req, res) => {
    try {
        const snapshot = await db.collection('kyc_pending')
            .orderBy('submittedAt', 'desc')
            .limit(200)
            .get();
        const list = [];
        snapshot.forEach(doc => {
            const d = doc.data();
            list.push({
                id: doc.id,
                userId: d.userId,
                fullName: d.fullName,
                birthDate: d.birthDate,
                username: d.username,
                displayName: d.displayName,
                frontUrl: `/files/${d.frontFile}?token=${encodeURIComponent(ADMIN_PASSWORD)}`,
                backUrl:  `/files/${d.backFile}?token=${encodeURIComponent(ADMIN_PASSWORD)}`,
                videoUrl: `/files/${d.videoFile}?token=${encodeURIComponent(ADMIN_PASSWORD)}`,
                submittedAt: d.submittedAt,
                status: d.status
            });
        });
        res.json(list);
    } catch (e) {
        console.error(e);
        res.status(500).json({ error: e.message });
    }
});

// ============================================================
// POST /admin/api/approve/:userId
// ============================================================
app.post('/admin/api/approve/:userId', requireAuth, async (req, res) => {
    const { userId } = req.params;
    try {
        await db.collection('users').doc(userId).update({
            kycStatus: 'approved',
            kycApprovedAt: new Date(),
            kycRejectionReason: null
        });
        await db.collection('kyc_pending').doc(userId).set(
            { status: 'approved', reviewedAt: new Date() },
            { merge: true }
        );
        console.log(`✅ Approved: ${userId}`);
        res.json({ success: true });
    } catch (e) {
        console.error(e);
        res.status(500).json({ error: e.message });
    }
});

// ============================================================
// POST /admin/api/reject/:userId
// ============================================================
app.post('/admin/api/reject/:userId', requireAuth, async (req, res) => {
    const { userId } = req.params;
    const { reason } = req.body || {};
    try {
        await db.collection('users').doc(userId).update({
            kycStatus: 'rejected',
            kycRejectedAt: new Date(),
            kycRejectionReason: reason || 'البيانات غير صحيحة أو الصور غير واضحة'
        });
        await db.collection('kyc_pending').doc(userId).set(
            { status: 'rejected', reviewedAt: new Date(), rejectionReason: reason || '' },
            { merge: true }
        );
        console.log(`❌ Rejected: ${userId}`);
        res.json({ success: true });
    } catch (e) {
        console.error(e);
        res.status(500).json({ error: e.message });
    }
});

// ============================================================
// GET /files/:name  — عرض الملفات (مع token)
// ============================================================
app.get('/files/:name', requireAuth, (req, res) => {
    const filePath = path.join(UPLOADS_DIR, req.params.name);
    if (!fs.existsSync(filePath)) return res.status(404).send('Not found');
    res.sendFile(filePath);
});

// ============================================================
// GET /admin  — لوحة الإدارة
// ============================================================
app.get('/admin', (req, res) => {
    res.sendFile(path.join(__dirname, 'admin.html'));
});

// ============================================================
// Health check
// ============================================================
app.get('/', (req, res) => res.send('Crynova KYC Server is running ✅'));

// ============================================================
// Start
// ============================================================
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
    console.log(`🚀 Server running on port ${PORT}`);
    console.log(`📋 Admin panel: /admin`);
});
