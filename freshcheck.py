"""freshcheck: small guards for building on Crustdata's people and company APIs.

1. clean_profile_urls / enrich_all: Person Enrich rejects a whole batch of 25 with HTTP 400 when one URL is malformed.
   Clean URLs first, and if a batch still fails, bisect it so one bad URL costs one URL, not 24 good ones.
2. resolve_company: Company Identify misses a startup by the domain its website uses today when the indexed record
   still holds the old domain (seen for 7 of 11 moved YC companies). Fall back to the brand on the live homepage,
   identify by name, and accept a candidate only if its indexed domain redirects to the domain asked about. No proof,
   no answer: a namesake with a higher score is never returned as a guess.
3. flag_agent_rows: Agent Search can return rows whose own verdict on a hard constraint is "violated". Split them out.

Stdlib only. Pure functions take injected `identify`, `fetch` and `api` callables; live adapters are at the bottom.
"""
from __future__ import annotations

import html
import re
import time
import urllib.parse
import urllib.request
from typing import Callable

LINKEDIN = re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?(?:www\.)?linkedin\.com/in/\s*([A-Za-z0-9\-_%]+)", re.I)
TWO_LEVEL = {"co.uk", "com.au", "co.in", "com.br", "co.jp", "com.sg", "co.nz", "co.za"}


# 1. Person Enrich -----------------------------------------------------------------------------------------------
def clean_profile_urls(urls: list[str]) -> tuple[list[str], list[str]]:
    ok, bad, seen = [], [], set()
    for u in urls:
        m = LINKEDIN.search(u or "")
        if not m:
            bad.append(u)
            continue
        url = f"https://www.linkedin.com/in/{m.group(1).lower()}"
        if url not in seen:
            seen.add(url)
            ok.append(url)
    return ok, bad


def enrich_all(api: Callable[[list[str]], tuple[int, object]], urls: list[str], batch: int = 25) -> dict:
    """api(urls) -> (status, body). Returns {"results": [...], "rejected": [...], "unparseable": [...]}."""
    ok, unparseable = clean_profile_urls(urls)
    results, rejected = [], []

    def run(chunk: list[str]) -> None:
        status, body = api(chunk)
        if status == 200 and isinstance(body, list):
            results.extend(body)
        elif status == 400 and len(chunk) > 1:
            mid = len(chunk) // 2
            run(chunk[:mid])
            run(chunk[mid:])
        elif status == 400:
            rejected.extend(chunk)
        else:
            raise RuntimeError(f"enrich failed: HTTP {status}")

    for i in range(0, len(ok), batch):
        run(ok[i : i + batch])
    return {"results": results, "rejected": rejected, "unparseable": unparseable}


# 2. Company resolution ------------------------------------------------------------------------------------------
def reg_domain(host: str | None) -> str:
    host = (host or "").lower().strip().split(":")[0].rstrip(".")
    host = re.sub(r"^https?://", "", host).split("/")[0]
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in TWO_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def brand_from_page(page: dict) -> str | None:
    if page.get("site_name"):
        return page["site_name"].strip()
    head = re.split(r"\s+[—–|:\-·]\s*|:\s", page.get("title") or "")[0].strip()
    return head or None


def resolve_company(domain: str, identify: Callable[..., list], fetch: Callable[[str], dict | None]) -> dict:
    """identify(domains=[..]) / identify(names=[..]) -> [{"matched_on", "matches": [{"id","name","domain"}]}].
    fetch(domain) -> {"final_domain","title","site_name"} or None."""
    domain = reg_domain(domain)
    hits = (identify(domains=[domain]) or [{}])[0].get("matches") or []
    if hits:
        return {"query": domain, "id": hits[0]["id"], "name": hits[0]["name"], "how": "domain", "stale_domain": None}
    page = fetch(domain)
    brand = brand_from_page(page) if page else None
    if not brand:
        return {"query": domain, "id": None, "name": None, "how": "unresolved", "stale_domain": None, "brand": None}
    for cand in (identify(names=[brand]) or [{}])[0].get("matches") or []:
        old = cand.get("domain")
        if not old:
            continue
        p = fetch(reg_domain(old))
        if p and reg_domain(p.get("final_domain")) == domain:
            return {"query": domain, "id": cand["id"], "name": cand["name"], "how": "name+redirect",
                    "stale_domain": reg_domain(old), "brand": brand}
    return {"query": domain, "id": None, "name": None, "how": "unresolved", "stale_domain": None, "brand": brand}


# 3. Agent Search ------------------------------------------------------------------------------------------------
def flag_agent_rows(answer: dict) -> tuple[list[dict], list[dict]]:
    hard = {c["id"] for c in answer.get("constraints", []) if c.get("importance") == "hard"}
    kept, flagged = [], []
    for row in answer.get("rows", []):
        bad = [v["constraint_id"] for v in row.get("constraints", [])
               if v.get("constraint_id") in hard and v.get("assessment") == "violated"]
        (flagged if bad else kept).append({**row, "violated_hard": bad} if bad else row)
    return kept, flagged


# Live adapters --------------------------------------------------------------------------------------------------
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"


def live_fetch(domain: str) -> dict | None:
    for scheme in ("https", "http"):
        try:
            req = urllib.request.Request(f"{scheme}://{domain}/", headers={"user-agent": UA, "accept": "text/html"})
            with urllib.request.urlopen(req, timeout=15) as r:
                raw, final = r.read(300_000).decode("utf-8", "replace"), r.url
            t = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
            og = re.search(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']*)', raw, re.I)
            return {"final_domain": reg_domain(urllib.parse.urlparse(final).hostname),
                    "title": html.unescape(t.group(1).strip()) if t else "",
                    "site_name": html.unescape(og.group(1)) if og else ""}
        except Exception:  # noqa: BLE001
            continue
    return None


class ApiError(RuntimeError):
    """A failed call. Never read as "no match": an error is not an answer."""


def live_identify(call: Callable[..., dict], sleep: Callable[[float], None] = time.sleep, retries: int = 3) -> Callable[..., list]:
    """Wrap cd.call (or any (method, path, body, tag=) -> {"status","body","headers"}) into the identify() shape.
    429: wait (X-RateLimit-Reset seconds, else 60) and retry. Any other non-200, or a non-list body: raise ApiError."""

    def identify(domains=None, names=None):
        body = {"domains": domains} if domains else {"names": names}
        for attempt in range(retries + 1):
            r = call("POST", "/company/identify", body, tag="freshcheck-identify")
            if r["status"] != 429 or attempt == retries:
                break
            sleep(float((r.get("headers") or {}).get("x-ratelimit-reset") or 60))
        if r["status"] != 200 or not isinstance(r["body"], list):
            raise ApiError(f"identify HTTP {r['status']}: {str(r['body'])[:200]}")
        out = []
        for e in r["body"]:
            ms = []
            for m in e.get("matches") or []:
                bi = (m.get("company_data") or m).get("basic_info") or {}
                ms.append({"id": (m.get("company_data") or m).get("crustdata_company_id") or bi.get("crustdata_company_id"),
                           "name": bi.get("name"), "domain": bi.get("primary_domain")})
            out.append({"matched_on": e.get("matched_on"), "matches": ms})
        return out

    return identify
