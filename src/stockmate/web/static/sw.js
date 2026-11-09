// 웹 푸시 전용 서비스 워커: 가격 알림을 띄우고, 누르면 대시보드를 연다. 화면 캐시는 하지 않는다.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data.json(); } catch { d = { title: "stockmate", body: e.data?.text() || "" }; }
  e.waitUntil(self.registration.showNotification(d.title || "stockmate", {
    body: d.body || "", tag: d.tag, icon: "/icon.png", badge: "/icon.png", data: { url: d.url || "/" },
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = new URL(e.notification.data?.url || "/", self.location.origin).href;
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const win = wins.find((w) => new URL(w.url).origin === self.location.origin);
    if (win) { await win.focus(); return win.navigate(url).catch(() => {}); }
    return self.clients.openWindow(url);
  })());
});
