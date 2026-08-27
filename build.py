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
print("1/9  Roster (Members of Service)…")
roster = soda("pmsy-ewrc", {
    "$select": "profile_id,name,rank,command,shield,appointment_date,arrests_total,department_recognitions,export_date",
    "$limit": 60000,
})
print(f"     {len(roster):,} officers")
export_date = (roster[0].get("export_date") or "")[:10] if roster else ""

# --------------------------------------------------------------------------
print("2/9  Disciplinary charges…")
charges = soda("uafj-ik29", {"$select": "profile_id,date,case_number,charge_description,disposition,penalty_and_quantity", "$limit": 60000})
print(f"     {len(charges):,} charges")

sustained = Counter(c["profile_id"] for c in charges if c.get("profile_id"))
disp_ct = Counter((c.get("disposition") or "—").strip().upper() for c in charges)

# --------------------------------------------------------------------------
print("3/9  Recognition records (awards + distinct recipients)…")
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
print("4/9  Training aggregate…")
training_agg = soda("n3mp-t5uj", {"$select": "training,count(*) as n", "$group": "training", "$order": "n desc", "$limit": 60})
training_summary = [{"training": t["training"], "n": to_int(t["n"])} for t in training_agg if t.get("training")]

# exact row counts for the source-datasets table (the two live-only tables + training total)
def count_of(ds):
    r = soda(ds, {"$select": "count(*) as n"})
    return to_int(r[0].get("n")) if r else 0
print("     source row counts…")
title_history_rows = count_of("sh6y-4tgb")
disc_summary_rows = count_of("wq9a-qu9a")
training_rows = count_of("n3mp-t5uj")

# --------------------------------------------------------------------------
print("5/9  Precinct boundaries…")
precincts_geo = soda("y76i-bdw7", {"$limit": 200}, geojson=True)

# --------------------------------------------------------------------------
print("6/9  Assembling roster + per-precinct stats…")
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
print("7/9  Overall stats…")
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

# ---- staffing caveats: what the 34,237 headcount does and doesn't include ----
cmd_list = [(r[3] or "") for r in rows]
numre = re.compile(r"^(\d{1,3})\b")
recruits = sum(1 for c in cmd_list if c.strip().upper() == "RTS RECRUITS")
academy = sum(1 for c in cmd_list if "POLICE ACADEMY" in c.upper())
military_leave = sum(1 for c in cmd_list if "MILITARY" in c.upper() and "LEAVE" in c.upper())
dv_officers = sum(1 for c in cmd_list if "DOMESTIC VIOLENCE" in c.upper())
dv_precinct_squads = sum(1 for c in cmd_list if "DOMESTIC VIOLENCE SQUAD" in c.upper() and numre.match(c))
prec_any = sum(1 for c in cmd_list if (numre.match(c) and 1 <= to_int(numre.match(c).group(1)) <= 123))
prec_num_mapped = sum(1 for r in rows if r[8] and numre.match(r[3] or ""))
staffing = {
    "total": len(rows),
    "recruits": recruits, "academy": academy, "military_leave": military_leave,
    "available_est": len(rows) - recruits - military_leave,
    "dv_officers": dv_officers, "dv_precinct_squads": dv_precinct_squads,
    "precinct_patrol": sum(1 for r in rows if r[8]),
    "precinct_any": prec_any,
    "precinct_squads_extra": prec_any - prec_num_mapped,  # det + DV squads not in the patrol count
}

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
    "staffing": staffing,
    "source_rows": {
        "roster": len(rows),
        "title_history": title_history_rows,
        "training": training_rows,
        "recognition": len(recog_all),
        "discipline_summary": disc_summary_rows,
        "discipline_charges": len(charges),
        "precincts": len(precincts_geo.get("features", [])),
    },
    "dispositions": [{"disposition": d, "n": disp_ct[d]} for d, _ in disp_ct.most_common()],
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

