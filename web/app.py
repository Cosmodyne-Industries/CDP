"""
web/app.py — FastAPI application for the procurement intelligence platform.
"""

import datetime
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlencode

import duckdb
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.middleware.sessions import SessionMiddleware

BASE_DIR = Path(__file__).parent.parent
DB_PATH = str(BASE_DIR / "data.duckdb")
CPV_LOOKUP_PATH = BASE_DIR / "data" / "cpv_lookup.json"
FW_PROVIDERS_PATH = BASE_DIR / "data" / "framework_providers.json"

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-in-production")

app = FastAPI(title="Procurement Intelligence")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "web" / "static")), name="static")

_jinja_env = Environment(
    loader=FileSystemLoader(str(BASE_DIR / "web" / "templates")),
    autoescape=select_autoescape(["html"]),
    cache_size=0,  # disable LRU cache — incompatible with Python 3.14
)
templates = Jinja2Templates(env=_jinja_env)


@lru_cache(maxsize=1)
def load_cpv_lookup() -> list[dict]:
    with open(CPV_LOOKUP_PATH) as f:
        return json.load(f)


@lru_cache(maxsize=1)
def load_fw_providers() -> list[dict]:
    with open(FW_PROVIDERS_PATH) as f:
        return json.load(f)


def fw_provider_for_authority(authority_name: str) -> dict | None:
    """Return the matching provider dict for an authority name, or None."""
    if not authority_name:
        return None
    name_lower = authority_name.lower()
    for p in load_fw_providers():
        if any(alias.lower() == name_lower for alias in p["authority_names"]):
            return p
    return None


def db() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(DB_PATH, read_only=True)


def _format_fts_ref(release_id: str) -> str:
    """Format FTS release_id 'NNNNNN-YYYY' as the standard 'YYYY/S 000-NNNNNN' reference."""
    parts = release_id.split("-")
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 4:
        return f"{parts[1]}/S 000-{parts[0]}"
    return release_id


def _format_duration(days: float | None) -> str | None:
    if not days:
        return None
    months = round(days / 30.44)
    years, rem = divmod(months, 12)
    if years and rem:
        return f"{years}y {rem}m"
    return f"{years}y" if years else f"{months}m"


def _row_to_dict(cols: list[str], row: tuple) -> dict:
    """Convert a DB row to a dict, serialising dates/datetimes to ISO strings."""
    import datetime as dt
    result = {}
    for k, v in zip(cols, row):
        if isinstance(v, (dt.datetime, dt.date)):
            result[k] = v.isoformat()
        else:
            result[k] = v
    return result


def cpv_filter(code: str, sub: bool) -> tuple[str, list]:
    """
    Return (WHERE clause, params) for filtering notices by CPV code.

    Exact mode (sub=False): match only notices that used this specific code.
    Sub-code mode (sub=True): prefix-match — strips trailing zeros to find the
    hierarchical prefix (e.g. '79710000' → '7971'), then matches any code
    starting with that prefix.
    """
    if not sub:
        return (
            "cpv_main = ? OR cpv_additional LIKE ?",
            [code, f"%{code}%"],
        )
    prefix = code.rstrip("0") or code[:2]
    return (
        "cpv_main LIKE ? OR cpv_additional LIKE ?",
        [f"{prefix}%", f"%{prefix}%"],
    )


def build_notice_where(code: str, sub: bool, kw1: str, kw2: str, op: str) -> tuple[str, list]:
    """Build the master WHERE clause for notice queries combining CPV + keywords."""
    if code:
        where, params = cpv_filter(code, sub)
    else:
        where, params = "1=1", []
    kw_where, kw_params = dual_keyword_filter(kw1, kw2, op)
    if kw_where:
        where = f"({where}) AND ({kw_where})" if code else kw_where
        params = params + kw_params
    return where, params


def parse_keywords(q: str) -> list[str]:
    """
    Parse keyword query into a list of terms (all must match — AND logic).
    Quoted phrases are treated as exact ordered sequences.
    e.g. 'security "managed print"' → ['managed print', 'security']
    """
    if not q or not q.strip():
        return []
    terms = [m.group(1).strip() for m in re.finditer(r'"([^"]+)"', q)]
    remainder = re.sub(r'"[^"]+"', "", q).strip()
    terms += [w for w in remainder.split() if w]
    return [t for t in terms if t]


def keyword_filter(terms: list[str]) -> tuple[str, list]:
    """AND-join ILIKE conditions for each keyword term against title + description."""
    if not terms:
        return "", []
    clauses, params = [], []
    for term in terms:
        clauses.append("(title ILIKE ? OR description ILIKE ?)")
        params.extend([f"%{term}%", f"%{term}%"])
    return " AND ".join(clauses), params


# CF above-threshold notices are excluded from all report queries.
# They duplicate FTS data and would inflate statistics. Included in notice search only.
# Exclude CF notices that are confirmed above-threshold AND not a framework call-off.
# Above-threshold CF would duplicate FTS data. Framework call-offs are included regardless
# of threshold (they're direct spend, not a parallel above-threshold notice on FTS).
CF_REPORT_EXCL = "NOT (regime = 'PCR2015_CF' AND above_threshold IS TRUE AND proc_route != 'framework')"


def dual_keyword_filter(kw1: str, kw2: str, op: str) -> tuple[str, list]:
    """
    Build WHERE clause for two keyword bars with AND/OR logic between them.
    Within each bar all terms must match (AND). The op controls bar1 vs bar2.
    """
    c1, p1 = keyword_filter(parse_keywords(kw1))
    c2, p2 = keyword_filter(parse_keywords(kw2))
    if c1 and c2:
        join = "OR" if op == "or" else "AND"
        return f"({c1}) {join} ({c2})", p1 + p2
    return (c1, p1) if c1 else (c2, p2)


def logged_in(request: Request) -> bool:
    return request.session.get("logged_in", False)


AUTH_DISABLED = os.getenv("AUTH_DISABLED", "false").lower() == "true"


def require_auth(request: Request) -> Optional[RedirectResponse]:
    if AUTH_DISABLED:
        return None
    if not logged_in(request):
        return RedirectResponse(url="/login", status_code=302)
    return None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.get("/login")
async def login_get(request: Request):
    if logged_in(request):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USER and password == ADMIN_PASS:
        request.session["logged_in"] = True
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": "Invalid credentials"})


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


# ---------------------------------------------------------------------------
# Home → redirect to CPV search
# ---------------------------------------------------------------------------

@app.get("/")
async def index(request: Request):
    redir = require_auth(request)
    if redir:
        return redir
    return templates.TemplateResponse(request, "home.html", {})


# ---------------------------------------------------------------------------
# CPV search
# ---------------------------------------------------------------------------

