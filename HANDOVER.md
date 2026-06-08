# Procurement Intelligence Platform — Handover Notes

## What this project is

A locally-run Python/FastAPI tool that pulls UK public procurement notices from two government
APIs plus an OCP bulk data source, stores them in a local DuckDB database, and serves a
web UI with analytical reports.

Two report types currently live:
1. **CPV report** — search by CPV code, get a picture of how authorities procure in
   that space: routes used, frameworks, top suppliers, competition levels, recent awards.
2. **Supplier report** — search by supplier name: win history, typical contract values,
   competition levels, buyer relationships. (Partial — route exists, full report in progress.)

The web UI runs via FastAPI + Jinja2 + Bootstrap 5. Run it with:
```
python run_web.py          # or: uvicorn web.app:app --reload
```

---

## Project structure

```
fts_researcher/
|- ingest.py                   # CLI: pull releases from FTS and CF live APIs
|- ocp_ingest.py               # CLI: pull PA23 notices from OCP bulk downloads
|- run_web.py                  # Starts the FastAPI dev server
|- web/
|   |- app.py                  # FastAPI routes and all DB query logic
|   |- templates/
|   |   |- base.html
|   |   |- cpv_search.html     # Search form
|   |   |- cpv_report.html     # Category report (tiles, charts, tables)
|   |   |- cpv_notices.html    # Paginated notices list (infinite scroll)
|   |   |- notice_detail.html  # Single notice detail page
|   |   |- login.html
|   +- static/
|- db/
|   |- schema.py               # DuckDB schema (raw_releases + notices tables)
|   |- store.py                # Upsert helpers for raw_releases
|   |- extractor.py            # Classifies raw OCDS releases → notices rows
|   |- migrate_v2.py           # Recreate notices with (release_id, award_id) composite PK
|   |- migrate_v3.py           # ALTER TABLE: add is_dps, is_dynamic_market columns
|   +- extract_pcr2015_fts.py  # Targeted re-extract for PCR2015_FTS notices only
|- connectors/
|   |- fts.py                  # Find a Tender Service OCDS API client
|   +- contracts_finder.py     # Contracts Finder OCDS API client
|- utils/
|   +- delta.py                # Delta sync window calculation
|- data/
|   +- cpv_lookup.json         # CPV code → description mapping
|- data.duckdb                 # Local DuckDB database (see current state below)
+- requirements.txt
```

