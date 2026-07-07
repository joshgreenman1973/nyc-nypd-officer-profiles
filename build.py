#!/usr/bin/env python3
"""
Build curated JSON for the NYPD Officer Profiles explorer from NYC Open Data (SODA).

Source datasets (all keyed on profile_id, refreshed weekly by the NYPD):
  pmsy-ewrc  Members of Service (the roster)          ~34k rows
  sh6y-4tgb  Title / Shield History                   ~53k rows  (fetched live per officer)
  n3mp-t5uj  Training                                 ~12.7M rows (aggregate only)
  i9n8-a8ed  Department Recognition (awards)          ~141k rows
  wq9a-qu9a  Disciplinary History Summary             ~1.5k rows (recomputed here)
  uafj-ik29  Disciplinary History Charges             ~3.9k rows

Outputs land in ./data as compact JSON the static site loads directly.
No API token required (anonymous SODA is rate-limited but fine for this volume).
"""

import json, re, time, urllib.parse, urllib.request
from collections import defaultdict, Counter
from pathlib import Path

BASE = "https://data.cityofnewyork.us/resource"
OUT = Path(__file__).parent / "data"
OUT.mkdir(exist_ok=True)

HIGH_HONORS = [
    "MEDAL OF HONOR", "MEDAL FOR VALOR", "POLICE COMBAT CROSS",
    "EXCEPTIONAL MERIT", "MEDAL FOR MERIT", "PURPLE SHIELD MEDAL",
]

def soda(dataset, params, geojson=False):
    """Fetch from a SODA endpoint with basic retry."""
    ext = "geojson" if geojson else "json"
    url = f"{BASE}/{dataset}.{ext}?" + urllib.parse.urlencode(params)
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nypd-profiles-build/1.0"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except Exception as e:
            print(f"  retry {attempt+1} ({e})")
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"failed: {url}")

def to_int(v):
    try: return int(float(v))
    except (TypeError, ValueError): return 0

def year_of(iso):
    return to_int(iso[:4]) if iso else 0

PRECINCT_RE = re.compile(r"^(\d{1,3})\s*(?:PCT|PRECINCT)")
def precinct_of(command):
    if not command: return None
    c = command.strip().upper()
    if c.startswith("CENTRAL PARK"): return 22   # 22nd Precinct = Central Park (no number in the command string)
    m = PRECINCT_RE.match(c)
    return to_int(m.group(1)) if m else None

# --------------------------------------------------------------------------
print("1/7  Roster (Members of Service)…")
roster = soda("pmsy-ewrc", {
    "$select": "profile_id,name,rank,command,appointment_date,arrests_total,department_recognitions,export_date",
    "$limit": 60000,
})
print(f"     {len(roster):,} officers")
export_date = (roster[0].get("export_date") or "")[:10] if roster else ""

# --------------------------------------------------------------------------
print("2/7  Disciplinary charges…")
charges = soda("uafj-ik29", {"$select": "profile_id,date,case_number,charge_description,disposition,penalty_and_quantity", "$limit": 60000})
print(f"     {len(charges):,} charges")

sustained = Counter(c["profile_id"] for c in charges if c.get("profile_id"))

# --------------------------------------------------------------------------
print("3/7  Recognition records (awards + distinct recipients)…")
recog_all = soda("i9n8-a8ed", {"$select": "profile_id,award", "$limit": 300000})
print(f"     {len(recog_all):,} award records")
award_ct = Counter(r["award"] for r in recog_all if r.get("award"))
award_rc = defaultdict(set)  # award -> set of officers who hold it
for r in recog_all:
    if r.get("award"):
        award_rc[r["award"]].add(r.get("profile_id"))
# award_summary: how many times each award was given AND how many distinct officers hold at least one
award_summary = [{"award": a, "n": award_ct[a], "officers": len(award_rc[a])} for a, _ in award_ct.most_common()]

honor_rows = soda("i9n8-a8ed", {
    "$select": "profile_id,award,date",
    "$where": "award in(" + ",".join("'%s'" % h for h in HIGH_HONORS) + ")",
    "$limit": 5000,
})
print(f"     {len(honor_rows):,} high-honor awards")

# --------------------------------------------------------------------------
print("4/7  Training aggregate…")
training_agg = soda("n3mp-t5uj", {"$select": "training,count(*) as n", "$group": "training", "$order": "n desc", "$limit": 60})
training_summary = [{"training": t["training"], "n": to_int(t["n"])} for t in training_agg if t.get("training")]

# --------------------------------------------------------------------------
print("5/7  Precinct boundaries…")
precincts_geo = soda("y76i-bdw7", {"$limit": 200}, geojson=True)

