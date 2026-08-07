from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import shutil
import socket
import tempfile
import time
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlparse, urlunparse

import pandas as pd
import phonenumbers
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

MAX_URLS = 12
DISCOVERY_TARGET = 3
MAX_UPLOAD = 10 * 1024 * 1024
PROVIDER_TIMEOUT = 8
STATIC_TIMEOUT = 6
BROWSER_TIMEOUT = 8
SOURCE_BUDGET = 12
MAX_BROWSER_FALLBACKS = 1
SEARCH_TIMEOUT = 35
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()
APP_ORIGIN = os.getenv("APP_ORIGIN", "https://scanner.jerrylang.workers.dev").rstrip("/")

EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,24}(?![\w.-])")
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s.()/-]*)?(?:\(?[2-9]\d{2}\)?[\s.()/-]*)[2-9]\d{2}[\s.()/-]*\d{4}"
    r"(?:\s*(?:x|ext\.?|extension)\s*\d{1,6})?(?!\d)",
    re.I,
)
INTERNATIONAL_PHONE_RE = re.compile(r"(?<!\w)\+\d(?:[\s.()/-]*\d){7,14}(?!\w)")
BUSINESS_HINT_RE = re.compile(
    r"\b(?:llc|inc|incorporated|corp|corporation|company|co\.?|manufacturing|service|services|business|owner|contact|phone|email)\b",
    re.I,
)
ZIP_RE = re.compile(r"(?<!\d)\d{5}(?:-\d{4})?(?!\d)")
ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+[A-Z0-9][^,\n]{1,70}(?:,\s*#?[A-Z0-9][^,\n]{0,15})?,\s*[A-Z][A-Za-z .'-]{2,40},\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b",
    re.I,
)
DIRECTORY_DOMAINS = {
    "mapquest.com", "whitepages.com", "yelp.com", "yellowpages.com", "superpages.com",
    "bizprofile.net", "openigloo.com", "buzzfile.com", "company-detail.com", "truepeoplesearch.com",
    "telephonedirectories.us", "thephoneindex.com", "areacodelocator.net", "tfrecipes.com", "local.us-info.com", "numlookup.com",
}
GENERIC_MAILBOXES = {"help", "support", "contact", "info", "sales", "privacy", "admin", "noreply", "no-reply", "webmaster", "me"}
NAME_STOPWORDS = {"from", "near", "in", "at", "on", "around", "zip", "zipcode", "county", "state", "phone", "email", "address"}

PROVIDER_STATE: dict[str, dict[str, Any]] = {
    "gemini_google": {"status": "unknown" if GEMINI_API_KEY else "not_configured"},
    "groq_web": {"status": "unknown" if GROQ_API_KEY else "not_configured"},
    "tavily": {"status": "unknown" if TAVILY_API_KEY else "not_configured"},
    "ddgs_brave": {"status": "unknown"},
    "ddgs_bing": {"status": "unknown"},
    "ddgs_bing_unquoted": {"status": "unknown"},
    "ddgs_duckduckgo": {"status": "unknown"},
}

app = FastAPI(title="QikReach Live Enrichment API", version="1.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[APP_ORIGIN],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["content-type", "accept"],
)


class Fields(BaseModel):
    company: str = ""
    person: str = ""
    phone: str = ""
    email: str = ""
    ein: str = ""
    state: str = ""
    address: str = ""


class SearchRequest(BaseModel):
    query: str = Field(default="", max_length=2000)
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


def redact(value: Any) -> str:
    text = clean(value)
    for secret in (GEMINI_API_KEY, GROQ_API_KEY, TAVILY_API_KEY):
        if secret:
            text = text.replace(secret, "[redacted]")
    return re.sub(r"(?i)([?&](?:key|api_key|token)=)[^&\s]+", r"\1[redacted]", text)


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
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            addresses = [ipaddress.ip_address(record[4][0]) for record in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]
        return bool(addresses) and all(
            not (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
                or address.is_multicast
            )
            for address in addresses
        )
    except Exception:
        return False


def canonical_url(value: str) -> str:
    parsed = urlparse(clean(value))
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.params, parsed.query, ""))


def visible_text(html: str) -> str:
    parser = VisibleText()
    try:
        parser.feed(html)
        parser.close()
        return "\n".join(parser.parts)[:60_000]
    except Exception:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()[:60_000]


def extraction_surface(html: str, text: str) -> str:
    decoded = unescape(html)
    extras: list[str] = []
    for match in re.finditer(r"(?is)\b(?:href|content|data-email|data-phone|value)\s*=\s*(['\"])(.*?)\1", decoded):
        value = match.group(2).strip()
        if value.lower().startswith(("mailto:", "tel:")) or EMAIL_RE.search(value) or PHONE_RE.search(value) or INTERNATIONAL_PHONE_RE.search(value):
            extras.append(value.replace("mailto:", "").replace("tel:", ""))
    for match in re.finditer(r"(?is)<script[^>]+type\s*=\s*(['\"])application/ld\+json\1[^>]*>(.*?)</script>", decoded):
        extras.append(match.group(2))
    return "\n".join((text, *extras))[:100_000]


