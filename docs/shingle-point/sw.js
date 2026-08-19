// Alfred Portal service worker — Shingle Point
// Caches the shell (HTML, logo, manifest) for offline use.
// data.json and chart images are always fetched fresh from the network.

const CACHE = 'alfred-shingle-v1';
const SHELL = [
  './',
  './alfred-logo.png',
  './manifest.json',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(SHELL))
  );
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // Always network-first for data.json and img/ (live data)
  if (url.pathname.endsWith('data.json') || url.pathname.includes('/img/')) {
    e.respondWith(
      fetch(e.request).catch(() => caches.match(e.request))
    );
    return;
  }
  // Cache-first for shell assets
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});