# --------------------------------------------------------------------------
print("6/7  Assembling roster + per-precinct stats…")
by_id = {}
COLS = ["profile_id", "name", "rank", "command", "year", "arrests", "recognitions", "charges", "precinct"]
rows = []
pct_stat = defaultdict(lambda: {"n": 0, "arrests": 0, "recognitions": 0, "charges": 0, "disciplined": 0})
rank_counter = Counter()

for o in roster:
    pid = o.get("profile_id")
    pct = precinct_of(o.get("command"))
    arr = to_int(o.get("arrests_total"))
    rec = to_int(o.get("department_recognitions"))
    ch = sustained.get(pid, 0)
    yr = year_of(o.get("appointment_date"))
    rank = o.get("rank") or "—"
    rank_counter[rank] += 1
    rows.append([pid, o.get("name") or "—", rank, o.get("command") or "—", yr, arr, rec, ch, pct])
    by_id[pid] = {"name": o.get("name"), "rank": rank, "command": o.get("command"), "precinct": pct}
    if pct:
        s = pct_stat[pct]
        s["n"] += 1; s["arrests"] += arr; s["recognitions"] += rec; s["charges"] += ch
        if ch: s["disciplined"] += 1

precinct_stats = []
for pct, s in sorted(pct_stat.items()):
    precinct_stats.append({
        "precinct": pct, "officers": s["n"],
        "avg_arrests": round(s["arrests"] / s["n"], 1) if s["n"] else 0,
        "avg_recognitions": round(s["recognitions"] / s["n"], 1) if s["n"] else 0,
        "charges": s["charges"], "disciplined": s["disciplined"],
        "disciplined_rate": round(100 * s["disciplined"] / s["n"], 1) if s["n"] else 0,
    })

# discipline rows joined to officer identity
disc_out = []
for c in charges:
    pid = c.get("profile_id")
    o = by_id.get(pid, {})
    disc_out.append({
        "name": o.get("name") or "(not in active roster)",
        "rank": o.get("rank") or "—", "command": o.get("command") or "—",
        "precinct": o.get("precinct"), "profile_id": pid,
        "date": (c.get("date") or "")[:10],
        "case": c.get("case_number") or "—",
        "charge": c.get("charge_description") or "—",
        "disposition": c.get("disposition") or "—",
        "penalty": c.get("penalty_and_quantity") or "—",
    })
disc_out.sort(key=lambda x: x["date"], reverse=True)

# decorated: high honors joined to identity, grouped per officer
dec_by_officer = defaultdict(lambda: {"awards": []})
for h in honor_rows:
    pid = h.get("profile_id")
    o = by_id.get(pid)
    if not o: continue  # award-holder no longer on active roster
    d = dec_by_officer[pid]
    d.update({"name": o["name"], "rank": o["rank"], "command": o["command"], "precinct": o["precinct"], "profile_id": pid})
    d["awards"].append({"award": h.get("award"), "date": (h.get("date") or "")[:10]})
decorated = sorted(dec_by_officer.values(), key=lambda x: (-len(x["awards"]), x["name"]))
honor_counts = Counter(h.get("award") for h in honor_rows)

# --------------------------------------------------------------------------
print("7/7  Overall stats…")
tot_arrests = sum(r[5] for r in rows)
tot_recognitions = sum(r[6] for r in rows)
disciplined_officers = sum(1 for r in rows if r[7] > 0)
def ten_band(yrs):
    return "25+" if yrs >= 25 else "20–24" if yrs >= 20 else "15–19" if yrs >= 15 else "10–14" if yrs >= 10 else "5–9" if yrs >= 5 else "0–4"
TEN_ORDER = ["0–4", "5–9", "10–14", "15–19", "20–24", "25+"]

tenure = Counter()
for r in rows:
    if r[4]:
        tenure[ten_band(2026 - r[4])] += 1

