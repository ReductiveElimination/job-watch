#!/usr/bin/env python3
"""
job_watch.py - a personal luxury/CRM/brand job poller.

Checks your target companies' career pages directly via their public ATS APIs
(Greenhouse, Lever, SmartRecruiters, Ashby), filters to roles that fit you,
skips ones you've already seen, and reports only NEW matches.

No LinkedIn, no Indeed, no dependencies. Pure standard library.

USAGE
    python3 job_watch.py            # normal run, reports new matches
    python3 job_watch.py --all      # ignore the "seen" file, show everything matching
    python3 job_watch.py --check     # just validate which company slugs are live

Set EMAIL_TO / SMTP_* env vars (see bottom) to get results emailed instead of printed.
Runs fine on a laptop or free on GitHub Actions (see job_watch.yml).
"""

import json
import os
import re
import sys
import csv
import ssl
import smtplib
import urllib.request
import urllib.error
from datetime import datetime, timezone
from email.mime.text import MIMEText

# ---------------------------------------------------------------------------
# 1. YOUR TARGET COMPANIES
# ---------------------------------------------------------------------------
# Each entry: (Display name, ATS, slug)
#   ATS is one of: greenhouse | lever | smartrecruiters | ashby
#
# HOW TO FIND A COMPANY'S ATS + SLUG (takes ~10 seconds each):
#   Open the brand's "Careers" / "Jobs" page and look at the URL it lands on:
#     job-boards.greenhouse.io/COMPANY   or boards.greenhouse.io/COMPANY  -> greenhouse, slug=COMPANY
#     jobs.lever.co/COMPANY                                               -> lever,      slug=COMPANY
#     jobs.smartrecruiters.com/COMPANY                                    -> smartrecruiters, slug=COMPANY
#     jobs.ashbyhq.com/COMPANY                                            -> ashby,      slug=COMPANY
#   If it's a bespoke site, Workday, or Welcome-to-the-Jungle (e.g. John Paul),
#   there's no clean public API - keep those on email alerts instead.
#
# The slugs below are STARTERS/GUESSES. Run `python3 job_watch.py --check`
# and it'll tell you which resolve. Fix or delete the ones that 404, then
# paste in your real targets. The script only reports jobs from live slugs.
TARGETS = [
    # name,                    ats,              slug
    ("Trinny London",          "greenhouse",     "trinnylondon"),
    ("Missoma",                "greenhouse",     "missoma"),
    ("Monica Vinader",         "greenhouse",     "monicavinader"),
    ("Mulberry",               "smartrecruiters","Mulberry"),
    ("Farfetch",               "greenhouse",     "farfetch"),
    ("Vestiaire Collective",   "lever",          "vestiairecollective"),
    ("Ten Lifestyle Group",    "greenhouse",     "tengroup"),
    # add your own below, then run --check to validate:
    # ("Chalhoub Group",       "smartrecruiters","ChalhoubGroup"),
    # ("Bremont",              "greenhouse",     "bremont"),
]

# ---------------------------------------------------------------------------
# 2. WHAT COUNTS AS A MATCH (edit freely)
# ---------------------------------------------------------------------------
# A role must contain at least one INCLUDE word in its title...
INCLUDE = [
    "crm", "brand", "marketing", "client development", "clienteling",
    "client", "partnership", "loyalty", "ecommerce", "e-commerce",
    "communications", "content", "consumer", "customer relationship",
]
# ...and NONE of these EXCLUDE words (your hard no's: retail floor, HR, tech, etc.)
EXCLUDE = [
    "sales associate", "store", "boutique", "retail associate", "shop",
    "concierge", "night", "warehouse", "hr ", "human resources",
    "recruit", "talent acquisition", "engineer", "developer", "software",
    "data engineer", "stylist", "beauty advisor", "keyholder", "supervisor",
]
# ...and its location must contain one of these (leave [] to allow anywhere).
LOCATIONS = [
    "united kingdom", "uk", "london", "england", "britain",
    "dubai", "uae", "united arab emirates", "abu dhabi",
    "remote",
]

SEEN_FILE = "seen_jobs.json"
CSV_FILE = "matches.csv"
UA = {"User-Agent": "personal-job-watcher/1.0"}


