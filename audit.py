#!/usr/bin/env python3
"""Fact-check: recompute every displayed number from the baked data files,
compare to stats.json, and spot-check against the live SODA API."""
import json, re, urllib.request, urllib.parse
from collections import Counter
from pathlib import Path

D = Path(__file__).parent / "data"
BASE = "https://data.cityofnewyork.us/resource"
def L(n): return json.load(open(D/n))
def api(ds, params):
    u=f"{BASE}/{ds}.json?"+urllib.parse.urlencode(params)
    return json.load(urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"audit/1"}),timeout=90))

ok=[]; bad=[]
def chk(name, a, b):
    (ok if a==b else bad).append((name,a,b))
    print(("  ok  " if a==b else "  ✗   ")+f"{name}: baked/derived={a!r} expected={b!r}")

stats=L("stats.json")
roster=L("roster.json"); cols=roster["cols"]; rows=roster["rows"]
ci={c:i for i,c in enumerate(cols)}
disc=L("discipline.json"); dec=L("decorated.json"); pstats=L("precinct_stats.json")

print("\n== INTERNAL CONSISTENCY: recompute from roster.json vs stats.json ==")
chk("officers", len(rows), stats["officers"])
chk("sum arrests", sum(r[ci['arrests']] for r in rows), stats["total_arrests"])
chk("sum recognitions", sum(r[ci['recognitions']] for r in rows), stats["total_recognitions"])
disciplined = sum(1 for r in rows if r[ci['charges']]>0)
chk("disciplined officers", disciplined, stats["disciplined_officers"])
chk("disciplined pct", round(100*disciplined/len(rows),2), stats["disciplined_pct"])
chk("precincts_mapped", sum(1 for r in rows if r[ci['precinct']]), stats["precincts_mapped"])
rc=Counter(r[ci['rank']] for r in rows)
chk("rank sum == officers", sum(rc.values()), stats["officers"])
chk("top rank", stats["ranks"][0], {"rank":rc.most_common(1)[0][0],"n":rc.most_common(1)[0][1]})

print("\n== DISCIPLINE (discipline.json) ==")
chk("charge rows", len(disc), stats["total_charges"])
dispo=Counter(d["disposition"] for d in disc)
print("     dispositions:", dict(dispo))
chk("dispositions sum to total", sum(dispo.values()), len(disc))
guilty_like={"GUILTY","PLEADED GUILTY","NOLO CONTENDRE","NOLO CONTENDERE"}
chk("all charges are guilty-type", all(k in guilty_like for k in dispo), True)
distinct_charged=len({d["profile_id"] for d in disc})
distinct_active=len({d["profile_id"] for d in disc if d["name"]!="(not in active roster)"})
print(f"     distinct profile_ids charged={distinct_charged}, of them active={distinct_active}")
chk("active-charged == disciplined_officers", distinct_active, stats["disciplined_officers"])

print("\n== DECORATED (decorated.json) ==")
chk("decorated officers", len(dec["officers"]), stats["decorated_officers"])
hc={c["award"]:c["n"] for c in dec["counts"]}
chk("high-honor awards sum", sum(hc.values()), stats["high_honor_awards"])
awards_in_dec=sum(len(o["awards"]) for o in dec["officers"])
print(f"     honor awards held by active officers (sum over officers)={awards_in_dec}")
print("     honor counts:", hc)

print("\n== TIME SERIES ==")
for key in ["appointments_by_year","charges_by_year","honors_by_year"]:
    s=stats[key]; peak=max(s,key=lambda d:d["n"])
    print(f"     {key}: {s[0]['year']}–{s[-1]['year']}, peak {peak['year']}={peak['n']}, last {s[-1]}")
# appointments series should sum to officers with a valid year in range
appt_from_roster=Counter(r[ci['precinct']] for r in rows)  # placeholder not used
yrs=[r[ci['year']] for r in rows if r[ci['year']]]
chk("appt series sum <= officers-with-year", sum(d["n"] for d in stats["appointments_by_year"]) <= len(yrs), True)

print("\n== PRECINCTS ==")
geo=json.load(open(D/"precincts.geojson"))
gp=sorted({f["properties"]["precinct"] for f in geo["features"]})
print(f"     geojson polygons={len(geo['features'])}, distinct precinct numbers={len(gp)}")
print(f"     precinct_stats entries={len(pstats)}")
print(f"     precincts: {gp}")

print("\n== LIVE API SPOT-CHECK (may reflect a newer weekly export) ==")
liveexp=api("pmsy-ewrc",{"$select":"max(export_date) as e"})[0]["e"][:10]
print(f"     current roster export_date={liveexp}  (baked snapshot={stats['export_date']})")
for ds,label in [("pmsy-ewrc","roster"),("uafj-ik29","charges"),("i9n8-a8ed","recognitions"),
                 ("sh6y-4tgb","title history"),("n3mp-t5uj","training"),("wq9a-qu9a","disc summary")]:
    n=int(api(ds,{"$select":"count(*) as n"})[0]["n"])
    print(f"     {label} ({ds}) live count = {n:,}")
hon=api("i9n8-a8ed",{"$select":"award,count(*) as n","$where":"award in('MEDAL OF HONOR','MEDAL FOR VALOR','POLICE COMBAT CROSS','EXCEPTIONAL MERIT','MEDAL FOR MERIT','PURPLE SHIELD MEDAL')","$group":"award","$order":"n desc"})
print("     live high-honor counts:", {h["award"]:int(h["n"]) for h in hon})

print(f"\n== SUMMARY: {len(ok)} checks passed, {len(bad)} failed ==")
for n,a,b in bad: print(f"   FAIL {n}: {a} != {b}")
