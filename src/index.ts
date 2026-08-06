import * as XLSX from "xlsx";

interface Env {
  DB: D1Database;
  ASSETS: Fetcher;
  BROWSER?: { quickAction(action: string, input: Record<string, unknown>): Promise<unknown> };
  BRAVE_SEARCH_API_KEY?: string;
}

type Lead = Record<string, unknown>;
type SearchFields = {
  name?: string;
  company?: string;
  phone?: string;
  email?: string;
  ein?: string;
  state?: string;
  zip?: string;
  address?: string;
};
type WebHit = { title: string; url: string; snippet: string; provider: string };

const MAX_UPLOAD = 10 * 1024 * 1024;
const encoder = new TextEncoder();
const clean = (value: unknown) => value == null ? "" : String(value).trim();
const normalize = (value: string) => clean(value).toLowerCase().normalize("NFKD")
  .replace(/[\u0300-\u036f]/g, "").replace(/&/g, " and ")
  .replace(/[^a-z0-9]+/g, " ").trim().replace(/\s+/g, " ");
const compact = (value: string) => normalize(value).replace(/\s/g, "");
const digits = (value: string) => clean(value).replace(/\D/g, "");
const phoneDigits = (value: string) => {
  const result = digits(value);
  return result.length === 11 && result.startsWith("1") ? result.slice(1) : result;
};

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: {
      "content-type": "application/json;charset=UTF-8",
      "cache-control": "no-store, max-age=0",
    },
  });
}

async function sha256(value: string) {
  const hash = await crypto.subtle.digest("SHA-256", encoder.encode(value));
  return [...new Uint8Array(hash)].map(byte => byte.toString(16).padStart(2, "0")).join("");
}

function valueFor(row: Lead, ...names: string[]) {
  const wanted = new Set(names.map(compact));
  const key = Object.keys(row).find(candidate => wanted.has(compact(candidate)));
  return clean(key ? row[key] : "");
}

function normalizePhones(value: string) {
  const seen = new Set<string>();
  return clean(value).split(/[|,;\n]+/).map(item => {
    const number = phoneDigits(item);
    return number.length === 10 ? `+1${number}` : "";
  }).filter(item => item && !seen.has(item) && seen.add(item)).join(" | ");
}

function normalizeEmails(value: string) {
  return [...new Set(clean(value).toLowerCase().match(/[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}/g) || [])].join(" | ");
}

function distance(a: string, b: string) {
  if (a === b) return 0;
  if (!a.length) return b.length;
  if (!b.length) return a.length;
  const previous = Array.from({ length: b.length + 1 }, (_, index) => index);
  for (let i = 1; i <= a.length; i++) {
    let left = i;
    let diagonal = i - 1;
    for (let j = 1; j <= b.length; j++) {
      const up = previous[j];
      const next = a[i - 1] === b[j - 1] ? diagonal : Math.min(diagonal, up, left) + 1;
      diagonal = up;
      previous[j] = next;
      left = next;
    }
  }
  return previous[b.length];
}

function textScore(query: string, value: string) {
  const q = normalize(query);
  const v = normalize(value);
  if (!q || !v) return 0;
  if (q === v || compact(query) === compact(value)) return 100;
  if (v.includes(q) || compact(value).includes(compact(query))) return 90;
  const ratio = 1 - distance(q.slice(0, 96), v.slice(0, 96)) / Math.max(q.length, v.length);
  if (ratio >= 0.88) return 72;
  if (ratio >= 0.78) return 55;
  if (ratio >= 0.68 && Math.min(q.length, v.length) >= 5) return 35;
  return 0;
}

function buildQuery(query: string, fields: SearchFields) {
  return [query, fields.name, fields.company, fields.phone, fields.email, fields.ein, fields.address, fields.state, fields.zip]
    .map(clean).filter(Boolean).join(" ").slice(0, 300);
}

function scoreLead(query: string, row: Lead) {
  let score = 0;
  const queryPhone = phoneDigits(query);
  const queryDigits = digits(query);
  if (queryPhone.length >= 4 && phoneDigits(clean(row.all_phones)).includes(queryPhone)) {
    score = Math.max(score, queryPhone.length >= 10 ? 100 : 82);
  }
  if (queryDigits.length >= 4 && digits(clean(row.ein)).includes(queryDigits)) {
    score = Math.max(score, queryDigits.length >= 9 ? 100 : 82);
  }
  for (const key of ["company_name", "owner_name", "address", "all_emails"]) {
    score = Math.max(score, textScore(query, clean(row[key])));
  }
  return score;
}

function decodeXml(value: string) {
  return value.replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1")
    .replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'");
}

