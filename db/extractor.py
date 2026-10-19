"""
db/extractor.py — Classifies raw OCDS releases and populates the notices table.

Each classifier returns a list of dicts — one per award entry — so multi-award
notices (lotted tenders) produce multiple rows sharing the same release_id but
with distinct award_id values.  Single-award notices produce exactly one row
with award_id='0'.

Regimes handled:
  PCR2015_CF   — Contracts Finder below-threshold awards
  PA23_FTS     — Procurement Act 2023 UK7 Contract Details Notices
  PCR2015_FTS  — Pre-PA23 Find a Tender F03 Contract Award Notices
"""

import json
import logging
from typing import Optional

import duckdb

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Field extraction helpers
# ---------------------------------------------------------------------------

def _str(val) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


# Supplier name values that are placeholder text, not real names.
# Publishers sometimes use these when the actual supplier list is elsewhere.
_PLACEHOLDER_SUPPLIER_NAMES: frozenset[str] = frozenset({
    "See Contracts Finder Notice for full supplier list",
    "See Contracts Finder Notice for the full supplier list",
    "Multiple Suppliers - See attached OJEU Contract Award Notices",
    "Please see the link in Section VI Complementary Information to the Contract Award Notice",
})


def _supplier_name(val) -> Optional[str]:
    """Like _str() but returns None for known placeholder text."""
    s = _str(val)
    if s in _PLACEHOLDER_SUPPLIER_NAMES:
        return None
    return s


def _decimal(val) -> Optional[float]:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _extract_cpv(tender: dict, awards: list = None) -> tuple[Optional[str], Optional[str]]:
    """Return (main_cpv, additional_cpvs_csv).

    Checks locations in priority order:
    1. tender.classification        (CF / PCR2015_FTS — single object)
    2. tender.additionalClassifications (CF supplementary)
    3. tender.items[*].additionalClassifications (PA23 FTS)
    4. awards[*].items[*].additionalClassifications (fallback)
    """
    cpvs = []

    cls = tender.get("classification") or {}
    if cls.get("scheme") == "CPV":
        cpv_id = _str(cls.get("id"))
        if cpv_id:
            cpvs.append(cpv_id)

    for cls in tender.get("additionalClassifications") or []:
        if cls.get("scheme") == "CPV":
            cpv_id = _str(cls.get("id"))
            if cpv_id and cpv_id not in cpvs:
                cpvs.append(cpv_id)

    if not cpvs:
        for item in tender.get("items") or []:
            for cls in item.get("additionalClassifications") or []:
                if cls.get("scheme") == "CPV":
                    cpv_id = _str(cls.get("id"))
                    if cpv_id and cpv_id not in cpvs:
                        cpvs.append(cpv_id)

    if not cpvs and awards:
        for award in awards:
            for item in award.get("items") or []:
                for cls in item.get("additionalClassifications") or []:
                    if cls.get("scheme") == "CPV":
                        cpv_id = _str(cls.get("id"))
                        if cpv_id and cpv_id not in cpvs:
                            cpvs.append(cpv_id)
            if cpvs:
                break

    if not cpvs:
        return None, None
    return cpvs[0], ",".join(cpvs[1:]) if len(cpvs) > 1 else None


