#!/usr/bin/env python3
"""Fetch recent wildlife sightings (iNaturalist + eBird) and flag the unusual ones.

Writes docs/data.json for the static page. Standard library only.

Flags
  new         species with no research-grade iNaturalist record in the area during the baseline period
  rare        species with only a handful of baseline records
  off_season  species that is usually here, but not at this time of year
  notable     eBird's own "notable" (locally/nationally rare) flag
"""
import datetime as dt
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
INAT = os.environ.get("INAT_BASE", "https://api.inaturalist.org/v1")
EBIRD = os.environ.get("EBIRD_BASE", "https://api.ebird.org/v2")
EBIRD_KEY = os.environ.get("EBIRD_API_KEY", "")
SLEEP = float(os.environ.get("API_SLEEP", "1.1"))  # iNat asks for <= ~60 requests/min
UA = "wildlife-watch-dashboard/1.0 (personal GitHub Pages project)"
RANKS_OK = {"species", "subspecies", "variety", "hybrid", "form"}
STATE = ROOT / "state"
DOCS = ROOT / "docs"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def load(path, default):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, obj):
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=False) + "\n")


def http_json(url, params=None, headers=None, retries=4):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    hdrs.update(headers or {})
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = 5 * (2 ** attempt)
                log(f"  HTTP {e.code}, retrying in {wait}s")
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(5 * (2 ** attempt))
                continue
            raise


def inat(path, params):
    time.sleep(SLEEP)
    return http_json(f"{INAT}{path}", params)


# --------------------------------------------------------------------------- taxa
def resolve_taxa(names, cache):
    ids = []
    for name in names:
        key = name.lower()
        if key not in cache:
            tid = None
            d = inat("/taxa", {"q": name, "per_page": 15, "is_active": "true"})
            for r in d.get("results", []):
                if r.get("name", "").lower() == key:
                    tid = r["id"]
                    break
            cache[key] = tid
            log(f"  taxon {name!r} -> {tid}")
        if cache[key]:
            ids.append(cache[key])
        else:
            log(f"  WARNING: could not resolve taxon {name!r}; skipping it")
    return ids


# ---------------------------------------------------------------- iNat helpers
def area_params(site):
    return {
        "lat": site["lat"],
        "lng": site["lng"],
        "radius": round(site["radius_miles"] * 1.609344, 2),
    }


def species_counts(params):
    out, page = {}, 1
    while True:
        p = dict(params, per_page=500, page=page, lrank="species")
        d = inat("/observations/species_counts", p)
        res = d.get("results", [])
        for r in res:
            t = r.get("taxon") or {}
            if t.get("rank") not in RANKS_OK:
                continue
            out[t["id"]] = {
                "count": r["count"],
                "sci": t.get("name"),
                "common": t.get("preferred_common_name"),
            }
        if len(res) < 500 or page >= 40:
            break
        page += 1
    return out


def parse_obs(o):
    t = o.get("taxon")
    if not t or not o.get("location"):
        return None
    try:
        lat, lng = (float(x) for x in o["location"].split(","))
    except ValueError:
        return None
    photo = None
    for p in o.get("photos") or []:
        # Only show photos that carry a Creative Commons style licence.
        if p.get("license_code") and p.get("url"):
            photo = {
                "url": p["url"].replace("square", "medium"),
                "attribution": p.get("attribution"),
                "license": p["license_code"],
            }
            break
    return t["id"], {
        "source": "inat",
        "date": o.get("observed_on") or (o.get("time_observed_at") or "")[:10],
        "place": o.get("place_guess"),
        "lat": lat,
        "lng": lng,
        "url": o.get("uri") or f"https://www.inaturalist.org/observations/{o['id']}",
        "quality": o.get("quality_grade"),
        "obscured": bool(o.get("obscured")),
    }, photo