> **IMPORTANT — OneDrive file locking:** `data.duckdb` lives inside an OneDrive-synced
> folder. OneDrive holds a Windows file lock on the DB file while uploading it (the file
> is ~3.3 GB). If a Python write process exits while OneDrive has the file open, DuckDB
> will refuse any new connections reporting "file is already open in pythoncore-3.14-64.exe
> (PID XXXX)". Fix: right-click OneDrive system tray icon → Pause syncing for 2 hours,
> then try again. Or simply restart the PC to release all handles. Strongly consider
> moving `data.duckdb` outside the OneDrive tree (e.g. `C:\Users\Cosmo\AppData\Local\`)
> to avoid this permanently.

---

## Data model

### Three data sources, three notice types (post-award only)

| Regime constant | Source | Notice form | Description |
|---|---|---|---|
| `PA23_FTS` | Find a Tender (OCP bulk or live API) | UK7 Contract Details Notice | PA23 notice published after contract signed. Richest data: real supplier, Companies House number, signed value, contract dates, tenders received. |
| `PCR2015_FTS` | Find a Tender (live API) | F03 Contract Award Notice | PCR2015 notice published on FTS under old EU Directive legal basis. Sparser: no contract period, no tender count. Includes both regular contracts AND framework/DPS establishments. |
| `PCR2015_CF` | Contracts Finder (live API) | CF award | Below-threshold PCR2015 award. Used for framework/DPS call-offs. No tender count, no authority type. |

### Why only post-award notices?

This is a **research system about things that have happened** — awarded contracts with
confirmed data. Post-award notices contain real supplier, real signed value, real contract
period. Earlier stages (tender, planning, pipeline) contain estimates and incomplete data.

**Core architectural decision:** The unit of data is the **notice**, not the contract.
Although PA23 compiled OCDS records link multiple notices per contract (UK4 → UK6 → UK7),
building the schema around contracts would create a two-tier system. Since the purpose is
consistent cross-regime analysis, one row = one award event is the right abstraction.

### How the classifier routes each raw release

The classifier in `db/extractor.py` (function `classify_release`) routes by legal basis:

```python
def classify_release(release_id, regime, data):
    if regime == "PCR2015_CF":
        return _classify_cf_award(...)          # all CF records
    if _is_pa23(data):                          # legalBasis.id == "2023/54"
        if _has_uk7(data):                      # contracts[*].documents[*].noticeType == "UK7"
            return _classify_pa23_uk7(...)
        return []                               # pre-award PA23, skip
    lb = tender.get("legalBasis") or {}
    if lb.get("id") == "32014L0024" and "award" in tags:
        return _classify_pcr2015_fts(...)       # PCR2015 F03 on FTS
    return []                                   # everything else, skip
```

Key point: PA23 records in `raw_releases` have `regime = 'PA23_FTS'`. PCR2015 FTS records
are ALSO stored under `regime = 'PA23_FTS'` in raw_releases (they come through the same FTS
API feed), but are detected by `legalBasis.id = '32014L0024'` and extracted to
`regime = 'PCR2015_FTS'` in the notices table.

### Notices table — composite primary key and multi-award expansion

```sql
PRIMARY KEY (release_id, award_id)
```

A single OCDS release can contain multiple `awards[]` entries (multi-lot tenders, framework
admissions, etc.). The extractor expands each award into a separate row:
- Single-award notices: one row with `award_id = '0'`
- Multi-award notices: one row per award, `award_id` = the OCDS award ID

`award_count` on every row reflects the total number of awards on the parent release.

**UI consequence:** The notices list page groups by `release_id` (GROUP BY) to show one
card per notice. The per-award rows are only visible on the notice detail page, which lists
all awards in a table.

### Full notices table schema

```sql
CREATE TABLE notices (
    regime                  VARCHAR,
    release_id              VARCHAR,
    award_id                VARCHAR NOT NULL DEFAULT '0',
    award_count             INTEGER,
    ocid                    VARCHAR,
    release_tags            VARCHAR,
    notice_type             VARCHAR,       -- 'UK7', 'F03', 'CF_AWARD'
    title                   VARCHAR,
    description             VARCHAR,
    cpv_main                VARCHAR,
    cpv_additional          VARCHAR,       -- CSV of additional CPV codes
    authority_name          VARCHAR,
    authority_id            VARCHAR,
    authority_type          VARCHAR,       -- e.g. "Central government"
    contact_name            VARCHAR,
    contact_email           VARCHAR,
    contact_phone           VARCHAR,
    proc_route              VARCHAR,       -- normalised: open/restricted/framework/direct_award/other
    proc_method             VARCHAR,       -- human-readable procurement method label
    is_framework_agreement  BOOLEAN,       -- TRUE = this notice ESTABLISHES a framework
    framework_ref           VARCHAR,
    has_lots                BOOLEAN,
    lot_count               INTEGER,
    value_estimated         DECIMAL(18,2),
    value_awarded           DECIMAL(18,2), -- net ex. VAT (PA23 contract.value.amount)
    value_gross             DECIMAL(18,2), -- gross inc. VAT (PA23 contract.value.amountGross)
    currency                VARCHAR,
    above_threshold         BOOLEAN,
    published_date          TIMESTAMPTZ,
    award_date              TIMESTAMPTZ,   -- use this for timeline analysis
    tender_deadline         TIMESTAMPTZ,
    contract_start          DATE,
    contract_end            DATE,
    contract_max_extent     DATE,          -- end date if all extensions exercised
    supplier_name           VARCHAR,
    supplier_id             VARCHAR,       -- Companies House / FTS ID
    tenders_received        INTEGER,
    is_termination          BOOLEAN,
    is_performance          BOOLEAN,
    is_psr                  BOOLEAN,
    status                  VARCHAR,
    is_dps                  BOOLEAN DEFAULT FALSE,  -- TRUE = DPS establishment notice
    is_dynamic_market       BOOLEAN DEFAULT FALSE,  -- TRUE = PA23 Dynamic Market establishment
    PRIMARY KEY (release_id, award_id)
)
```

### Key boolean flags

| Flag | Set by | Meaning |
|---|---|---|
| `is_framework_agreement` | PA23_FTS, PCR2015_FTS classifiers | This notice **establishes** a new framework agreement. Excluded from value/spend stats (ceiling estimates, not real spend). |
| `is_dps` | PCR2015_FTS classifier | This notice **establishes** a Dynamic Purchasing System (batch supplier admission F03). Detection: `hasDynamicPurchasingSystem=true` AND `awards[0].suppliers` has >1 entry. |
| `is_dynamic_market` | PA23_FTS classifier | This notice establishes a PA23 Dynamic Market. Detection: `techniques.dynamicPurchasingSystem` object present AND `is_framework_agreement = false`. Currently no data (PA23 DM is very new). |

### CF proc_route normalisation

CF notices use `procurementMethodDetails` free text. The extractor normalises to:

| `proc_route` | `proc_method` displayed | Trigger |
|---|---|---|
| `framework` | Below threshold - award under framework | "Call-off from a framework agreement", "Call-off from a dynamic purchasing system", or keyword match on "framework", "call-off", "g-cloud" |
| `open` | Below threshold - open competition | "Open procedure" variants |
| `restricted` | Below threshold - limited competition | "Restricted procedure" variants |
| `direct_award` | Below threshold - without competition | "Direct award", "Single tender action" variants |
| `other` | Below threshold - unknown | Everything else |

Note: "Call-off from a dynamic purchasing system" currently maps to `proc_route = 'framework'`
and `is_framework_agreement = true`. This is a known inaccuracy — DPS call-offs are not
framework call-offs. A future fix would separate them with `proc_route = 'dps_calloff'`
and `is_dps = false` (it's a call-off, not an establishment).

### PA23 proc_method values (above threshold)

Extracted verbatim from `tender.procurementMethodDetails`. Valid values under PA23:
- Open procedure
- Competitive flexible procedure
- Direct award procedure
- Award under framework
- Below threshold - by reference to dynamic market
- Below threshold - without competition
- Below threshold - limited competition
- Below threshold - open competition
- Below threshold - direct award

### PCR2015_FTS proc_method mapping

```python
_PCR2015_FTS_PROC_METHOD = {
    "Open procedure": "Open procedure",
    "Award procedure without prior publication of a call for competition": "Direct award",
    # everything else → "Other PCR2015 Procedure"
}
```

### Key report filters

Two SQL fragments applied to almost all report queries:

```python
# Exclude CF notices confirmed as above-threshold non-framework.
# These duplicate FTS data and would inflate statistics.
CF_REPORT_EXCL = "NOT (regime = 'PCR2015_CF' AND above_threshold IS TRUE AND proc_route != 'framework')"

# Exclude framework ESTABLISHMENT notices from value/spend stats.
# Their 'value' is an estimated ceiling, not real spend. CF call-offs are IS included.
FW_ESTAB = "NOT (is_framework_agreement = TRUE AND regime IN ('PA23_FTS', 'PCR2015_FTS'))"
```

### Key date fields — use the right one

- `award_date` — **use this for timeline analysis**. When the contract was
  actually awarded/signed. Extracted from `contracts[0].dateSigned` (PA23) or
  `awards[0].date` (PCR2015). Can pre-date `published_date` by months or years.
- `published_date` — when the notice appeared on the platform (`release.date`).
  Only useful for "when was this on the API", not "when was the contract awarded".
- `contract_start` / `contract_end` — populated from `contracts[0].period`.
  Always present on PA23 notices (mandatory fields). Usually absent on PCR2015.
- `contract_max_extent` — end date if all possible extensions are exercised.

---

## Current database state (as of May 2026)

| Regime | Notices rows | Distinct releases | Notes |
|---|---|---|---|
| `PA23_FTS` | ~217,000 | ~87,000 | UK7 Contract Details Notices. Multi-award expansion accounts for extra rows. |
| `PCR2015_FTS` | **96,845** | **47,681** | F03 Contract Award Notices. Includes ~118 DPS establishment notices. Multi-award expanded to 96,845 rows. |
| `PCR2015_CF` | ~99,000 | ~99,000 | CF below-threshold awards. Mostly single-award. |
| **Total** | **~413,515** | | |

Raw releases: 351,836 total (264,441 PA23_FTS + 87,395 PCR2015_CF)

### DPS / Dynamic Market counts

| Flag | Count | Notes |
|---|---|---|
| `is_framework_agreement = TRUE` (PA23_FTS) | ~1,200 | Framework establishments |
| `is_dps = TRUE` | **266** | DPS establishment notices (PCR2015_FTS only). 118 releases × multi-award expansion. |
| `is_dynamic_market = TRUE` | **0** | PA23 DM infrastructure ready; no live data yet. |

---

## DPS and Dynamic Markets — detailed findings

This was investigated carefully in May 2026. Key findings:

### What IS and IS NOT being extracted

| Notice type | Tag | Extracted? | Why |
|---|---|---|---|
| DPS establishment (F03, batch supplier admission) | `award` | YES — as `is_dps = TRUE` PCR2015_FTS rows | legalBasis 32014L0024 + award tag |
| DPS mini-competition contract awards | `award` | YES — as regular PCR2015_FTS rows | Same path; single supplier per award |
| DPS establishment (call for suppliers to apply) | `tender` | NO | No award tag → skipped |
| Supplier-joining notices for existing DPS | `tender` / `compiled` | NO | No award tag → skipped |
| PA23 Dynamic Market establishment | `tender` | NO | Tender-tagged, no contracts → skipped |
| CF DPS call-offs | `compiled` | YES — as CF_AWARD with proc_route='framework' | Detected by "Call-off from a dynamic purchasing system" pmd |

**The supplier-joining inflation concern is not a real problem.** Supplier-joining notices
for both DPS (PCR2015) and Dynamic Markets (PA23) are tender-tagged or planning-tagged, so
they are never extracted. Only genuine award-tagged notices (actual contract awards or batch
admissions) are extracted.

### DPS establishment detection

Detection signal in `db/extractor.py` (`_is_dps_establishment`):
```python
def _is_dps_establishment(tender, awards):
    techniques = tender.get("techniques") or {}
    if not techniques.get("hasDynamicPurchasingSystem", False):
        return False
    # Batch admission F03: first award lists multiple admitted suppliers
    if awards and len(awards[0].get("suppliers", [])) > 1:
        return True
    return False
```

This works because DPS establishment F03 notices list all admitted suppliers inside a
single award object (e.g. 90 suppliers admitted to the LARC DPS in one notice). Mini-
competition awards have exactly one supplier per award.

### PCR2015 DPS mini-competitions

676 records in PCR2015_FTS with `hasDynamicPurchasingSystem=true` and a single supplier
per award. These are genuine mini-competition contract awards (call-offs through the DPS),
equivalent to framework call-offs. They are included in the dataset. They often have no
contract value or date — this is a data quality limitation, not a code error.

These 676 records have `is_dps = FALSE` (they are call-offs, not establishments) and
`proc_method = "Other PCR2015 Procedure"` (Restricted procedure maps here).

---

## Web application

### CPV report tiles (cpv_report.html)

| Tile | Data source | Notes |
|---|---|---|
| Contracts Awarded | COUNT(*) | Links to notices list |
| Typical Contract Value | MEDIAN(value) FILTER (WHERE FW_ESTAB) | Median used because CF data has large outliers (COVID quarantine, etc.) |
| Most Active Suppliers | Top 3 by COUNT(*) | Excludes framework establishments |
| Avg. Tenders Received | AVG(tenders_received) | Open procedure, above-threshold only |
| New Framework Agreements | COUNT(*) FILTER (WHERE is_framework_agreement=TRUE AND regime='PA23_FTS') | Links to filtered notices list. Only PA23 shown (PCR2015 framework data is less reliable). |
| Dynamic Procurement | COUNT(is_dps), COUNT(is_dynamic_market) | Combined tile. Shows DPS and DM establishment counts separately. |
| Typical Contract Duration | AVG(contract_end - contract_start) | Mean used (not median) — short call-offs are real data. Shows full duration incl. extensions as headline, avg extension period as secondary. |

Tiles are conditional — only shown when there is data to display.

### Notices list badges (cpv_notices.html)

Badges appear on the notice card title line:

| Badge | Colour | Condition |
|---|---|---|
| CF | grey `bg-secondary` | `regime = 'PCR2015_CF'` |
| PCR2015 | grey `bg-secondary` | `regime = 'PCR2015_FTS'` |
| Framework | green `bg-success` | `is_framework_agreement=TRUE AND regime IN ('PA23_FTS','PCR2015_FTS')` |
| DPS | purple `#7c3aed` | `is_dps = TRUE` |
| Dynamic Market | teal `#0891b2` | `is_dynamic_market = TRUE` |
| Call-off | blue `bg-info` | `proc_method IN ('Award under framework', 'Below threshold - award under framework')` OR CF+framework |
| X contracts | light border | `award_count > 1` — multi-award notice |

### Notice detail page (notice_detail.html)

- "View on Find a Tender" link only shown for `PA23_FTS` and `PCR2015_FTS` (not CF).
- Single-award: supplier shown inline in the summary grid.
- Multi-award: "X contracts awarded — see table below" shown inline; full awards table at bottom.
- Value card: single-award shows gross/net; multi-award shows total across all awards.

### API endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/cpv` | CPV search form and report |
| GET | `/cpv/notices` | Notices list page (first 50, infinite scroll) |
| GET | `/api/cpv/notices` | JSON API for infinite scroll (offset/limit) |
| GET | `/notice/{release_id}` | Notice detail |
| GET | `/api/cpv-suggest` | CPV autocomplete |
| POST | `/login` | Auth |

---

## Sync architecture

### Three data sources in every sync

| Step | Script | What it does |
|---|---|---|
| 1 | `ingest.py --sync` | Delta pull from FTS and CF live APIs (date-filtered) |
| 2 | `ocp_ingest.py --sync --no-extract` | Download current-year OCP file; import new PA23 notices |
| 3 | `db.extractor` | Extract all unprocessed raw releases into `notices` |

The web UI sync button (`POST /admin/sync`) runs all three steps in sequence.

### Migration scripts

Run in order on a fresh database or after schema changes:

```bash
python db/migrate_v2.py   # Drop/recreate notices with composite PK (release_id, award_id)
python db/migrate_v3.py   # ALTER TABLE: add is_dps, is_dynamic_market columns
```

`migrate_v3.py` is safe to re-run (catches "already exists" errors).

### Manual sync commands

```bash
# Standard delta sync (run weekly)
python ingest.py --sync
python ocp_ingest.py --sync --no-extract
python -m db.extractor

# OCP full run (current + previous year — run after a long gap or new year)
python ocp_ingest.py

# OCP specific year
python ocp_ingest.py --year 2025

# FTS live API only (e.g. for troubleshooting)
python ingest.py --days 30 --source fts

# Re-extract PCR2015_FTS notices only (fast — ~821 candidates)
python db/extract_pcr2015_fts.py

# Status check
python ingest.py --status
```

### Full re-extraction

```bash
python db/migrate_v2.py    # drops notices and recreates empty
python db/migrate_v3.py    # adds is_dps, is_dynamic_market
python -m db.extractor     # ~20 mins for 351K raw_releases
```

---

## Regulatory context

**PCR2015 (old)** — Public Contracts Regulations 2015. Being phased out but still
active for call-offs against existing frameworks/DPS. Above-threshold contracts published
on FTS; below-threshold on Contracts Finder. Legal basis: CELEX `32014L0024`.

**PA23 (new)** — Procurement Act 2023, in force from **24 February 2025**. All new
procurements must use this regime. Only published on Find a Tender. Legal basis: UKPGA `2023/54`.

**PA23 notice form codes:**
- UK1: Prior information notice
- UK2: Contract notice (above threshold)
- UK3: Contract notice (below threshold)
- UK4: Dynamic Market notice (DM establishment)
- UK5: Contract award notice (above threshold — intention to award, NOT confirmed)
- UK6: Contract award notice (below threshold — intention to award)
- UK7: Transparency notice / Contract Details Notice — published after signing, confirmed data. **This is what we extract.**

**PSR** — Provider Selection Regime (health sector only). Uses PA23 or PCR2015 forms.
Flagged with `is_psr`; not a separate regime.

### Critical naming difference

"Contract Award Notice" means different things in different regimes:

| Regime | What "Contract Award" means |
|---|---|
| CF / PCR2015 FTS | Contract has been **signed** (confirmed data) |
| PA23 | **Intention to award** — still in 10-day standstill (NOT confirmed) |

This is why we capture **UK7 Contract Details Notices** for PA23, not the intention-to-award
notice. UK7 is published after the contract is signed.

### PA23 vs PCR2015 field differences

| Field | PA23 UK7 | PCR2015 F03 |
|---|---|---|
| `contract_start` / `contract_end` | Always present (mandatory) | Usually absent |
| `tenders_received` | Always present | Absent |
| `value_awarded` | `contracts[0].value.amount` | Sometimes in `awards[0].value` |
| `value_gross` | `contracts[0].value.amountGross` | Not available |
| `supplier_id` (Companies House) | Usually present | Often absent |
| `award_date` | `contracts[0].dateSigned` | `awards[0].date` (if present) |
| `above_threshold` | `contracts[0].aboveThreshold` | Inferred from pmd text |

---

## Critical discoveries (from earlier sessions)

### PA23 OCDS compiled records are full contract timelines

The OCP bulk files store PA23 notices as **compiled OCDS records** — one JSON object per
contract OCID that aggregates every notice for that contract. A single record may contain
UK4 → UK6 → UK7 in sequence. The `_has_uk7()` function checks for UK7 in
`contracts[*].documents[*].noticeType` to confirm a contract was signed before extracting.

### FTS OCDS API PA23 indexing gap

The FTS live API misses PA23 notices published between Feb 2025 and ~Mar 2026 due to a
backend indexing gap (the `updatedAt` index was not populated for PA23 during that period).
The fix is the OCP bulk import (`ocp_ingest.py`), which covers all published notices
regardless of the FTS indexing gap.

---

## API reference

### FTS live OCDS API
`GET https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages`
- Params: `updatedFrom`, `updatedTo`, `stages`, `cursor`
- Pagination: cursor from `links.next`
- Rate limit: 429 with `Retry-After` header
- **Known issue**: misses PA23 notices published before ~April 2026. Use OCP bulk to fill.

### Contracts Finder OCDS API
`GET https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search`
- Rate limit: 403 (not 429) — wait ~5 minutes
- **Platform limitation**: only returns notices from ~20 April 2026 onwards.

### OCP Data Registry (FTS bulk)
`https://data.open-contracting.org/en/publication/41`
- Files: `2025.jsonl.gz`, `2026.jsonl.gz`, `full.jsonl.gz`
- Format: one compiled OCDS record per line, gzipped JSONL
- Updated: weekly

---

## Known data quality issues

- **"Future" award dates**: Some records show award dates in 2027–2030. Data entry
  errors by authorities. Not a code issue.

- **Supplier name variants**: "Amey plc" vs "Amey PLC". Entity resolution via fuzzy
  matching is planned but not built.

- **DPS mini-comp value/date gaps**: PCR2015 DPS mini-competition awards (676 records)
  frequently have no contract value and no award date. This is a data quality limitation
  of the source notices, not an extraction error.

- **CF DPS call-offs misclassified as framework call-offs**: "Call-off from a dynamic
  purchasing system" currently maps to `proc_route='framework'` and `is_framework_agreement=TRUE`
  in CF records. This is a known inaccuracy. Fix: add a `dps_calloff` proc_route in the
  CF classifier and set `is_framework_agreement=FALSE` for these.

- **CF historical data gap**: CF API only returns notices from ~April 2026. Pre-April data
  not available via the live API. OCP has a CF bulk download (publication/128) covering
  Nov 2016 onwards but it has not been ingested.

- **Windows console encoding**: All `print()` output must use ASCII only. `UnicodeEncodeError`
  on em-dashes, arrows, pound signs etc. in terminal output (but not in files or web UI).

---

## Future work

### High priority

1. **CF historical data via OCP** — OCP publication/128 covers CF from Nov 2016 onwards.
   Files: `2021.jsonl.gz` to `2025.jsonl.gz`. Would extend CF coverage significantly.
   Check party ID prefixes in the bulk data before assuming `GB-CFS-` works for historic records.

2. **Fix CF DPS call-off classification** — Separate "Call-off from a dynamic purchasing system"
   from framework call-offs. Change to `proc_route='dps_calloff'`, `is_framework_agreement=FALSE`,
   new `proc_method="Below threshold - call-off from DPS"`. Will change ~13,220 CF rows.

3. **Entity resolution** — Supplier name deduplication via `rapidfuzz` or LLM canonicalisation.
   Would significantly improve supplier reports.

4. **Scheduled sync** — Windows Task Scheduler running the three-step sync weekly.

### Medium priority

5. **PA23 Dynamic Market extraction** — DM establishment notices are tender-tagged (not award)
   and currently skipped. Schema has `is_dynamic_market` column ready. Would need a new
   extractor path triggered by `procurementMethodDetails = "Dynamic market"`. Data is very
   sparse currently (PA23 DM is new as of 2025).

6. **PCR2015 FTS history via OCP** — OCP FTS bulk files for 2021–2024 contain historical
   PCR2015 F03 notices. `ocp_ingest.py` currently only imports PA23 records — needs extension.

7. **Supplier report** — Route exists (`/supplier`), full report implementation pending.

8. **Area/region filter on CPV report** — Filter by mid-level UK region (West Midlands, Wales,
   etc.). Both CF and PA23 have delivery area fields. Needs data inspection to confirm field
   coverage before building UI.

9. **Move data.duckdb outside OneDrive** — Prevents file-locking conflicts during sync.
   Suggested path: `C:\Users\Cosmo\AppData\Local\fts_researcher\data.duckdb`. Requires
   updating `DB_PATH` in `web/app.py` and any direct scripts.

### Low priority

10. **Multi-user auth** — Currently simple user:pass from env vars. Adequate for internal use.

11. **Report export** — CSV/XLSX download from CPV and supplier reports.

12. **Slim DB for production hosting** — Add `source_url` column to notices, then drop
    `raw_releases` table before hosting. This reduces the DB from ~3.3 GB to under 500 MB.
    Raw releases are only needed for re-extraction.