@app.get("/cpv")
async def cpv_search(request: Request, code: str = "",
                     sub: bool = True, kw1: str = "", kw2: str = "", op: str = "and"):
    redir = require_auth(request)
    if redir:
        return redir

    if not code and not kw1 and not kw2:
        return templates.TemplateResponse(request, "cpv_search.html",
                                          {"sub": sub, "kw1": kw1, "kw2": kw2, "op": op})

    cpv_list = load_cpv_lookup()
    cpv_desc = next((c["description"] for c in cpv_list if c["code"] == code), None)

    where, params = build_notice_where(code, sub, kw1, kw2, op)

    con = db()
    # Framework establishments (PA23 is_framework_agreement=TRUE) are excluded from
    # value/spend stats — their "value" is an estimated max, not real spend.
    # CF call-offs (also is_framework_agreement=TRUE on CF) ARE real awards, so included.
    FW_ESTAB = "NOT (is_framework_agreement = TRUE AND regime IN ('PA23_FTS', 'PCR2015_FTS'))"

    row = con.execute(f"""
        SELECT
            COUNT(*)                                                               AS notice_count,
            AVG(COALESCE(value_gross, value_awarded))
                FILTER (WHERE {FW_ESTAB})                                          AS avg_value,
            MEDIAN(COALESCE(value_gross, value_awarded))
                FILTER (WHERE {FW_ESTAB})                                          AS median_value,
            MIN(CAST(award_date AS DATE))                                          AS earliest,
            MAX(CAST(award_date AS DATE))                                          AS latest
        FROM notices
        WHERE ({where}) AND {CF_REPORT_EXCL}
    """, params).fetchone()

    proc_rows = con.execute(f"""
        SELECT
            CASE
                WHEN proc_route = 'framework' THEN 'Award under Framework'
                ELSE COALESCE(proc_method, 'Unknown')
            END AS method,
            COUNT(*) AS n
        FROM notices
        WHERE ({where}) AND {CF_REPORT_EXCL}
        GROUP BY 1
        ORDER BY n DESC
    """, params).fetchall()

    avg_tenders_row = con.execute(f"""
        SELECT MEDIAN(tenders_received)
        FROM notices
        WHERE ({where})
          AND above_threshold = TRUE
          AND proc_method = 'Open procedure'
          AND tenders_received IS NOT NULL
          AND tenders_received > 0
    """, params).fetchone()

    fw_row = con.execute(f"""
        SELECT COUNT(DISTINCT release_id), SUM(COALESCE(value_gross, value_awarded))
        FROM notices
        WHERE ({where}) AND {CF_REPORT_EXCL}
          AND is_framework_agreement = TRUE AND regime IN ('PA23_FTS', 'PCR2015_FTS')
    """, params).fetchone()

    dps_dm_row = con.execute(f"""
        SELECT
            COUNT(*) FILTER (WHERE is_dps = TRUE)            AS dps_count,
            COUNT(*) FILTER (WHERE is_dynamic_market = TRUE) AS dm_count
        FROM notices
        WHERE ({where}) AND {CF_REPORT_EXCL}
    """, params).fetchone()

    recent_buyer_rows = con.execute(f"""
        SELECT release_id, title, authority_name, contact_name, contact_email, award_date
        FROM notices
        WHERE ({where}) AND {CF_REPORT_EXCL} AND authority_name IS NOT NULL
        ORDER BY award_date DESC NULLS LAST
        LIMIT 3
    """, params).fetchall()

    top_supplier_rows = con.execute(f"""
        SELECT supplier_name, COUNT(*) AS n,
               SUM(COALESCE(value_gross, value_awarded)) AS total
        FROM notices
        WHERE ({where}) AND {CF_REPORT_EXCL}
          AND supplier_name IS NOT NULL
          AND {FW_ESTAB}
        GROUP BY supplier_name
        ORDER BY total DESC NULLS LAST
        LIMIT 5
    """, params).fetchall()

    top_by_count_rows = con.execute(f"""
        SELECT supplier_name, COUNT(*) AS n
        FROM notices
        WHERE ({where}) AND {CF_REPORT_EXCL}
          AND supplier_name IS NOT NULL
          AND {FW_ESTAB}
        GROUP BY supplier_name
        ORDER BY n DESC
        LIMIT 5
    """, params).fetchall()

    renewal_count_row = con.execute(f"""
        SELECT COUNT(*)
        FROM notices
        WHERE ({where})
          AND above_threshold = TRUE
          AND proc_method IN ('Open procedure', 'Competitive flexible procedure')
          AND contract_end >= CURRENT_DATE
          AND contract_end <= CURRENT_DATE + INTERVAL 18 MONTH
    """, params).fetchone()

    renewal_rows = con.execute(f"""
        SELECT release_id, title, authority_name, contract_end, contract_max_extent
        FROM notices
        WHERE ({where})
          AND above_threshold = TRUE
          AND proc_method IN ('Open procedure', 'Competitive flexible procedure')
          AND contract_end >= CURRENT_DATE
          AND contract_end <= CURRENT_DATE + INTERVAL 18 MONTH
        ORDER BY contract_end ASC
        LIMIT 5
    """, params).fetchall()

    duration_row = con.execute(f"""
        SELECT
            AVG(DATEDIFF('day', contract_start, contract_end))                        AS avg_base_days,
            AVG(DATEDIFF('day', contract_start, COALESCE(contract_max_extent, contract_end))) AS avg_full_days
        FROM notices
        WHERE ({where}) AND {CF_REPORT_EXCL}
          AND contract_start IS NOT NULL AND contract_end IS NOT NULL
          AND {FW_ESTAB}
    """, params).fetchone()

    spend_rows = con.execute(f"""
        SELECT YEAR(award_date) AS yr, COUNT(*) AS n,
               SUM(COALESCE(value_gross, value_awarded))
                   FILTER (WHERE {FW_ESTAB}) AS total
        FROM notices
        WHERE ({where}) AND {CF_REPORT_EXCL} AND award_date IS NOT NULL
        GROUP BY yr ORDER BY yr
    """, params).fetchall()

    con.close()

    notice_count, avg_value, median_value, earliest, latest = row
    proc_methods = [{"method": r[0], "count": r[1]} for r in proc_rows]
    avg_tenders = avg_tenders_row[0]
    fw_count, fw_total = fw_row
    dps_count, dm_count = dps_dm_row[0] or 0, dps_dm_row[1] or 0

    buyer_cols = ["release_id", "title", "authority_name", "contact_name", "contact_email", "award_date"]
    recent_buyers = [_row_to_dict(buyer_cols, r) for r in recent_buyer_rows]

    top_suppliers = [{"name": r[0], "count": r[1], "total": r[2]} for r in top_supplier_rows]
    top_by_count = [{"name": r[0], "count": r[1]} for r in top_by_count_rows]

    renewal_count = renewal_count_row[0]
    renewal_cols = ["release_id", "title", "authority_name", "contract_end", "contract_max_extent"]
    upcoming_renewals = [_row_to_dict(renewal_cols, r) for r in renewal_rows]

    avg_base_days, avg_full_days = duration_row[0], duration_row[1]
    avg_base_str = _format_duration(avg_base_days)
    avg_full_str = _format_duration(avg_full_days)
    ext_days = (avg_full_days - avg_base_days) if (avg_full_days and avg_base_days) else None
    avg_extension_str = _format_duration(ext_days) if ext_days and ext_days > 30 else None

    spend_chart = [
        {"year": r[0], "count": r[1], "total": float(r[2]) if r[2] else 0}
        for r in spend_rows if r[0] and r[0] >= 2022
    ]

    return templates.TemplateResponse(request, "cpv_report.html", {
        "code": code,
        "description": cpv_desc,
        "sub": sub,
        "kw1": kw1,
        "kw2": kw2,
        "op": op,
        "notice_count": notice_count or 0,
        "avg_value": avg_value,
        "median_value": median_value,
        "earliest": earliest,
        "latest": latest,
        "avg_tenders": avg_tenders,
        "fw_count": fw_count or 0,
        "fw_total": fw_total,
        "dps_count": dps_count,
        "dm_count": dm_count,
        "proc_methods": proc_methods,
        "recent_buyers": recent_buyers,
        "top_suppliers": top_suppliers,
        "top_by_count": top_by_count,
        "renewal_count": renewal_count,
        "upcoming_renewals": upcoming_renewals,
        "avg_base_str": avg_base_str,
        "avg_full_str": avg_full_str,
        "avg_extension_str": avg_extension_str,
        "spend_chart": spend_chart,
    })


