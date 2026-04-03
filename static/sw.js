// Omnisconn Service Worker — PWA offline support
// CACHE_NAME must be bumped when static assets change.
// The app injects STATIC_VERSION at registration time so the SW
// automatically invalidates when any CSS/JS file changes.
const CACHE_NAME = 'igb-v2';
const PRECACHE_URLS = [
  '/dashboard',
  '/static/favicon.svg',
  '/static/icons/icon-192x192.png',
  '/static/icons/icon-512x512.png',
  '/static/manifest.json'
];

// Install — cache shell assets, skip waiting to activate immediately
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
  );
});

// Activate — purge ALL old caches so stale CSS/JS are gone
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// Fetch strategy:
//   /api/, /voice/, /webhook, /stripe, /oauth/ — bypass (no cache)
//   /static/ CSS/JS — network-first (always fresh, cache as fallback)
//   /static/ other  — stale-while-revalidate (images, fonts)
//   HTML pages      — network-first with offline fallback
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // Skip non-GET and cross-origin
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) return;

  // API calls and SSE — always network, no cache
  if (url.pathname.startsWith('/api/') ||
      url.pathname.startsWith('/voice/') ||
      url.pathname.startsWith('/webhook') ||
      url.pathname.startsWith('/stripe') ||
      url.pathname.startsWith('/oauth/')) {
    return;
  }

  // Static CSS/JS — network-first so ?v= busted URLs always fetch fresh
  if (url.pathname.startsWith('/static/') &&
      (url.pathname.endsWith('.css') || url.pathname.endsWith('.js'))) {
    event.respondWith(
      fetch(event.request)
        .then(response => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
          }
          return response;
        })
        .catch(() => caches.match(event.request).then(cached => cached || new Response('', { status: 503 })))
    );
    return;
  }

  // Static images/fonts/icons — stale-while-revalidate (fast + eventually fresh)
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(event.request).then(cached => {
        const fetchPromise = fetch(event.request).then(response => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
          }
          return response;
        }).catch(() => cached);
        return cached || fetchPromise;
      })
    );
    return;
  }

  // HTML pages — network-first with cache fallback
  if (event.request.headers.get('Accept')?.includes('text/html')) {
    event.respondWith(
      fetch(event.request)
        .then(response => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
          }
          return response;
        })
        .catch(() => caches.match(event.request).then(cached => cached || caches.match('/dashboard')))
    );
    return;
  }
});
