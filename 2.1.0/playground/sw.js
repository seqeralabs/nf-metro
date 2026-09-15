"use strict";

// Keep in sync with PYODIDE_VERSION in worker.js. Bumping this constant
// changes the cache name, which causes the activate handler to evict all
// assets from the previous version.
const PYODIDE_VERSION = "v0.27.2";
const PYODIDE_BASE = `https://cdn.jsdelivr.net/pyodide/${PYODIDE_VERSION}/full/`;
const CACHE_NAME = `nfm-playground-${PYODIDE_VERSION}`;

// The deploy stamps a content hash into each dev wheel's build tag, so a
// wheel URL names one immutable build and is safe to cache indefinitely.
const WHEEL_URL = /\/wheels\/[^/]+\.whl$/;

function shouldCache(url) {
  // wheels/index.json is fetched with cache: "no-store" by app.js for wheel
  // discovery, so it is excluded by WHEEL_URL requiring the .whl suffix.
  if (url.startsWith(PYODIDE_BASE)) return true;
  return WHEEL_URL.test(url);
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
        await cache.put(url, response.clone());
        // A fresh wheel hash supersedes earlier builds; drop their entries so
        // the cache holds only the current wheel rather than one per deploy.
        if (WHEEL_URL.test(url)) {
          for (const req of await cache.keys()) {
            if (req.url !== url && WHEEL_URL.test(req.url)) {
              await cache.delete(req);
            }
          }
        }
      }
      return response;
    })(),
  );
});