# ---------------------------------------------------------------------------
# CPV notices list (infinite scroll page)
# ---------------------------------------------------------------------------

@app.get("/cpv/notices")
async def cpv_notices(request: Request, code: str = "",
                      sub: bool = True, kw1: str = "", kw2: str = "", op: str = "and",
                      proc_method: str = "", is_framework: Optional[bool] = None,
                      notice_type: str = "", dps: Optional[bool] = None):
    redir = require_auth(request)
    if redir:
        return redir

    cpv_list = load_cpv_lookup()
    cpv_desc = next((c["description"] for c in cpv_list if c["code"] == code), None)

    where, params = build_notice_where(code, sub, kw1, kw2, op)
    where = f"({where}) AND {CF_REPORT_EXCL}"
    if proc_method:
        if proc_method == "Unknown":
            where = f"({where}) AND proc_method IS NULL"
        elif proc_method == "Award under Framework":
            where = f"({where}) AND proc_route = 'framework'"
        else:
            where = f"({where}) AND proc_method = ?"
            params = params + [proc_method]
    if is_framework is True:
        # Framework tile: establishment notices only — excludes CF call-offs
        where = f"({where}) AND is_framework_agreement = TRUE AND regime IN ('PA23_FTS', 'PCR2015_FTS')"
    elif is_framework is False:
        where = f"({where}) AND (is_framework_agreement IS FALSE OR is_framework_agreement IS NULL)"
    if notice_type:
        where = f"({where}) AND notice_type = ?"
        params = params + [notice_type]
    if dps:
        where = f"({where}) AND (is_dps = TRUE OR is_dynamic_market = TRUE)"

    con = db()
    total = con.execute(
        f"SELECT COUNT(DISTINCT release_id) FROM notices WHERE {where}", params
    ).fetchone()[0]

    rows = con.execute(f"""
        SELECT release_id,
               MAX(title)                                        AS title,
               MAX(authority_name)                              AS authority_name,
               MIN(supplier_name)                               AS supplier_name,
               SUM(COALESCE(value_gross, value_awarded))        AS value,
               MAX(award_date)                                  AS award_date,
               MAX(award_count)                                 AS award_count,
               MAX(is_framework_agreement::INTEGER)::BOOLEAN    AS is_framework_agreement,
               MAX(regime)                                      AS regime,
               MAX(proc_route)                                  AS proc_route,
               MAX(proc_method)                                 AS proc_method,
               MAX(is_dps::INTEGER)::BOOLEAN                    AS is_dps,
               MAX(is_dynamic_market::INTEGER)::BOOLEAN         AS is_dynamic_market,
               MAX(contract_end)                                AS contract_end,
               MAX(contract_max_extent)                         AS contract_max_extent
        FROM notices
        WHERE {where}
        GROUP BY release_id
        ORDER BY MAX(award_date) DESC NULLS LAST
        LIMIT 50
    """, params).fetchall()
    con.close()

    cols = ["release_id", "title", "authority_name", "supplier_name",
            "value", "award_date", "award_count",
            "is_framework_agreement", "regime", "proc_route", "proc_method",
            "is_dps", "is_dynamic_market", "contract_end", "contract_max_extent"]
    notices = [_row_to_dict(cols, r) for r in rows]

    return templates.TemplateResponse(request, "cpv_notices.html", {
        "code": code,
        "description": cpv_desc,
        "sub": sub,
        "kw1": kw1,
        "kw2": kw2,
        "op": op,
        "proc_method": proc_method,
        "is_framework": is_framework,
        "notice_type": notice_type,
        "dps": dps,
        "notices": notices,
        "total": total,
        "today": datetime.date.today().isoformat(),
    })


# ---------------------------------------------------------------------------
# CPV notices JSON API (infinite scroll)
# ---------------------------------------------------------------------------

@app.get("/api/cpv/notices")
async def api_cpv_notices(code: str = "", sub: bool = True, kw1: str = "", kw2: str = "",
                          op: str = "and", proc_method: str = "",
                          is_framework: Optional[bool] = None, notice_type: str = "",
                          dps: Optional[bool] = None, offset: int = 0, limit: int = 50):
    limit = min(limit, 100)
    where, params = build_notice_where(code, sub, kw1, kw2, op)
    where = f"({where}) AND {CF_REPORT_EXCL}"
    if proc_method:
        if proc_method == "Unknown":
            where = f"({where}) AND proc_method IS NULL"
        elif proc_method == "Award under Framework":
            where = f"({where}) AND proc_route = 'framework'"
        else:
            where = f"({where}) AND proc_method = ?"
            params = params + [proc_method]
    if is_framework is True:
        where = f"({where}) AND is_framework_agreement = TRUE AND regime IN ('PA23_FTS', 'PCR2015_FTS')"
    elif is_framework is False:
        where = f"({where}) AND (is_framework_agreement IS FALSE OR is_framework_agreement IS NULL)"
    if notice_type:
        where = f"({where}) AND notice_type = ?"
        params = params + [notice_type]
    if dps:
        where = f"({where}) AND (is_dps = TRUE OR is_dynamic_market = TRUE)"
    con = db()
    rows = con.execute(f"""
        SELECT release_id,
               MAX(title)                                        AS title,
               MAX(authority_name)                              AS authority_name,
               MIN(supplier_name)                               AS supplier_name,
               SUM(COALESCE(value_gross, value_awarded))        AS value,
               MAX(award_date)                                  AS award_date,
               MAX(award_count)                                 AS award_count,
               MAX(is_framework_agreement::INTEGER)::BOOLEAN    AS is_framework_agreement,
               MAX(regime)                                      AS regime,
               MAX(proc_route)                                  AS proc_route,
               MAX(proc_method)                                 AS proc_method,
               MAX(is_dps::INTEGER)::BOOLEAN                    AS is_dps,
               MAX(is_dynamic_market::INTEGER)::BOOLEAN         AS is_dynamic_market,
               MAX(contract_end)                                AS contract_end,
               MAX(contract_max_extent)                         AS contract_max_extent
        FROM notices
        WHERE {where}
        GROUP BY release_id
        ORDER BY MAX(award_date) DESC NULLS LAST
        LIMIT ? OFFSET ?
    """, params + [limit, offset]).fetchall()
    con.close()
    cols = ["release_id", "title", "authority_name", "supplier_name",
            "value", "award_date", "award_count",
            "is_framework_agreement", "regime", "proc_route", "proc_method",
            "is_dps", "is_dynamic_market", "contract_end", "contract_max_extent"]
    return [_row_to_dict(cols, r) for r in rows]


