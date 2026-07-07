#!/usr/bin/env python3
"""
job_watch.py  (v2)  - personal luxury / CRM / brand / client-dev job poller.

TWO engines in one:
  A) AGGREGATORS (wide net): Adzuna + Reed search across most of the UK job
     market by keyword. This is the "search everything relevant" half.
  B) COMPANY WATCH (targeted): checks specific brands' own career pages via
     their public ATS APIs (Greenhouse, Lever, SmartRecruiters, Ashby).

It filters to roles that fit you, skips ones already seen, and reports only
NEW matches. No LinkedIn, no dependencies. Standard library only.

USAGE
    python3 job_watch.py            # normal run
    python3 job_watch.py --all      # show all matches, ignore "seen" file
    python3 job_watch.py --check    # validate which company slugs are live

KEYS (added as GitHub Secrets, not pasted in this file):
    ADZUNA_APP_ID, ADZUNA_APP_KEY   -> from developer.adzuna.com
    REED_KEY                        -> from reed.co.uk/developers
If a key is missing, that source is simply skipped - the rest still runs.
"""

import os, sys, csv, ssl, json, base64, smtplib
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone
from email.mime.text import MIMEText

# ---------------------------------------------------------------------------
# 1. WHAT YOU'RE LOOKING FOR
# ---------------------------------------------------------------------------
SEARCH_TERMS = [
    "luxury marketing", "brand marketing", "crm executive", "crm manager",
    "client development", "clienteling", "brand executive", "marketing executive",
    "partnerships marketing", "loyalty crm", "ecommerce marketing",
    "brand strategy", "customer relationship marketing",
]

INCLUDE = [
    "crm", "brand", "marketing", "client development", "clienteling",
    "client", "partnership", "loyalty", "ecommerce", "e-commerce",
    "communications", "content", "consumer", "customer relationship",
]
EXCLUDE = [
    "sales associate", "store", "boutique", "retail associate", "shop floor",
    "concierge", "night", "warehouse", "hr ", "human resources", "recruit",
    "talent acquisition", "engineer", "developer", "software", "data engineer",
    "stylist", "beauty advisor", "keyholder", "supervisor", "cleaner",
    "security", "driver", "assistant manager retail",
]
LOCATIONS = [
    "united kingdom", "uk", "london", "england", "britain", "manchester",
    "birmingham", "leeds", "glasgow", "edinburgh",
    "dubai", "uae", "united arab emirates", "abu dhabi", "remote",
]

# ---------------------------------------------------------------------------
# 2. COMPANIES TO WATCH DIRECTLY  (name, ATS, slug)
# ---------------------------------------------------------------------------
TARGETS = [
    ("Trinny London",        "greenhouse",      "trinnylondon"),
    ("Missoma",              "greenhouse",      "missoma"),
    ("Monica Vinader",       "greenhouse",      "monicavinader"),
    ("Farfetch",             "greenhouse",      "farfetch"),
    ("Vestiaire Collective", "lever",           "vestiairecollective"),
    ("Mulberry",             "smartrecruiters", "Mulberry"),
    ("Chalhoub Group",       "smartrecruiters", "ChalhoubGroup"),
    ("Ten Lifestyle Group",  "greenhouse",      "tengroup"),
]

SEEN_FILE = "seen_jobs.json"
CSV_FILE = "matches.csv"
UA = {"User-Agent": "personal-job-watcher/2.0"}


def _get(url, headers=None):
    h = dict(UA)
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=25, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_adzuna():
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        return [], "Adzuna: no keys set (skipped)"
    out = []
    for term in SEARCH_TERMS:
        params = urllib.parse.urlencode({
            "app_id": app_id, "app_key": app_key,
            "what_phrase": term, "results_per_page": 50,
            "content-type": "application/json", "sort_by": "date",
        })
        url = f"https://api.adzuna.com/v1/api/jobs/gb/search/1?{params}"
        try:
            data = _get(url)
            for j in data.get("results", []):
                loc = (j.get("location") or {}).get("display_name", "") or ""
                out.append({"title": j.get("title", ""), "location": loc,
                            "url": j.get("redirect_url", "")})
        except Exception:
            continue
    return out, f"Adzuna: {len(out)} raw results"


