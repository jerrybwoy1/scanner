from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
import tempfile
import time
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd
import phonenumbers
from duckduckgo_search import DDGS
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

MAX_URLS = 14
MAX_UPLOAD = 10 * 1024 * 1024
TARGET_SITES = (
    "facebook.com", "linkedin.com/in", "truepeoplesearch.com", "bizapedia.com",
    "opencorporates.com", "bbb.org", "chamberofcommerce.com", "manta.com",
)
EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,24}(?![\w.-])")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.()/-]*)?(?:\(?[2-9]\d{2}\)?[\s.()/-]*)[2-9]\d{2}[\s.()/-]*\d{4}(?:\s*(?:x|ext\.?|extension)\s*\d{1,6})?(?!\d)", re.I)
APP_ORIGIN = os.getenv("APP_ORIGIN", "https://scanner.jerrylang.workers.dev").rstrip("/")

app = FastAPI(title="QikReach Live Enrichment API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=[APP_ORIGIN], allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["content-type", "accept"])


class Fields(BaseModel):
    company: str = ""
    person: str = ""
    phone: str = ""
    email: str = ""
    ein: str = ""
    state: str = ""
    address: str = ""


class SearchRequest(BaseModel):
    query: str = Field(default="", max_length=300)
    fields: Fields = Field(default_factory=Fields)
    proxy: str = Field(default="", max_length=1000)
    verify_email_domains: bool = True
    use_ollama: bool = False


class VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg", "canvas", "template"}:
            self.skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg", "canvas", "template"} and self.skip:
            self.skip -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip:
            value = re.sub(r"\s+", " ", unescape(data)).strip()
            if value:
                self.parts.append(value)


def clean(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    return "" if text.lower() in {"nan", "nat", "<na>"} else text


def validate_proxy(proxy: str) -> str:
    proxy = clean(proxy)
    if not proxy:
        return ""
    parsed = urlparse(proxy)
    if parsed.scheme.lower() not in {"http", "https", "socks4", "socks5"} or not parsed.hostname:
        raise ValueError("Proxy must use http://, https://, socks4://, or socks5://")
    return proxy


def is_public_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return False
        host = parsed.hostname.lower().rstrip(".")
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal", ".localhost")):
            return False
        try:
            address = ipaddress.ip_address(host)
            return not (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast)
        except ValueError:
            records = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            for record in records:
                address = ipaddress.ip_address(record[4][0])
                if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast:
                    return False
            return True
    except Exception:
        return False


def visible_text(html: str) -> str:
    parser = VisibleText()
    try:
        parser.feed(html)
        parser.close()
        return "\n".join(parser.parts)[:30000]
    except Exception:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()[:30000]


def normalize_phone(raw: str) -> dict[str, str] | None:
    try:
        parsed = phonenumbers.parse(raw, "US")
        if not phonenumbers.is_valid_number(parsed):
            return None
        extension = parsed.extension or ""
        return {"number": phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164), "extension": extension}
    except Exception:
        return None


def extract_contacts(text: str) -> tuple[list[dict[str, str]], list[str]]:
    deobfuscated = re.sub(r"(?i)\s*[\[(]\s*at\s*[\])]\s*", "@", text)
    deobfuscated = re.sub(r"(?i)\s*[\[(]\s*dot\s*[\])]\s*", ".", deobfuscated)
    emails = sorted({x.strip(".,;:()[]{}<>\"'").lower() for x in EMAIL_RE.findall(deobfuscated)})[:25]
    phones: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in PHONE_RE.findall(text):
        item = normalize_phone(raw)
        if not item:
            continue
        key = (item["number"], item["extension"])
        if key not in seen:
            seen.add(key)
            phones.append(item)
    return phones[:25], emails


def identity(request: SearchRequest) -> list[str]:
    values = [request.query, request.fields.company, request.fields.person, request.fields.phone, request.fields.email, request.fields.ein, request.fields.address, request.fields.state]
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = clean(value)
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            output.append(value)
    return output


def discovery_queries(terms: list[str]) -> list[str]:
    core = " ".join(f'"{term}"' if " " in term else term for term in terms[:5])
    return [core, f"{core} phone email contact", *(f"{core} site:{site}" for site in TARGET_SITES)]


def discover(queries: list[str], proxy: str) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    urls: list[dict[str, str]] = []
    reports: list[dict[str, Any]] = []
    seen: set[str] = set()
    ddgs_kwargs: dict[str, Any] = {}
    if proxy:
        ddgs_kwargs["proxy"] = proxy
    with DDGS(**ddgs_kwargs) as client:
        for query in queries:
            try:
                results = list(client.text(query, max_results=3))
                reports.append({"query": query, "status": "complete", "results": len(results)})
                for result in results:
                    url = clean(result.get("href") or result.get("url"))
                    if url and url not in seen and is_public_url(url):
                        seen.add(url)
                        urls.append({"url": url, "title": clean(result.get("title")), "snippet": clean(result.get("body"))})
                        if len(urls) >= MAX_URLS:
                            return urls, reports
            except Exception as exc:
                reports.append({"query": query, "status": "error", "error": str(exc)})
    return urls, reports


async def static_fetch(url: str, proxy: str) -> tuple[str, str]:
    import aiohttp
    from aiohttp_socks import ProxyConnector

    connector = ProxyConnector.from_url(proxy) if proxy.startswith(("socks4://", "socks5://")) else aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=28)
    kwargs: dict[str, Any] = {"allow_redirects": False, "headers": {"User-Agent": "Mozilla/5.0 QikReach/1.0"}}
    if proxy.startswith(("http://", "https://")):
        kwargs["proxy"] = proxy
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        current = url
        for _ in range(5):
            if not is_public_url(current):
                raise RuntimeError("Unsafe or private destination blocked")
            async with session.get(current, **kwargs) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location", "")
                    current = str(response.url.join(aiohttp.client_reqrep.URL(location)))
                    continue
                if response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status}")
                content_type = response.headers.get("content-type", "")
                if "text" not in content_type and "html" not in content_type and "json" not in content_type:
                    raise RuntimeError("Unsupported content type")
                data = await response.content.read(1_500_000)
                return visible_text(data.decode(response.charset or "utf-8", errors="replace")), "static"
        raise RuntimeError("Too many redirects")