def normalize_phone(raw: str) -> dict[str, str] | None:
    try:
        parsed = phonenumbers.parse(raw, "US")
        if not phonenumbers.is_valid_number(parsed):
            return None
        if parsed.country_code == 1 and len(str(parsed.national_number)) != 10:
            return None
        phone_types = {
            phonenumbers.PhoneNumberType.MOBILE: "mobile",
            phonenumbers.PhoneNumberType.FIXED_LINE: "landline",
            phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE: "mobile or landline",
            phonenumbers.PhoneNumberType.VOIP: "voip",
            phonenumbers.PhoneNumberType.TOLL_FREE: "toll-free",
            phonenumbers.PhoneNumberType.PREMIUM_RATE: "premium-rate",
            phonenumbers.PhoneNumberType.SHARED_COST: "shared-cost",
            phonenumbers.PhoneNumberType.PERSONAL_NUMBER: "personal-number",
            phonenumbers.PhoneNumberType.PAGER: "pager",
            phonenumbers.PhoneNumberType.UAN: "uan",
            phonenumbers.PhoneNumberType.VOICEMAIL: "voicemail",
        }
        return {
            "number": phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164),
            "extension": parsed.extension or "",
            "line_type": phone_types.get(phonenumbers.number_type(parsed), "unknown"),
            "region": phonenumbers.region_code_for_number(parsed) or "",
        }
    except Exception:
        return None


def extract_contacts(text: str) -> tuple[list[dict[str, str]], list[str]]:
    deobfuscated = re.sub(r"(?i)\s*[\[(]\s*at\s*[\])]\s*", "@", text)
    deobfuscated = re.sub(r"(?i)\s*[\[(]\s*dot\s*[\])]\s*", ".", deobfuscated)
    emails = sorted({match.strip(".,;:()[]{}<>\"'").lower() for match in EMAIL_RE.findall(deobfuscated)})[:25]
    phones: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in phonenumbers.PhoneNumberMatcher(text, "US"):
        phone = normalize_phone(match.raw_string)
        if not phone:
            continue
        key = (phone["number"], phone["extension"])
        if key not in seen:
            seen.add(key)
            phones.append(phone)
    return phones[:25], emails


def requested_phone_digits(terms: list[str]) -> set[str]:
    """Return the last ten digits of phone identifiers explicitly supplied by the user."""
    requested: set[str] = set()
    for term in terms:
        for match in phonenumbers.PhoneNumberMatcher(term, "US"):
            normalized = normalize_phone(match.raw_string)
            if normalized:
                requested.add(re.sub(r"\D", "", normalized["number"])[-10:])
    return requested


def requested_emails(terms: list[str]) -> set[str]:
    return {value.casefold() for term in terms for value in EMAIL_RE.findall(term)}


def filter_contacts_for_identity(
    phones: list[dict[str, str]], emails: list[str], terms: list[str]
) -> tuple[list[dict[str, str]], list[str]]:
    """Avoid attributing unrelated contacts found on a directory page to a known identity."""
    phone_keys = requested_phone_digits(terms)
    email_keys = requested_emails(terms)
    if phone_keys:
        phones = [
            phone for phone in phones
            if re.sub(r"\D", "", phone.get("number", ""))[-10:] in phone_keys
        ]
    if email_keys:
        emails = [email for email in emails if email.casefold() in email_keys]
    return phones, emails


def source_host(url: str) -> str:
    return (urlparse(clean(url)).hostname or "").lower().removeprefix("www.")


def filter_source_emails(emails: list[str], item: dict[str, str], terms: list[str]) -> list[str]:
    requested = requested_emails(terms)
    host = source_host(item.get("url", ""))
    filtered: list[str] = []
    for email in emails:
        local, _, domain = email.casefold().partition("@")
        if email.casefold() in requested:
            filtered.append(email)
            continue
        if host in DIRECTORY_DOMAINS and domain == host and local in GENERIC_MAILBOXES:
            continue
        filtered.append(email)
    return filtered


def extract_business_details(item: dict[str, str], html: str, text: str) -> dict[str, str]:
    """Extract only explicit page context; missing fields stay absent rather than guessed."""
    surface = "\n".join(value for value in (item.get("title", ""), item.get("snippet", ""), text) if value)
    details: dict[str, str] = {}
    addresses = ADDRESS_RE.findall(surface)
    if addresses:
        details["address"] = re.sub(r"\s+", " ", addresses[0]).strip(" .,;")
    patterns = (
        r"(?:specializes in|specialised in)\s+([^.;\n]{3,120})",
        r"(?:is in the|operates in the)\s+([^.;\n]{3,120})\s+business",
        r"(?:provides?|providing|offers?)\s+([^.;\n]{3,120})",
    )
    for pattern in patterns:
        match = re.search(pattern, surface, re.I)
        if match:
            value = re.sub(r"\s+", " ", match.group(1)).strip(" ,;:")
            if value:
                details["business_type"] = value[:160]
                break
    owner = re.search(
        r"(?:owner|owned by|proprietor|founder|president|contact person)\s*(?:(?:is|:|-)\s*)?([A-Z][A-Za-z'-]+(?:\s+[A-Z][A-Za-z'-]+){0,3})",
        surface,
    )
    if owner:
        details["owner"] = owner.group(1).strip()
    if item.get("snippet"):
        details["summary"] = re.sub(r"\s+", " ", item["snippet"]).strip()[:700]
    return details