# ---------------------------------------------------------------------------
# Notice detail
# ---------------------------------------------------------------------------

@app.get("/notice/{release_id}")
async def notice_detail(request: Request, release_id: str):
    redir = require_auth(request)
    if redir:
        return redir

    con = db()
    rows = con.execute("""
        SELECT regime, release_id, award_id, award_count, notice_type,
               title, description, cpv_main, cpv_additional,
               authority_name, authority_type, contact_name, contact_email,
               proc_route, proc_method, is_framework_agreement,
               above_threshold, published_date, tenders_received,
               supplier_name, supplier_id,
               value_awarded, value_gross, award_date,
               contract_start, contract_end, contract_max_extent,
               ocid
        FROM notices WHERE release_id = ?
        ORDER BY award_id
    """, [release_id]).fetchall()

    if not rows:
        con.close()
        from fastapi.responses import Response
        return Response("Notice not found", status_code=404)

    cols = ["regime", "release_id", "award_id", "award_count", "notice_type",
            "title", "description", "cpv_main", "cpv_additional",
            "authority_name", "authority_type", "contact_name", "contact_email",
            "proc_route", "proc_method", "is_framework_agreement",
            "above_threshold", "published_date", "tenders_received",
            "supplier_name", "supplier_id",
            "value_awarded", "value_gross", "award_date",
            "contract_start", "contract_end", "contract_max_extent",
            "ocid"]
    award_rows = [_row_to_dict(cols, r) for r in rows]
    n = award_rows[0]  # release-level metadata from first row

    # Build the external source URL for the "View on …" button.
    # CF: the correct notice URL lives in awards[*].documents[*].url (documentType=awardNotice)
    # in the raw OCDS data — it uses an internal GUID unrelated to the OCID.
    source_url = None
    source_label = None
    if n["regime"] in ("PA23_FTS", "PCR2015_FTS"):
        source_url = f"https://www.find-tender.service.gov.uk/Notice/{n['release_id']}"
        source_label = "View on Find a Tender"
    elif n["regime"] == "PCR2015_CF":
        raw_row = con.execute(
            "SELECT raw_json FROM raw_releases WHERE release_id = ?",
            [n["release_id"]]
        ).fetchone()
        if raw_row:
            raw_data = json.loads(raw_row[0]) if isinstance(raw_row[0], str) else raw_row[0]
            for award in raw_data.get("awards", []):
                for doc in award.get("documents", []):
                    if doc.get("documentType") == "awardNotice" and doc.get("url"):
                        source_url = doc["url"]
                        source_label = "View on Contracts Finder"
                        break
                if source_url:
                    break

    con.close()

    if n["regime"] == "PCR2015_FTS":
        display_ref = _format_fts_ref(n["release_id"])
    elif n["regime"] == "PCR2015_CF":
        display_ref = n.get("ocid") or n["release_id"]
    else:
        display_ref = n["release_id"]

    back_url = request.headers.get("referer", "")

    return templates.TemplateResponse(request, "notice_detail.html", {
        "n": n,
        "awards": award_rows,
        "back_url": back_url,
        "source_url": source_url,
        "source_label": source_label,
        "display_ref": display_ref,
    })


# ---------------------------------------------------------------------------
# CPV autocomplete API
# ---------------------------------------------------------------------------

@app.get("/api/cpv-suggest")
async def cpv_suggest(q: str = "", limit: int = 12):
    if len(q) < 2:
        return []
    q_lower = q.lower().strip()
    cpv_list = load_cpv_lookup()
    matches = []
    for item in cpv_list:
        code = item["code"]
        desc = item["description"].lower()
        if code.startswith(q_lower) or q_lower in desc:
            matches.append(item)
        if len(matches) >= limit:
            break
    return matches


# ---------------------------------------------------------------------------
# Notice search
# ---------------------------------------------------------------------------

def build_search_where(kw1: str, kw2: str, op: str, code: str, sub: bool,
                       supplier: str, authority: str,
                       date_from: str, date_to: str) -> tuple[str, list]:
    """Build WHERE clause for the notice search from any combination of filters."""
    clauses: list[str] = []
    params: list = []

    kw_where, kw_params = dual_keyword_filter(kw1, kw2, op)
    if kw_where:
        clauses.append(f"({kw_where})")
        params.extend(kw_params)

    if code:
        cpv_clause, cpv_params = cpv_filter(code, sub)
        clauses.append(f"({cpv_clause})")
        params.extend(cpv_params)

    if supplier:
        clauses.append("supplier_name ILIKE ?")
        params.append(f"%{supplier}%")

    if authority:
        clauses.append("authority_name ILIKE ?")
        params.append(f"%{authority}%")

    if date_from:
        clauses.append("CAST(award_date AS DATE) >= ?")
        params.append(date_from)

    if date_to:
        clauses.append("CAST(award_date AS DATE) <= ?")
        params.append(date_to)

    return (" AND ".join(clauses) if clauses else "1=0"), params