def _extract_buyer(parties: list) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Return (name, id, authority_type, contact_name, contact_email) from buyer party."""
    for p in parties:
        if "buyer" in p.get("roles", []):
            cp = p.get("contactPoint", {})
            details = p.get("details", {})
            auth_type = None
            for cls in details.get("classifications", []):
                if cls.get("scheme") == "UK_CA_TYPE":
                    auth_type = _str(cls.get("description"))
                    break
            return (
                _str(p.get("name")),
                _str(p.get("id")),
                auth_type,
                _str(cp.get("name")),
                _str(cp.get("email")),
            )
    return None, None, None, None, None


def _extract_cf_buyer(data: dict) -> tuple[Optional[str], Optional[str]]:
    """Return (name, id) preferring the parties[] buyer role entry."""
    for p in data.get("parties", []):
        if "buyer" in p.get("roles", []):
            return _str(p.get("name")), _str(p.get("id"))
    b = data.get("buyer", {})
    return _str(b.get("name")), _str(b.get("id"))


def _tenders_received(bids: dict) -> Optional[int]:
    if not bids:
        return None
    for stat in bids.get("statistics", []):
        if stat.get("measure") in ("bids", "requests"):
            try:
                return int(stat["value"])
            except (KeyError, TypeError, ValueError):
                pass
    return None


def _is_framework(tender: dict) -> bool:
    """True if this notice IS establishing a framework agreement."""
    techniques = tender.get("techniques") or {}
    return bool(techniques.get("hasFrameworkAgreement", False))


def _is_dps_establishment(tender: dict, awards: list) -> bool:
    """True if this notice is establishing (or admitting a batch to) a DPS.

    Signal: hasDynamicPurchasingSystem=true AND the first award lists more than
    one supplier, indicating a batch-admission F03 rather than a mini-comp award.
    """
    techniques = tender.get("techniques") or {}
    if not techniques.get("hasDynamicPurchasingSystem", False):
        return False
    if awards and len(awards[0].get("suppliers", [])) > 1:
        return True
    return False


def _contract_for_award(award_id: str, award_index: int,
                        contracts: list, contract_by_award: dict) -> dict:
    """Return the best matching contract dict for a given award."""
    if award_id and award_id in contract_by_award:
        return contract_by_award[award_id]
    if award_index < len(contracts):
        return contracts[award_index]
    return contracts[0] if contracts else {}


# ---------------------------------------------------------------------------
# PA23 UK7 classifier
# ---------------------------------------------------------------------------

def _is_pa23(data: dict) -> bool:
    lb = data.get("tender", {}).get("legalBasis", {})
    return lb.get("id") == "2023/54"


def _has_uk7(data: dict) -> bool:
    for c in data.get("contracts", []):
        for d in c.get("documents", []):
            if d.get("noticeType") == "UK7":
                return True
    return False


def _classify_pa23_uk7(release_id: str, regime: str, data: dict) -> list[dict]:
    tender = data.get("tender", {})
    contracts = data.get("contracts", [])
    awards = data.get("awards", [])
    parties = data.get("parties", [])
    bids = data.get("bids", {})

    buyer_name, buyer_id, authority_type, contact_name, contact_email = _extract_buyer(parties)
    cpv_main, cpv_additional = _extract_cpv(tender, awards)

    contract_by_award = {c["awardID"]: c for c in contracts if c.get("awardID")}

    published_date = _str(data.get("date"))
    proc_method = _str(tender.get("procurementMethodDetails"))
    proc_route = _str(tender.get("procurementMethod"))
    is_fw = _is_framework(tender)
    tenders_received = _tenders_received(bids)
    above_threshold_tender = tender.get("aboveThreshold")

    # PA23 Dynamic Market: check for dynamicPurchasingSystem object with status field
    techniques = tender.get("techniques") or {}
    is_dm = bool(techniques.get("dynamicPurchasingSystem")) and not is_fw

    award_count = len(awards) if awards else 1

    base = {
        "regime": regime,
        "release_id": release_id,
        "award_count": award_count,
        "ocid": _str(data.get("ocid")),
        "release_tags": ",".join(data.get("tag", [])),
        "notice_type": "UK7",
        "title": _str(tender.get("title")),
        "description": _str(tender.get("description")),
        "cpv_main": cpv_main,
        "cpv_additional": cpv_additional,
        "authority_name": buyer_name,
        "authority_id": buyer_id,
        "authority_type": authority_type,
        "contact_name": contact_name,
        "contact_email": contact_email,
        "contact_phone": None,
        "proc_route": proc_route,
        "proc_method": proc_method,
        "is_framework_agreement": is_fw,
        "framework_ref": None,
        "has_lots": award_count > 1,
        "lot_count": award_count,
        "value_estimated": _decimal(tender.get("value", {}).get("amount")),
        "currency": "GBP",
        "published_date": published_date,
        "tender_deadline": _str(tender.get("tenderPeriod", {}).get("endDate")),
        "tenders_received": tenders_received,
        "is_termination": False,
        "is_performance": False,
        "is_psr": False,
        "status": _str(tender.get("status")),
        "is_dps": False,
        "is_dynamic_market": is_dm,
    }

    if not awards:
        contract = contracts[0] if contracts else {}
        cv = contract.get("value", {})
        cp = contract.get("period", {})
        above_threshold = contract.get("aboveThreshold", above_threshold_tender)
        award_date = _str(contract.get("dateSigned"))
        return [{
            **base,
            "award_id": "0",
            "supplier_name": None,
            "supplier_id": None,
            "value_awarded": _decimal(cv.get("amount")),
            "value_gross": _decimal(cv.get("amountGross")),
            "above_threshold": above_threshold,
            "award_date": award_date,
            "contract_start": (_str(cp.get("startDate")) or "")[:10] or None,
            "contract_end": (_str(cp.get("endDate")) or "")[:10] or None,
            "contract_max_extent": (_str(cp.get("maxExtentDate")) or "")[:10] or None,
        }]

    records = []
    for i, award in enumerate(awards):
        award_id = _str(award.get("id")) or str(i)
        contract = _contract_for_award(award_id, i, contracts, contract_by_award)
        cv = contract.get("value", {})
        cp = contract.get("period", {})
        above_threshold = contract.get("aboveThreshold", above_threshold_tender)
        award_date = _str(contract.get("dateSigned")) or _str(award.get("date"))

        suppliers = award.get("suppliers", [])
        supplier = suppliers[0] if suppliers else {}

        records.append({
            **base,
            "award_id": award_id,
            "supplier_name": _supplier_name(supplier.get("name")),
            "supplier_id": _str(supplier.get("id")),
            "value_awarded": _decimal(cv.get("amount")),
            "value_gross": _decimal(cv.get("amountGross")),
            "above_threshold": above_threshold,
            "award_date": award_date,
            "contract_start": (_str(cp.get("startDate")) or "")[:10] or None,
            "contract_end": (_str(cp.get("endDate")) or "")[:10] or None,
            "contract_max_extent": (_str(cp.get("maxExtentDate")) or "")[:10] or None,
        })
    return records


# ---------------------------------------------------------------------------
# CF award classifier
# ---------------------------------------------------------------------------

_CF_PROC_METHOD = {
    "framework":    "Below threshold - award under framework",
    "open":         "Below threshold - open competition",
    "restricted":   "Below threshold - limited competition",
    "direct_award": "Below threshold - without competition",
    "other":        "Below threshold - unknown",
}

_CF_FRAMEWORK_CALLOFF_EXACT = frozenset({
    "Call-off from a framework agreement",
    "Call-off from a dynamic purchasing system",
})
_CF_OPEN_EXACT = frozenset({
    "Open procedure",
    "Open procedure (below threshold)",
    "Open procedure (above threshold)",
    "Accelerated Open procedure (above threshold)",
    "Competitive quotation (below threshold)",
})
_CF_RESTRICTED_EXACT = frozenset({
    "Restricted procedure",
    "Restricted procedure (above threshold)",
    "Accelerated Restricted procedure (above threshold)",
    "Competitive procedure with negotiation (above threshold)",
    "Accelerated Competitive procedure with negotiation (above threshold)",
    "Competitive dialogue (above threshold)",
    "Competitive dialog",
    "Innovation partnership (above threshold)",
    "Negotiated procedure",
    "Accelerated negotiated procedure",
    "Negotiated without a call for competition",
})
_CF_DIRECT_EXACT = frozenset({
    "Single tender action (below threshold)",
    "Direct award",
    "Negotiated procedure without prior publication (above threshold)",
    "Award of contract without prior publication of a contract notice",
    "Catalogue purchase",
})

_CF_CALLOFF_KW   = ("call-off", "call off", "g-cloud", "g cloud")
_CF_FRAMEWORK_KW = ("framework",)
_CF_DIRECT_KW    = ("direct award", "single tender", "single source", "sole source",
                    "sole supplier", "waiver", "exemption", "without prior publication",
                    "without competition")


def _cf_proc_route(pmd: Optional[str]) -> tuple[str, bool]:
    """Return (proc_route, is_framework_establishment).

    is_framework_establishment=True only for notices that ESTABLISH a framework
    agreement. Call-off awards from an existing framework get False here
    (they are real spend, not max-value estimates).
    """
    if not pmd:
        return "other", False
    # Explicit call-off PMD strings → call-off, not an establishment
    if pmd in _CF_FRAMEWORK_CALLOFF_EXACT:
        return "framework", False
    if pmd in _CF_OPEN_EXACT:
        return "open", False
    if pmd in _CF_RESTRICTED_EXACT:
        return "restricted", False
    if pmd in _CF_DIRECT_EXACT:
        return "direct_award", False
    lower = pmd.lower()
    # Call-off keywords (call-off, g-cloud, etc.) → call-off, not an establishment
    if any(kw in lower for kw in _CF_CALLOFF_KW):
        return "framework", False
    # "framework" without call-off keywords → this notice establishes a framework
    if any(kw in lower for kw in _CF_FRAMEWORK_KW):
        return "framework", True
    if any(kw in lower for kw in _CF_DIRECT_KW):
        return "direct_award", False
    return "other", False


def _cf_above_threshold(pmd: Optional[str]) -> Optional[bool]:
    if not pmd:
        return None
    lower = pmd.lower()
    if "above threshold" in lower:
        return True
    if "below threshold" in lower or "single tender action" in lower:
        return False
    return None


def _classify_cf_award(release_id: str, regime: str, data: dict) -> list[dict]:
    tender = data.get("tender", {})
    awards = data.get("awards", [])

    if not awards:
        return []

    buyer_name, buyer_id = _extract_cf_buyer(data)
    cpv_main, cpv_additional = _extract_cpv(tender, awards)

    pmd = _str(tender.get("procurementMethodDetails"))
    proc_route, is_framework = _cf_proc_route(pmd)
    above_threshold = _cf_above_threshold(pmd)
    proc_method = pmd if above_threshold is True else _CF_PROC_METHOD.get(proc_route, "Below threshold - unknown")

    contract_period = tender.get("contractPeriod") or {}
    award_count = len(awards)

    base = {
        "regime": regime,
        "release_id": release_id,
        "award_count": award_count,
        "ocid": _str(data.get("ocid")),
        "release_tags": ",".join(data.get("tag", [])),
        "notice_type": "CF_AWARD",
        "title": _str(tender.get("title")),
        "description": _str(tender.get("description")),
        "cpv_main": cpv_main,
        "cpv_additional": cpv_additional,
        "authority_name": buyer_name,
        "authority_id": buyer_id,
        "authority_type": None,
        "contact_name": None,
        "contact_email": None,
        "contact_phone": None,
        "proc_route": proc_route,
        "proc_method": proc_method,
        "is_framework_agreement": is_framework,
        "framework_ref": None,
        "has_lots": award_count > 1,
        "lot_count": award_count,
        "value_estimated": None,
        "value_gross": None,
        "currency": "GBP",
        "above_threshold": above_threshold,
        "published_date": _str(data.get("date")),
        "tender_deadline": None,
        "contract_start": (_str(contract_period.get("startDate")) or "")[:10] or None,
        "contract_end": (_str(contract_period.get("endDate")) or "")[:10] or None,
        "contract_max_extent": None,
        "tenders_received": None,
        "is_termination": False,
        "is_performance": False,
        "is_psr": False,
        "status": _str(tender.get("status")),
        "is_dps": False,
        "is_dynamic_market": False,
    }

    records = []
    for i, award in enumerate(awards):
        award_id = _str(award.get("id")) or str(i)
        av = award.get("value") or {}
        suppliers = award.get("suppliers", [])
        supplier = suppliers[0] if suppliers else {}
        records.append({
            **base,
            "award_id": award_id,
            "supplier_name": _supplier_name(supplier.get("name")),
            "supplier_id": _str(supplier.get("id")),
            "value_awarded": _decimal(av.get("amount")),
            "award_date": _str(award.get("date")),
        })
    return records


# ---------------------------------------------------------------------------
# PCR2015 FTS F03 classifier
# ---------------------------------------------------------------------------

_PCR2015_FTS_PROC_METHOD = {
    "Open procedure": "Open procedure",
    "Award procedure without prior publication of a call for competition": "Direct award",
}


def _classify_pcr2015_fts(release_id: str, regime: str, data: dict) -> list[dict]:
    tender = data.get("tender", {})
    awards = data.get("awards", [])
    contracts = data.get("contracts", [])
    parties = data.get("parties", [])
    bids = data.get("bids", {})

    if not awards:
        return []

    buyer_name, buyer_id, authority_type, contact_name, contact_email = _extract_buyer(parties)
    if not buyer_name:
        b = data.get("buyer", {})
        buyer_name = _str(b.get("name"))
        buyer_id = _str(b.get("id"))

    cpv_main, cpv_additional = _extract_cpv(tender, awards)

    pmd_raw = _str(tender.get("procurementMethodDetails"))
    proc_method = _PCR2015_FTS_PROC_METHOD.get(pmd_raw, "Other PCR2015 Procedure")
    proc_route = _str(tender.get("procurementMethod"))
    is_fw = _is_framework(tender)
    is_dps = _is_dps_establishment(tender, awards)
    tenders_received = _tenders_received(bids)

    contract_by_award = {c["awardID"]: c for c in contracts if c.get("awardID")}
    lot_count = len(awards)
    # Each award may list multiple suppliers (common in framework establishments).
    # award_count = total (award × supplier) rows we will emit.
    award_count = sum(max(len(a.get("suppliers", [])), 1) for a in awards)

    base = {
        "regime": "PCR2015_FTS",
        "release_id": release_id,
        "award_count": award_count,
        "ocid": _str(data.get("ocid")),
        "release_tags": ",".join(data.get("tag", [])),
        "notice_type": "F03",
        "title": _str(tender.get("title")),
        "description": _str(tender.get("description")),
        "cpv_main": cpv_main,
        "cpv_additional": cpv_additional,
        "authority_name": buyer_name,
        "authority_id": buyer_id,
        "authority_type": authority_type,
        "contact_name": contact_name,
        "contact_email": contact_email,
        "contact_phone": None,
        "proc_route": proc_route,
        "proc_method": proc_method,
        "is_framework_agreement": is_fw,
        "framework_ref": None,
        "has_lots": lot_count > 1,
        "lot_count": lot_count,
        "value_estimated": _decimal((tender.get("value") or {}).get("amount")),
        "value_gross": None,
        "currency": "GBP",
        "above_threshold": True,
        "published_date": _str(data.get("date")),
        "tender_deadline": None,
        "contract_start": None,
        "contract_end": None,
        "contract_max_extent": None,
        "tenders_received": tenders_received,
        "is_termination": False,
        "is_performance": False,
        "is_psr": False,
        "status": _str(tender.get("status")),
        "is_dps": is_dps,
        "is_dynamic_market": False,
    }

    records = []
    for i, award in enumerate(awards):
        award_id_base = _str(award.get("id")) or str(i)
        contract = _contract_for_award(award_id_base, i, contracts, contract_by_award)
        cv = contract.get("value", {}) if contract else {}
        suppliers = award.get("suppliers", [])
        award_date = (_str(contract.get("dateSigned")) if contract else None) or _str(award.get("date"))

        if not suppliers:
            records.append({
                **base,
                "award_id": award_id_base,
                "supplier_name": None,
                "supplier_id": None,
                "value_awarded": _decimal(cv.get("amount")),
                "award_date": award_date,
            })
        elif len(suppliers) == 1:
            records.append({
                **base,
                "award_id": award_id_base,
                "supplier_name": _supplier_name(suppliers[0].get("name")),
                "supplier_id": _str(suppliers[0].get("id")),
                "value_awarded": _decimal(cv.get("amount")),
                "award_date": award_date,
            })
        else:
            # Framework/DPS awards listing multiple suppliers per lot — create one row each.
            for j, sup in enumerate(suppliers):
                records.append({
                    **base,
                    "award_id": f"{award_id_base}_{j}",
                    "supplier_name": _supplier_name(sup.get("name")),
                    "supplier_id": _str(sup.get("id")),
                    "value_awarded": _decimal(cv.get("amount")),
                    "award_date": award_date,
                })
    return records


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def classify_release(release_id: str, regime: str, data: dict) -> list[dict]:
    """Return a list of notice dicts for this release (empty = skip)."""
    if regime == "PCR2015_CF":
        return _classify_cf_award(release_id, regime, data)

    if _is_pa23(data):
        if _has_uk7(data):
            return _classify_pa23_uk7(release_id, regime, data)
        return []

    # PCR2015 FTS F03: legalBasis 32014L0024, tag contains 'award'
    lb = (data.get("tender") or {}).get("legalBasis") or {}
    if lb.get("id") == "32014L0024" and "award" in data.get("tag", []):
        return _classify_pcr2015_fts(release_id, regime, data)

    return []


# ---------------------------------------------------------------------------
# Bulk insert
# ---------------------------------------------------------------------------

_INSERT_SQL = """
INSERT OR REPLACE INTO notices (
    regime, release_id, award_id, award_count,
    ocid, release_tags, notice_type,
    title, description, cpv_main, cpv_additional,
    authority_name, authority_id, authority_type,
    contact_name, contact_email, contact_phone,
    proc_route, proc_method, is_framework_agreement, framework_ref,
    has_lots, lot_count,
    value_estimated, value_awarded, value_gross, currency, above_threshold,
    published_date, award_date, tender_deadline,
    contract_start, contract_end, contract_max_extent,
    supplier_name, supplier_id, tenders_received,
    is_termination, is_performance, is_psr, status,
    is_dps, is_dynamic_market
) VALUES (
    $regime, $release_id, $award_id, $award_count,
    $ocid, $release_tags, $notice_type,
    $title, $description, $cpv_main, $cpv_additional,
    $authority_name, $authority_id, $authority_type,
    $contact_name, $contact_email, $contact_phone,
    $proc_route, $proc_method, $is_framework_agreement, $framework_ref,
    $has_lots, $lot_count,
    $value_estimated, $value_awarded, $value_gross, $currency, $above_threshold,
    $published_date, $award_date, $tender_deadline,
    $contract_start, $contract_end, $contract_max_extent,
    $supplier_name, $supplier_id, $tenders_received,
    $is_termination, $is_performance, $is_psr, $status,
    $is_dps, $is_dynamic_market
)
"""


def run_extraction(db_path: str = "data.duckdb", batch_size: int = 500,
                   only_missing: bool = False, id_chunk: int = 100) -> dict:
    # Two-phase approach to avoid OOM on large raw_json payloads:
    # Phase 1 — fetch ALL missing (release_id, regime) pairs with NO raw_json.
    #            IDs are tiny; 400k rows ≈ 20 MB, safe for fetchall().
    # Phase 2 — for each ID, fetch raw_json individually (one row at a time),
    #            classify it, and accumulate an insert batch.
    # This keeps raw_json footprint at O(id_chunk) rows at any moment.
    conn = duckdb.connect(db_path)

    if only_missing:
        id_sql = """
            SELECT r.release_id, r.regime
            FROM raw_releases r
            WHERE NOT EXISTS (
                SELECT 1 FROM notices n
                WHERE n.regime = r.regime AND n.release_id = r.release_id
            )
        """
    else:
        id_sql = "SELECT release_id, regime FROM raw_releases"

    id_rows = conn.execute(id_sql).fetchall()
    total = len(id_rows)

    extracted = 0
    skipped = 0
    errors = 0
    batch = []

    for i in range(0, total, id_chunk):
        chunk_ids = id_rows[i:i + id_chunk]
        for release_id, regime in chunk_ids:
            row = conn.execute(
                "SELECT raw_json FROM raw_releases WHERE release_id = ? AND regime = ?",
                [release_id, regime],
            ).fetchone()
            if not row:
                skipped += 1
                continue
            raw_json = row[0]
            try:
                data = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
                records = classify_release(release_id, regime, data)
                if not records:
                    skipped += 1
                    continue
                batch.extend(records)
                extracted += len(records)
                if len(batch) >= batch_size:
                    conn.executemany(_INSERT_SQL, batch)
                    conn.commit()
                    batch = []
            except Exception as e:
                log.warning("Error processing %s: %s", release_id, e)
                errors += 1

        processed = i + len(chunk_ids)
        if processed % 10000 < id_chunk:
            print(f"  [extractor] {processed}/{total} processed...", flush=True)

    if batch:
        conn.executemany(_INSERT_SQL, batch)
        conn.commit()

    conn.close()
    return {"total": total, "extracted": extracted, "skipped": skipped, "errors": errors}


if __name__ == "__main__":
    import argparse as _ap
    _parser = _ap.ArgumentParser()
    _parser.add_argument("--only-missing", action="store_true")
    _parser.add_argument("--db", default="data.duckdb")
    _args = _parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = run_extraction(db_path=_args.db, only_missing=_args.only_missing)
    print(f"Extraction complete: {result['extracted']} rows extracted from "
          f"{result['total'] - result['skipped']} releases, "
          f"{result['skipped']} skipped, {result['errors']} errors")
