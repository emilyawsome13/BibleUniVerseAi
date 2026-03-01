const SHELL_CACHE = 'bible-ai-shell-v2';
const STATIC_CACHE = 'bible-ai-static-v2';
const API_CACHE = 'bible-ai-api-v1';
const ALL_CACHES = [SHELL_CACHE, STATIC_CACHE, API_CACHE];

const SHELL_URLS = [
  '/',
  '/static/manifest.json',
  '/static/images/cross-particle.png'
];

const API_CACHEABLE_PATHS = [
  '/api/i18n/catalog',
  '/api/bible/books'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_URLS)).catch(() => Promise.resolve())
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key.startsWith('bible-ai-') && !ALL_CACHES.includes(key))
          .map((key) => caches.delete(key))
      )
    )
  );
  self.clients.claim();
});

function shouldCacheApi(url) {
  return API_CACHEABLE_PATHS.some((path) => url.pathname.startsWith(path));
}

async function networkFirst(request, cacheName) {
  try {
    const response = await fetch(request);
    if (response && response.ok) {
      const cache = await caches.open(cacheName);
      cache.put(request, response.clone()).catch(() => {});
    }
    return response;
  } catch (err) {
    const cached = await caches.match(request);
    if (cached) return cached;
    throw err;
  }
}

async function staleWhileRevalidate(request, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request);
  const networkPromise = fetch(request)
    .then((response) => {
      if (response && response.ok) cache.put(request, response.clone()).catch(() => {});
      return response;
    })
    .catch(() => null);
  if (cached) return cached;
  const fresh = await networkPromise;
  if (fresh) return fresh;
  return caches.match('/');
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === 'navigate') {
    event.respondWith(
      networkFirst(request, SHELL_CACHE).catch(async () => {
        return (await caches.match('/')) || Response.error();
      })
    );
    return;
  }

  if (url.pathname.startsWith('/static/')) {
    event.respondWith(staleWhileRevalidate(request, STATIC_CACHE));
    return;
  }

  if (url.pathname.startsWith('/api/')) {
    if (shouldCacheApi(url)) {
      event.respondWith(networkFirst(request, API_CACHE));
      return;
    }
    event.respondWith(fetch(request).catch(() => caches.match(request)));
    return;
  }

  event.respondWith(fetch(request).catch(() => caches.match(request)));
});