def identity(request: SearchRequest) -> list[str]:
    values = [
        request.query,
        request.fields.company,
        request.fields.person,
        request.fields.phone,
        request.fields.email,
        request.fields.ein,
        request.fields.address,
        request.fields.state,
    ]
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = clean(value)
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            output.append(value)
    return output


def search_subject(terms: list[str]) -> str:
    subject = " ".join(terms).strip()
    if len(subject.split()) >= 2 and any(BUSINESS_HINT_RE.search(term) for term in terms):
        return f'"{subject}"'[:600]
    return subject[:600]


def search_mode(request: SearchRequest) -> str:
    if any(clean(value) for value in request.fields.model_dump().values()):
        return "contact"
    query = clean(request.query)
    if EMAIL_RE.search(query) or PHONE_RE.search(query) or INTERNATIONAL_PHONE_RE.search(query) or ZIP_RE.search(query) or BUSINESS_HINT_RE.search(query):
        return "contact"
    return "general"


async def emit_progress(emit: Any, event_type: str, **payload: Any) -> None:
    if emit:
        await emit({"type": event_type, "at": round(time.time(), 3), **payload})


def mark_provider(name: str, status: str, error: str = "") -> None:
    state = PROVIDER_STATE.setdefault(name, {})
    state["status"] = status
    state["checked_at"] = round(time.time(), 3)
    if error:
        state["last_error"] = redact(error)[:300]
    else:
        state.pop("last_error", None)


def provider_chain() -> list[tuple[str, Callable[..., Awaitable[list[dict[str, str]]]] | None, str]]:
    chain: list[tuple[str, Callable[..., Awaitable[list[dict[str, str]]]] | None, str]] = []
    if GEMINI_API_KEY:
        chain.append(("gemini_google", gemini_search, ""))
    if GROQ_API_KEY:
        chain.append(("groq_web", groq_search, ""))
    if TAVILY_API_KEY:
        chain.append(("tavily", tavily_search, ""))
    chain.extend((("ddgs_brave", None, "brave"), ("ddgs_bing", None, "bing"), ("ddgs_bing_unquoted", None, "bing_unquoted"), ("ddgs_duckduckgo", None, "duckduckgo")))
    return chain


async def gemini_search(query: str, session: Any, _: str) -> list[dict[str, str]]:
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    prompt = (
        "Search the public web for the user's exact query and identify the most relevant source pages. "
        "The query may be anything: a business, person with a location or ZIP code, phone number, email, product, or general topic. "
        "For businesses or people, prioritize official websites, contact/about pages, public business profiles, and pages that may contain "
        "publicly posted contact information. Disambiguate names using every location or identity clue in the query. "
        "For general topics, return authoritative relevant sources. Do not invent facts or contact details.\n\n"
        f"User query: {query}"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 1200},
    }
    async with session.post(endpoint, params={"key": GEMINI_API_KEY}, json=body) as response:
        data = await response.json(content_type=None)
        if response.status >= 400:
            message = data.get("error", {}).get("message") if isinstance(data, dict) else clean(data)
            raise RuntimeError(f"Gemini HTTP {response.status}: {message or 'request failed'}")
    candidates = data.get("candidates") or []
    if not candidates:
        return []
    candidate = candidates[0]
    answer = " ".join(clean(part.get("text")) for part in candidate.get("content", {}).get("parts", []) if isinstance(part, dict))
    grounding = candidate.get("groundingMetadata") or {}
    results: list[dict[str, str]] = []
    for chunk in grounding.get("groundingChunks") or []:
        web = chunk.get("web") if isinstance(chunk, dict) else None
        if not web:
            continue
        url = clean(web.get("uri"))
        if url:
            results.append({"url": url, "title": clean(web.get("title")), "snippet": answer[:1200]})
    return results


async def tavily_search(query: str, session: Any, _: str) -> list[dict[str, str]]:
    body = {
        "query": query,
        "search_depth": "basic",
        "include_answer": False,
        "include_raw_content": False,
        "max_results": 8,
    }
    headers = {"Authorization": f"Bearer {TAVILY_API_KEY}", "Content-Type": "application/json"}
    async with session.post("https://api.tavily.com/search", headers=headers, json=body) as response:
        data = await response.json(content_type=None)
        if response.status >= 400:
            message = data.get("detail") or data.get("error") if isinstance(data, dict) else clean(data)
            raise RuntimeError(f"Tavily HTTP {response.status}: {message or 'request failed'}")
    return [
        {
            "url": clean(item.get("url")),
            "title": clean(item.get("title")),
            "snippet": clean(item.get("content")),
        }
        for item in data.get("results", [])
        if isinstance(item, dict) and clean(item.get("url"))
    ]