def fetch_observations(base, tids, d1, d2):
    found = {}
    for i in range(0, len(tids), 20):
        chunk = tids[i:i + 20]
        page = 1
        while True:
            p = dict(base, taxon_id=",".join(map(str, chunk)), d1=d1, d2=d2,
                     quality_grade="research,needs_id", per_page=200, page=page,
                     order_by="observed_on", order="desc")
            p.pop("without_taxon_id", None)
            d = inat("/observations", p)
            res = d.get("results", [])
            for o in res:
                parsed = parse_obs(o)
                if parsed:
                    tid, sighting, photo = parsed
                    e = found.setdefault(tid, {"sightings": [], "photo": None})
                    e["sightings"].append(sighting)
                    if photo and not e["photo"]:
                        e["photo"] = photo
            if len(res) < 200 or page >= 5:
                break
            page += 1
    return found


def months_around(dates):
    ms = set()
    for d in dates:
        for k in (-1, 0, 1):
            ms.add((d.month - 1 + k) % 12 + 1)
    return sorted(ms)


# -------------------------------------------------------------------- eBird
def ebird_notable(site, back):
    if not EBIRD_KEY:
        log("  EBIRD_API_KEY not set - skipping eBird (iNaturalist birds only)")
        return {}
    km = min(50, int(round(site["radius_miles"] * 1.609344)))
    d = http_json(
        f"{EBIRD}/data/obs/geo/recent/notable",
        {"lat": site["lat"], "lng": site["lng"], "dist": km, "back": min(back, 30), "detail": "full"},
        headers={"X-eBirdApiToken": EBIRD_KEY},
    )
    out = {}
    for r in d:
        sci = (r.get("sciName") or "").strip()
        if not sci or r.get("lat") is None:
            continue
        s = {
            "source": "ebird",
            "date": (r.get("obsDt") or "")[:10],
            "time": r.get("obsDt"),
            "place": r.get("locName"),
            "lat": r["lat"],
            "lng": r["lng"],
            "count": r.get("howMany"),
            "url": f"https://ebird.org/checklist/{r['subId']}" if r.get("subId") else None,
            "quality": "confirmed" if r.get("obsValid") and r.get("obsReviewed") else "unreviewed",
        }
        e = out.setdefault(sci.lower(), {
            "sci": sci, "common": r.get("comName"), "code": r.get("speciesCode"), "sightings": []})
        e["sightings"].append(s)
    return out