# ---- arrest breakdown (arrests_total is lifetime cumulative; no charge/type detail exists) ----
def median(lst):
    s = sorted(lst); m = len(s)
    return 0 if not m else (s[m//2] if m % 2 else (s[m//2-1] + s[m//2]) / 2)

arrs = [r[5] for r in rows]
n = len(arrs); tot_a = sum(arrs)
asc = sorted(arrs); desc = asc[::-1]
def top_share(frac):
    k = max(1, int(frac * n))
    return round(100 * sum(desc[:k]) / tot_a, 1)
bands = [(0,0,"0"),(1,24,"1–24"),(25,49,"25–49"),(50,99,"50–99"),
         (100,199,"100–199"),(200,499,"200–499"),(500,999,"500–999"),(1000,10**9,"1,000+")]
hist = [{"band": lab, "n": sum(1 for a in arrs if lo <= a <= hi)} for lo, hi, lab in bands]

rank_arr = defaultdict(list); rank_yrs = defaultdict(int)
ten_arr = defaultdict(int); ten_yrs = defaultdict(int); ten_n = defaultdict(int)
for r in rows:
    rank_arr[r[2]].append(r[5])
    sv = max(1, 2026 - r[4]) if r[4] else 0
    if sv: rank_yrs[r[2]] += sv
    if r[4]:
        b = ten_band(2026 - r[4])
        ten_arr[b] += r[5]; ten_yrs[b] += max(1, 2026 - r[4]); ten_n[b] += 1
by_rank = []
for rk, lst in rank_arr.items():
    if len(lst) >= 100:
        yrs = rank_yrs[rk] or len(lst)
        by_rank.append({"rank": rk, "n": len(lst), "avg": round(sum(lst)/len(lst), 1),
                        "median": median(lst), "per_year": round(sum(lst)/yrs, 1)})
by_rank.sort(key=lambda x: -x["avg"])
by_tenure = [{"band": b, "n": ten_n[b], "avg": round(ten_arr[b]/ten_n[b], 1),
              "per_year": round(ten_arr[b]/ten_yrs[b], 1)} for b in TEN_ORDER if ten_n[b]]

arrests_break = {
    "total": tot_a, "mean": round(tot_a/n, 1), "median": median(arrs),
    "p90": asc[int(.9*n)], "max": desc[0],
    "zero": sum(1 for a in arrs if a == 0),
    "zero_pct": round(100*sum(1 for a in arrs if a == 0)/n, 1),
    "hist": hist, "by_rank": by_rank, "by_tenure": by_tenure,
    "concentration": {"top1": top_share(.01), "top10": top_share(.10),
                      "top25": top_share(.25), "bottom50": round(100*sum(asc[:n//2])/tot_a, 1)},
}

def year4(s):
    return to_int(s[:4]) if s and s[:4].isdigit() else 0

def continuous_series(counter, floor):
    present = [y for y in counter if y]
    if not present:
        return []
    lo = max(min(present), floor)
    return [{"year": y, "n": counter.get(y, 0)} for y in range(lo, 2027)]

appt_years = Counter(r[4] for r in rows if 1900 < (r[4] or 0) <= 2026)
charge_years = Counter(year4(c.get("date")) for c in charges)
honor_years = Counter(year4(h.get("date")) for h in honor_rows)
appointments_by_year = continuous_series(appt_years, 1980)
charges_by_year = continuous_series(charge_years, 1995)
honors_by_year = continuous_series(honor_years, 1985)

stats = {
    "export_date": export_date,
    "officers": len(rows),
    "total_arrests": tot_arrests,
    "avg_arrests": round(tot_arrests / len(rows), 1),
    "total_recognitions": tot_recognitions,
    "recognitions_awarded": sum(a["n"] for a in award_summary),  # itemized award records (matches the awards chart)
    "avg_recognitions": round(tot_recognitions / len(rows), 1),
    "total_charges": len(charges),
    "disciplined_officers": disciplined_officers,
    "disciplined_pct": round(100 * disciplined_officers / len(rows), 2),
    "high_honor_awards": len(honor_rows),
    "decorated_officers": len(decorated),
    "ranks": [{"rank": r, "n": n} for r, n in rank_counter.most_common()],
    "tenure": [{"band": b, "n": tenure[b]} for b in ["0–4", "5–9", "10–14", "15–19", "20–24", "25+"]],
    "awards": award_summary,
    "honor_counts": [{"award": a, "n": honor_counts[a], "officers": len(award_rc[a])} for a in HIGH_HONORS if honor_counts[a]],
    "training": training_summary,
    "precincts_mapped": sum(1 for r in rows if r[8]),
    "appointments_by_year": appointments_by_year,
    "charges_by_year": charges_by_year,
    "honors_by_year": honors_by_year,
    "arrests": arrests_break,
}

# --------------------------------------------------------------------------
def dump(name, obj):
    p = OUT / name
    p.write_text(json.dumps(obj, separators=(",", ":")))
    print(f"  wrote {name}  ({p.stat().st_size/1024:.0f} KB)")

def round_coords(x, nd=4):
    if isinstance(x, (int, float)): return round(x, nd)
    if isinstance(x, list): return [round_coords(i, nd) for i in x]
    return x
for feat in precincts_geo.get("features", []):
    g = feat.get("geometry")
    if g and "coordinates" in g:
        g["coordinates"] = round_coords(g["coordinates"])
    feat["properties"] = {"precinct": to_int(feat.get("properties", {}).get("precinct"))}

dump("roster.json", {"cols": COLS, "export_date": export_date, "rows": rows})
dump("discipline.json", disc_out)
dump("decorated.json", {"officers": decorated, "counts": stats["honor_counts"]})
dump("precinct_stats.json", precinct_stats)
dump("stats.json", stats)
(OUT / "precincts.geojson").write_text(json.dumps(precincts_geo, separators=(",", ":")))
print(f"  wrote precincts.geojson  ({(OUT/'precincts.geojson').stat().st_size/1024:.0f} KB)")
print("Done.")
