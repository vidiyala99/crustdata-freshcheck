# freshcheck

Small guards for building on [Crustdata](https://crustdata.com)'s people and company APIs (`x-api-version: 2025-11-01`).
Python standard library only. Bring your own client: every function takes the API call as an argument, so it works
with any HTTP wrapper and is tested without network.

Written after running Crustdata on a real event guest list (172 people) and on 300 recent YC companies from its own
index, on a self-serve key, 2026-11-19. Write-up: https://crustdata-field-test.vercel.app

## What it guards against

**1. One bad URL fails the whole batch.** `POST /person/enrich` returns HTTP 400 for all 25 URLs when one of them is
malformed. `clean_profile_urls` normalizes and dedupes LinkedIn profile URLs (and repairs a stray space after `/in/`);
`enrich_all` batches by 25 and, if a batch still comes back 400, bisects it so one bad URL costs one URL.

```python
from freshcheck import enrich_all

def api(urls):                      # your client: returns (status, body)
    r = client.post("/person/enrich", {"professional_network_profile_urls": urls})
    return r.status_code, r.json()

out = enrich_all(api, urls)         # {"results": [...], "rejected": [...], "unparseable": [...]}
```

**2. A startup that moved can't be found by the domain it uses today.** In the 300-company sample, 11 had moved to a
new domain; `POST /company/identify` on the new domain found the same record for 2, nothing for 7 and a separate record
for 2. `resolve_company` tries the domain first; on a miss it reads the brand from the live homepage, identifies by
name, and accepts a candidate only if that candidate's indexed domain redirects to the domain you asked about. No
redirect proof, no answer: it never returns a higher-scoring namesake as a guess. On the 7 misses it recovered 5.

```python
from freshcheck import resolve_company, live_fetch, live_identify

identify = live_identify(my_call)   # my_call(method, path, body, tag=...) -> {"status", "body", "headers"}
resolve_company("hexclave.com", identify=identify, fetch=live_fetch)
# {"id": ..., "name": "Hexclave (YC S24)", "how": "name+redirect", "stale_domain": "stack-auth.com", ...}
```

`live_identify` waits on HTTP 429 for `X-RateLimit-Reset` seconds and raises `ApiError` on any other failure. An error
is never read as "no match".

**3. Agent Search rows that break a hard constraint.** A row can come back with `assessment: "violated"` on a
constraint whose `importance` is `hard`. `flag_agent_rows(answer)` returns `(kept, flagged)`, with the violated
constraint ids on each flagged row.

## Tests

```
python -m unittest discover -s tests -t .
```

12 tests, no network. The guards were checked by breaking each one (redirect proof, hard-constraint filter, batch
bisect) and confirming a test fails.

## License

MIT