def fetch_reed():
    key = os.environ.get("REED_KEY")
    if not key:
        return [], "Reed: no key set (skipped)"
    token = base64.b64encode(f"{key}:".encode()).decode()
    auth = {"Authorization": f"Basic {token}"}
    out = []
    for term in SEARCH_TERMS:
        params = urllib.parse.urlencode({"keywords": term, "resultsToTake": 50})
        url = f"https://www.reed.co.uk/api/1.0/search?{params}"
        try:
            data = _get(url, headers=auth)
            for j in data.get("results", []):
                loc = j.get("locationName", "") or ""
                out.append({"title": j.get("jobTitle", ""), "location": loc,
                            "url": j.get("jobUrl", "")})
        except Exception:
            continue
    return out, f"Reed: {len(out)} raw results"


def fetch_greenhouse(slug):
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false")
    return [{"title": j.get("title", ""),
             "location": (j.get("location") or {}).get("name", "") or "",
             "url": j.get("absolute_url", "")} for j in data.get("jobs", [])]


def fetch_lever(slug):
    data = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    return [{"title": j.get("text", ""),
             "location": (j.get("categories") or {}).get("location", "") or "",
             "url": j.get("hostedUrl", "")} for j in data]


def fetch_smartrecruiters(slug):
    data = _get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
    out = []
    for j in data.get("content", []):
        loc = j.get("location", {}) or {}
        loc_str = ", ".join(filter(None, [loc.get("city"), loc.get("country")]))
        out.append({"title": j.get("name", ""), "location": loc_str,
                    "url": f"https://jobs.smartrecruiters.com/{slug}/{j.get('id','')}"})
    return out


def fetch_ashby(slug):
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false")
    return [{"title": j.get("title", ""), "location": j.get("location", "") or "",
             "url": j.get("jobUrl", "")} for j in data.get("jobs", [])]


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever,
            "smartrecruiters": fetch_smartrecruiters, "ashby": fetch_ashby}


def is_match(title, location):
    t, l = title.lower(), location.lower()
    if not any(w in t for w in INCLUDE):
        return False
    if any(w in t for w in EXCLUDE):
        return False
    if LOCATIONS and not any(w in l for w in LOCATIONS):
        return False
    return True


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)


def main():
    check_only = "--check" in sys.argv
    show_all = "--all" in sys.argv
    seen = set() if show_all else load_seen()
    matches, notes, live, dead = [], [], [], []

    for name, ats, slug in TARGETS:
        fetcher = FETCHERS.get(ats)
        if not fetcher:
            dead.append(f"{name}: unknown ATS '{ats}'"); continue
        try:
            jobs = fetcher(slug)
            live.append(f"{name} ({ats}:{slug}) - {len(jobs)} roles")
            if not check_only:
                for job in jobs:
                    _consider(job, name, seen, matches)
        except Exception as e:
            dead.append(f"{name} ({ats}:{slug}) - {type(e).__name__}")

    if check_only:
        print("LIVE company slugs:")
        for x in live: print("  +", x)
        if dead:
            print("\nBROKEN (fix or remove in TARGETS):")
            for x in dead: print("  -", x)
        return

    for fetch in (fetch_adzuna, fetch_reed):
        jobs, note = fetch()
        notes.append(note)
        for job in jobs:
            _consider(job, "Aggregator", seen, matches)

    for n in notes: print("·", n)
    if dead:
        print("Skipped companies:", "; ".join(dead))
    print()

    if not matches:
        print("No new matching roles this run.")
        save_seen(seen); return

    lines = [f"{len(matches)} new role(s) - {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}\n"]
    for m in matches:
        tag = "" if m["company"] == "Aggregator" else f"[{m['company']}] "
        lines.append(f"- {tag}{m['title']}  |  {m['location']}\n  {m['url']}")
    report = "\n".join(lines)
    print(report)

    new_file = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["found_at", "company", "title", "location", "url"])
        stamp = datetime.now(timezone.utc).isoformat()
        for m in matches:
            w.writerow([stamp, m["company"], m["title"], m["location"], m["url"]])

    maybe_email(report)
    save_seen(seen)


def _consider(job, company, seen, matches):
    if not is_match(job.get("title", ""), job.get("location", "")):
        return
    key = job.get("url") or f"{company}|{job.get('title','')}"
    if key in seen:
        return
    seen.add(key)
    matches.append({"company": company, **job})


def maybe_email(report):
    to = os.environ.get("EMAIL_TO"); host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER"); pw = os.environ.get("SMTP_PASS")
    if not (to and host and user and pw):
        return
    msg = MIMEText(report)
    msg["Subject"] = "New luxury/CRM roles"; msg["From"] = user; msg["To"] = to
    with smtplib.SMTP_SSL(host, int(os.environ.get("SMTP_PORT", "465"))) as s:
        s.login(user, pw); s.send_message(msg)
    print(f"\n(emailed to {to})")


if __name__ == "__main__":
    main()