async def browser_fetch(url: str, proxy: str) -> tuple[str, str]:
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, ProxyConfig
        browser_args: dict[str, Any] = {"headless": True, "enable_stealth": True}
        if proxy:
            browser_args["proxy_config"] = ProxyConfig.from_string(proxy)
        run = CrawlerRunConfig(wait_until="domcontentloaded", page_timeout=25000, bypass_cache=True, magic=True)
        async with AsyncWebCrawler(config=BrowserConfig(**browser_args)) as crawler:
            result = await crawler.arun(url=url, config=run)
            if result.success:
                text = clean(getattr(result, "markdown", "")) or visible_text(clean(getattr(result, "html", "")))
                if text:
                    return text[:30000], "crawl4ai-chromium"
    except Exception:
        pass
    return await static_fetch(url, proxy)


def relevance(text: str, terms: list[str]) -> int:
    haystack = re.sub(r"[^a-z0-9]+", " ", text.lower())
    score = 0
    for term in terms:
        normalized = re.sub(r"[^a-z0-9]+", " ", term.lower()).strip()
        if len(normalized) >= 4 and normalized in haystack:
            score += 2
        else:
            tokens = [x for x in normalized.split() if len(x) >= 4]
            score += min(1, sum(1 for token in tokens if token in haystack))
    return score


async def verify_domains(emails: list[str]) -> list[dict[str, str]]:
    import dns.resolver

    def check(email: str) -> dict[str, str]:
        try:
            dns.resolver.resolve(email.rsplit("@", 1)[1], "MX", lifetime=3)
            return {"email": email, "domain_status": "mx_found"}
        except Exception:
            return {"email": email, "domain_status": "no_mx_or_dns_error"}

    return await asyncio.to_thread(lambda: [check(email) for email in emails])


async def run_search(request: SearchRequest) -> dict[str, Any]:
    started = time.perf_counter()
    terms = identity(request)
    if not terms:
        raise ValueError("Enter a name, company, phone, email, EIN, address, state, or natural-language query")
    proxy = validate_proxy(request.proxy)
    queries = discovery_queries(terms)
    discovered, query_reports = await asyncio.to_thread(discover, queries, proxy)
    semaphore = asyncio.Semaphore(4)

    async def scrape(item: dict[str, str]) -> dict[str, Any]:
        async with semaphore:
            try:
                text, method = await browser_fetch(item["url"], proxy)
                score = relevance("\n".join((item["title"], item["snippet"], text)), terms)
                phones, emails = extract_contacts(text) if score >= 2 else ([], [])
                return {**item, "status": "scraped", "method": method, "relevance": score, "phones": phones, "emails": emails}
            except Exception as exc:
                return {**item, "status": "blocked_or_failed", "error": str(exc), "phones": [], "emails": []}

    sources = await asyncio.gather(*(scrape(item) for item in discovered))
    phone_map: dict[tuple[str, str], dict[str, str]] = {}
    email_set: set[str] = set()
    for source in sources:
        for phone in source["phones"]:
            phone_map[(phone["number"], phone["extension"])] = phone
        email_set.update(source["emails"])
    emails = sorted(email_set)
    email_records = await verify_domains(emails) if request.verify_email_domains else [{"email": value, "domain_status": "not_checked"} for value in emails]
    return {
        "status": "success" if phone_map or emails else ("blocked" if discovered and not any(x["status"] == "scraped" for x in sources) else "no_contacts" if discovered else "no_results"),
        "identity": request.model_dump(),
        "phones": list(phone_map.values()),
        "emails": email_records,
        "officers": [],
        "companies": [],
        "sources": sources,
        "discovery_queries": query_reports,
        "discovered_count": len(discovered),
        "scraped_count": sum(1 for x in sources if x["status"] == "scraped"),
        "relevant_count": sum(1 for x in sources if x.get("relevance", 0) >= 2),
        "duration_seconds": round(time.perf_counter() - started, 2),
        "proxy_used": bool(proxy),
    }


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "qikreach-live-enrichment", "status": "online"}