function stripHtml(value: string) {
  return value.replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

async function discover(query: string, env: Env): Promise<WebHit[]> {
  if (env.BRAVE_SEARCH_API_KEY) {
    const response = await fetch(`https://api.search.brave.com/res/v1/web/search?q=${encodeURIComponent(query)}&count=8`, {
      headers: { Accept: "application/json", "X-Subscription-Token": env.BRAVE_SEARCH_API_KEY },
    });
    if (!response.ok) throw new Error(`Brave search failed (${response.status})`);
    const body = await response.json() as { web?: { results?: Array<{ title?: string; url?: string; description?: string }> } };
    return (body.web?.results || []).filter(item => item.url).map(item => ({
      title: clean(item.title), url: clean(item.url), snippet: clean(item.description), provider: "Brave Search",
    }));
  }

  const response = await fetch(`https://www.bing.com/search?format=rss&q=${encodeURIComponent(query)}`, {
    headers: { "user-agent": "Mozilla/5.0 QikReach/1.0" },
  });
  if (!response.ok) throw new Error(`Bing discovery failed (${response.status})`);
  const xml = await response.text();
  const hits: WebHit[] = [];
  for (const match of xml.matchAll(/<item>([\s\S]*?)<\/item>/gi)) {
    const item = match[1];
    const get = (tag: string) => decodeXml((item.match(new RegExp(`<${tag}>([\\s\\S]*?)<\\/${tag}>`, "i")) || [])[1] || "");
    const url = get("link");
    if (url) hits.push({ title: stripHtml(get("title")), url, snippet: stripHtml(get("description")), provider: "Bing RSS" });
  }
  return hits.slice(0, 8);
}

async function pageText(url: string, env: Env) {
  if (env.BROWSER) {
    try {
      const output = await env.BROWSER.quickAction("markdown", { url });
      if (typeof output === "string") return { text: output.slice(0, 200000), mode: "Browser Run" };
      if (output instanceof Response) return { text: (await output.text()).slice(0, 200000), mode: "Browser Run" };
      const record = output as { result?: unknown; markdown?: unknown };
      const text = clean(record.result || record.markdown || JSON.stringify(output));
      if (text) return { text: text.slice(0, 200000), mode: "Browser Run" };
    } catch { /* static fallback below */ }
  }
  const response = await fetch(url, { redirect: "follow", headers: { "user-agent": "Mozilla/5.0 QikReach/1.0" } });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return { text: stripHtml((await response.text()).slice(0, 500000)), mode: "Static fetch" };
}

function extractPublic(text: string) {
  const emails = [...new Set((text.toLowerCase().match(/[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}/g) || [])
    .filter(item => !item.endsWith("@example.com")))].slice(0, 10);
  const matches = text.match(/(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}/g) || [];
  const phones = [...new Set(matches.map(phoneDigits).filter(item => item.length === 10).map(item => `+1${item}`))].slice(0, 10);
  return { emails, phones };
}

async function saveFinding(hit: WebHit, emails: string[], phones: string[], env: Env) {
  if (!emails.length && !phones.length) return false;
  const hash = await sha256(`${hit.url}|${emails.join("|")}|${phones.join("|")}`.toLowerCase());
  await env.DB.prepare("INSERT INTO master_leads (lead_hash,company_name,owner_name,revenue,address,dob,ssn,ein,start_date,all_phones,all_emails,sources,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(lead_hash) DO UPDATE SET all_phones=excluded.all_phones,all_emails=excluded.all_emails,sources=excluded.sources,updated_at=CURRENT_TIMESTAMP")
    .bind(hash, hit.title, "", "", hit.url, "", "", "", "", phones.join(" | "), emails.join(" | "), `${hit.provider}: ${hit.url}`).run();
  return true;
}

async function api(request: Request, env: Env, url: URL) {
  if (url.pathname === "/api/health") {
    try {
      const count = await env.DB.prepare("SELECT COUNT(*) AS count FROM master_leads").first<{ count: number }>();
      return json({ ok: true, public_access: true, vault_count: Number(count?.count || 0), browser_run: Boolean(env.BROWSER), brave_configured: Boolean(env.BRAVE_SEARCH_API_KEY) });
    } catch (error) {
      return json({ ok: false, error: error instanceof Error ? error.message : "D1 unavailable" }, 500);
    }
  }

  if (url.pathname === "/api/search" && request.method === "POST") {
    const started = Date.now();
    const body = await request.json() as { query?: string; fields?: SearchFields };
    const query = buildQuery(clean(body.query), body.fields || {});
    if (!query) return json({ error: "Enter something to search" }, 400);

    const stages: Array<Record<string, unknown>> = [];
    const local = await env.DB.prepare("SELECT * FROM master_leads ORDER BY updated_at DESC LIMIT 3000").all<Lead>();
    const ranked = (local.results || []).map(row => ({ ...row, match_score: scoreLead(query, row) }))
      .filter(row => Number(row.match_score) >= 34)
      .sort((a, b) => Number(b.match_score) - Number(a.match_score)).slice(0, 100);
    stages.push({ stage: "Vault search", status: "complete", matches: ranked.length, total_records: (local.results || []).length });

    let hits: WebHit[] = [];
    try {
      hits = await discover(query, env);
      stages.push({ stage: "Public web discovery", status: "complete", provider: hits[0]?.provider || "none", results: hits.length });
    } catch (error) {
      stages.push({ stage: "Public web discovery", status: "error", message: error instanceof Error ? error.message : "Discovery failed" });
    }

    const sources: Array<Record<string, unknown>> = [];
    let saved = 0;
    for (const hit of hits.slice(0, 5)) {
      try {
        const page = await pageText(hit.url, env);
        const found = extractPublic(`${hit.title}\n${hit.snippet}\n${page.text}`);
        if (await saveFinding(hit, found.emails, found.phones, env)) saved++;
        sources.push({ ...hit, fetch_mode: page.mode, emails: found.emails, phones: found.phones, status: "checked" });
      } catch (error) {
        sources.push({ ...hit, emails: [], phones: [], status: "blocked_or_failed", error: error instanceof Error ? error.message : "Fetch failed" });
      }
    }
    stages.push({ stage: "Page enrichment", status: "complete", checked: sources.length, saved });
    return json({ ok: true, query, results: ranked, sources, stages, elapsed_ms: Date.now() - started, vault_count: (local.results || []).length });
  }

  if (url.pathname === "/api/batch" && request.method === "POST") {
    const file = (await request.formData()).get("file");
    if (!(file instanceof File) || !file.name.toLowerCase().endsWith(".xlsx")) return json({ error: "Select an .xlsx file" }, 400);
    if (file.size > MAX_UPLOAD) return json({ error: "Maximum upload is 10 MiB" }, 413);
    const workbook = XLSX.read(await file.arrayBuffer(), { type: "array" });
    const rows = XLSX.utils.sheet_to_json<Lead>(workbook.Sheets[workbook.SheetNames[0]], { defval: "" });
    if (rows.length > 1000) return json({ error: "Maximum batch is 1,000 rows" }, 413);

    const output = [];
    for (const row of rows) {
      const company = valueFor(row, "company", "company name", "business name");
      const owner = valueFor(row, "owner", "owner name", "contact", "contact name", "name");
      const phones = normalizePhones(valueFor(row, "phone", "phones", "phone number", "mobile"));
      const emails = normalizeEmails(valueFor(row, "email", "emails", "email address"));
      const ein = valueFor(row, "ein");
      const values = [await sha256([company, owner, phones, emails, ein].join("|").toLowerCase() || crypto.randomUUID()), company, owner,
        valueFor(row, "revenue", "monthly revenue", "annual revenue"), valueFor(row, "address", "business address"),
        valueFor(row, "dob", "date of birth"), valueFor(row, "ssn"), ein,
        valueFor(row, "start date", "business start date"), phones, emails, "uploaded spreadsheet"];
      await env.DB.prepare("INSERT INTO master_leads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(lead_hash) DO UPDATE SET company_name=excluded.company_name,owner_name=excluded.owner_name,revenue=excluded.revenue,address=excluded.address,dob=excluded.dob,ssn=excluded.ssn,ein=excluded.ein,start_date=excluded.start_date,all_phones=excluded.all_phones,all_emails=excluded.all_emails,sources=excluded.sources,updated_at=CURRENT_TIMESTAMP")
        .bind(...values).run();
      output.push({ ...row, "Normalized Phones": phones, "Validated Emails": emails, "QikReach Sources": "uploaded spreadsheet" });
    }
    const result = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(result, XLSX.utils.json_to_sheet(output), "Enriched Leads");
    return new Response(XLSX.write(result, { bookType: "xlsx", type: "array" }), {
      headers: {
        "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "content-disposition": `attachment; filename="qikreach-enriched-${Date.now()}.xlsx"`,
      },
    });
  }
  return json({ error: "Not found" }, 404);
}

export default {
  async fetch(request: Request, env: Env) {
    try {
      const url = new URL(request.url);
      if (url.pathname.startsWith("/api/")) return api(request, env, url);
      const response = await env.ASSETS.fetch(request);
      const headers = new Headers(response.headers);
      headers.set("cache-control", "no-store, max-age=0");
      return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
    } catch (error) {
      return json({ error: error instanceof Error ? error.message : "Unexpected worker error" }, 500);
    }
  },
} satisfies ExportedHandler<Env>;