@app.get("/search")
async def notice_search(request: Request, kw1: str = "", kw2: str = "",
                        op: str = "and", code: str = "", sub: bool = True,
                        supplier: str = "", authority: str = "",
                        date_from: str = "", date_to: str = "",
                        modify: bool = False):
    redir = require_auth(request)
    if redir:
        return redir

    today = datetime.date.today().isoformat()
    ctx = {
        "kw1": kw1, "kw2": kw2, "op": op,
        "code": code, "sub": sub,
        "supplier": supplier, "authority": authority,
        "date_from": date_from, "date_to": date_to,
        "today": today,
    }

    has_query = any([kw1, kw2, code, supplier, authority, date_from, date_to])
    if not has_query or modify:
        return templates.TemplateResponse(request, "search.html", ctx)

    where, params = build_search_where(kw1, kw2, op, code, sub, supplier, authority, date_from, date_to)
    con = db()
    total = con.execute(
        f"SELECT COUNT(DISTINCT release_id) FROM notices WHERE {where}", params
    ).fetchone()[0]

    rows = con.execute(f"""
        SELECT release_id,
               MAX(title)                                        AS title,
               MAX(authority_name)                              AS authority_name,
               MIN(supplier_name)                               AS supplier_name,
               SUM(COALESCE(value_gross, value_awarded))        AS value,
               MAX(award_date)                                  AS award_date,
               MAX(award_count)                                 AS award_count,
               MAX(is_framework_agreement::INTEGER)::BOOLEAN    AS is_framework_agreement,
               MAX(regime)                                      AS regime,
               MAX(proc_route)                                  AS proc_route,
               MAX(proc_method)                                 AS proc_method,
               MAX(is_dps::INTEGER)::BOOLEAN                    AS is_dps,
               MAX(is_dynamic_market::INTEGER)::BOOLEAN         AS is_dynamic_market,
               MAX(contract_end)                                AS contract_end,
               MAX(contract_max_extent)                         AS contract_max_extent
        FROM notices
        WHERE {where}
        GROUP BY release_id
        ORDER BY MAX(award_date) DESC NULLS LAST
        LIMIT 50
    """, params).fetchall()
    con.close()

    cols = ["release_id", "title", "authority_name", "supplier_name",
            "value", "award_date", "award_count",
            "is_framework_agreement", "regime", "proc_route", "proc_method",
            "is_dps", "is_dynamic_market", "contract_end", "contract_max_extent"]
    notices = [_row_to_dict(cols, r) for r in rows]

    return templates.TemplateResponse(request, "search_results.html", {
        **ctx,
        "notices": notices,
        "total": total,
    })


@app.get("/api/search/notices")
async def api_search_notices(kw1: str = "", kw2: str = "",
                             op: str = "and", code: str = "", sub: bool = True,
                             supplier: str = "", authority: str = "",
                             date_from: str = "", date_to: str = "",
                             offset: int = 0, limit: int = 50):
    limit = min(limit, 100)
    where, params = build_search_where(kw1, kw2, op, code, sub, supplier, authority, date_from, date_to)
    con = db()
    rows = con.execute(f"""
        SELECT release_id,
               MAX(title)                                        AS title,
               MAX(authority_name)                              AS authority_name,
               MIN(supplier_name)                               AS supplier_name,
               SUM(COALESCE(value_gross, value_awarded))        AS value,
               MAX(award_date)                                  AS award_date,
               MAX(award_count)                                 AS award_count,
               MAX(is_framework_agreement::INTEGER)::BOOLEAN    AS is_framework_agreement,
               MAX(regime)                                      AS regime,
               MAX(proc_route)                                  AS proc_route,
               MAX(proc_method)                                 AS proc_method,
               MAX(is_dps::INTEGER)::BOOLEAN                    AS is_dps,
               MAX(is_dynamic_market::INTEGER)::BOOLEAN         AS is_dynamic_market,
               MAX(contract_end)                                AS contract_end,
               MAX(contract_max_extent)                         AS contract_max_extent
        FROM notices
        WHERE {where}
        GROUP BY release_id
        ORDER BY MAX(award_date) DESC NULLS LAST
        LIMIT ? OFFSET ?
    """, params + [limit, offset]).fetchall()
    con.close()
    cols = ["release_id", "title", "authority_name", "supplier_name",
            "value", "award_date", "award_count",
            "is_framework_agreement", "regime", "proc_route", "proc_method",
            "is_dps", "is_dynamic_market", "contract_end", "contract_max_extent"]
    return [_row_to_dict(cols, r) for r in rows]


@app.get("/api/supplier-suggest")
async def supplier_suggest(q: str = "", limit: int = 8):
    if len(q) < 2:
        return []
    con = db()
    rows = con.execute("""
        SELECT supplier_name, COUNT(*) AS n
        FROM notices
        WHERE supplier_name ILIKE ? AND supplier_name IS NOT NULL
        GROUP BY supplier_name
        ORDER BY n DESC
        LIMIT ?
    """, [f"%{q}%", limit]).fetchall()
    con.close()
    return [{"name": r[0], "count": r[1]} for r in rows]


@app.get("/api/authority-suggest")
async def authority_suggest(q: str = "", limit: int = 8):
    if len(q) < 2:
        return []
    con = db()
    rows = con.execute("""
        SELECT authority_name, COUNT(*) AS n
        FROM notices
        WHERE authority_name ILIKE ? AND authority_name IS NOT NULL
        GROUP BY authority_name
        ORDER BY n DESC
        LIMIT ?
    """, [f"%{q}%", limit]).fetchall()
    con.close()
    return [{"name": r[0], "count": r[1]} for r in rows]


# ---------------------------------------------------------------------------
# Supplier report
# ---------------------------------------------------------------------------

def supplier_name_filter(names: list[str]) -> tuple[str, list]:
    """Build WHERE clause matching any of the given supplier names (case-insensitive exact)."""
    if not names:
        return "1=0", []
    clauses = ["supplier_name ILIKE ?" for _ in names]
    return " OR ".join(clauses), list(names)


@app.get("/supplier")
async def supplier_search(request: Request, name: List[str] = Query(default=[])):
    redir = require_auth(request)
    if redir:
        return redir
    if name:
        qs = urlencode([("name", n) for n in name])
        return RedirectResponse(url=f"/supplier/report?{qs}", status_code=302)
    return templates.TemplateResponse(request, "supplier_search.html", {})


