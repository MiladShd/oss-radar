// Reverse proxy: serves the Cloud Run dashboard under radar.miladblog.com.
//
// Data refreshes once a day (pipeline runs 09:30 UTC), so read-only responses are cached at
// Cloudflare's edge. Visitors get a response from the nearest edge instead of waking Cloud Run,
// which also removes cold starts from the common path.
const EDGE_TTL = 300; // seconds an edge location serves a cached copy
const BROWSER_TTL = 60; // seconds a visitor's browser reuses it

// Never cached: liveness/health must be fresh, and audit runs per-request computation.
const NO_CACHE = new Set(["/health", "/api/system-health", "/api/audit"]);

// Build the upstream URL from the configured origin and ONLY the request's path and query.
// `new URL(path, origin)` is unsafe here: a path like "//evil.example/x" is parsed as an authority and
// silently replaces the host (an open proxy). Assigning pathname/search onto the parsed origin cannot
// change the host, and the final origin check makes that a hard invariant.
function upstreamUrl(requestUrl, origin) {
  const base = new URL(origin);
  const incoming = new URL(requestUrl);
  const target = new URL(base.origin);
  // A leading run of slashes or backslashes is never a legitimate dashboard path; collapse it.
  target.pathname = incoming.pathname.replace(/^[/\\]+/, "/");
  target.search = incoming.search;
  if (target.origin !== base.origin) return null;
  return target;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const isAudit = request.method === "POST" && url.pathname === "/api/audit";
    const isRead = request.method === "GET" || request.method === "HEAD";
    if (!isRead && !isAudit) {
      return new Response("Method not allowed", { status: 405 });
    }

    const target = upstreamUrl(request.url, env.ORIGIN);
    if (!target) return new Response("Bad request", { status: 400 });

    const cacheable = isRead && !NO_CACHE.has(target.pathname);
    const init = {
      method: request.method,
      headers: {
        accept: request.headers.get("accept") ?? "*/*",
        "content-type": request.headers.get("content-type") ?? "",
      },
      body: isAudit ? request.body : undefined,
      redirect: "manual", // never follow an upstream redirect off the configured origin
      cf: cacheable
        ? { cacheEverything: true, cacheTtlByStatus: { "200-299": EDGE_TTL, "404": 30, "500-599": 0 } }
        : { cacheEverything: false },
    };

    const upstream = await fetch(target, init);
    const headers = new Headers(upstream.headers);
    headers.set("x-served-by", "oss-radar-proxy");
    if (cacheable && upstream.ok) {
      headers.set("cache-control", `public, max-age=${BROWSER_TTL}`);
    }
    return new Response(upstream.body, { status: upstream.status, headers });
  },
};