async def groq_search(query: str, session: Any, _: str) -> list[dict[str, str]]:
    body = {
        "model": "groq/compound-mini",
        "messages": [{
            "role": "user",
            "content": (
                "Search the public web for this exact query. Return relevant official, contact, business-profile, and public social-page "
                "sources when appropriate. Use every identity or location clue and do not invent contact details. Query: " + query
            ),
        }],
        "temperature": 0,
    }
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    async with session.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=body) as response:
        data = await response.json(content_type=None)
        if response.status >= 400:
            message = data.get("error", {}).get("message") if isinstance(data, dict) else clean(data)
            raise RuntimeError(f"Groq HTTP {response.status}: {message or 'request failed'}")
    choices = data.get("choices") or []
    message = choices[0].get("message", {}) if choices else {}
    results: list[dict[str, str]] = []
    for tool in message.get("executed_tools") or []:
        for item in tool.get("search_results") or []:
            if not isinstance(item, dict) or not clean(item.get("url")):
                continue
            results.append({
                "url": clean(item.get("url")),
                "title": clean(item.get("title")),
                "snippet": clean(item.get("content")),
            })
    return results


def ddgs_search(query: str, proxy: str, backend: str) -> list[dict[str, str]]:
    from ddgs import DDGS

    if backend.endswith("_unquoted"):
        query = query.strip('"')
        backend = backend.removesuffix("_unquoted")
    kwargs: dict[str, Any] = {"timeout": 5}
    if proxy:
        kwargs["proxy"] = proxy
    results = DDGS(**kwargs).text(query, max_results=6, backend=backend)
    return [
        {
            "url": clean(item.get("href") or item.get("url")),
            "title": clean(item.get("title")),
            "snippet": clean(item.get("body")),
        }
        for item in results
        if isinstance(item, dict)
    ]


async def discover(subject: str, proxy: str, emit: Any = None) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    import aiohttp

    providers = provider_chain()
    urls: list[dict[str, str]] = []
    reports: list[dict[str, Any]] = []
    seen: set[str] = set()
    timeout = aiohttp.ClientTimeout(total=PROVIDER_TIMEOUT)
    await emit_progress(emit, "discovery_started", query_count=len(providers))
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "QikReach/1.2"}) as session:
        for index, (name, async_provider, backend) in enumerate(providers, start=1):
            await emit_progress(emit, "query_started", index=index, total=len(providers), query=subject, provider=name)
            try:
                if async_provider:
                    results = await asyncio.wait_for(async_provider(subject, session, backend), timeout=PROVIDER_TIMEOUT)
                else:
                    results = await asyncio.wait_for(
                        asyncio.to_thread(ddgs_search, subject, proxy, backend), timeout=PROVIDER_TIMEOUT
                    )
                mark_provider(name, "online")
                accepted = 0
                for result in results:
                    url = clean(result.get("url"))
                    key = canonical_url(url)
                    if not url or not key or key in seen or not await asyncio.to_thread(is_public_url, url):
                        continue
                    seen.add(key)
                    item = {
                        "url": url,
                        "title": clean(result.get("title")),
                        "snippet": clean(result.get("snippet")),
                        "provider": name,
                    }
                    urls.append(item)
                    accepted += 1
                    await emit_progress(
                        emit,
                        "url_discovered",
                        index=len(urls),
                        url=url,
                        title=item["title"],
                        query=subject,
                        provider=name,
                    )
                    if len(urls) >= DISCOVERY_TARGET or len(urls) >= MAX_URLS:
                        break
                reports.append({"query": subject, "provider": name, "status": "complete", "results": accepted})
                await emit_progress(
                    emit,
                    "query_completed",
                    index=index,
                    total=len(providers),
                    query=subject,
                    provider=name,
                    results=accepted,
                )
                if len(urls) >= DISCOVERY_TARGET or len(urls) >= MAX_URLS:
                    await emit_progress(
                        emit,
                        "discovery_stopped",
                        completed=index,
                        total=len(providers),
                        reason=f"{name} returned enough unique public sources; slower fallbacks were skipped.",
                    )
                    break
            except asyncio.TimeoutError:
                message = f"{name} timed out after {PROVIDER_TIMEOUT}s"
                mark_provider(name, "offline", message)
                reports.append({"query": subject, "provider": name, "status": "error", "error": message})
                await emit_progress(
                    emit,
                    "query_failed",
                    index=index,
                    total=len(providers),
                    query=subject,
                    provider=name,
                    category="timeout",
                    error=message,
                )
            except Exception as exc:
                message = redact(exc)
                lowered = message.lower()
                category = "rate_limit" if "429" in lowered or "rate" in lowered or "quota" in lowered else "search_provider_error"
                mark_provider(name, "offline", message)
                reports.append({"query": subject, "provider": name, "status": "error", "error": message})
                await emit_progress(
                    emit,
                    "query_failed",
                    index=index,
                    total=len(providers),
                    query=subject,
                    provider=name,
                    category=category,
                    error=message,
                )
    await emit_progress(emit, "discovery_completed", discovered=len(urls), query_reports=len(reports))
    return urls, reports


