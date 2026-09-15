const express = require('express');
const multer = require('multer');
const path = require('path');
const fs = require('fs');
const admin = require('firebase-admin');
const fetch = require('node-fetch');

// ============================================================
// CONFIG
// ============================================================
const BOT_TOKEN = process.env.BOT_TOKEN || '';
const ADMIN_TELEGRAM_ID = process.env.ADMIN_TELEGRAM_ID || '';
const WEBHOOK_SECRET = process.env.WEBHOOK_SECRET || 'crynova_kyc_2026';

console.log('🔧 Config:');
console.log('  BOT_TOKEN:', BOT_TOKEN ? 'SET ✅' : 'MISSING ❌');
console.log('  ADMIN_ID:', ADMIN_TELEGRAM_ID || 'MISSING ❌');

// ============================================================
// Firebase
// ============================================================
let serviceAccount;
if (process.env.FIREBASE_SERVICE_ACCOUNT) {
    serviceAccount = JSON.parse(process.env.FIREBASE_SERVICE_ACCOUNT);
} else if (fs.existsSync('./serviceAccountKey.json')) {
    serviceAccount = require('./serviceAccountKey.json');
} else {
    console.error('❌ FIREBASE_SERVICE_ACCOUNT missing!');
    process.exit(1);
}
admin.initializeApp({ credential: admin.credential.cert(serviceAccount) });
const db = admin.firestore();

// ============================================================
// Express
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
const upload = multer({ storage, limits: { fileSize: 50 * 1024 * 1024 } });

app.use((req, res, next) => {
    res.header('Access-Control-Allow-Origin', '*');
    res.header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
    res.header('Access-Control-Allow-Headers', 'Content-Type');
    if (req.method === 'OPTIONS') return res.sendStatus(200);
    next();
});

