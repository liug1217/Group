// ★Bi_藍冰團★ 的 service worker：
//   1. 離線也能打開：網站自己的檔案（Group.html、玻璃引擎、圖片）都存在手機裡
//      - Group.html：先上網抓最新的（3 秒內沒回應或沒網路就用存的），所以一更新大家馬上看得到新版
//      - 其他檔案：先用存的（秒開），背景再上網更新一份，下次打開就是新的
//      - 聊天伺服器、短影音影片這些別的網站的東西不存，照常連網（沒網路時聊天會顯示連不上）
//   2. 推播通知（有人傳訊息、打電話來）：{ title, body, tag, url }；點通知 → 切到 / 打開網頁
const CACHE = 'esn-offline-v1';
const PRECACHE = [
  './Group.html',
  './manifest.json',
  './assets/lib/components.js',
  './assets/lib/liquid-glass.js',
  './assets/home-bg.jpg',
  './assets/group-avatar.jpg',
  './assets/member-1.jpg',
  './assets/member-2.jpg',
  './assets/member-3.jpg',
  './assets/member-4.jpg',
  './assets/member-5.jpg',
  './assets/member-6.jpg',
  './assets/member-7.jpg',
  './assets/login-facebook.png',
  './assets/login-google.jpg',
  './assets/login-instagram.jpg',
  './assets/login-line.png',
  './assets/icon-bell.svg',
  './assets/icon-chat.svg',
  './assets/icon-clock.svg',
  './assets/icon-eye.svg',
  './assets/icon-home.svg',
  './assets/icon-lock.svg',
  './assets/icon-user.svg',
  './assets/icon-video.svg',
  './assets/tab-bell.svg',
  './assets/tab-chat.svg',
  './assets/tab-film.svg',
  './assets/tab-house.svg',
];
const PAGE = new URL('./Group.html', self.location).href;

self.addEventListener('install', (event) => {
  // cache: 'reload'：跳過瀏覽器自己的快取，存最新的
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(PRECACHE.map((url) => new Request(url, { cache: 'reload' })))));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  // 換版本時把舊的快取清掉
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((key) => key.startsWith('esn-offline-') && key !== CACHE).map((key) => caches.delete(key))))
    .then(() => self.clients.claim()));
});

function timeout(ms) {
  return new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), ms));
}

// 網頁本身：先上網（最多等 3 秒），拿到就順便存起來；沒網路就用存的
async function pageRequest(request) {
  const cache = await caches.open(CACHE);
  try {
    const response = await Promise.race([fetch(request), timeout(3000)]);
    if (response.ok) cache.put(PAGE, response.clone());
    return response;
  } catch {
    const cached = await cache.match(PAGE);
    if (cached) return cached;
    throw new Error('offline and not cached');
  }
}

// 圖片、玻璃引擎：先用存的，背景再更新
async function assetRequest(event) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(event.request, { ignoreSearch: true });
  const update = fetch(event.request)
    .then((response) => {
      if (response.ok) cache.put(event.request, response.clone());
      return response;
    })
    .catch(() => null);
  if (cached) {
    event.waitUntil(update);
    return cached;
  }
  const fresh = await update;
  return fresh || Response.error();
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  // 只管自己網站的檔案；聊天伺服器（/api）、影片、別的網站照常連網
  if (url.origin !== self.location.origin || url.pathname.includes('/api/')) return;
  // 影片用 Range 分段抓，不經過快取
  if (request.headers.has('range')) return;
  if (request.mode === 'navigate' || url.pathname.endsWith('/Group.html') || url.pathname.endsWith('/')) {
    event.respondWith(pageRequest(request));
    return;
  }
  event.respondWith(assetRequest(event));
});

// ---------- 推播通知 ----------
self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { body: event.data ? event.data.text() : '' };
  }
  const isCall = data.tag === 'call';
  event.waitUntil(self.registration.showNotification(data.title || '★Bi_藍冰團★', {
    body: data.body || '',
    icon: 'assets/group-avatar.jpg',
    badge: 'assets/group-avatar.jpg',
    tag: data.tag || 'esn', // 同一個聊天室只留最新一則
    renotify: true,
    requireInteraction: isCall, // 來電要一直顯示到使用者處理
    vibrate: isCall ? [400, 200, 400, 200, 400] : [120],
    data: { url: data.url || './Group.html' },
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || './Group.html';
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
    for (const client of list) {
      if (/Group\.html/.test(client.url) && 'focus' in client) return client.focus();
    }
    return self.clients.openWindow(url);
  }));
});
