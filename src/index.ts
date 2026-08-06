interface Env {
  ASSETS: Fetcher;
  BACKEND_URL?: string;
}

const JSON_HEADERS = {
  "content-type": "application/json;charset=UTF-8",
  "cache-control": "no-store, max-age=0",
};

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: JSON_HEADERS });
}

function backendTarget(env: Env, path: string) {
  const base = (env.BACKEND_URL || "").trim().replace(/\/+$/, "");
  if (!base) return null;
  return `${base}${path}`;
}

async function proxyApi(request: Request, env: Env, url: URL) {
  const routeMap: Record<string, string> = {
    "/api/health": "/health",
    "/api/search": "/search",
    "/api/batch": "/batch",
  };
  const targetPath = routeMap[url.pathname];
  if (!targetPath) return json({ error: "API route not found" }, 404);
  const target = backendTarget(env, targetPath);
  if (!target) {
    return json({
      error: "Python backend is not connected yet",
      setup_required: "Add the BACKEND_URL variable to the scanner Worker after deploying the Render service.",
    }, 503);
  }

  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("cookie");
  headers.set("accept", request.headers.get("accept") || "application/json");
  headers.set("x-qikreach-gateway", "cloudflare-worker");

  const controller = new AbortController();
  const timeoutMs = url.pathname === "/api/batch" ? 900_000 : url.pathname === "/api/search" ? 190_000 : 30_000;
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(target, {
      method: request.method,
      headers,
      body: request.method === "GET" || request.method === "HEAD" ? undefined : request.body,
      redirect: "manual",
      signal: controller.signal,
    });
    const outputHeaders = new Headers(response.headers);
    outputHeaders.set("cache-control", "no-store, max-age=0");
    outputHeaders.delete("set-cookie");
    return new Response(response.body, { status: response.status, headers: outputHeaders });
  } catch (error) {
    const message = error instanceof Error ? error.message : "Backend request failed";
    return json({ error: `Python backend unavailable: ${message}` }, 502);
  } finally {
    clearTimeout(timeout);
  }
}

export default {
  async fetch(request: Request, env: Env) {
    try {
      const url = new URL(request.url);
      if (url.pathname.startsWith("/api/")) return proxyApi(request, env, url);
      return env.ASSETS.fetch(request);
    } catch (error) {
      return json({ error: error instanceof Error ? error.message : "Unexpected Worker error" }, 500);
    }
  },
} satisfies ExportedHandler<Env>;
