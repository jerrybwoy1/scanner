from __future__ import annotations

import asyncio
import ipaddress
import json
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
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

MAX_URLS = 14
MAX_UPLOAD = 10 * 1024 * 1024
DISCOVERY_TIMEOUT = 8
SEARCH_PROVIDER_FAILURE_LIMIT = 2
SOURCE_TIMEOUT = 40
TARGET_SITES = (
    "facebook.com", "linkedin.com/in", "truepeoplesearch.com", "bizapedia.com",
    "opencorporates.com", "bbb.org", "chamberofcommerce.com", "manta.com",
)
EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,24}(?![\w.-])")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.()/-]*)?(?:\(?[2-9]\d{2}\)?[\s.()/-]*)[2-9]\d{2}[\s.()/-]*\d{4}(?:\s*(?:x|ext\.?|extension)\s*\d{1,6})?(?!\d)", re.I)
APP_ORIGIN = os.getenv("APP_ORIGIN", "https://scanner.jerrylang.workers.dev").rstrip("/")

app = FastAPI(title="QikReach Live Enrichment API", version="1.1.0")
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


async def emit_progress(emit: Any, event_type: str, **payload: Any) -> None:
    if emit:
        await emit({"type": event_type, "at": round(time.time(), 3), **payload})


def search_one_query(query: str, proxy: str) -> list[dict[str, str]]:
    kwargs: dict[str, Any] = {"timeout": 6}
    if proxy:
        kwargs["proxy"] = proxy
    with DDGS(**kwargs) as client:
        return list(client.text(query, max_results=3))


async def discover(queries: list[str], proxy: str, emit: Any = None) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    urls: list[dict[str, str]] = []
    reports: list[dict[str, Any]] = []
    seen: set[str] = set()
    provider_failures = 0

    await emit_progress(emit, "discovery_started", query_count=len(queries))
    for index, query in enumerate(queries, start=1):
        await emit_progress(emit, "query_started", index=index, total=len(queries), query=query)
        try:
            results = await asyncio.wait_for(asyncio.to_thread(search_one_query, query, proxy), timeout=DISCOVERY_TIMEOUT)
            provider_failures = 0
            reports.append({"query": query, "status": "complete", "results": len(results)})
            await emit_progress(emit, "query_completed", index=index, total=len(queries), query=query, results=len(results))
            for result in results:
                url = clean(result.get("href") or result.get("url"))
                if not url or url in seen:
                    continue
                public = await asyncio.to_thread(is_public_url, url)
                if not public:
                    continue
                seen.add(url)
                item = {"url": url, "title": clean(result.get("title")), "snippet": clean(result.get("body"))}
                urls.append(item)
                await emit_progress(emit, "url_discovered", index=len(urls), url=url, title=item["title"], query=query)
                if len(urls) >= MAX_URLS:
                    await emit_progress(emit, "discovery_completed", discovered=len(urls), query_reports=len(reports))
                    return urls, reports
        except asyncio.TimeoutError:
            message = f"Discovery query timed out after {DISCOVERY_TIMEOUT}s"
            reports.append({"query": query, "status": "error", "error": message})
            await emit_progress(emit, "query_failed", index=index, total=len(queries), query=query, error=message, category="timeout")
            provider_failures += 1
        except Exception as exc:
            message = str(exc)
            message_lower = message.lower()
            category = "timeout" if "timeout" in message_lower else "rate_limit" if "rate" in message_lower or "429" in message else "search_provider_error"
            reports.append({"query": query, "status": "error", "error": message})
            await emit_progress(emit, "query_failed", index=index, total=len(queries), query=query, error=message, category=category)
            provider_failures += 1

        if provider_failures >= SEARCH_PROVIDER_FAILURE_LIMIT:
            await emit_progress(emit, "discovery_stopped", completed=index, total=len(queries), reason="Search provider repeatedly timed out or failed; remaining query variations were skipped.")
            break

    await emit_progress(emit, "discovery_completed", discovered=len(urls), query_reports=len(reports))
    return urls, reports


async def static_fetch(url: str, proxy: str) -> tuple[str, str]:
    import aiohttp
    from aiohttp_socks import ProxyConnector

    connector = ProxyConnector.from_url(proxy) if proxy.startswith(("socks4://", "socks5://")) else aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=22)
    kwargs: dict[str, Any] = {"allow_redirects": False, "headers": {"User-Agent": "Mozilla/5.0 QikReach/1.1"}}
    if proxy.startswith(("http://", "https://")):
        kwargs["proxy"] = proxy
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        current = url
        for _ in range(5):
            if not await asyncio.to_thread(is_public_url, current):
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


async def browser_fetch(url: str, proxy: str) -> tuple[str, str, str]:
    static_error = ""
    try:
        text, method = await static_fetch(url, proxy)
        if text:
            return text, method, ""
    except Exception as exc:
        static_error = str(exc)

    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, ProxyConfig
        browser_args: dict[str, Any] = {"headless": True, "enable_stealth": True}
        if proxy:
            browser_args["proxy_config"] = ProxyConfig.from_string(proxy)
        run = CrawlerRunConfig(wait_until="domcontentloaded", page_timeout=22000, bypass_cache=True, magic=True)
        async with AsyncWebCrawler(config=BrowserConfig(**browser_args)) as crawler:
            result = await crawler.arun(url=url, config=run)
            if result.success:
                text = clean(getattr(result, "markdown", "")) or visible_text(clean(getattr(result, "html", "")))
                if text:
                    return text[:30000], "crawl4ai-chromium", static_error
            browser_error = clean(getattr(result, "error_message", "")) or "Crawler returned no usable page content"
    except Exception as exc:
        browser_error = str(exc)

    combined = "; ".join(x for x in (static_error, browser_error) if x)
    raise RuntimeError(combined or "Source could not be retrieved")


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


