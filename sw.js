// ★Bi_藍冰團★ 的 service worker：只做推播通知（有人傳訊息、打電話來），不快取網頁（網頁照常從 GitHub 抓最新的）
//   推播內容：{ title, body, tag, url }；title 是誰傳的，body 是訊息（或「視訊來電」）
//   點通知 → 已經開著的網頁就切過去，沒有就開新的
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

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