# ------------------------------------------------------------------- one group
def build_group(g, cfg, site, taxa_cache, baseline_cache, history, today):
    log(f"[{g['id']}]")
    window = cfg.get("window_days", 14)
    years = cfg.get("baseline_years", 5)
    rare_max = cfg.get("rare_threshold", 2)
    off_min = cfg.get("offseason_min_baseline", 5)
    recent_start = today - dt.timedelta(days=window)
    base_end = recent_start - dt.timedelta(days=1)
    base_start = today.replace(year=today.year - years) if not (today.month == 2 and today.day == 29) \
        else today.replace(year=today.year - years, day=28)

    base = area_params(site)
    inc = resolve_taxa(g["include"], taxa_cache)
    if not inc:
        raise RuntimeError("no taxa resolved for include list")
    base["taxon_id"] = ",".join(map(str, inc))
    exc = resolve_taxa(g.get("exclude", []), taxa_cache)
    if exc:
        base["without_taxon_id"] = ",".join(map(str, exc))

    months = months_around([recent_start, today])
    cache_key = json.dumps([base, years, months, base_start.isoformat()[:7]], sort_keys=True)
    bc = baseline_cache.get(g["id"])
    fresh = bc and bc.get("key") == cache_key and \
        (today - dt.date.fromisoformat(bc["fetched"])).days < cfg.get("baseline_refresh_days", 7)
    if fresh:
        log("  baseline: using cache")
        b_all = {int(k): v for k, v in bc["all"].items()}
        b_month = {int(k): v for k, v in bc["month"].items()}
    else:
        log("  baseline: fetching")
        win = dict(base, d1=base_start.isoformat(), d2=base_end.isoformat(), quality_grade="research")
        b_all = {k: v["count"] for k, v in species_counts(win).items()}
        b_month = {k: v["count"] for k, v in species_counts(dict(win, month=",".join(map(str, months)))).items()}
        baseline_cache[g["id"]] = {"key": cache_key, "fetched": today.isoformat(),
                                   "all": b_all, "month": b_month}

    log("  recent species")
    recent = species_counts(dict(base, d1=recent_start.isoformat(), d2=today.isoformat(),
                                 quality_grade="research,needs_id"))
    items = {}
    flagged = []
    for tid, info in recent.items():
        b = b_all.get(tid, 0)
        flags = []
        if b == 0:
            flags.append("new")
        elif b <= rare_max:
            flags.append("rare")
        if b >= off_min and b_month.get(tid, 0) == 0:
            flags.append("off_season")
        if flags:
            flagged.append(tid)
            items[info["sci"].lower()] = {
                "taxon_id": tid, "sci": info["sci"], "common": info["common"] or info["sci"],
                "flags": flags, "baseline_count": b, "recent_count": info["count"],
                "sources": ["inat"], "photo": None, "sightings": [],
            }
    log(f"  {len(recent)} species recently, {len(flagged)} flagged")

    if flagged:
        obs = fetch_observations(base, flagged, recent_start.isoformat(), today.isoformat())
        for it in list(items.values()):
            e = obs.get(it["taxon_id"])
            if not e:
                continue
            it["sightings"] = e["sightings"]
            it["photo"] = e["photo"]
            if all(s["quality"] != "research" for s in e["sightings"]):
                it["unconfirmed"] = True

    if g.get("ebird"):
        log("  eBird notable")
        for key, e in ebird_notable(site, window).items():
            it = items.get(key)
            if it is None:
                it = items[key] = {
                    "taxon_id": None, "sci": e["sci"], "common": e["common"], "flags": [],
                    "baseline_count": None, "recent_count": 0, "sources": [], "photo": None,
                    "sightings": [],
                }
            if "notable" not in it["flags"]:
                it["flags"].append("notable")
            if "ebird" not in it["sources"]:
                it["sources"].append("ebird")
            it["code"] = e["code"]
            it["sightings"].extend(e["sightings"])

    out = []
    for key, it in items.items():
        it["sightings"].sort(key=lambda s: s.get("time") or s.get("date") or "", reverse=True)
        it["sightings"] = it["sightings"][:10]
        it["latest"] = max((s.get("date") or "" for s in it["sightings"]), default="")
        hk = f"{g['id']}:{key}"
        history.setdefault(hk, {"first_flagged": today.isoformat()})["last_seen"] = today.isoformat()
        it["first_flagged"] = history[hk]["first_flagged"]
        out.append(it)

    return {"id": g["id"], "label": g["label"], "emoji": g.get("emoji", ""),
            "recent_species_total": len(recent), "items": out}


def main():
    cfg = load(ROOT / "config.json", None)
    if not cfg:
        log("config.json missing")
        return 2
    site = cfg["site"]
    today = dt.datetime.now(dt.timezone.utc).date()
    taxa_cache = load(STATE / "taxa.json", {})
    baseline_cache = load(STATE / "baseline.json", {})
    history = load(STATE / "history.json", {})
    prev = {g["id"]: g for g in load(DOCS / "data.json", {}).get("groups", [])}

    groups, failures = [], 0
    for g in cfg["groups"]:
        try:
            groups.append(build_group(g, cfg, site, taxa_cache, baseline_cache, history, today))
        except Exception as e:  # keep the page alive with last good data
            failures += 1
            log(f"  FAILED {g['id']}: {type(e).__name__}: {e}")
            if g["id"] in prev:
                old = prev[g["id"]]
                old["stale"] = True
                groups.append(old)
            else:
                groups.append({"id": g["id"], "label": g["label"], "emoji": g.get("emoji", ""),
                               "recent_species_total": 0, "items": [], "stale": True})

    cutoff = (today - dt.timedelta(days=120)).isoformat()
    history = {k: v for k, v in history.items() if v.get("last_seen", "") >= cutoff}

    save(STATE / "taxa.json", taxa_cache)
    save(STATE / "baseline.json", baseline_cache)
    save(STATE / "history.json", history)
    save(DOCS / "data.json", {
        "site": {**site, "radius_km": round(site["radius_miles"] * 1.609344, 1)},
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "window_days": cfg.get("window_days", 14),
        "baseline_years": cfg.get("baseline_years", 5),
        "groups": groups,
    })
    log(f"done; {failures} group(s) failed")
    return 1 if failures == len(cfg["groups"]) else 0


if __name__ == "__main__":
    sys.exit(main())