# ---------------------------------------------------------------------------
# 3. ATS FETCHERS  (each returns a list of {title, location, url})
# ---------------------------------------------------------------------------
def _get(url):
    req = urllib.request.Request(url, headers=UA)
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_greenhouse(slug):
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false")
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "") or ""
        out.append({"title": j.get("title", ""), "location": loc,
                    "url": j.get("absolute_url", "")})
    return out


def fetch_lever(slug):
    data = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for j in data:
        loc = (j.get("categories") or {}).get("location", "") or ""
        out.append({"title": j.get("text", ""), "location": loc,
                    "url": j.get("hostedUrl", "")})
    return out


def fetch_smartrecruiters(slug):
    data = _get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
    out = []
    for j in data.get("content", []):
        loc = j.get("location", {}) or {}
        loc_str = ", ".join(filter(None, [loc.get("city"), loc.get("country")]))
        job_id = j.get("id", "")
        out.append({"title": j.get("name", ""), "location": loc_str,
                    "url": f"https://jobs.smartrecruiters.com/{slug}/{job_id}"})
    return out


def fetch_ashby(slug):
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false")
    out = []
    for j in data.get("jobs", []):
        out.append({"title": j.get("title", ""), "location": j.get("location", "") or "",
                    "url": j.get("jobUrl", "")})
    return out


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "smartrecruiters": fetch_smartrecruiters,
    "ashby": fetch_ashby,
}


# ---------------------------------------------------------------------------
# 4. FILTER LOGIC
# ---------------------------------------------------------------------------
def matches(title, location):
    t = title.lower()
    l = location.lower()
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


# ---------------------------------------------------------------------------
# 5. MAIN
# ---------------------------------------------------------------------------
def main():
    check_only = "--check" in sys.argv
    show_all = "--all" in sys.argv

    seen = set() if show_all else load_seen()
    new_matches = []
    live, dead = [], []

    for name, ats, slug in TARGETS:
        fetcher = FETCHERS.get(ats)
        if not fetcher:
            print(f"  ! {name}: unknown ATS '{ats}'")
            continue
        try:
            jobs = fetcher(slug)
            live.append(f"{name} ({ats}:{slug}) - {len(jobs)} roles")
            if check_only:
                continue
            for job in jobs:
                if not matches(job["title"], job["location"]):
                    continue
                key = job["url"] or f"{name}|{job['title']}"
                if key in seen:
                    continue
                seen.add(key)
                new_matches.append({"company": name, **job})
        except (urllib.error.HTTPError, urllib.error.URLError, Exception) as e:
            dead.append(f"{name} ({ats}:{slug}) - {type(e).__name__}: {e}")

    # --check mode: just report config health
    if check_only:
        print("LIVE slugs:")
        for x in live:
            print("  +", x)
        if dead:
            print("\nBROKEN slugs (fix or remove these):")
            for x in dead:
                print("  -", x)
        return

    if dead:
        print("Skipped (slug not resolving - fix in TARGETS):")
        for x in dead:
            print("  -", x)
        print()

    # Report new matches
    if not new_matches:
        print("No new matching roles this run.")
        save_seen(seen)
        return

    lines = [f"{len(new_matches)} new role(s) - {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}\n"]
    for m in new_matches:
        lines.append(f"- [{m['company']}] {m['title']}  |  {m['location']}\n  {m['url']}")
    report = "\n".join(lines)
    print(report)

    # Append to CSV
    new_file = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["found_at", "company", "title", "location", "url"])
        stamp = datetime.now(timezone.utc).isoformat()
        for m in new_matches:
            w.writerow([stamp, m["company"], m["title"], m["location"], m["url"]])

    # Optional email
    maybe_email(report)
    save_seen(seen)


def maybe_email(report):
    to = os.environ.get("EMAIL_TO")
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    if not (to and host and user and pw):
        return
    msg = MIMEText(report)
    msg["Subject"] = "New luxury/CRM roles"
    msg["From"] = user
    msg["To"] = to
    port = int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port) as s:
        s.login(user, pw)
        s.send_message(msg)
    print(f"\n(emailed to {to})")


if __name__ == "__main__":
    main()