@app.get("/supplier/report")
async def supplier_report(request: Request, name: List[str] = Query(default=[])):
    redir = require_auth(request)
    if redir:
        return redir
    names = [n for n in name if n.strip()]
    if not names:
        return RedirectResponse(url="/supplier", status_code=302)

    # Exclude framework/DPS establishments and above-threshold CF notices from spend figures
    FW_ESTAB = (
        "NOT ((is_framework_agreement = TRUE AND regime IN ('PA23_FTS', 'PCR2015_FTS'))"
        "     OR is_dps = TRUE OR is_dynamic_market = TRUE"
        "     OR (regime = 'PCR2015_CF' AND above_threshold IS TRUE AND proc_route != 'framework'))"
    )
    name_where, name_params = supplier_name_filter(names)
    notices_qs = urlencode([("name", n) for n in names])
    notices_url = f"/supplier/notices?{notices_qs}"

    con = db()

    row = con.execute(f"""
        SELECT
            COUNT(DISTINCT release_id)                                              AS notice_count,
            SUM(COALESCE(value_gross, value_awarded)) FILTER (WHERE {FW_ESTAB})     AS total_spend,
            MEDIAN(COALESCE(value_gross, value_awarded)) FILTER (WHERE {FW_ESTAB})  AS median_value,
            MIN(CAST(award_date AS DATE))                                            AS earliest,
            MAX(CAST(award_date AS DATE))                                            AS latest
        FROM notices
        WHERE {name_where}
    """, name_params).fetchone()

    id_row = con.execute(f"""
        SELECT supplier_id, COUNT(*) AS n
        FROM notices
        WHERE ({name_where}) AND supplier_id IS NOT NULL
        GROUP BY supplier_id
        ORDER BY n DESC
        LIMIT 1
    """, name_params).fetchone()

    ch_url = None
    if id_row and id_row[0] and id_row[0].startswith("GB-COH-"):
        ch_url = (
            "https://find-and-update.company-information.service.gov.uk/company/"
            + id_row[0][7:]
        )

    buyer_rows = con.execute(f"""
        SELECT authority_name, COUNT(DISTINCT release_id) AS n,
               SUM(COALESCE(value_gross, value_awarded)) FILTER (WHERE {FW_ESTAB}) AS total
        FROM notices
        WHERE ({name_where}) AND authority_name IS NOT NULL
        GROUP BY authority_name
        ORDER BY n DESC
        LIMIT 8
    """, name_params).fetchall()

    cpv_rows = con.execute(f"""
        SELECT cpv_main, COUNT(DISTINCT release_id) AS n
        FROM notices
        WHERE ({name_where}) AND cpv_main IS NOT NULL
        GROUP BY cpv_main
        ORDER BY n DESC
        LIMIT 6
    """, name_params).fetchall()

    spend_rows = con.execute(f"""
        SELECT YEAR(award_date) AS yr, COUNT(DISTINCT release_id) AS n,
               SUM(COALESCE(value_gross, value_awarded)) FILTER (WHERE {FW_ESTAB}) AS total
        FROM notices
        WHERE ({name_where}) AND award_date IS NOT NULL
        GROUP BY yr ORDER BY yr
    """, name_params).fetchall()

    framework_rows = con.execute(f"""
        SELECT release_id,
               MAX(title)                                        AS title,
               MAX(regime)                                       AS regime,
               MAX(is_framework_agreement::INTEGER)::BOOLEAN    AS is_framework_agreement,
               MAX(is_dps::INTEGER)::BOOLEAN                    AS is_dps,
               MAX(is_dynamic_market::INTEGER)::BOOLEAN         AS is_dynamic_market,
               MAX(contract_end)                                 AS contract_end,
               MAX(contract_max_extent)                          AS contract_max_extent,
               MAX(award_date)                                   AS award_date
        FROM notices
        WHERE ({name_where})
          AND (
            (is_framework_agreement = TRUE AND regime IN ('PA23_FTS', 'PCR2015_FTS'))
            OR is_dps = TRUE
            OR is_dynamic_market = TRUE
          )
        GROUP BY release_id
        ORDER BY MAX(contract_end) DESC NULLS LAST
    """, name_params).fetchall()

    con.close()

    cpv_list = load_cpv_lookup()
    cpv_lookup_map = {c["code"]: c["description"] for c in cpv_list}

    notice_count, total_spend, median_value, earliest, latest = row

    fw_cols = ["release_id", "title", "regime", "is_framework_agreement",
               "is_dps", "is_dynamic_market", "contract_end", "contract_max_extent", "award_date"]
    frameworks = [_row_to_dict(fw_cols, r) for r in framework_rows]

    # Compute status and keep only active / likely active
    today_str = datetime.date.today().isoformat()
    fya_str   = datetime.date.today().replace(year=datetime.date.today().year - 4).isoformat()
    for fw in frameworks:
        best_end = fw["contract_max_extent"] or fw["contract_end"]
        be = best_end[:10] if best_end else None
        if be and be > today_str:
            fw["status"] = "active"
        elif be:
            fw["status"] = "likely_expired"
        else:
            ad = (fw["award_date"] or "")[:10]
            fw["status"] = "likely_active" if ad >= fya_str else "likely_expired"
    frameworks = [fw for fw in frameworks if fw["status"] in ("active", "likely_active")]

    return templates.TemplateResponse(request, "supplier_report.html", {
        "names": names,
        "notices_url": notices_url,
        "ch_url": ch_url,
        "notice_count": notice_count or 0,
        "total_spend": total_spend,
        "median_value": median_value,
        "earliest": earliest.isoformat() if earliest else None,
        "latest": latest.isoformat() if latest else None,
        "top_buyers": [{"name": r[0], "count": r[1], "total": r[2]} for r in buyer_rows],
        "top_cpvs": [{"code": r[0], "description": cpv_lookup_map.get(r[0], ""), "count": r[1]} for r in cpv_rows],
        "spend_chart": [
            {"year": r[0], "count": r[1], "total": float(r[2]) if r[2] else 0}
            for r in spend_rows if r[0] and r[0] >= 2022
        ],
        "frameworks": frameworks,
        "today": datetime.date.today().isoformat(),
    })


@app.get("/supplier/notices")
async def supplier_notices(request: Request, name: List[str] = Query(default=[])):
    redir = require_auth(request)
    if redir:
        return redir
    names = [n for n in name if n.strip()]
    if not names:
        return RedirectResponse(url="/supplier", status_code=302)

    name_where, name_params = supplier_name_filter(names)
    report_qs = urlencode([("name", n) for n in names])

    con = db()
    total = con.execute(
        f"SELECT COUNT(DISTINCT release_id) FROM notices WHERE {name_where}", name_params
    ).fetchone()[0]

    rows = con.execute(f"""
        SELECT release_id,
               MAX(title)                                        AS title,
               MAX(authority_name)                              AS authority_name,
               MIN(supplier_name)                               AS supplier_name,
               SUM(COALESCE(value_gross, value_awarded))        AS value,
               MAX(award_date)                                  AS award_date,
               MAX(award_count)                                 AS award_count,
               MAX(is_framework_agreement::INTEGER)::BOOLEAN    AS is_framework_agreement,
               MAX(regime)                                      AS regime,
               MAX(proc_route)                                  AS proc_route,
               MAX(proc_method)                                 AS proc_method,
               MAX(is_dps::INTEGER)::BOOLEAN                    AS is_dps,
               MAX(is_dynamic_market::INTEGER)::BOOLEAN         AS is_dynamic_market,
               MAX(contract_end)                                AS contract_end,
               MAX(contract_max_extent)                         AS contract_max_extent
        FROM notices
        WHERE {name_where}
        GROUP BY release_id
        ORDER BY MAX(award_date) DESC NULLS LAST
        LIMIT 50
    """, name_params).fetchall()
    con.close()

    cols = ["release_id", "title", "authority_name", "supplier_name",
            "value", "award_date", "award_count",
            "is_framework_agreement", "regime", "proc_route", "proc_method",
            "is_dps", "is_dynamic_market", "contract_end", "contract_max_extent"]
    notices = [_row_to_dict(cols, r) for r in rows]

    return templates.TemplateResponse(request, "supplier_notices.html", {
        "names": names,
        "report_url": f"/supplier/report?{report_qs}",
        "notices": notices,
        "total": total,
        "today": datetime.date.today().isoformat(),
    })