async def static_fetch(session: Any, url: str, proxy: str) -> tuple[str, str, str]:
    kwargs: dict[str, Any] = {
        "allow_redirects": False,
        "headers": {
            "User-Agent": "Mozilla/5.0 (compatible; QikReach/1.2; +https://scanner.jerrylang.workers.dev/)"
        },
    }
    if proxy.startswith(("http://", "https://")):
        kwargs["proxy"] = proxy
    current = url
    for _ in range(5):
        if not await asyncio.to_thread(is_public_url, current):
            raise RuntimeError("Unsafe or private destination blocked")
        async with session.get(current, **kwargs) as response:
            if response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get("location", "")
                if not location:
                    raise RuntimeError(f"HTTP {response.status} redirect had no destination")
                current = urljoin(str(response.url), location)
                continue
            if response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}")
            content_type = response.headers.get("content-type", "").lower()
            if not any(kind in content_type for kind in ("text", "html", "json", "xml")):
                raise RuntimeError("Unsupported content type")
            data = bytearray()
            async for chunk in response.content.iter_chunked(65_536):
                remaining = 1_500_000 - len(data)
                if remaining <= 0:
                    break
                data.extend(chunk[:remaining])
                if len(data) >= 1_500_000:
                    break
            html = data.decode(response.charset or "utf-8", errors="replace")
            text = visible_text(html)
            if len(text) < 120:
                raise RuntimeError("Static response contained too little readable content")
            return html, text, "static"
    raise RuntimeError("Too many redirects")


def relevance(text: str, terms: list[str]) -> int:
    haystack = re.sub(r"[^a-z0-9@.+-]+", " ", text.casefold())
    score = 0
    tokens_seen: set[str] = set()
    for term in terms:
        normalized = re.sub(r"[^a-z0-9@.+-]+", " ", term.casefold()).strip()
        if not normalized:
            continue
        if normalized in haystack:
            score += 3
        for token in normalized.split():
            if token in tokens_seen or (len(token) < 3 and not any(character.isdigit() for character in token)):
                continue
            tokens_seen.add(token)
            if token in haystack:
                score += 1
    return min(score, 12)


def identity_constraints_met(text: str, terms: list[str]) -> bool:
    folded = text.casefold()
    digits = re.sub(r"\D", "", text)
    normalized_text = re.sub(r"[^a-z0-9]+", " ", folded).strip()
    text_tokens = normalized_text.split()
    required_zips = {value for term in terms for value in ZIP_RE.findall(term)}
    required_emails = {value.casefold() for term in terms for value in EMAIL_RE.findall(term)}
    required_phones: set[str] = set()
    for term in terms:
        for match in phonenumbers.PhoneNumberMatcher(term, "US"):
            normalized = normalize_phone(match.raw_string)
            if normalized:
                required_phones.add(re.sub(r"\D", "", normalized["number"])[-10:])
    for term in terms:
        if not BUSINESS_HINT_RE.search(term):
            continue
        phrase = re.sub(r"[^a-z0-9]+", " ", term.casefold()).strip()
        phrase_tokens = phrase.split()
        if len(phrase_tokens) >= 2 and not any(text_tokens[index:index + len(phrase_tokens)] == phrase_tokens for index in range(len(text_tokens))):
            return False
    return (
        all(value.casefold() in folded for value in required_zips)
        and all(value in folded for value in required_emails)
        and all(value in digits for value in required_phones)
    )


def source_result(item: dict[str, str], html: str, text: str, method: str, terms: list[str], mode: str, duration: float, fallback_reason: str = "") -> dict[str, Any]:
    combined = "\n".join((item.get("title", ""), item.get("snippet", ""), text))
    score = relevance(combined, terms)
    identity_match = identity_constraints_met(combined, terms)
    phones, emails = extract_contacts(extraction_surface(html, text)) if mode == "contact" and score >= 2 and identity_match else ([], [])
    phones, emails = filter_contacts_for_identity(phones, emails, terms)
    emails = filter_source_emails(emails, item, terms)
    result: dict[str, Any] = {
        **item,
        "status": "scraped",
        "method": method,
        "relevance": score,
        "identity_match": identity_match,
        "phones": phones,
        "emails": emails,
        "details": extract_business_details(item, html, text) if identity_match else {},
        "duration_seconds": round(duration, 2),
    }
    if fallback_reason:
        result["fallback_reason"] = fallback_reason
    return result


def snippet_result(item: dict[str, str], terms: list[str], mode: str, reason: str, duration: float) -> dict[str, Any] | None:
    if mode != "contact" or item.get("provider") == "gemini_google":
        return None
    surface = "\n".join((item.get("title", ""), item.get("snippet", "")))
    score = relevance(surface, terms)
    identity_match = identity_constraints_met(surface, terms)
    phones, emails = extract_contacts(surface) if score >= 2 and identity_match else ([], [])
    phones, emails = filter_contacts_for_identity(phones, emails, terms)
    emails = filter_source_emails(emails, item, terms)
    if not phones and not emails:
        return None
    return {
        **item,
        "status": "snippet_only",
        "method": "search_snippet",
        "relevance": score,
        "identity_match": identity_match,
        "phones": phones,
        "emails": emails,
        "details": extract_business_details(item, "", "") if identity_match else {},
        "fallback_reason": reason,
        "duration_seconds": round(duration, 2),
    }


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


