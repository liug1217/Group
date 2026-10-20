// ESN 推播小幫手（Web Push）：server.py 是 Python，標準函式庫沒有推播要的加密，
// 所以加密和簽章交給這支 Node 程式（只用 Node 內建的 crypto，不用裝套件）。
// 做法照智行地圖的 lib/webpush.js：內容加密 RFC 8291（aes128gcm），身分 RFC 8292（VAPID，JWT ES256）。
//
//   node push.js keys          → 印出新的一組 VAPID 金鑰 {"publicKey","privateKey"}（第一次用時由 server.py 呼叫）
//   node push.js send < 輸入    → 輸入 JSON {vapid, subject, subs:[訂閱…], message:{title, body, url, tag}, urgency}
//                                 印出 {"results":[{"endpoint","status"}]}；404 / 410 代表訂閱已經不在了
const crypto = require('crypto');

const b64u = (buf) => Buffer.from(buf).toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const unb64u = (s) => Buffer.from(String(s).replace(/-/g, '+').replace(/_/g, '/'), 'base64');

function newKeys() {
  const ecdh = crypto.createECDH('prime256v1');
  ecdh.generateKeys();
  return { publicKey: b64u(ecdh.getPublicKey()), privateKey: b64u(ecdh.getPrivateKey()) };
}

// VAPID 私鑰（原始 32 bytes）→ Node 的 KeyObject（簽 JWT 用）
function privateKeyObject(v) {
  const pub = unb64u(v.publicKey);
  return crypto.createPrivateKey({ key: { kty: 'EC', crv: 'P-256', d: v.privateKey, x: b64u(pub.subarray(1, 33)), y: b64u(pub.subarray(33, 65)) }, format: 'jwk' });
}
function vapidHeader(endpoint, v, subject) {
  const aud = new URL(endpoint).origin;
  const head = b64u(JSON.stringify({ typ: 'JWT', alg: 'ES256' }));
  const body = b64u(JSON.stringify({ aud, exp: Math.floor(Date.now() / 1000) + 12 * 3600, sub: subject }));
  const sig = crypto.sign('sha256', Buffer.from(`${head}.${body}`), { key: privateKeyObject(v), dsaEncoding: 'ieee-p1363' });
  return `vapid t=${head}.${body}.${b64u(sig)}, k=${v.publicKey}`;
}

const hmac = (key, data) => crypto.createHmac('sha256', key).update(data).digest();
// RFC 8291：用訂閱的 p256dh、auth 把內容加密成 aes128gcm 格式
function encrypt(sub, payload) {
  const uaPublic = unb64u(sub.keys.p256dh);
  const auth = unb64u(sub.keys.auth);
  const ecdh = crypto.createECDH('prime256v1');
  ecdh.generateKeys();
  const asPublic = ecdh.getPublicKey();
  const secret = ecdh.computeSecret(uaPublic);
  const prkKey = hmac(auth, secret);
  const ikm = hmac(prkKey, Buffer.concat([Buffer.from('WebPush: info\0'), uaPublic, asPublic, Buffer.from([1])]));
  const salt = crypto.randomBytes(16);
  const prk = hmac(salt, ikm);
  const cek = hmac(prk, Buffer.concat([Buffer.from('Content-Encoding: aes128gcm\0'), Buffer.from([1])])).subarray(0, 16);
  const nonce = hmac(prk, Buffer.concat([Buffer.from('Content-Encoding: nonce\0'), Buffer.from([1])])).subarray(0, 12);
  const cipher = crypto.createCipheriv('aes-128-gcm', cek, nonce);
  const body = Buffer.concat([cipher.update(Buffer.concat([Buffer.from(payload), Buffer.from([2])])), cipher.final(), cipher.getAuthTag()]);
  const rs = Buffer.alloc(4);
  rs.writeUInt32BE(4096);
  return Buffer.concat([salt, rs, Buffer.from([asPublic.length]), asPublic, body]);
}

async function send({ vapid, subject, subs, message, urgency = 'normal' }) {
  const payload = JSON.stringify(message);
  return Promise.all((subs || []).map(async (sub) => {
    try {
      const res = await fetch(sub.endpoint, {
        method: 'POST',
        body: encrypt(sub, payload),
        headers: {
          'Content-Encoding': 'aes128gcm',
          'Content-Type': 'application/octet-stream',
          TTL: urgency === 'high' ? '60' : '86400',
          Urgency: urgency,
          Authorization: vapidHeader(sub.endpoint, vapid, subject),
        },
      });
      return { endpoint: sub.endpoint, status: res.status };
    } catch (error) {
      return { endpoint: sub.endpoint, status: 0, error: String(error?.message || error) };
    }
  }));
}

async function main() {
  const mode = process.argv[2];
  if (mode === 'keys') {
    process.stdout.write(JSON.stringify(newKeys()));
    return;
  }
  if (mode === 'send') {
    let input = '';
    for await (const chunk of process.stdin) input += chunk;
    const results = await send(JSON.parse(input));
    process.stdout.write(JSON.stringify({ results }));
    return;
  }
  process.stderr.write('usage: node push.js keys | send\n');
  process.exit(2);
}

main().catch((error) => {
  process.stderr.write(String(error?.stack || error));
  process.exit(1);
});