# --------------------------------------------------------------------------
# 8  Civilian Complaint Review Board.
#
# The NYPD's own discipline tables carry guilty findings only. The CCRB — a separate
# agency with its own case numbering — publishes every civilian allegation, what it
# concluded, and what penalty (if any) the NYPD then imposed. There is no shared key
# between the two systems: the NYPD publishes profile_id, the CCRB publishes tax_id.
# They are joined here on name plus shield, and only where that is unambiguous on both
# sides. Unmatched officers are reported, never guessed at.
print("8/9  Civilian Complaint Review Board…")

def soda_paged(dataset, select, page=50000, cap=800000):
    """Page a large table. Raises rather than returning a partial pull."""
    out, offset = [], 0
    while offset < cap:
        chunk = soda(dataset, {"$select": select, "$order": ":id", "$limit": page, "$offset": offset})
        out.extend(chunk)
        if len(chunk) < page:
            return out
        offset += page
    raise RuntimeError(f"{dataset}: hit the {cap:,}-row cap; raise it rather than truncating")

ccrb_officers = soda_paged("2fir-qns4",
    "tax_id,officer_first_name,officer_last_name,shield_no,active_per_last_reported_status,"
    "total_complaints,total_substantiated_complaints,as_of_date")
print(f"     {len(ccrb_officers):,} CCRB officer records")
allegations = soda_paged("6xgr-kwjq",
    "complaint_id,tax_id,fado_type,allegation,ccrb_allegation_disposition,officer_command_at_incident")
print(f"     {len(allegations):,} allegations")
complaints = soda_paged("2mby-ccnw",
    "complaint_id,incident_date,precinct_of_incident_occurrence,borough_of_incident_occurrence,"
    "ccrb_complaint_disposition,bwc_evidence,video_evidence,reason_for_police_contact")
print(f"     {len(complaints):,} complaints")
penalties = soda_paged("keep-pkmh", "complaint_id,tax_id,nypd_officer_penalty,board_discipline_recommendation")
print(f"     {len(penalties):,} penalty records")
if not (len(ccrb_officers) > 50000 and len(allegations) > 300000 and len(complaints) > 100000):
    raise RuntimeError("CCRB pull came back short — refusing to publish a partial complaint history")

def name_key(nypd_name):
    """'GILLIAM, STEPHEN W' -> ('GILLIAM', 'STEPHEN')"""
    if "," not in (nypd_name or ""):
        return ((nypd_name or "").strip().upper(), "")
    last, rest = nypd_name.split(",", 1)
    parts = rest.strip().split()
    return (last.strip().upper(), parts[0].upper() if parts else "")

def shield_key(s):
    s = (s or "").strip().lstrip("0")
    return s or None

nypd_by3, nypd_by2 = defaultdict(list), defaultdict(list)
for o in roster:
    last, first = name_key(o.get("name"))
    nypd_by3[(last, first, shield_key(o.get("shield")))].append(o["profile_id"])
    nypd_by2[(last, first)].append(o["profile_id"])

ccrb_by3, ccrb_by2 = defaultdict(list), defaultdict(list)
for o in ccrb_officers:
    last = (o.get("officer_last_name") or "").strip().upper()
    first = (o.get("officer_first_name") or "").strip().upper()
    ccrb_by3[(last, first, shield_key(o.get("shield_no")))].append(o)
    ccrb_by2[(last, first)].append(o)

pid_for_tax, tax_for_pid = {}, {}
for k, pids in nypd_by3.items():
    if k[2] and len(pids) == 1 and len(ccrb_by3.get(k, [])) == 1:
        tax = ccrb_by3[k][0]["tax_id"]
        pid_for_tax[tax] = pids[0]; tax_for_pid[pids[0]] = tax
for k, pids in nypd_by2.items():           # fall back to name alone, still only when unique
    if len(pids) == 1 and pids[0] not in tax_for_pid and len(ccrb_by2.get(k, [])) == 1:
        tax = ccrb_by2[k][0]["tax_id"]
        if tax not in pid_for_tax:
            pid_for_tax[tax] = pids[0]; tax_for_pid[pids[0]] = tax
matched = len(tax_for_pid)
print(f"     matched {matched:,} of {len(roster):,} active officers to a CCRB record")