@app.get("/api/supplier/notices")
async def api_supplier_notices(name: List[str] = Query(default=[]),
                               offset: int = 0, limit: int = 50):
    limit = min(limit, 100)
    names = [n for n in name if n.strip()]
    if not names:
        return []
    name_where, name_params = supplier_name_filter(names)
    con = db()
    rows = con.execute(f"""
        SELECT release_id,
               MAX(title)                                        AS title,
               MAX(authority_name)                              AS authority_name,
               MIN(supplier_name)                               AS supplier_name,
               SUM(COALESCE(value_gross, value_awarded))        AS value,
               MAX(award_date)                                  AS award_date,
               MAX(award_count)                                 AS award_count,
               MAX(is_framework_agreement::INTEGER)::BOOLEAN    AS is_framework_agreement,
               MAX(regime)                                      AS regime,
               MAX(proc_route)                                  AS proc_route,
               MAX(proc_method)                                 AS proc_method,
               MAX(is_dps::INTEGER)::BOOLEAN                    AS is_dps,
               MAX(is_dynamic_market::INTEGER)::BOOLEAN         AS is_dynamic_market,
               MAX(contract_end)                                AS contract_end,
               MAX(contract_max_extent)                         AS contract_max_extent
        FROM notices
        WHERE {name_where}
        GROUP BY release_id
        ORDER BY MAX(award_date) DESC NULLS LAST
        LIMIT ? OFFSET ?
    """, name_params + [limit, offset]).fetchall()
    con.close()
    cols = ["release_id", "title", "authority_name", "supplier_name",
            "value", "award_date", "award_count",
            "is_framework_agreement", "regime", "proc_route", "proc_method",
            "is_dps", "is_dynamic_market", "contract_end", "contract_max_extent"]
    return [_row_to_dict(cols, r) for r in rows]


# ---------------------------------------------------------------------------
# Framework Report — search / list / detail
# ---------------------------------------------------------------------------

@app.get("/framework")
async def framework_search_page(request: Request, code: str = "", q: str = ""):
    redir = require_auth(request)
    if redir:
        return redir

    providers = load_fw_providers()
    con = db()
    for p in providers:
        placeholders = ",".join("?" for _ in p["authority_names"])
        row = con.execute(f"""
            SELECT COUNT(DISTINCT release_id)
            FROM notices
            WHERE is_framework_agreement = TRUE
              AND regime IN ('PA23_FTS', 'PCR2015_FTS')
              AND authority_name IN ({placeholders})
        """, p["authority_names"]).fetchone()
        p["framework_count"] = row[0] if row else 0
    con.close()

    return templates.TemplateResponse(request, "framework_search.html", {
        "providers": providers,
        "code": code,
        "q": q,
    })


