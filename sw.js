/* 卡码简历 - Service Worker 缓存 */
const CACHE_NAME = 'kama-resume-v2';

// 需要缓存的静态资源
const PRECACHE_URLS = [
    '/',
    '/index.html',
    '/resume-editor.html',
    '/about.html',
    '/styles.css',
    '/logo.ico',
    '/wechat_public.bmp',
    '/wechat_qr.png',
];

// 安装时预缓存
self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => {
            return cache.addAll(PRECACHE_URLS);
        })
    );
    self.skipWaiting();
});

// 激活时清理旧缓存
self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(keys => {
            return Promise.all(
                keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
            );
        })
    );
    self.clients.claim();
});

// 拦截请求：优先从缓存读取，同时更新缓存
// 只处理 GET 请求 — Cache API 不支持 POST/PUT/DELETE 等方法
self.addEventListener('fetch', event => {
    if (event.request.method !== 'GET') {
        return;  // 不拦截非 GET 请求，让浏览器直接发送
    }
    event.respondWith(
        caches.match(event.request).then(cached => {
            const fetchPromise = fetch(event.request).then(response => {
                if (response && response.ok && response.type === 'basic') {
                    const clone = response.clone();
                    caches.open(CACHE_NAME).then(cache => {
                        cache.put(event.request, clone);
                    });
                }
                return response;
            }).catch(() => cached);

            return cached || fetchPromise;
        })
    );
});