async def emit_source_completed(emit: Any, position: int, total: int, source: dict[str, Any]) -> None:
    await emit_progress(
        emit,
        "source_completed",
        index=position,
        total=total,
        url=source["url"],
        method=source["method"],
        relevance=source["relevance"],
        identity_match=source["identity_match"],
        phones=source["phones"],
        emails=source["emails"],
        title=source.get("title", ""),
        snippet=source.get("snippet", ""),
        details=source.get("details", {}),
        fallback_reason=source.get("fallback_reason", ""),
        duration_seconds=source["duration_seconds"],
    )


async def scrape_sources(discovered: list[dict[str, str]], terms: list[str], mode: str, proxy: str, emit: Any) -> list[dict[str, Any]]:
    import aiohttp
    from aiohttp_socks import ProxyConnector

    semaphore = asyncio.Semaphore(5)
    connector = ProxyConnector.from_url(proxy) if proxy.startswith(("socks4://", "socks5://")) else aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=STATIC_TIMEOUT)
    sources: list[dict[str, Any] | None] = [None] * len(discovered)
    pending: list[tuple[int, dict[str, str], str, float]] = []

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        async def scrape_static(position: int, item: dict[str, str]) -> None:
            async with semaphore:
                await emit_progress(emit, "source_started", index=position, total=len(discovered), url=item["url"], title=item["title"])
                started = time.perf_counter()
                try:
                    html, text, method = await asyncio.wait_for(static_fetch(session, item["url"], proxy), timeout=STATIC_TIMEOUT)
                    source = source_result(item, html, text, method, terms, mode, time.perf_counter() - started)
                    sources[position - 1] = source
                    await emit_source_completed(emit, position, len(discovered), source)
                except Exception as exc:
                    message = redact(exc)
                    snippet = snippet_result(item, terms, mode, message, time.perf_counter() - started)
                    if snippet:
                        sources[position - 1] = snippet
                        await emit_source_completed(emit, position, len(discovered), snippet)
                    else:
                        pending.append((position, item, message, started))

        await asyncio.gather(*(scrape_static(index, item) for index, item in enumerate(discovered, start=1)))

    browser_candidates = sorted(pending, key=lambda entry: relevance("\n".join((entry[1].get("title", ""), entry[1].get("snippet", ""))), terms), reverse=True)
    selected = browser_candidates[:MAX_BROWSER_FALLBACKS]
    skipped = browser_candidates[MAX_BROWSER_FALLBACKS:]

    if selected:
        try:
            from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig, ProxyConfig

            browser_args: dict[str, Any] = {"headless": True, "enable_stealth": True}
            if proxy:
                browser_args["proxy_config"] = ProxyConfig.from_string(proxy)
            run_config = CrawlerRunConfig(
                wait_until="domcontentloaded",
                page_timeout=12_000,
                cache_mode=CacheMode.ENABLED,
                magic=False,
            )
            async with AsyncWebCrawler(config=BrowserConfig(**browser_args)) as crawler:
                for position, item, static_error, started in selected:
                    try:
                        remaining = SOURCE_BUDGET - (time.perf_counter() - started)
                        if remaining <= 1:
                            raise RuntimeError(f"Source exceeded the {SOURCE_BUDGET}-second time budget before browser fallback")
                        result = await asyncio.wait_for(
                            crawler.arun(url=item["url"], config=run_config),
                            timeout=min(BROWSER_TIMEOUT, remaining),
                        )
                        if not result.success:
                            raise RuntimeError(clean(getattr(result, "error_message", "")) or "Crawler returned no usable page content")
                        html = clean(getattr(result, "html", ""))
                        markdown = getattr(result, "markdown", "")
                        text = clean(getattr(markdown, "raw_markdown", markdown)) or visible_text(html)
                        if not text:
                            raise RuntimeError("Crawler returned no readable content")
                        source = source_result(
                            item,
                            html,
                            text,
                            "crawl4ai-chromium",
                            terms,
                            mode,
                            time.perf_counter() - started,
                            static_error,
                        )
                        sources[position - 1] = source
                        await emit_source_completed(emit, position, len(discovered), source)
                    except Exception as exc:
                        message = "; ".join(value for value in (static_error, redact(exc)) if value)
                        snippet = snippet_result(item, terms, mode, message, time.perf_counter() - started)
                        if snippet:
                            sources[position - 1] = snippet
                            await emit_source_completed(emit, position, len(discovered), snippet)
                            continue
                        category = error_category(message)
                        sources[position - 1] = {
                            **item,
                            "status": "blocked_or_failed",
                            "error": message,
                            "error_category": category,
                            "phones": [],
                            "emails": [],
                        }
                        await emit_progress(
                            emit,
                            "source_failed",
                            index=position,
                            total=len(discovered),
                            url=item["url"],
                            error=message,
                            category=category,
                        )
        except Exception as exc:
            browser_error = redact(exc)
            for position, item, static_error, _ in selected:
                message = "; ".join(value for value in (static_error, browser_error) if value)
                snippet = snippet_result(item, terms, mode, message, 0)
                if snippet:
                    sources[position - 1] = snippet
                    await emit_source_completed(emit, position, len(discovered), snippet)
                    continue
                category = error_category(message)
                sources[position - 1] = {
                    **item,
                    "status": "blocked_or_failed",
                    "error": message,
                    "error_category": category,
                    "phones": [],
                    "emails": [],
                }
                await emit_progress(emit, "source_failed", index=position, total=len(discovered), url=item["url"], error=message, category=category)

    for position, item, static_error, _ in skipped:
        message = f"{static_error}; browser fallback skipped to keep the search within its time budget"
        snippet = snippet_result(item, terms, mode, message, 0)
        if snippet:
            sources[position - 1] = snippet
            await emit_source_completed(emit, position, len(discovered), snippet)
            continue
        category = error_category(message)
        sources[position - 1] = {
            **item,
            "status": "blocked_or_failed",
            "error": message,
            "error_category": category,
            "phones": [],
            "emails": [],
        }
        await emit_progress(emit, "source_failed", index=position, total=len(discovered), url=item["url"], error=message, category=category)

    return [source for source in sources if source is not None]