def error_category(message: str) -> str:
    text = message.lower()
    if "429" in text or "rate" in text:
        return "rate_limit"
    if "403" in text or "forbidden" in text or "captcha" in text or "challenge" in text:
        return "blocked"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "dns" in text or "name resolution" in text or "getaddrinfo" in text:
        return "dns"
    if "ssl" in text or "certificate" in text:
        return "ssl"
    if "proxy" in text:
        return "proxy"
    return "source_error"


async def run_search(request: SearchRequest, emit: Any = None) -> dict[str, Any]:
    started = time.perf_counter()
    await emit_progress(emit, "search_started")
    terms = identity(request)
    if not terms:
        raise ValueError("Enter a name, company, phone, email, EIN, address, state, or natural-language query")
    proxy = validate_proxy(request.proxy)
    queries = discovery_queries(terms)
    await emit_progress(emit, "input_validated", terms=terms, verify_email_domains=request.verify_email_domains, proxy_used=bool(proxy))
    await emit_progress(emit, "queries_prepared", count=len(queries), queries=queries)
    discovered, query_reports = await discover(queries, proxy, emit)
    semaphore = asyncio.Semaphore(3)

    async def scrape(position: int, item: dict[str, str]) -> dict[str, Any]:
        async with semaphore:
            await emit_progress(emit, "source_started", index=position, total=len(discovered), url=item["url"], title=item["title"])
            source_started = time.perf_counter()
            try:
                text, method, fallback_reason = await asyncio.wait_for(browser_fetch(item["url"], proxy), timeout=SOURCE_TIMEOUT)
                score = relevance("\n".join((item["title"], item["snippet"], text)), terms)
                phones, emails = extract_contacts(text) if score >= 2 else ([], [])
                source = {
                    **item,
                    "status": "scraped",
                    "method": method,
                    "relevance": score,
                    "phones": phones,
                    "emails": emails,
                    "duration_seconds": round(time.perf_counter() - source_started, 2),
                }
                if fallback_reason:
                    source["fallback_reason"] = fallback_reason
                await emit_progress(
                    emit,
                    "source_completed",
                    index=position,
                    total=len(discovered),
                    url=item["url"],
                    method=method,
                    relevance=score,
                    phones=phones,
                    emails=emails,
                    fallback_reason=fallback_reason,
                    duration_seconds=source["duration_seconds"],
                )
                return source
            except asyncio.TimeoutError:
                message = f"Source exceeded {SOURCE_TIMEOUT}s safety limit"
            except Exception as exc:
                message = str(exc)
            category = error_category(message)
            await emit_progress(emit, "source_failed", index=position, total=len(discovered), url=item["url"], error=message, category=category)
            return {**item, "status": "blocked_or_failed", "error": message, "error_category": category, "phones": [], "emails": []}

    sources = await asyncio.gather(*(scrape(index, item) for index, item in enumerate(discovered, start=1)))
    phone_map: dict[tuple[str, str], dict[str, str]] = {}
    email_set: set[str] = set()
    for source in sources:
        for phone in source["phones"]:
            phone_map[(phone["number"], phone["extension"])] = phone
        email_set.update(source["emails"])

    emails = sorted(email_set)
    await emit_progress(emit, "extraction_completed", phones=len(phone_map), emails=len(emails), scraped=sum(1 for x in sources if x["status"] == "scraped"))
    if request.verify_email_domains and emails:
        await emit_progress(emit, "verification_started", emails=len(emails))
        email_records = await verify_domains(emails)
        await emit_progress(emit, "verification_completed", records=email_records)
    else:
        email_records = [{"email": value, "domain_status": "not_checked"} for value in emails]
        await emit_progress(emit, "verification_skipped", reason="disabled" if not request.verify_email_domains else "no_emails")

    result = {
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
    await emit_progress(emit, "result_ready", status=result["status"], discovered_count=result["discovered_count"], scraped_count=result["scraped_count"], relevant_count=result["relevant_count"], duration_seconds=result["duration_seconds"])
    return result


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


@app.post("/search/stream")
async def search_stream(request: SearchRequest) -> StreamingResponse:
    async def stream():
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def emit(event: dict[str, Any]) -> None:
            await queue.put(event)

        async def runner() -> None:
            try:
                result = await asyncio.wait_for(run_search(request, emit), timeout=180)
                await emit({"type": "complete", "at": round(time.time(), 3), "result": result})
            except asyncio.TimeoutError:
                await emit({"type": "error", "at": round(time.time(), 3), "category": "timeout", "error": "Search exceeded the 180-second safety limit"})
            except ValueError as exc:
                await emit({"type": "error", "at": round(time.time(), 3), "category": "invalid_input", "error": str(exc)})
            except Exception as exc:
                await emit({"type": "error", "at": round(time.time(), 3), "category": "server_error", "error": str(exc)})
            finally:
                await queue.put(None)

        task = asyncio.create_task(runner())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield json.dumps(event, separators=(",", ":")) + "\n"
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return StreamingResponse(stream(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


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
