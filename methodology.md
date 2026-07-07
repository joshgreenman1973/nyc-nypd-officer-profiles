# The NYPD, officer by officer — methodology

An interactive explorer built entirely from the New York Police Department's own
**Officer Profile** datasets, which the department began publishing on NYC Open Data in
September 2025 and refreshes weekly. Nothing here is estimated or hand-entered: every
figure is computed directly from the source data at build time, and per-officer detail is
fetched live from the city's servers when you open an officer.

**Snapshot built from the 2026-07-05 export.**

## Source datasets

All six Officer Profile tables share one key, `profile_id`. The roster is the hub; the
rest hang off it. Precinct boundaries come from a seventh (City Planning) dataset.

| Dataset | ID | Rows | Role in this site |
|---|---|---|---|
| Members of Service (roster) | `pmsy-ewrc` | 34,237 | The officer list; all headline stats |
| Title / Shield History | `sh6y-4tgb` | ~53,000 | Per-officer career timeline (fetched live) |
| Training | `n3mp-t5uj` | ~12.7 million | Aggregate training chart only |
| Department Recognition | `i9n8-a8ed` | 141,235 | Awards chart; "the decorated" |
| Disciplinary History — Summary | `wq9a-qu9a` | 1,543 | Recomputed here from the charges table |
| Disciplinary History — Charges | `uafj-ik29` | 3,925 | Discipline view; per-officer discipline |
| Police Precincts (boundaries) | `y76i-bdw7` | 78 | The precinct choropleth |

Base API pattern: `https://data.cityofnewyork.us/resource/<id>.json`

## How the data is processed

`build.py` fetches each dataset from the SODA API and writes compact JSON into `data/`:

- **`roster.json`** — all 34,237 officers as a compact columnar array. A per-officer
  count of sustained charges and a parsed precinct number are added.
- **`discipline.json`** — all 3,925 charges, each joined to the officer's name, rank and
  command via `profile_id`.
- **`decorated.json`** — holders of the six top medals, grouped per officer.
- **`precinct_stats.json`** — per-precinct officer count, average arrests, average
  recognitions and share with a sustained charge.
- **`stats.json`** — all overview aggregates (rank distribution, tenure bands, award
  tiers, top training types, headline totals).
- **`precincts.geojson`** — precinct polygons, coordinates rounded to 4 decimal places
  (~11 m) to shrink the file for the web.

Per-officer drawers are **not** baked in. When you open an officer, the site queries
`sh6y-4tgb`, `i9n8-a8ed` and `uafj-ik29` live by `profile_id`, so the detail always
reflects the current published record.

To rebuild: `python3 build.py` (standard library only, no API token required).

## Caveats — read these before citing anything

- **Discipline is guilty findings only.** All 3,925 published charges carry a disposition
  of guilty, pleaded guilty or no contest (2,677 / 1,227 / 21 — which sum to exactly
  3,925). Dismissed, unsubstantiated and pending matters are **absent**, and Civilian
  Complaint Review Board complaints are in a separate system entirely. An officer with no
  record here has no *sustained departmental charge* — not necessarily a clean complaint
  history.
- **Active officers only.** This is a live snapshot of the current uniformed force.
  Retired or separated members drop off. Medal-holders who have left the force are not
  among "the decorated."
- **Arrests and recognitions are lifetime cumulative totals** as published by the
  department; they cannot be broken out by year from this data alone.
- **The precinct map covers patrol precincts only.** Commands were mapped to a precinct by
  parsing strings like `075 PRECINCT`, with the Central Park precinct (the 22nd) matched by
  name because its command carries no number. About 13,167 of 34,237 officers sit in a
  patrol precinct; the rest (housing, transit, headquarters, academy recruits and
  specialized units) are not on the map. The boundary file contains 78 precinct areas.
  Precinct averages are computed over precinct-assigned officers only.
- **Awards given vs officers who hold them.** A single officer can receive the same award
  many times, so the count of awards is much larger than the count of recipients. Excellent
  Police Duty, for example, is awarded 91,330 times but to 16,468 distinct officers (about
  5.5 each); Meritorious Police Duty 46,724 times to 10,687 officers. The awards table shows
  both columns. The six top medals are almost exactly one per officer.
- **Two ways to count recognitions.** The awards table and the "recognitions awarded"
  headline (141,235) count individual award records in `i9n8-a8ed`. The roster also carries
  a per-officer recognition counter (`department_recognitions`) that sums to 144,444 —
  slightly higher, a quirk of how the two are maintained. We use the itemized figure so the
  headline and the chart agree.
- **Training records contain known data-entry errors** per the department's own dataset
  description, so the training chart shows broad scale, not exact tallies.
- **Officer names are published by the NYPD.** This site republishes only what the
  department already releases and adds no new personal information.

## Reading the headcount

Total NYPD strength is a perennial fight at City Council budget hearings, and the raw
34,237 needs context before it is cited:

- **Recruits are in the count.** 2,194 officers are in the `RTS RECRUITS` command —
  recruits in the Police Academy, and the single largest command on the whole force —
  plus about 126 in the Police Academy itself. They are active members but not deployed.
- **So are officers who aren't working.** 776 sit at the `MILITARY & EXTENDED LEAVE DESK`,
  on military deployment or extended leave. Setting recruits and this group aside, the
  number available for duty is nearer 31,300 than 34,200.
- **Domestic-violence officers moved out of patrol.** About 504 officers hold
  domestic-violence assignments, 455 of them in precinct-numbered `DOMESTIC VIOLENCE SQUAD`
  commands that are organizationally separate from the precinct patrol roster. The work
  stayed local even as the officers were reorganized out of patrol.
- **Patrol strength is not a precinct's full footprint.** The precinct map counts patrol
  and field-training commands (13,167 officers). Another ~1,477 work in precinct-based
  detective and domestic-violence squads that fall outside that patrol count.

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