async def verify_domains(emails: list[str]) -> list[dict[str, str]]:
    import dns.resolver

    semaphore = asyncio.Semaphore(5)

    def check(email: str) -> dict[str, str]:
        try:
            dns.resolver.resolve(email.rsplit("@", 1)[1], "MX", lifetime=3)
            return {"email": email, "domain_status": "mx_found"}
        except Exception:
            return {"email": email, "domain_status": "no_mx_or_dns_error"}

    async def limited(email: str) -> dict[str, str]:
        async with semaphore:
            return await asyncio.to_thread(check, email)

    return list(await asyncio.gather(*(limited(email) for email in emails)))


async def run_search(request: SearchRequest, emit: Any = None) -> dict[str, Any]:
    started = time.perf_counter()
    await emit_progress(emit, "search_started")
    terms = identity(request)
    if not terms:
        raise ValueError("Enter anything to search")
    proxy = validate_proxy(request.proxy)
    subject = search_subject(terms)
    mode = search_mode(request)
    await emit_progress(
        emit,
        "input_validated",
        terms=terms,
        mode=mode,
        verify_email_domains=request.verify_email_domains,
        proxy_used=bool(proxy),
    )
    await emit_progress(emit, "queries_prepared", count=1, queries=[subject], mode=mode)
    discovered, query_reports = await discover(subject, proxy, emit)
    sources = await scrape_sources(discovered, terms, mode, proxy, emit) if discovered else []

    phone_map: dict[tuple[str, str], dict[str, Any]] = {}
    email_set: set[str] = set()
    email_sources: dict[str, list[str]] = {}
    business_details: list[dict[str, Any]] = []
    for source in sources:
        details = source.get("details") or {}
        if details:
            business_details.append({"source_url": source["url"], "source_title": source.get("title", ""), **details})
        for phone in source.get("phones", []):
            key = (phone["number"], phone["extension"])
            record = phone_map.setdefault(key, {**phone, "source_urls": []})
            if source["url"] not in record["source_urls"]:
                record["source_urls"].append(source["url"])
        for email in source.get("emails", []):
            email_set.add(email)
            email_sources.setdefault(email, [])
            if source["url"] not in email_sources[email]:
                email_sources[email].append(source["url"])

    emails = sorted(email_set)
    scraped_count = sum(1 for source in sources if source["status"] == "scraped")
    await emit_progress(emit, "extraction_completed", phones=len(phone_map), emails=len(emails), scraped=scraped_count)
    if request.verify_email_domains and emails:
        await emit_progress(emit, "verification_started", emails=len(emails))
        email_records = await verify_domains(emails)
        await emit_progress(emit, "verification_completed", records=email_records)
    else:
        email_records = [{"email": value, "domain_status": "not_checked"} for value in emails]
        await emit_progress(emit, "verification_skipped", reason="disabled" if not request.verify_email_domains else "no_emails")
    for record in email_records:
        record["source_urls"] = email_sources.get(record["email"], [])

    provider_unavailable = not discovered and bool(query_reports) and all(report.get("status") == "error" for report in query_reports)
    if phone_map or emails or (mode == "general" and scraped_count):
        status = "success"
    elif discovered and not scraped_count:
        status = "blocked"
    elif discovered:
        status = "no_contacts"
    elif provider_unavailable:
        status = "search_provider_unavailable"
    else:
        status = "no_results"

    result = {
        "status": status,
        "mode": mode,
        "identity": request.model_dump(),
        "phones": list(phone_map.values()),
        "emails": email_records,
        "business_details": business_details[:12],
        "officers": [],
        "companies": [],
        "sources": sources,
        "discovery_queries": query_reports,
        "discovered_count": len(discovered),
        "scraped_count": scraped_count,
        "relevant_count": sum(1 for source in sources if source.get("relevance", 0) >= 2 and source.get("identity_match", True)),
        "duration_seconds": round(time.perf_counter() - started, 2),
        "proxy_used": bool(proxy),
    }
    await emit_progress(
        emit,
        "result_ready",
        status=result["status"],
        mode=mode,
        discovered_count=result["discovered_count"],
        scraped_count=result["scraped_count"],
        relevant_count=result["relevant_count"],
        duration_seconds=result["duration_seconds"],
    )
    return result