FADO_KEYS = ["Force", "Abuse of Authority", "Discourtesy", "Offensive Language", "Untruthful Statement"]
SUBSTANTIATED = lambda d: (d or "").startswith("Substantiated")

alleg_by_tax = defaultdict(lambda: {"n": 0, "sub": 0, "fado": Counter()})
for a in allegations:
    t = a.get("tax_id")
    if not t:
        continue
    rec = alleg_by_tax[t]
    rec["n"] += 1
    rec["fado"][a.get("fado_type") or "—"] += 1
    if SUBSTANTIATED(a.get("ccrb_allegation_disposition")):
        rec["sub"] += 1

CCRB_COLS = ["tax_id", "allegations", "substantiated", "complaints", "sub_complaints"] + FADO_KEYS
by_pid = {}
ccrb_officer_by_tax = {o["tax_id"]: o for o in ccrb_officers if o.get("tax_id")}
for pid, tax in tax_for_pid.items():
    rec = alleg_by_tax.get(tax)
    off = ccrb_officer_by_tax.get(tax, {})
    comp = to_int(off.get("total_complaints"))
    subc = to_int(off.get("total_substantiated_complaints"))
    if not rec and not comp:
        continue                           # matched, but never had a complaint — nothing to store
    fado = (rec or {}).get("fado", Counter())
    by_pid[pid] = [tax, (rec or {}).get("n", 0), (rec or {}).get("sub", 0), comp, subc] + \
                  [fado.get(k, 0) for k in FADO_KEYS]

# ---- citywide aggregates ----
comp_by_id = {c["complaint_id"]: c for c in complaints if c.get("complaint_id")}
year_of_complaint = {cid: (c.get("incident_date") or "")[:4] for cid, c in comp_by_id.items()}

fado_counts = Counter(a.get("fado_type") or "—" for a in allegations)
allegation_counts = Counter(a.get("allegation") or "—" for a in allegations)
disp_counts = Counter(a.get("ccrb_allegation_disposition") or "—" for a in allegations)
penalty_counts = Counter((p.get("nypd_officer_penalty") or "Not yet reported").strip() for p in penalties)

# Complaint outcomes by year of incident, so the trend is about when things happened.
by_year = defaultdict(lambda: {"complaints": 0, "substantiated": 0})
for c in complaints:
    y = (c.get("incident_date") or "")[:4]
    if not (y.isdigit() and 2000 <= int(y) <= 2026):
        continue
    b = by_year[int(y)]
    b["complaints"] += 1
    if SUBSTANTIATED(c.get("ccrb_complaint_disposition")):
        b["substantiated"] += 1

# Does footage change the finding? Only complaints from 2019 on, when body cameras were
# citywide, and only cases the board actually adjudicated (uncooperative/withdrawn closures
# are excluded — they say nothing about the merits).
ADJUDICATED = {"Substantiated", "Unsubstantiated", "Exonerated", "Unfounded", "Within NYPD Guidelines"}
def adjudicated(d):
    d = d or ""
    return d.startswith("Substantiated") or d in ADJUDICATED
bwc = {"with": {"n": 0, "sub": 0}, "without": {"n": 0, "sub": 0}}
for c in complaints:
    y = (c.get("incident_date") or "")[:4]
    if not (y.isdigit() and int(y) >= 2019):
        continue
    d = c.get("ccrb_complaint_disposition")
    if not adjudicated(d):
        continue
    k = "with" if (c.get("bwc_evidence") or "") == "Yes" else "without"
    bwc[k]["n"] += 1
    if SUBSTANTIATED(d):
        bwc[k]["sub"] += 1
for k in bwc:
    bwc[k]["pct"] = round(100 * bwc[k]["sub"] / bwc[k]["n"], 1) if bwc[k]["n"] else 0

