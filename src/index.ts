import * as XLSX from "xlsx";

interface Env {
  DB: D1Database;
  ASSETS: Fetcher;
  QIKREACH_USERNAME: string;
  QIKREACH_PASSWORD: string;
  QIKREACH_SESSION_SECRET: string;
}

type LeadRow = Record<string, unknown>;
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

const E = new TextEncoder();
const MAX = 10 * 1024 * 1024;
const STATES: Record<string, string> = {
  alabama:"al",alaska:"ak",arizona:"az",arkansas:"ar",california:"ca",colorado:"co",connecticut:"ct",delaware:"de",florida:"fl",georgia:"ga",hawaii:"hi",idaho:"id",illinois:"il",indiana:"in",iowa:"ia",kansas:"ks",kentucky:"ky",louisiana:"la",maine:"me",maryland:"md",massachusetts:"ma",michigan:"mi",minnesota:"mn",mississippi:"ms",missouri:"mo",montana:"mt",nebraska:"ne",nevada:"nv","new hampshire":"nh","new jersey":"nj","new mexico":"nm","new york":"ny","north carolina":"nc","north dakota":"nd",ohio:"oh",oklahoma:"ok",oregon:"or",pennsylvania:"pa","rhode island":"ri","south carolina":"sc","south dakota":"sd",tennessee:"tn",texas:"tx",utah:"ut",vermont:"vt",virginia:"va",washington:"wa","west virginia":"wv",wisconsin:"wi",wyoming:"wy","district of columbia":"dc"
};
const STATE_CODES = new Set(Object.values(STATES));

const J = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
  status,
  headers: { "content-type": "application/json;charset=UTF-8", "cache-control": "no-store" },
});
const C = (value: unknown) => value == null ? "" : String(value).trim();
const normalizeText = (value: string) => C(value).toLowerCase().normalize("NFKD")
  .replace(/[\u0300-\u036f]/g, "").replace(/&/g, " and ")
  .replace(/[^a-z0-9]+/g, " ").trim().replace(/\s+/g, " ");
const compact = (value: string) => normalizeText(value).replace(/\s/g, "");
const onlyDigits = (value: string) => C(value).replace(/[^0-9]/g, "");
const phoneDigits = (value: string) => {
  const digits = onlyDigits(value);
  return digits.length === 11 && digits.startsWith("1") ? digits.slice(1) : digits;
};
const normalizeEmail = (value: string) => C(value).toLowerCase().replace(/\s+/g, "").replace(/[;,]+$/, "");