// ============================================================
// Telegram API Helpers
// ============================================================
async function tgApi(method, body) {
    if (!BOT_TOKEN) return { ok: false, description: 'No BOT_TOKEN' };
    try {
        const res = await fetch(`https://api.telegram.org/bot${BOT_TOKEN}/${method}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        return await res.json();
    } catch (e) {
        console.error(`❌ TG ${method}:`, e.message);
        return { ok: false, description: e.message };
    }
}

async function tgGetFileUrl(fileId) {
    const r = await tgApi('getFile', { file_id: fileId });
    if (!r.ok) return null;
    return `https://api.telegram.org/file/bot${BOT_TOKEN}/${r.result.file_path}`;
}

async function downloadTgFile(fileId, destPath) {
    const url = await tgGetFileUrl(fileId);
    if (!url) return false;
    try {
        const res = await fetch(url);
        if (!res.ok) return false;
        const buffer = await res.buffer();
        fs.writeFileSync(destPath, buffer);
        return true;
    } catch (e) {
        console.error('Download failed:', e.message);
        return false;
    }
}

// ============================================================
// Bot Webhook — يستقبل كل رسالة من المستخدمين
// ============================================================
app.post(`/webhook/${WEBHOOK_SECRET}`, async (req, res) => {
    res.sendStatus(200); // الرد السريع

    try {
        const update = req.body;
        const msg = update.message || update.edited_message;
        if (!msg || !msg.from) return;

        const from = msg.from;
        const userId = String(from.id);
        const username = from.username || '';
        const displayName = `${from.first_name || ''} ${from.last_name || ''}`.trim();

        // تجاهل رسائل الأدمن نفسه
        if (userId === ADMIN_TELEGRAM_ID) return;

        // تجاهل الأوامر
        if (msg.text && msg.text.startsWith('/')) return;

        // حفظ في Firestore
        const messageDoc = {
            userId,
            username,
            displayName,
            messageId: msg.message_id,
            date: new Date((msg.date || Date.now() / 1000) * 1000),
            text: msg.text || msg.caption || '',
            hasMedia: !!(msg.photo || msg.video || msg.document),
            photoFileId: msg.photo ? msg.photo[msg.photo.length - 1].file_id : null,
            videoFileId: msg.video ? msg.video.file_id : null,
            documentFileId: msg.document ? msg.document.file_id : null,
            chatId: msg.chat.id,
            source: 'bot'
        };

        // إذا كان يحتوي على ميديا، حمّلها فوراً
        if (msg.photo) {
            const fileId = msg.photo[msg.photo.length - 1].file_id;
            const fname = `bot_${userId}_${Date.now()}_photo.jpg`;
            const ok = await downloadTgFile(fileId, path.join(UPLOADS_DIR, fname));
            if (ok) messageDoc.localFile = fname;
        }
        if (msg.video) {
            const fname = `bot_${userId}_${Date.now()}_video.mp4`;
            const ok = await downloadTgFile(msg.video.file_id, path.join(UPLOADS_DIR, fname));
            if (ok) messageDoc.localFile = fname;
        }

        await db.collection('bot_messages').add(messageDoc);

        // إذا كانت رسالة "بداية توثيق"، سجّل الطلب
        if (msg.text && (msg.text.includes('KYC') || msg.text.includes('توثيق') || msg.text.includes('تحقق'))) {
            await db.collection('kyc_pending').doc(userId).set({
                userId,
                username,
                displayName,
                fullName: displayName,
                birthDate: '',
                frontFile: null,
                backFile: null,
                videoFile: null,
                submittedAt: new Date(),
                status: 'pending',
                source: 'bot'
            }, { merge: true });

            await db.collection('users').doc(userId).set({
                telegramId: userId,
                kycStatus: 'pending',
                kycSubmittedAt: new Date()
            }, { merge: true });
        }

        console.log(`📩 Bot message from ${userId} (${displayName})`);
    } catch (e) {
        console.error('❌ Webhook error:', e);
    }
});

// ============================================================
// POST /kyc — من التطبيق
// ============================================================
app.post('/kyc',
    upload.fields([
        { name: 'front', maxCount: 1 },
        { name: 'back', maxCount: 1 },
        { name: 'video', maxCount: 1 }
    ]),
    async (req, res) => {
        try {
            const { userId, firstName, lastName, birthDate, username, displayName } = req.body;
            const files = req.files || {};

            if (!userId) return res.status(400).json({ error: 'userId مطلوب' });
            if (!files.front || !files.back || !files.video) {
                return res.status(400).json({ error: 'الملفات ناقصة' });
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
                backFile: files.back[0].filename,
                videoFile: files.video[0].filename,
                submittedAt: new Date(),
                status: 'pending',
                source: 'app'
            };

            await db.collection('kyc_pending').doc(userId).set(kycData, { merge: true });
            await db.collection('users').doc(userId).set({
                telegramId: userId,
                kycStatus: 'pending',
                kycSubmittedAt: new Date(),
                kycRejectionReason: null,
                kycFullName: kycData.fullName,
                kycBirthDate: birthDate || ''
            }, { merge: true });

            // إشعار الأدمن على Telegram
            if (ADMIN_TELEGRAM_ID && BOT_TOKEN) {
                await tgApi('sendMessage', {
                    chat_id: ADMIN_TELEGRAM_ID,
                    text: `🔔 <b>طلب KYC جديد</b>\n\n👤 ${kycData.fullName}\n🆔 <code>${userId}</code>\n📱 من التطبيق`,
                    parse_mode: 'HTML'
                });
            }

            console.log(`✅ KYC from app: ${userId}`);
            res.json({ success: true });
        } catch (e) {
            console.error('❌ KYC error:', e);
            res.status(500).json({ error: e.message });
        }
    }
);

// ============================================================
// Admin API — Requests (KYC submissions)
// ============================================================
app.get('/admin/api/requests', async (req, res) => {
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
                frontUrl: d.frontFile ? `/files/${d.frontFile}` : null,
                backUrl: d.backFile ? `/files/${d.backFile}` : null,
                videoUrl: d.videoFile ? `/files/${d.videoFile}` : null,
                submittedAt: d.submittedAt,
                status: d.status,
                source: d.source || 'app'
            });
        });
        res.json(list);
    } catch (e) {
        res.status(500).json({ error: e.message });
    }
});

