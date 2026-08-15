interface Env { ASSETS: Fetcher; BACKEND_URL?: string; }

const JSON_HEADERS = {"content-type":"application/json;charset=UTF-8","cache-control":"no-store,max-age=0"};

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {status, headers: JSON_HEADERS});
}

function targetPath(pathname: string): string | null {
  const fixed: Record<string, string> = {
    "/api/health": "/health",
    "/api/search": "/search",
    "/api/search/stream": "/search/stream",
    "/api/batch/start": "/batch/start",
  };
  if (fixed[pathname]) return fixed[pathname];
  if (pathname.startsWith("/api/batch/status/")) return pathname.replace("/api", "");
  if (pathname.startsWith("/api/batch/control/")) return pathname.replace("/api", "");
  if (pathname.startsWith("/api/batch/download/")) return pathname.replace("/api", "");
  return null;
}

async function proxy(request: Request, env: Env, url: URL) {
  const path = targetPath(url.pathname);
  if (!path) return json({error:"API route not found"}, 404);
  const base = (env.BACKEND_URL || "").trim().replace(/\/+$/, "");
  if (!base) return json({error:"Backend URL is not configured"}, 503);
  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("cookie");
  const controller = new AbortController();
  const timeoutMs = path.startsWith("/batch/") ? 60_000 : 190_000;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${base}${path}`, {
      method: request.method,
      headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
      redirect: "manual",
      signal: controller.signal,
    });
    const outputHeaders = new Headers(response.headers);
    outputHeaders.set("cache-control", "no-store,max-age=0");
    outputHeaders.delete("set-cookie");
    return new Response(response.body, {status: response.status, headers: outputHeaders});
  } catch (error) {
    return json({error: error instanceof Error ? error.message : "Backend request failed"}, 502);
  } finally {
    clearTimeout(timer);
  }
}

export default {
  async fetch(request: Request, env: Env) {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/api/")) return proxy(request, env, url);
    return env.ASSETS.fetch(request);
  },
} satisfies ExportedHandler<Env>;