@app.get("/health")
async def health() -> dict[str, Any]:
    modules: dict[str, bool] = {}
    for module in ("duckduckgo_search", "crawl4ai", "aiohttp", "aiohttp_socks", "pandas", "openpyxl", "phonenumbers", "dns"):
        try:
            __import__(module)
            modules[module] = True
        except Exception:
            modules[module] = False
    return {"ok": modules["duckduckgo_search"] and modules["aiohttp"], "search_ready": modules["duckduckgo_search"] and modules["aiohttp"], "browser_crawler_ready": modules["crawl4ai"], "proxy_ready": modules["aiohttp_socks"], "batch_ready": modules["pandas"] and modules["openpyxl"], "modules": modules}


@app.post("/search")
async def search(request: SearchRequest) -> JSONResponse:
    try:
        result = await asyncio.wait_for(run_search(request), timeout=180)
        return JSONResponse({"ok": True, "result": result})
    except asyncio.TimeoutError as exc:
        raise HTTPException(504, "Search exceeded the 180-second safety limit") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"Search failed: {exc}") from exc


@app.post("/batch")
async def batch(file: UploadFile = File(...), concurrency: int = Form(2), delay: float = Form(0), proxy: str = Form(""), verify_email_domains: bool = Form(True), use_ollama: bool = Form(False)) -> FileResponse:
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(400, "Select an .xlsx workbook")
    temp_dir = Path(tempfile.mkdtemp(prefix="qikreach-"))
    input_path = temp_dir / Path(file.filename).name
    total = 0
    with input_path.open("wb") as handle:
        while chunk := await file.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_UPLOAD:
                raise HTTPException(413, "Maximum upload is 10 MiB")
            handle.write(chunk)
    frame = pd.read_excel(input_path)
    for column in ("Enriched_Phones", "Enriched_Emails", "Data_Sources", "Enrichment_Status"):
        if column not in frame.columns:
            frame[column] = ""
    semaphore = asyncio.Semaphore(max(1, min(concurrency, 4)))

    async def enrich(index: Any, row: Any) -> None:
        async with semaphore:
            if delay:
                await asyncio.sleep(max(0, min(delay, 10)))
            request = SearchRequest(query=clean(row.get("Company") or row.get("Name") or row.get("Phone") or row.get("Email")), fields=Fields(company=clean(row.get("Company")), person=clean(row.get("Owner") or row.get("Name")), phone=clean(row.get("Phone") or row.get("Phones")), email=clean(row.get("Email") or row.get("Emails")), ein=clean(row.get("EIN")), state=clean(row.get("State")), address=clean(row.get("Address"))), proxy=proxy, verify_email_domains=verify_email_domains, use_ollama=use_ollama)
            try:
                result = await run_search(request)
                frame.at[index, "Enriched_Phones"] = " | ".join(x["number"] for x in result["phones"]) or "None"
                frame.at[index, "Enriched_Emails"] = " | ".join(x["email"] for x in result["emails"]) or "None"
                frame.at[index, "Data_Sources"] = " | ".join(x["url"] for x in result["sources"]) or "None"
                frame.at[index, "Enrichment_Status"] = result["status"]
            except Exception as exc:
                frame.at[index, "Enrichment_Status"] = f"error: {exc}"

    await asyncio.wait_for(asyncio.gather(*(enrich(index, row) for index, row in frame.iterrows())), timeout=900)
    output_path = temp_dir / f"{input_path.stem}_ENRICHED.xlsx"
    frame.to_excel(output_path, index=False)
    return FileResponse(output_path, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=output_path.name)