# Allegations per precinct of occurrence, last five full years, for the map.
# Two traps here. The board leaves the precinct blank on a slice of complaints, and it has
# never once coded a complaint to the 121st Precinct — a real precinct, carved out of the
# 122nd on Staten Island in 2013. Mapping either as a zero would paint the emptiest
# precincts as the cleanest, so precincts the board never codes are marked no-data instead.
pct_alleg = Counter()
pct_ever = Counter()
alleg_no_precinct = 0
recent_ids = {cid for cid, y in year_of_complaint.items() if y.isdigit() and int(y) >= 2021}
for a in allegations:
    c = comp_by_id.get(a.get("complaint_id"))
    if not c:
        continue
    pnum = to_int(c.get("precinct_of_incident_occurrence"))
    if not (1 <= pnum <= 123):
        alleg_no_precinct += 1
        continue
    pct_ever[pnum] += 1
    if a.get("complaint_id") in recent_ids:
        pct_alleg[pnum] += 1

# How complaints sit across today's force.
band_counts = Counter()
for r in rows:
    v = by_pid.get(r[0])
    n = v[1] if v else 0
    band_counts["10+" if n >= 10 else "5–9" if n >= 5 else "2–4" if n >= 2 else "1" if n == 1 else "0"] += 1

repeat = sorted(((v[1], v[2], pid) for pid, v in by_pid.items()), reverse=True)
subs_total = sum(1 for a in allegations if SUBSTANTIATED(a.get("ccrb_allegation_disposition")))
no_penalty = penalty_counts.get("No penalty", 0)
penalty_known = sum(n for p, n in penalty_counts.items() if p != "Not yet reported")

stats["ccrb"] = {
    "as_of": (ccrb_officers[0].get("as_of_date") or "")[:10] if ccrb_officers else "",
    "allegations": len(allegations),
    "complaints": len(complaints),
    "officers_in_ccrb": len(ccrb_officers),
    "substantiated_allegations": subs_total,
    "substantiated_pct": round(100 * subs_total / len(allegations), 1) if allegations else 0,
    "matched_officers": matched,
    "matched_pct": round(100 * matched / len(rows), 1) if rows else 0,
    "roster_with_complaint": len(by_pid),
    "roster_with_substantiated": sum(1 for v in by_pid.values() if v[2] > 0),
    "roster_allegations": sum(v[1] for v in by_pid.values()),
    "bands": [{"band": b, "n": band_counts.get(b, 0)} for b in ["0", "1", "2–4", "5–9", "10+"]],
    "fado": sorted([{"type": t, "n": fado_counts[t]} for t in FADO_KEYS if fado_counts.get(t)],
                   key=lambda x: -x["n"]),
    "allegation_types": [{"type": t, "n": n} for t, n in allegation_counts.most_common(12)],
    "dispositions": [{"disposition": d, "n": n} for d, n in disp_counts.most_common(12)],
    "penalties": [{"penalty": p, "n": n} for p, n in penalty_counts.most_common(12)],
    "penalty_records": len(penalties),
    "no_penalty": no_penalty,
    "no_penalty_pct": round(100 * no_penalty / penalty_known, 1) if penalty_known else 0,
    "bwc": bwc,
    "precincts_never_coded": [],   # filled in below, once precinct_stats is annotated
    "by_year": [{"year": y, **by_year[y]} for y in sorted(by_year) if 2006 <= y <= 2026],
    "max_allegations": repeat[0][0] if repeat else 0,
    "allegations_without_precinct": alleg_no_precinct,
}

for p in precinct_stats:
    if not pct_ever.get(p["precinct"]):
        p["ccrb_allegations"] = None       # the board has never coded a complaint here
        p["ccrb_per_officer"] = None
        continue
    p["ccrb_allegations"] = pct_alleg.get(p["precinct"], 0)
    p["ccrb_per_officer"] = round(pct_alleg.get(p["precinct"], 0) / p["officers"], 2) if p["officers"] else 0

stats["ccrb"]["precincts_never_coded"] = sorted(
    p["precinct"] for p in precinct_stats if p["ccrb_allegations"] is None)

# by_pid carries only officers who have a complaint. Everyone else is either matched with a
# clean record or not matched at all, and the drawer must not conflate the two — so the
# smaller of those groups (the unmatched) is listed explicitly.
unmatched_pids = [r[0] for r in rows if r[0] not in tax_for_pid]
dump("ccrb.json", {
    "as_of": stats["ccrb"]["as_of"],
    "cols": CCRB_COLS,
    "matched": matched,
    "roster": len(rows),
    "by_pid": by_pid,
    "unmatched": unmatched_pids,
})

