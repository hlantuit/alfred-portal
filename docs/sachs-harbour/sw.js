// Alfred Portal service worker
// HTML pages: network-first (always get latest, fall back to cache when offline)
// Static assets (logo, manifest): cache-first
// data.json and images: always network-first (live data, never stale)

const CACHE = 'alfred-portal-v2';
const SHELL_STATIC = [
  './alfred-logo.png',
  './manifest.json',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(SHELL_STATIC))
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
  const path = url.pathname;

  // Always network for live data and images
  if (path.endsWith('data.json') || path.includes('/img/') || path.includes('/data/')) {
    e.respondWith(
      fetch(e.request).catch(() => caches.match(e.request))
    );
    return;
  }

  // Network-first for HTML — always serve the latest page, cache only for offline
  if (e.request.mode === 'navigate' || path.endsWith('.html') || path.endsWith('/')) {
    e.respondWith(
      fetch(e.request)
        .then(res => {
          const clone = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
          return res;
        })
        .catch(() => caches.match(e.request))
    );
    return;
  }

  // Cache-first for other static assets (logo, manifest, fonts)
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});
