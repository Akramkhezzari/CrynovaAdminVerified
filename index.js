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
    try {
        serviceAccount = JSON.parse(process.env.FIREBASE_SERVICE_ACCOUNT);
        console.log('✅ Firebase config loaded from env variable');
    } catch (e) {
        console.error('❌ Failed to parse FIREBASE_SERVICE_ACCOUNT:', e.message);
        process.exit(1);
    }
} else if (fs.existsSync('./serviceAccountKey.json')) {
    serviceAccount = require('./serviceAccountKey.json');
    console.log('✅ Firebase config loaded from file');
} else {
    console.error('❌ FIREBASE_SERVICE_ACCOUNT not found!');
    process.exit(1);
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
    limits: { fileSize: 50 * 1024 * 1024 }
});

// ============================================================
// CORS
// ============================================================
app.use((req, res, next) => {
    res.header('Access-Control-Allow-Origin', '*');
    res.header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
    res.header('Access-Control-Allow-Headers', 'Content-Type');
    if (req.method === 'OPTIONS') return res.sendStatus(200);
    next();
});

// TEST MODE — بدون تحقق
function requireAdmin(req, res, next) {
    next();
}

// ============================================================
// POST /kyc
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

            await db.collection('kyc_pending').doc(userId).set(kycData);

            await db.collection('users').doc(userId).set({
                telegramId: userId,
                kycStatus: 'pending',
                kycSubmittedAt: new Date(),
                kycRejectionReason: null,
                kycFullName: kycData.fullName,
                kycBirthDate: birthDate || ''
            }, { merge: true });

            console.log(`✅ KYC received for ${userId}`);
            res.json({ success: true });
        } catch (e) {
            console.error('❌ KYC error:', e);
            res.status(500).json({ error: e.message });
        }
    }
);

// ============================================================
// GET /admin/api/requests
// ============================================================
app.get('/admin/api/requests', requireAdmin, async (req, res) => {
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
                frontUrl: `/files/${d.frontFile}`,
                backUrl:  `/files/${d.backFile}`,
                videoUrl: `/files/${d.videoFile}`,
                submittedAt: d.submittedAt,
                status: d.status
            });
        });
        console.log(`📋 Returning ${list.length} requests`);
        res.json(list);
    } catch (e) {
        console.error('❌ requests error:', e);
        res.status(500).json({ error: e.message });
    }
});

// ============================================================
// POST /admin/api/approve/:userId
// ============================================================
app.post('/admin/api/approve/:userId', requireAdmin, async (req, res) => {
    const { userId } = req.params;
    try {
        await db.collection('users').doc(userId).set({
            kycStatus: 'approved',
            kycApprovedAt: new Date(),
            kycRejectionReason: null
        }, { merge: true });
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
app.post('/admin/api/reject/:userId', requireAdmin, async (req, res) => {
    const { userId } = req.params;
    const { reason } = req.body || {};
    try {
        await db.collection('users').doc(userId).set({
            kycStatus: 'rejected',
            kycRejectedAt: new Date(),
            kycRejectionReason: reason || 'البيانات غير صحيحة أو الصور غير واضحة'
        }, { merge: true });
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
// GET /files/:name
// ============================================================
app.get('/files/:name', requireAdmin, (req, res) => {
    const filePath = path.join(UPLOADS_DIR, req.params.name);
    if (!fs.existsSync(filePath)) return res.status(404).send('Not found');
    res.sendFile(filePath);
});

// ============================================================
// GET /admin
// ============================================================
app.get('/admin', (req, res) => {
    res.sendFile(path.join(__dirname, 'admin.html'));
});

// ============================================================
// Root
// ============================================================
app.get('/', (req, res) => {
    res.json({
        status: 'Crynova KYC Server is running ✅',
        mode: 'TEST (no auth)',
        time: new Date().toISOString()
    });
});

// ============================================================
// Start
// ============================================================
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
    console.log(`🚀 Server running on port ${PORT}`);
    console.log(`⚠️  TEST MODE — Authentication DISABLED`);
    console.log(`📋 Admin panel: /admin`);
});