# --------------------------------------------------------------------------
print("9/9  Writing files…")
dump("roster.json", {"cols": COLS, "export_date": export_date, "rows": rows})
dump("discipline.json", disc_out)
dump("decorated.json", {"officers": decorated, "counts": stats["honor_counts"]})
dump("precinct_stats.json", precinct_stats)
dump("stats.json", stats)
(OUT / "precincts.geojson").write_text(json.dumps(precincts_geo, separators=(",", ":")))
print(f"  wrote precincts.geojson  ({(OUT/'precincts.geojson').stat().st_size/1024:.0f} KB)")

# --------------------------------------------------------------------------
# Regenerate methodology.md from the same numbers so it can never drift out of
# sync with the data. Every {value} below is computed above.
def award_row(name):
    for a in award_summary:
        if a["award"] == name:
            return a
    return {"n": 0, "officers": 0}
exc, mer = award_row("EXCELLENT POLICE DUTY"), award_row("MERITORIOUS POLICE DUTY")
c = lambda x: f"{x:,}"
sr, sf, cc = stats["source_rows"], staffing, stats["ccrb"]
g_, pg_, nc_ = disp_ct.get("GUILTY", 0), disp_ct.get("PLEADED GUILTY", 0), disp_ct.get("NOLO CONTENDRE", 0)
guilty_family = g_ + pg_ + nc_
exc_each = round(exc["n"] / exc["officers"], 1) if exc["officers"] else 0

