"use strict";

// Keep in sync with PYODIDE_VERSION in app.js. Bumping this constant
// changes the cache name, which causes the activate handler to evict all
// assets from the previous version.
const PYODIDE_VERSION = "v0.27.2";
const PYODIDE_BASE = `https://cdn.jsdelivr.net/pyodide/${PYODIDE_VERSION}/full/`;
const CACHE_NAME = `nfm-playground-${PYODIDE_VERSION}`;

function shouldCache(url) {
  // wheels/index.json is fetched with cache: "no-store" by app.js for wheel
  // discovery, so exclude it; .whl files are content-addressed and safe to cache.
  if (url.startsWith(PYODIDE_BASE)) return true;
  return /\/wheels\/[^/]+\.whl$/.test(url);
}

self.addEventListener("install", (event) => {
  self.skipWaiting();
  // WASM and other large assets are lazily cached on first fetch once the SW
  // controls the page; only the entry script is precached here.
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE_NAME);
      try {
        await cache.add(`${PYODIDE_BASE}pyodide.js`);
      } catch (_) {
        // Don't fail install when the CDN is unreachable.
      }
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys
          .filter((k) => k.startsWith("nfm-playground-") && k !== CACHE_NAME)
          .map((k) => caches.delete(k)),
      );
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("fetch", (event) => {
  const url = event.request.url;
  if (!shouldCache(url)) return;

  event.respondWith(
    (async () => {
      const cache = await caches.open(CACHE_NAME);
      const cached = await cache.match(url);
      if (cached) return cached;
      const response = await fetch(event.request);
      if (response.ok) {
        cache.put(url, response.clone());
      }
      return response;
    })(),
  );
});