@app.get("/framework/report")
async def framework_report_page(
    request: Request,
    code: str = "",
    q: str = "",
    name: List[str] = Query(default=[]),
    sub: bool = True,
):
    redir = require_auth(request)
    if redir:
        return redir

    if not code and not q and not name:
        return RedirectResponse(url="/framework", status_code=302)

    conditions = [
        "is_framework_agreement = TRUE",
        "regime IN ('PA23_FTS', 'PCR2015_FTS')",
    ]
    params: list = []

    if code:
        cpv_clause, cpv_params = cpv_filter(code, sub)
        conditions.append(f"({cpv_clause})")
        params.extend(cpv_params)

    if q:
        conditions.append("(title ILIKE ? OR description ILIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])

    if name:
        name_where, name_params = supplier_name_filter(name)
        conditions.append(f"({name_where})")
        params.extend(name_params)

    where = " AND ".join(conditions)
    con = db()
    rows = con.execute(f"""
        SELECT
            release_id,
            MAX(title)                               AS title,
            MAX(authority_name)                      AS authority_name,
            MAX(description)                         AS description,
            MAX(cpv_main)                            AS cpv_main,
            regime,
            MAX(is_dps::INTEGER)::BOOLEAN            AS is_dps,
            MAX(is_dynamic_market::INTEGER)::BOOLEAN AS is_dynamic_market,
            MIN(contract_start)                      AS contract_start,
            MAX(contract_end)                        AS contract_end,
            MAX(contract_max_extent)                 AS contract_max_extent,
            MAX(CAST(award_date AS DATE))            AS award_date,
            MAX(lot_count)                           AS lot_count,
            COUNT(DISTINCT supplier_name)            AS supplier_count,
            MAX(CAST(published_date AS DATE))        AS published_date
        FROM notices
        WHERE {where}
        GROUP BY release_id, regime
        ORDER BY MAX(award_date) DESC NULLS LAST
        LIMIT 500
    """, params).fetchall()
    con.close()

    cols = ["release_id", "title", "authority_name", "description", "cpv_main", "regime",
            "is_dps", "is_dynamic_market", "contract_start", "contract_end",
            "contract_max_extent", "award_date", "lot_count", "supplier_count", "published_date"]
    frameworks = [_row_to_dict(cols, r) for r in rows]

    today_str = datetime.date.today().isoformat()
    fya_str   = datetime.date.today().replace(year=datetime.date.today().year - 4).isoformat()

    known_providers, other_authorities = [], []
    total_active = total_active_or_likely = total_known_active_or_likely = 0

    for fw in frameworks:
        best_end = fw["contract_max_extent"] or fw["contract_end"]
        be = best_end[:10] if best_end else None
        if be and be > today_str:
            fw["status"] = "active"
            total_active += 1
            total_active_or_likely += 1
        elif be:
            fw["status"] = "likely_expired"
        else:
            ad = (fw["award_date"] or "")[:10]
            if ad >= fya_str:
                fw["status"] = "likely_active"
                total_active_or_likely += 1
            else:
                fw["status"] = "likely_expired"

        fw["provider"] = fw_provider_for_authority(fw["authority_name"])
        if fw["provider"]:
            if fw["status"] in ("active", "likely_active"):
                total_known_active_or_likely += 1
            known_providers.append(fw)
        else:
            other_authorities.append(fw)

    search_qs       = urlencode([("code", code)] + [("q", q)] + [("name", n) for n in name] + [("sub", str(sub).lower())])
    search_qs_no_q  = urlencode([("code", code)]             + [("name", n) for n in name] + [("sub", str(sub).lower())])

    return templates.TemplateResponse(request, "framework_report.html", {
        "known_providers":            known_providers,
        "other_authorities":          other_authorities,
        "code":                       code,
        "q":                          q,
        "names":                      name,
        "sub":                        sub,
        "today":                      today_str,
        "total":                      len(frameworks),
        "total_active":               total_active,
        "total_active_or_likely":     total_active_or_likely,
        "total_known_active_or_likely": total_known_active_or_likely,
        "total_lots":                 sum(fw["lot_count"] or 0 for fw in frameworks),
        "search_qs":                  search_qs,
        "search_qs_no_q":             search_qs_no_q,
    })


@app.get("/framework/list")
async def framework_list_page(
    request: Request,
    code: str = "",
    q: str = "",
    name: List[str] = Query(default=[]),
    provider: str = "",
    sub: bool = True,
):
    redir = require_auth(request)
    if redir:
        return redir

    conditions = [
        "is_framework_agreement = TRUE",
        "regime IN ('PA23_FTS', 'PCR2015_FTS')",
    ]
    params: list = []

    # Provider filter: look up authority names for this provider id
    provider_obj = None
    if provider:
        providers = load_fw_providers()
        provider_obj = next((p for p in providers if p["id"] == provider), None)
        if provider_obj:
            placeholders = ",".join("?" for _ in provider_obj["authority_names"])
            conditions.append(f"authority_name IN ({placeholders})")
            params.extend(provider_obj["authority_names"])

    if code:
        cpv_clause, cpv_params = cpv_filter(code, sub)
        conditions.append(f"({cpv_clause})")
        params.extend(cpv_params)

    if q:
        conditions.append("(title ILIKE ? OR description ILIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])

    if name:
        name_where, name_params = supplier_name_filter(name)
        conditions.append(f"({name_where})")
        params.extend(name_params)

    where = " AND ".join(conditions)
    con = db()
    rows = con.execute(f"""
        SELECT
            release_id,
            MAX(title)                               AS title,
            MAX(authority_name)                      AS authority_name,
            MAX(description)                         AS description,
            MAX(cpv_main)                            AS cpv_main,
            regime,
            MAX(is_dps::INTEGER)::BOOLEAN            AS is_dps,
            MAX(is_dynamic_market::INTEGER)::BOOLEAN AS is_dynamic_market,
            MIN(contract_start)                      AS contract_start,
            MAX(contract_end)                        AS contract_end,
            MAX(contract_max_extent)                 AS contract_max_extent,
            MAX(CAST(award_date AS DATE))            AS award_date,
            MAX(lot_count)                           AS lot_count,
            MAX(CAST(published_date AS DATE))        AS published_date
        FROM notices
        WHERE {where}
        GROUP BY release_id, regime
        ORDER BY MAX(award_date) DESC NULLS LAST
        LIMIT 500
    """, params).fetchall()
    con.close()

    cols = ["release_id", "title", "authority_name", "description", "cpv_main", "regime",
            "is_dps", "is_dynamic_market", "contract_start", "contract_end",
            "contract_max_extent", "award_date", "lot_count", "published_date"]
    frameworks = [_row_to_dict(cols, r) for r in rows]

    today_str = datetime.date.today().isoformat()
    fya_str   = datetime.date.today().replace(year=datetime.date.today().year - 4).isoformat()

    for fw in frameworks:
        best_end = fw["contract_max_extent"] or fw["contract_end"]
        be = best_end[:10] if best_end else None
        if be and be > today_str:
            fw["status"] = "active"
        elif be:
            fw["status"] = "likely_expired"
        else:
            ad = (fw["award_date"] or "")[:10]
            fw["status"] = "likely_active" if ad >= fya_str else "likely_expired"
        fw["provider"] = fw_provider_for_authority(fw["authority_name"])

    search_qs = urlencode(
        [("code", code)] + [("q", q)] + [("name", n) for n in name]
        + ([("provider", provider)] if provider else [])
    )

    return templates.TemplateResponse(request, "framework_list.html", {
        "frameworks": frameworks,
        "code": code,
        "q": q,
        "names": name,
        "provider_obj": provider_obj,
        "today": today_str,
        "total": len(frameworks),
        "search_qs": search_qs,
    })


@app.get("/framework/{release_id}")
async def framework_detail_page(request: Request, release_id: str):
    redir = require_auth(request)
    if redir:
        return redir

    con = db()
    row = con.execute("""
        SELECT
            regime, title, description, authority_name, cpv_main,
            MAX(is_dps::INTEGER)::BOOLEAN            AS is_dps,
            MAX(is_dynamic_market::INTEGER)::BOOLEAN AS is_dynamic_market,
            MAX(has_lots::INTEGER)::BOOLEAN          AS has_lots,
            MAX(lot_count)                           AS lot_count,
            MAX(CAST(published_date AS DATE))        AS published_date,
            MIN(contract_start)                      AS contract_start,
            MAX(contract_end)                        AS contract_end,
            MAX(contract_max_extent)                 AS contract_max_extent,
            MAX(CAST(award_date AS DATE))            AS award_date
        FROM notices
        WHERE release_id = ?
        GROUP BY regime, title, description, authority_name, cpv_main
        LIMIT 1
    """, [release_id]).fetchone()

    if not row:
        con.close()
        from fastapi.responses import Response as _R
        return _R("Framework not found", status_code=404)

    cols = ["regime", "title", "description", "authority_name", "cpv_main",
            "is_dps", "is_dynamic_market", "has_lots", "lot_count", "published_date",
            "contract_start", "contract_end", "contract_max_extent", "award_date"]
    meta = _row_to_dict(cols, row)

    # Get lot/supplier structure from notices table (award_title populated by extractors)
    notice_rows = con.execute("""
        SELECT award_id, award_title, supplier_name
        FROM notices
        WHERE release_id = ?
        ORDER BY award_id
    """, [release_id]).fetchall()
    con.close()

    _suffix_re = re.compile(r"_\d+$")

    lots_dict: dict = {}
    for (award_id, award_title, supplier_name) in notice_rows:
        # Group by base award id (strip trailing _{j} supplier-index suffix)
        base = _suffix_re.sub("", award_id)
        if base not in lots_dict:
            lots_dict[base] = {
                "id":       base,
                "title":    award_title or f"Lot {base}",
                "suppliers": [],
            }
        elif award_title and not lots_dict[base].get("award_title_set"):
            # Take first non-null title we see
            lots_dict[base]["title"] = award_title
            lots_dict[base]["award_title_set"] = True
        if supplier_name:
            lots_dict[base]["suppliers"].append(supplier_name)

    def _natural_key(s: str) -> list:
        return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", s)]

    lots = []
    for lot in sorted(lots_dict.values(), key=lambda l: _natural_key(l["id"])):
        lot["supplier_count"] = len(lot["suppliers"])
        lots.append(lot)

    today_str = datetime.date.today().isoformat()
    fya_str   = datetime.date.today().replace(year=datetime.date.today().year - 4).isoformat()

    best_end = meta["contract_max_extent"] or meta["contract_end"]
    be = best_end[:10] if best_end else None
    if be and be > today_str:
        status = "active"
    elif be:
        status = "likely_expired"
    elif (meta["award_date"] or "")[:10] >= fya_str:
        status = "likely_active"
    else:
        status = "likely_expired"

    if meta["regime"] in ("PA23_FTS", "PCR2015_FTS"):
        display_ref = _format_fts_ref(release_id)
    else:
        display_ref = release_id

    provider = fw_provider_for_authority(meta["authority_name"])

    return templates.TemplateResponse(request, "framework_detail.html", {
        "n":           meta,
        "release_id":  release_id,
        "lots":        lots,
        "status":      status,
        "today":       today_str,
        "display_ref": display_ref,
        "source_url":  f"https://www.find-tender.service.gov.uk/Notice/{release_id}",
        "back_url":    request.headers.get("referer", ""),
        "provider":    provider,
    })