def module_status() -> dict[str, bool]:
    modules: dict[str, bool] = {}
    for module in ("ddgs", "crawl4ai", "aiohttp", "aiohttp_socks", "pandas", "openpyxl", "phonenumbers", "dns"):
        try:
            __import__(module)
            modules[module] = True
        except Exception:
            modules[module] = False
    modules["duckduckgo_search"] = modules["ddgs"]
    return modules


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "qikreach-live-enrichment", "status": "online"}


@app.get("/health")
async def health() -> dict[str, Any]:
    modules = module_status()
    configured = [name for name, _, _ in provider_chain()]
    return {
        "ok": modules["aiohttp"],
        "search_ready": modules["aiohttp"] and bool(configured),
        "browser_crawler_ready": modules["crawl4ai"],
        "proxy_ready": modules["aiohttp_socks"],
        "batch_ready": modules["pandas"] and modules["openpyxl"],
        "primary_provider": configured[0] if configured else None,
        "provider_chain": configured,
        "providers": {name: PROVIDER_STATE.get(name, {"status": "unknown"}) for name in configured},
        "modules": modules,
    }


@app.post("/search")
async def search(request: SearchRequest) -> JSONResponse:
    try:
        result = await asyncio.wait_for(run_search(request), timeout=SEARCH_TIMEOUT)
        return JSONResponse({"ok": True, "result": result})
    except asyncio.TimeoutError as exc:
        raise HTTPException(504, f"Search exceeded the {SEARCH_TIMEOUT}-second safety limit") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"Search failed: {redact(exc)}") from exc


@app.post("/search/stream")
async def search_stream(request: SearchRequest) -> StreamingResponse:
    async def stream():
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def emit(event: dict[str, Any]) -> None:
            await queue.put(event)

        async def runner() -> None:
            try:
                result = await asyncio.wait_for(run_search(request, emit), timeout=SEARCH_TIMEOUT)
                await emit({"type": "complete", "at": round(time.time(), 3), "result": result})
            except asyncio.TimeoutError:
                await emit({"type": "error", "at": round(time.time(), 3), "category": "timeout", "error": f"Search exceeded the {SEARCH_TIMEOUT}-second safety limit"})
            except ValueError as exc:
                await emit({"type": "error", "at": round(time.time(), 3), "category": "invalid_input", "error": str(exc)})
            except Exception as exc:
                await emit({"type": "error", "at": round(time.time(), 3), "category": "server_error", "error": redact(exc)})
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
async def batch(
    file: UploadFile = File(...),
    concurrency: int = Form(2),
    delay: float = Form(0),
    proxy: str = Form(""),
    verify_email_domains: bool = Form(True),
    use_ollama: bool = Form(False),
) -> FileResponse:
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(400, "Select an .xlsx workbook")
    temp_dir = Path(tempfile.mkdtemp(prefix="qikreach-"))
    input_path = temp_dir / Path(file.filename).name
    try:
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
                request = SearchRequest(
                    query=clean(row.get("Company") or row.get("Name") or row.get("Phone") or row.get("Email")),
                    fields=Fields(
                        company=clean(row.get("Company")),
                        person=clean(row.get("Owner") or row.get("Name")),
                        phone=clean(row.get("Phone") or row.get("Phones")),
                        email=clean(row.get("Email") or row.get("Emails")),
                        ein=clean(row.get("EIN")),
                        state=clean(row.get("State")),
                        address=clean(row.get("Address")),
                    ),
                    proxy=proxy,
                    verify_email_domains=verify_email_domains,
                    use_ollama=use_ollama,
                )
                try:
                    result = await asyncio.wait_for(run_search(request), timeout=SEARCH_TIMEOUT)
                    frame.at[index, "Enriched_Phones"] = " | ".join(item["number"] for item in result["phones"]) or "None"
                    frame.at[index, "Enriched_Emails"] = " | ".join(item["email"] for item in result["emails"]) or "None"
                    frame.at[index, "Data_Sources"] = " | ".join(item["url"] for item in result["sources"]) or "None"
                    frame.at[index, "Enrichment_Status"] = result["status"]
                except Exception as exc:
                    frame.at[index, "Enrichment_Status"] = f"error: {redact(exc)}"

        await asyncio.wait_for(asyncio.gather(*(enrich(index, row) for index, row in frame.iterrows())), timeout=900)
        output_path = temp_dir / f"{input_path.stem}_ENRICHED.xlsx"
        frame.to_excel(output_path, index=False)
        return FileResponse(
            output_path,
            filename=output_path.name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
        )
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
