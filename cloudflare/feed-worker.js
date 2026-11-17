// Cloudflare Worker: serves client XML feeds from the PRIVATE data repository.
//
//   https://<worker>.workers.dev/feeds/<code>.xml   ->  storewatch-data: feeds/<code>.xml
//
// <code> is a random 32-character code per client (niche/clients.sh makes it), so a link
// cannot be guessed, and the files are never in a public repository.
// Settings (Worker → Settings → Variables and Secrets):
//   GITHUB_TOKEN  (Secret)  fine-grained token, Contents: Read-only, ONLY on storewatch-data
//   DATA_REPO     (Text, optional)  default "arvened/storewatch-data"
// Copy this whole file into the Worker editor and press Deploy.

const CODE = /^\/feeds\/([a-f0-9]{24,64})\.xml$/;
const CACHE_SECONDS = 3600; // the feed changes once a day; integrators may poll often

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method not allowed", { status: 405 });
    }
    const m = url.pathname.match(CODE);
    if (!m) return new Response("Not found", { status: 404 });
    if (!env.GITHUB_TOKEN) return new Response("Feed service not configured", { status: 503 });

    const cache = caches.default;
    const key = new Request(url.origin + url.pathname, { method: "GET" });
    const hit = await cache.match(key);
    if (hit) return request.method === "HEAD" ? new Response(null, hit) : hit;

    const repo = env.DATA_REPO || "arvened/storewatch-data";
    const gh = await fetch(`https://api.github.com/repos/${repo}/contents/feeds/${m[1]}.xml`, {
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github.raw+json",
        "User-Agent": "eupriceindex-feeds",
        "X-GitHub-Api-Version": "2022-11-28",
      },
    });
    if (gh.status === 404) return new Response("Not found", { status: 404 });
    if (!gh.ok) return new Response("Feed temporarily unavailable", { status: 502 });

    const res = new Response(await gh.text(), {
      headers: {
        "Content-Type": "application/xml; charset=utf-8",
        "Cache-Control": `public, max-age=${CACHE_SECONDS}`,
        "X-Robots-Tag": "noindex",
      },
    });
    ctx.waitUntil(cache.put(key, res.clone()));
    return request.method === "HEAD" ? new Response(null, res) : res;
  },
};