async function mac(secret: string, value: string) {
  const key = await crypto.subtle.importKey("raw", E.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return [...new Uint8Array(await crypto.subtle.sign("HMAC", key, E.encode(value)))]
    .map(x => x.toString(16).padStart(2, "0")).join("");
}
function cookie(request: Request, name: string) {
  for (const part of (request.headers.get("cookie") || "").split(";")) {
    const [key, ...value] = part.trim().split("=");
    if (key === name) return decodeURIComponent(value.join("="));
  }
  return "";
}
async function auth(request: Request, env: Env) {
  const parts = cookie(request, "qikreach_session").split("|");
  return parts.length === 3 && parts[0] === env.QIKREACH_USERNAME
    && Number(parts[1]) > Date.now() / 1000
    && parts[2] === await mac(env.QIKREACH_SESSION_SECRET, `${parts[0]}|${parts[1]}`);
}

function valueFor(row: LeadRow, ...names: string[]) {
  const wanted = new Set(names.map(compact));
  const key = Object.keys(row).find(candidate => wanted.has(compact(candidate)));
  return C(key ? row[key] : "");
}
function normalizePhones(value: string) {
  const seen = new Set<string>();
  return C(value).split(/[|,;\n]+/).map(item => {
    const digits = phoneDigits(item);
    return digits.length === 10 ? `+1${digits}` : "";
  }).filter(item => item && !seen.has(item) && seen.add(item)).join(" | ");
}
function normalizeEmails(value: string) {
  return [...new Set(C(value).toLowerCase().match(/[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}/g) || [])].join(" | ");
}
function distance(a: string, b: string) {
  if (a === b) return 0;
  if (!a.length) return b.length;
  if (!b.length) return a.length;
  const previous = Array.from({ length: b.length + 1 }, (_, index) => index);
  for (let i = 1; i <= a.length; i++) {
    let left = i, diagonal = i - 1;
    for (let j = 1; j <= b.length; j++) {
      const up = previous[j];
      const next = a[i - 1] === b[j - 1] ? diagonal : Math.min(diagonal, up, left) + 1;
      diagonal = up; previous[j] = next; left = next;
    }
  }
  return previous[b.length];
}
function similarity(a: string, b: string) {
  if (!a || !b) return 0;
  return 1 - distance(a.slice(0, 96), b.slice(0, 96)) / Math.max(a.length, b.length);
}
function textScore(query: string, value: string) {
  const q = normalizeText(query), v = normalizeText(value), qc = compact(query), vc = compact(value);
  if (!q || !v) return 0;
  if (q === v || qc === vc) return 100;
  if (v.includes(q) || vc.includes(qc)) return 90;
  if (q.includes(v) && v.length >= 4) return 75;
  const queryTokens = q.split(" ").filter(Boolean), valueTokens = v.split(" ").filter(Boolean);
  let tokenScore = 0;
  for (const queryToken of queryTokens) {
    const best = Math.max(...valueTokens.map(valueToken => {
      if (valueToken === queryToken) return 1;
      if (valueToken.includes(queryToken) || queryToken.includes(valueToken)) return .9;
      return similarity(queryToken, valueToken);
    }), 0);
    if (best >= .72) tokenScore += best;
  }
  if (queryTokens.length && tokenScore) {
    const ratio = tokenScore / queryTokens.length;
    if (ratio >= .95) return 82;
    if (ratio >= .8) return 68;
    if (ratio >= .65) return 45;
  }
  const ratio = similarity(q, v);
  if (ratio >= .88) return 72;
  if (ratio >= .78) return 55;
  if (ratio >= .68 && Math.min(q.length, v.length) >= 5) return 35;
  return 0;
}
function emailScore(query: string, stored: string) {
  const q = normalizeEmail(query);
  if (!q.includes("@")) return 0;
  const emails = C(stored).toLowerCase().split(/[|,;\s]+/).filter(item => item.includes("@"));
  let best = 0;
  for (const candidate of emails) {
    if (candidate === q) return 100;
    const [qLocal = "", qDomain = ""] = q.split("@");
    const [cLocal = "", cDomain = ""] = candidate.split("@");
    if (!qLocal || !qDomain || !cLocal || !cDomain) continue;
    const local = similarity(qLocal, cLocal), domain = similarity(qDomain, cDomain);
    if (qLocal === cLocal && domain >= .72) best = Math.max(best, 88);
    else if (qDomain === cDomain && local >= .72) best = Math.max(best, 84);
    else if (local >= .8 && domain >= .8) best = Math.max(best, 76);
  }
  return best;
}
function stateMatch(value: string, state: string) {
  const address = normalizeText(value), requested = normalizeText(state);
  const code = STATES[requested] || (STATE_CODES.has(requested) ? requested : "");
  if (!requested) return true;
  if (address.includes(requested)) return true;
  if (code && new RegExp(`(^|\\s)${code}(\\s|$)`).test(address)) return true;
  return textScore(requested, address) >= 68;
}
function parseNaturalQuery(query: string) {
  let text = normalizeText(query);
  let state = "", zip = "";
  const zipMatch = text.match(/\b\d{5}(?:\d{4})?\b/);
  if (zipMatch) { zip = zipMatch[0].slice(0, 5); text = text.replace(zipMatch[0], " "); }
  const names = Object.keys(STATES).sort((a, b) => b.length - a.length);
  for (const name of names) {
    const pattern = new RegExp(`(?:\\bin\\s+|\\bfrom\\s+|\\b)${name}\\b`);
    if (pattern.test(text)) { state = name; text = text.replace(pattern, " "); break; }
  }
  if (!state) {
    const tokens = text.split(" ");
    const last = tokens[tokens.length - 1];
    if (STATE_CODES.has(last)) { state = last; tokens.pop(); text = tokens.join(" "); }
  }
  text = text.replace(/\b(in|from|near|located|at)\b/g, " ").replace(/\s+/g, " ").trim();
  return { text, state, zip };
}
function fieldPass(value: string, query: string, threshold = 55) {
  return !C(query) || textScore(query, value) >= threshold;
}
function scoreLead(query: string, fields: SearchFields, row: LeadRow) {
  const natural = parseNaturalQuery(query);
  const address = C(row.address), company = C(row.company_name), owner = C(row.owner_name);
  const phone = C(row.all_phones), email = C(row.all_emails), ein = C(row.ein);
  const requestedState = C(fields.state) || natural.state;
  const requestedZip = onlyDigits(C(fields.zip) || natural.zip).slice(0, 5);

  if (requestedState && !stateMatch(address, requestedState)) return 0;
  if (requestedZip && !onlyDigits(address).includes(requestedZip)) return 0;
  if (!fieldPass(owner, C(fields.name), 45)) return 0;
  if (!fieldPass(company, C(fields.company), 45)) return 0;
  if (!fieldPass(address, C(fields.address), 45)) return 0;
  if (C(fields.phone) && !phoneDigits(phone).includes(phoneDigits(C(fields.phone)))) return 0;
  if (C(fields.ein) && !onlyDigits(ein).includes(onlyDigits(C(fields.ein)))) return 0;
  if (C(fields.email) && emailScore(C(fields.email), email) < 72) return 0;

  let score = 0;
  const main = natural.text;
  const queryDigits = phoneDigits(main);
  if (queryDigits.length >= 4) {
    if (phoneDigits(phone).includes(queryDigits)) score = Math.max(score, queryDigits.length >= 10 ? 100 : 82);
    if (onlyDigits(ein).includes(onlyDigits(main))) score = Math.max(score, onlyDigits(main).length >= 9 ? 100 : 82);
  }
  score = Math.max(score, emailScore(main, email), textScore(main, owner), textScore(main, company), textScore(main, address), textScore(main, ein));
  if (!main && Object.values(fields).some(value => C(value))) score = 70;
  if (requestedState) score += 12;
  if (requestedZip) score += 15;
  if (C(fields.name)) score += 10;
  if (C(fields.company)) score += 10;
  return Math.min(score, 120);
}
function identifyQuery(query: string, fields: SearchFields) {
  const parsed = parseNaturalQuery(query), raw = C(query), digits = phoneDigits(raw);
  let type = "name/company/text";
  if (raw.includes("@")) type = "email";
  else if (digits.length === 10) type = "phone";
  else if (digits.length === 9 && onlyDigits(raw).length >= 9) type = "EIN";
  return { type, normalized: parsed.text || normalizeText(raw), state: C(fields.state) || parsed.state, zip: C(fields.zip) || parsed.zip };
}
async function sha(value: string) {
  return [...new Uint8Array(await crypto.subtle.digest("SHA-256", E.encode(value)))]
    .map(x => x.toString(16).padStart(2, "0")).join("");
}

async function api(request: Request, env: Env, url: URL) {
  if (!await auth(request, env)) return J({ error: "Authentication required" }, 401);
  if (url.pathname === "/api/health") return J({ ok: true });
  if (url.pathname === "/api/search" && request.method === "POST") {
    try {
      const body = await request.json() as { query?: string; fields?: SearchFields };
      const raw = C(body.query).slice(0, 200), fields = body.fields || {};
      if (!raw && !Object.values(fields).some(value => C(value))) return J({ error: "Enter a search or at least one advanced field" }, 400);
      const result = await env.DB.prepare("SELECT * FROM master_leads ORDER BY updated_at DESC LIMIT 2000").all();
      const rows = (result.results || []) as LeadRow[];
      const ranked = rows.map(row => ({ row, score: scoreLead(raw, fields, row) }))
        .filter(item => item.score >= 34).sort((a, b) => b.score - a.score).slice(0, 100);
      const interpreted = identifyQuery(raw, fields);
      return J({ query: raw, interpreted_type: interpreted.type, normalized_query: interpreted.normalized,
        interpreted_state: interpreted.state, interpreted_zip: interpreted.zip,
        results: ranked.map(item => ({ ...item.row, match_score: item.score })) });
    } catch (error) {
      const message = error instanceof Error ? error.message : "Search failed";
      return J({ error: `Search could not be completed: ${message}` }, 500);
    }
  }
  if (url.pathname === "/api/batch" && request.method === "POST") {
    const file = (await request.formData()).get("file");
    if (!(file instanceof File) || !file.name.toLowerCase().endsWith(".xlsx")) return J({ error: "Select an .xlsx file" }, 400);
    if (file.size > MAX) return J({ error: "Maximum upload is 10 MiB" }, 413);
    const workbook = XLSX.read(await file.arrayBuffer(), { type: "array" });
    const rows = XLSX.utils.sheet_to_json<LeadRow>(workbook.Sheets[workbook.SheetNames[0]], { defval: "" });
    if (rows.length > 1000) return J({ error: "Maximum batch is 1,000 rows" }, 413);
    const output = [];
    for (const row of rows) {
      const company = valueFor(row, "company", "company name", "business name");
      const owner = valueFor(row, "owner", "owner name", "contact", "contact name", "name");
      const phones = normalizePhones(valueFor(row, "phone", "phones", "phone number", "mobile"));
      const emails = normalizeEmails(valueFor(row, "email", "emails", "email address"));
      const ein = valueFor(row, "ein");
      const lead = [await sha([company, owner, phones, emails, ein].join("|").toLowerCase() || crypto.randomUUID()), company, owner,
        valueFor(row, "revenue", "monthly revenue", "annual revenue"), valueFor(row, "address", "business address"),
        valueFor(row, "dob", "date of birth"), valueFor(row, "ssn"), ein,
        valueFor(row, "start date", "business start date"), phones, emails, "uploaded spreadsheet"];
      await env.DB.prepare("INSERT INTO master_leads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(lead_hash) DO UPDATE SET company_name=excluded.company_name,owner_name=excluded.owner_name,revenue=excluded.revenue,address=excluded.address,dob=excluded.dob,ssn=excluded.ssn,ein=excluded.ein,start_date=excluded.start_date,all_phones=excluded.all_phones,all_emails=excluded.all_emails,sources=excluded.sources,updated_at=CURRENT_TIMESTAMP").bind(...lead).run();
      output.push({ ...row, "Normalized Phones": phones, "Validated Emails": emails, "QikReach Sources": "uploaded spreadsheet" });
    }
    const resultWorkbook = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(resultWorkbook, XLSX.utils.json_to_sheet(output), "Enriched Leads");
    return new Response(XLSX.write(resultWorkbook, { bookType: "xlsx", type: "array" }), {
      headers: { "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "content-disposition": `attachment; filename="qikreach-enriched-${Date.now()}.xlsx"` },
    });
  }
  return J({ error: "Not found" }, 404);
}

export default {
  async fetch(request: Request, env: Env) {
    try {
      const url = new URL(request.url);
      if (!env.QIKREACH_PASSWORD || !env.QIKREACH_SESSION_SECRET) return new Response("Missing required secrets", { status: 500 });
      if (url.pathname === "/login" && request.method === "POST") {
        const form = await request.formData();
        if (C(form.get("username")) !== env.QIKREACH_USERNAME || C(form.get("password")) !== env.QIKREACH_PASSWORD) return Response.redirect(new URL("/?error=1", url), 303);
        const expires = Math.floor(Date.now() / 1000) + 43200, body = `${env.QIKREACH_USERNAME}|${expires}`;
        const token = `${body}|${await mac(env.QIKREACH_SESSION_SECRET, body)}`;
        return new Response(null, { status: 303, headers: { location: "/", "set-cookie": `qikreach_session=${encodeURIComponent(token)}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=43200` } });
      }
      if (url.pathname === "/logout") return new Response(null, { status: 303, headers: { location: "/", "set-cookie": "qikreach_session=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0" } });
      if (url.pathname.startsWith("/api/")) return api(request, env, url);
      if (!await auth(request, env)) return env.ASSETS.fetch(new Request(new URL("/login.html", url).toString(), request));
      return env.ASSETS.fetch(request);
    } catch (error) {
      return J({ error: error instanceof Error ? error.message : "Unexpected worker error" }, 500);
    }
  },
} satisfies ExportedHandler<Env>;