// ============================================================
// Admin API — Bot messages
// ============================================================
app.get('/admin/api/messages', async (req, res) => {
    try {
        const snapshot = await db.collection('bot_messages')
            .orderBy('date', 'desc')
            .limit(200)
            .get();
        const list = [];
        snapshot.forEach(doc => {
            const d = doc.data();
            list.push({
                id: doc.id,
                userId: d.userId,
                username: d.username,
                displayName: d.displayName,
                text: d.text,
                hasMedia: d.hasMedia,
                localFile: d.localFile,
                mediaUrl: d.localFile ? `/files/${d.localFile}` : null,
                date: d.date,
                source: 'bot'
            });
        });
        res.json(list);
    } catch (e) {
        res.status(500).json({ error: e.message });
    }
});

// ============================================================
// Approve / Reject
// ============================================================
app.post('/admin/api/approve/:userId', async (req, res) => {
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

        // إشعار المستخدم
        await tgApi('sendMessage', {
            chat_id: userId,
            text: '✅ <b>تمت الموافقة على طلب التحقق من هويتك</b>\n\nمرحباً بك في Crynova!',
            parse_mode: 'HTML'
        });

        res.json({ success: true });
    } catch (e) {
        res.status(500).json({ error: e.message });
    }
});

app.post('/admin/api/reject/:userId', async (req, res) => {
    const { userId } = req.params;
    const { reason } = req.body || {};
    const finalReason = reason || 'البيانات غير صحيحة أو الصور غير واضحة';
    try {
        await db.collection('users').doc(userId).set({
            kycStatus: 'rejected',
            kycRejectedAt: new Date(),
            kycRejectionReason: finalReason
        }, { merge: true });
        await db.collection('kyc_pending').doc(userId).set(
            { status: 'rejected', reviewedAt: new Date(), rejectionReason: finalReason },
            { merge: true }
        );

        // إشعار المستخدم
        await tgApi('sendMessage', {
            chat_id: userId,
            text: `❌ <b>تم رفض طلب التحقق</b>\n\nالسبب: ${finalReason}\n\nيمكنك إعادة الإرسال.`,
            parse_mode: 'HTML'
        });

        res.json({ success: true });
    } catch (e) {
        res.status(500).json({ error: e.message });
    }
});

// ============================================================
// Delete a request
// ============================================================
app.post('/admin/api/delete/:userId', async (req, res) => {
    const { userId } = req.params;
    try {
        await db.collection('kyc_pending').doc(userId).delete();
        res.json({ success: true });
    } catch (e) {
        res.status(500).json({ error: e.message });
    }
});

// ============================================================
// Files
// ============================================================
app.get('/files/:name', (req, res) => {
    const filePath = path.join(UPLOADS_DIR, req.params.name);
    if (!fs.existsSync(filePath)) return res.status(404).send('Not found');
    res.sendFile(filePath);
});

// ============================================================
// Admin HTML
// ============================================================
app.get('/admin', (req, res) => {
    res.sendFile(path.join(__dirname, 'admin.html'));
});

app.get('/', (req, res) => {
    res.json({
        status: 'Crynova KYC Server ✅',
        bot: BOT_TOKEN ? 'configured' : 'missing',
        adminId: ADMIN_TELEGRAM_ID || 'missing',
        time: new Date().toISOString()
    });
});

// ============================================================
// Set Webhook (auto on startup)
// ============================================================
async function setupWebhook() {
    if (!BOT_TOKEN || !ADMIN_TELEGRAM_ID) return;
    const renderUrl = process.env.RENDER_EXTERNAL_URL;
    if (!renderUrl) {
        console.log('⚠️  RENDER_EXTERNAL_URL not set, skipping webhook setup');
        return;
    }
    const webhookUrl = `${renderUrl}/webhook/${WEBHOOK_SECRET}`;
    const r = await tgApi('setWebhook', { url: webhookUrl, drop_pending_updates: true });
    if (r.ok) {
        console.log(`✅ Webhook set: ${webhookUrl}`);
    } else {
        console.error('❌ Webhook setup failed:', r.description);
    }
}

// ============================================================
// Start
// ============================================================
const PORT = process.env.PORT || 3000;
app.listen(PORT, async () => {
    console.log(`🚀 Server on port ${PORT}`);
    await setupWebhook();
});