methodology_md = f"""# The NYPD, officer by officer — methodology

An interactive explorer built entirely from the New York Police Department's own
**Officer Profile** datasets, which the department publishes on NYC Open Data (the roster
dataset was added in 2024, disciplinary records in 2021) and refreshes weekly. Nothing here
is estimated or hand-entered: every figure is computed directly from the source data at
build time, and per-officer detail is fetched live from the city's servers when you open an
officer.

**This file is generated by `build.py` from the {export_date} export — do not edit by hand.**

## Source datasets

All six Officer Profile tables share one key, `profile_id`. The roster is the hub; the
rest hang off it. Precinct boundaries come from a seventh (City Planning) dataset.

| Dataset | ID | Rows | Role in this site |
|---|---|---|---|
| Members of Service (roster) | `pmsy-ewrc` | {c(sr['roster'])} | The officer list; all headline stats |
| Title / Shield History | `sh6y-4tgb` | {c(sr['title_history'])} | Per-officer career timeline (fetched live) |
| Training | `n3mp-t5uj` | {c(sr['training'])} | Aggregate training chart only |
| Department Recognition | `i9n8-a8ed` | {c(sr['recognition'])} | Awards chart; "the decorated" |
| Disciplinary History — Summary | `wq9a-qu9a` | {c(sr['discipline_summary'])} | Recomputed here from the charges table |
| Disciplinary History — Charges | `uafj-ik29` | {c(sr['discipline_charges'])} | Discipline view; per-officer discipline |
| Police Precincts (boundaries) | `y76i-bdw7` | {c(sr['precincts'])} | The precinct choropleth |

Base API pattern: `https://data.cityofnewyork.us/resource/<id>.json`

### The Civilian Complaint Review Board tables

The NYPD's discipline file is the department judging itself, and it publishes guilty findings
only. The CCRB is a separate agency with its own case file, published as four more datasets:

| Dataset | ID | Rows | Role in this site |
|---|---|---|---|
| Allegations Against Police Officers | `6xgr-kwjq` | {c(cc['allegations'])} | Complaint counts per officer; the FADO and disposition charts; per-officer detail (fetched live) |
| Police Officers | `2fir-qns4` | {c(cc['officers_in_ccrb'])} | The name-and-shield match to the NYPD roster |
| Complaints Against Police Officers | `2mby-ccnw` | {c(cc['complaints'])} | Incident date, precinct and body-camera evidence |
| Penalties | `keep-pkmh` | {c(cc['penalty_records'])} | What the NYPD did after the board substantiated |

**The join.** The two agencies share no key: the NYPD publishes a `profile_id`, the CCRB a
`tax_id`. Officers are matched on surname, first name and shield number, and only where that
combination is unique on **both** sides; anything left over is matched on name alone, again only
when unique. {c(cc['matched_officers'])} of {c(stats['officers'])} serving officers
({cc['matched_pct']}%) match. The rest are shown as unmatched in the officer drawer rather than
as having a clean record — an unmatched officer is not an officer without complaints.

## How the data is processed

`build.py` fetches each dataset from the SODA API and writes compact JSON into `data/`:

- **`roster.json`** — all {c(sr['roster'])} officers as a compact columnar array. A
  per-officer count of sustained charges and a parsed precinct number are added.
- **`discipline.json`** — all {c(sr['discipline_charges'])} charges, each joined to the
  officer's name, rank and command via `profile_id`.
- **`decorated.json`** — holders of the six top medals, grouped per officer.
- **`precinct_stats.json`** — per-precinct officer count, average arrests, average
  recognitions and share with a sustained charge.
- **`stats.json`** — all overview aggregates (rank distribution, tenure bands, award
  tiers, top training types, headline totals, and the CCRB block).
- **`ccrb.json`** — per-officer complaint counts for the {c(cc['roster_with_complaint'])}
  serving officers who have one, plus the tax id the drawer needs to fetch their record live,
  plus an explicit list of the officers who could not be matched at all.
- **`precincts.geojson`** — precinct polygons, coordinates rounded to 4 decimal places
  (~11 m) to shrink the file for the web.

Per-officer drawers are **not** baked in. When you open an officer, the site queries
`sh6y-4tgb`, `i9n8-a8ed` and `uafj-ik29` live by `profile_id`, so the detail always
reflects the current published record.

To rebuild: `python3 build.py` (standard library only, no API token required).

## Caveats — read these before citing anything

### Civilian complaints

- **An allegation is an account, not a finding.** {c(cc['allegations'])} allegations are on
  file; {c(cc['substantiated_allegations'])} ({cc['substantiated_pct']}%) were substantiated by
  the board. The largest single disposition category is not a judgment about the officer at all
  — it is a case closed because the complainant stopped participating.
- **Substantiated is not punished.** The board investigates; the NYPD decides the penalty and
  reports it back. Across {c(cc['penalty_records'])} referrals, the most common single outcome
  is **no penalty at all** — {c(cc['no_penalty'])}, or {cc['no_penalty_pct']}% of referrals with
  a reported outcome.
- **Only serving officers appear.** The complaint tables cover
  {c(cc['officers_in_ccrb'])} officers, most of whom have left the force. This site shows the
  {c(cc['roster_with_complaint'])} who are still on today's roster, so an officer who
  accumulated complaints and then retired is absent.
- **The body-camera comparison is not causal.** Adjudicated complaints from 2019 on were
  substantiated {cc['bwc']['with']['pct']}% of the time with body-worn camera evidence and
  {cc['bwc']['without']['pct']}% without. Cases that draw footage differ from cases that do not
  — there was a recorded encounter to begin with — so this compares two kinds of case, not the
  effect of the camera. Cases closed as withdrawn or uncooperative are excluded from both sides.
- **The complaint map is by place, not by roster.** Allegations are counted where the incident
  occurred. {c(cc['allegations_without_precinct'])} allegations carry no precinct and are left
  out. The **121st Precinct** is shown as no-data, not zero: the board has never coded a single
  complaint to it since the precinct was created on Staten Island in 2013.

### The rest

- **Discipline is guilty findings only.** All {c(sr['discipline_charges'])} published
  charges carry a disposition of guilty, pleaded guilty or no contest
  ({c(g_)} / {c(pg_)} / {c(nc_)} — which sum to {c(guilty_family)}). Dismissed,
  unsubstantiated and pending matters are **absent**, and Civilian Complaint Review Board
  complaints are in a separate system entirely. An officer with no record here has no
  *sustained departmental charge* — not necessarily a clean complaint history.
- **Active officers only.** This is a live snapshot of the current uniformed force.
  Retired or separated members drop off. Medal-holders who have left the force are not
  among "the decorated."
- **Arrests and recognitions are lifetime cumulative totals** as published by the
  department; they cannot be broken out by year from this data alone.
- **The precinct map covers patrol precincts only.** Commands were mapped to a precinct by
  parsing strings like `075 PRECINCT`, with the Central Park precinct (the 22nd) matched by
  name because its command carries no number. About {c(sf['precinct_patrol'])} of
  {c(stats['officers'])} officers sit in a patrol precinct; the rest (housing, transit,
  headquarters, academy recruits and specialized units) are not on the map. The boundary
  file contains {c(sr['precincts'])} precinct areas. Precinct averages are computed over
  precinct-assigned officers only.
- **Awards given vs officers who hold them.** A single officer can receive the same award
  many times, so the count of awards is much larger than the count of recipients. Excellent
  Police Duty, for example, is awarded {c(exc['n'])} times but to {c(exc['officers'])}
  distinct officers (about {exc_each} each); Meritorious Police Duty {c(mer['n'])} times to
  {c(mer['officers'])} officers. The awards table shows both columns. The six top medals are
  almost exactly one per officer.
- **Two ways to count recognitions.** The awards table and the "recognitions awarded"
  headline ({c(stats['recognitions_awarded'])}) count individual award records in
  `i9n8-a8ed`. The roster also carries a per-officer recognition counter
  (`department_recognitions`) that sums to {c(stats['total_recognitions'])} — slightly
  higher, a quirk of how the two are maintained. We use the itemized figure so the headline
  and the chart agree.
- **Training records contain known data-entry errors** per the department's own dataset
  description, so the training chart shows broad scale, not exact tallies.
- **Officer names are published by the NYPD.** This site republishes only what the
  department already releases and adds no new personal information.

## Reading the headcount

Total NYPD strength is a perennial fight at City Council budget hearings, and the raw
{c(stats['officers'])} needs context before it is cited:

- **Recruits are in the count.** {c(sf['recruits'])} officers are in the `RTS RECRUITS`
  command — recruits in the Police Academy, and the single largest command on the whole
  force — plus about {c(sf['academy'])} in the Police Academy itself. They are active
  members but not deployed.
- **So are officers who aren't working.** {c(sf['military_leave'])} sit at the
  `MILITARY & EXTENDED LEAVE DESK`, on military deployment or extended leave. Setting
  recruits and this group aside, the number available for duty is nearer
  {c(sf['available_est'])} than {c(stats['officers'])}.
- **Domestic-violence officers moved out of patrol.** About {c(sf['dv_officers'])} officers
  hold domestic-violence assignments, {c(sf['dv_precinct_squads'])} of them in
  precinct-numbered `DOMESTIC VIOLENCE SQUAD` commands that are organizationally separate
  from the precinct patrol roster. The work stayed local even as the officers were
  reorganized out of patrol.
- **Patrol strength is not a precinct's full footprint.** The precinct map counts patrol
  and field-training commands ({c(sf['precinct_patrol'])} officers). Another
  ~{c(sf['precinct_squads_extra'])} work in precinct-based detective and domestic-violence
  squads that fall outside that patrol count.

These figures are computed from the `command` field in the roster and are exposed in
`stats.json` under `staffing`.

## Reading the charts

All counts start at zero. The awards chart uses a **log scale** because Excellent and
Meritorious Police Duty dwarf every other award type; all other charts are linear. The
precinct choropleth uses a linear color ramp between the lowest and highest precinct
values for the selected metric.

## Confidence

High confidence: all counts and totals, which are derived mechanically from the source
files and were cross-checked against the live SODA API. Lower confidence: the
precinct-command parsing (a small number of unusual command strings may not map), and
anything dependent on the completeness of the department's underlying records. Confirm any
specific claim against the original datasets before publishing.

## AI disclosure

The data pipeline and interface were assembled with AI assistance. Every number is
computed from the sources above; none is invented. The build and this document are
reproducible from `build.py`.
"""
(OUT.parent / "methodology.md").write_text(methodology_md)
print(f"  wrote methodology.md  (regenerated from {export_date} data)")
print("Done.")
