"""Parsing of CMS Hospital Price Transparency machine-readable files.

Handles the three shapes hospitals actually publish under the CY2024 schema:
CSV "tall", CSV "wide", and JSON. Everything streams -- MRFs are routinely
multi-gigabyte and must never be materialised in memory.
"""

from __future__ import annotations

import csv
import gzip
import io
import logging
import re
import sys
import zipfile
from typing import Any, Iterator

import requests

import config
from etl import db

log = logging.getLogger(__name__)

# Some MRFs carry enormous free-text notes fields.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

DB_COLUMNS = [
    "facility_name",
    "cms_certification_number",
    "billing_code",
    "billing_code_type",
    "description",
    "setting",
    "payer_name",
    "plan_name",
    "gross_charge",
    "discounted_cash_price",
    "payer_specific_negotiated_charge",
    "min_negotiated_charge",
    "max_negotiated_charge",
    "source_url",
]

_APR_PATTERN = re.compile(r"APR[ _-]?(?:DRG)?[ _-]?0*([0-9]{1,3})\b", re.I)
_MS_PATTERN = re.compile(r"MS[ _-]?DRG[^0-9]{0,4}0*([0-9]{1,3})", re.I)
_BARE_DRG_PATTERN = re.compile(r"(?:^|[^A-Z])DRG[^0-9]{0,4}0*([0-9]{1,3})", re.I)
_PLAIN_CODE = re.compile(r"^0*([0-9]{1,3})$")
_CURRENCY_STRIP = re.compile(r"[^0-9.\-]")

MS_DRG = "MS-DRG"
APR_DRG = "APR-DRG"


def normalize_drg(code: str | None) -> str | None:
    """Extract a zero-padded 3-digit DRG number from a billing code.

    `'470' in code` -- the original test -- also matches 4700, 1470 and the CPT
    code 47010, dragging unrelated line items into the price median.

    This returns only the NUMBER. It says nothing about which DRG system the
    number belongs to; use classify_code for that.
    """
    if not code:
        return None
    text = str(code).strip()
    if not text:
        return None

    for pattern in (_PLAIN_CODE, _APR_PATTERN, _MS_PATTERN, _BARE_DRG_PATTERN):
        match = pattern.search(text.upper())
        if match:
            digits = match.group(1).lstrip("0")
            return digits.zfill(3) if digits else None
    return None


def classify_code(raw: str | None, declared_type: str | None) -> tuple[str, str] | None:
    """Resolve a billing code to (drg_system, 3-digit number), or None.

    Distinguishing the two systems is not pedantry. APR-DRG 470 and MS-DRG 470
    are different procedures, and a hospital that publishes 'APR470-1' under a
    declared type of merely 'DRG' will silently contribute the wrong price to
    the MS-DRG 470 median unless the APR prefix is honoured.
    """
    if not raw:
        return None
    text = str(raw).strip()
    declared = (declared_type or "").strip().upper()
    if not text:
        return None

    number = normalize_drg(text)
    if not number:
        return None

    # The code string itself is the strongest signal.
    if _APR_PATTERN.search(text.upper()) and not _PLAIN_CODE.match(text):
        return APR_DRG, number
    if _MS_PATTERN.search(text.upper()):
        return MS_DRG, number

    # Otherwise fall back to the declared type column.
    if "APR" in declared:
        return APR_DRG, number
    if "MS" in declared and "DRG" in declared:
        return MS_DRG, number
    if declared == "DRG" or "DRG" in declared:
        # A bare 'DRG' with a bare number is conventionally MS-DRG.
        return MS_DRG, number

    return None


def is_target_code(raw: str | None, declared_type: str | None) -> tuple[str, str] | None:
    """True when this code is one the project actually prices."""
    result = classify_code(raw, declared_type)
    if result is None:
        return None
    system, number = result
    if system == MS_DRG and number == config.TARGET_MS_DRG.zfill(3):
        return system, number
    if system == APR_DRG and number in {c.zfill(3) for c in config.TARGET_APR_DRGS}:
        return system, number
    return None


def clean_currency(value: Any) -> float | None:
    """Parse '$68,016.00', '68016', '' -> float or None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"n/a", "na", "none", "null", "-"}:
        return None
    text = _CURRENCY_STRIP.sub("", text)
    if not text or text in {"-", ".", "-."}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    # Negative or zero prices are placeholders, not real charges.
    return number if number > 0 else None


def is_drg_type(code_type: str | None) -> bool:
    """True when the file declares this code as some flavour of DRG."""
    return bool(code_type) and "drg" in str(code_type).lower()


# ---------------------------------------------------------------------------
# Streaming transport
# ---------------------------------------------------------------------------
class _IterStream(io.RawIOBase):
    """File-like adapter over requests' iter_content.

    Reading `response.raw` directly is fragile: urllib3 can close the underlying
    socket part-way through a large transfer, which surfaces as
    "ValueError: I/O operation on closed file" several seconds into a
    multi-gigabyte MRF and loses the whole file. iter_content is the supported
    streaming path and handles chunked transfer encoding consistently.
    """

    def __init__(self, iterator):
        self._iter = iterator
        self._buf = bytearray()

    def readable(self) -> bool:
        return True

    def readinto(self, target) -> int:  # type: ignore[override]
        want = len(target)
        while len(self._buf) < want:
            try:
                chunk = next(self._iter)
            except StopIteration:
                break
            if chunk:
                self._buf.extend(chunk)
        take = bytes(self._buf[:want])
        del self._buf[: len(take)]
        target[: len(take)] = take
        return len(take)


def _request(url: str):
    """GET with streaming, retrying once with a browser agent on 403.

    The original code passed the URL straight to pandas.read_csv, which sends no
    User-Agent at all; a large share of hospital CDNs answer that with 403.
    """
    response = requests.get(
        url,
        headers=config.HTTP_HEADERS,
        timeout=config.HTTP_TIMEOUT,
        stream=True,
        allow_redirects=True,
    )
    if response.status_code in (401, 403, 406):
        log.info("  %s on the default agent, retrying as a browser", response.status_code)
        response.close()
        response = requests.get(
            url,
            headers=config.HTTP_HEADERS_FALLBACK,
            timeout=config.HTTP_TIMEOUT,
            stream=True,
            allow_redirects=True,
        )
    response.raise_for_status()
    return response


def _filename_hints(url: str, response) -> str:
    """Everything we know about what this file is called and what it contains.

    The URL suffix alone is not enough: Maimonides publishes its MRF behind
    `download.aspx?pi=...`, which has no extension at all, so format detection
    also has to consider Content-Disposition and Content-Type.
    """
    parts = [url.lower().split("?")[0]]
    disposition = response.headers.get("Content-Disposition", "")
    parts.append(disposition.lower())
    parts.append(response.headers.get("Content-Type", "").lower())
    return " ".join(parts)


def _open_stream(url: str) -> tuple[io.TextIOWrapper, Any, bool]:
    """Open a remote MRF as a text stream.

    Returns (text_stream, response, is_json). Format is decided by sniffing the
    first non-whitespace byte where possible, falling back to filename and
    content-type hints -- a JSON body served from a .aspx endpoint is common
    enough that guessing from the URL alone mislabels real files.
    """
    response = _request(url)
    hints = _filename_hints(url, response)
    is_zip = ".zip" in hints or "application/zip" in hints

    if is_zip:
        # ZIP needs random access, so this one format must be buffered whole.
        log.info("  buffering zipped MRF...")
        blob = io.BytesIO(response.content)
        archive = zipfile.ZipFile(blob)
        names = [n for n in archive.namelist() if n.lower().endswith((".csv", ".json"))]
        if not names:
            raise ValueError(f"No .csv or .json inside {url}")
        binary: Any = io.BufferedReader(archive.open(names[0]))
        is_json = names[0].lower().endswith(".json")
    else:
        # iter_content transparently reverses Content-Encoding gzip.
        binary = io.BufferedReader(
            _IterStream(response.iter_content(chunk_size=65536)),
            buffer_size=262144,
        )
        # A body that is itself a .gz FILE still needs explicit decompression.
        if ".gz" in hints or "gzip" in response.headers.get("Content-Type", "").lower():
            binary = io.BufferedReader(gzip.GzipFile(fileobj=binary))  # type: ignore[arg-type]

        is_json = _sniff_json(binary)
        if is_json is None:
            is_json = ".json" in hints or "application/json" in hints

    text = io.TextIOWrapper(binary, encoding="utf-8-sig", errors="replace", newline="")
    return text, response, bool(is_json)


def _sniff_json(binary: io.BufferedReader) -> bool | None:
    """Peek at the first non-whitespace byte without consuming it."""
    try:
        head = binary.peek(512)[:512]
    except Exception:  # noqa: BLE001 - not every stream supports peek
        return None
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if not stripped:
        return None
    return stripped[:1] in (b"{", b"[")


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------
def _find_header_row(rows: list[list[str]]) -> int | None:
    """Locate the real column-header row in a CMS v2.0 CSV.

    The CMS template puts file-level metadata on rows 1-2 (hospital_name,
    last_updated_on, version, license number...) and the actual data headers on
    row 3. Reading with a default header=0, as the original did, takes
    'hospital_name' and 'last_updated_on' as the column names and every
    subsequent lookup misses.
    """
    for index, row in enumerate(rows):
        lowered = [c.strip().lower() for c in row]
        has_description = "description" in lowered
        has_code = any(c == "code" or c.startswith("code|") for c in lowered)
        has_charge = any(c.startswith("standard_charge") for c in lowered)
        if has_description and (has_code or has_charge):
            return index
    return None


def _extract_metadata(rows: list[list[str]], header_index: int) -> dict[str, str]:
    """Pull hospital_name / license number out of the metadata block."""
    meta: dict[str, str] = {}
    if header_index >= 2:
        keys = [c.strip().lower() for c in rows[0]]
        values = rows[1] if len(rows) > 1 else []
        for key, value in zip(keys, values):
            if key:
                meta[key] = value.strip()
    return meta


def _code_column_pairs(header: list[str]) -> list[tuple[int, int | None]]:
    """Return (code_index, code_type_index) pairs for code|1, code|2, ..."""
    lowered = [c.strip().lower() for c in header]
    pairs: list[tuple[int, int | None]] = []

    for index, name in enumerate(lowered):
        if name == "code" or (name.startswith("code|") and not name.endswith("|type")):
            suffix = name[len("code|"):] if name.startswith("code|") else ""
            type_name = f"code|{suffix}|type" if suffix else "code|type"
            type_index = lowered.index(type_name) if type_name in lowered else None
            if type_index is None and "code_type" in lowered:
                type_index = lowered.index("code_type")
            pairs.append((index, type_index))

    return pairs


def _matching_code(
    row: list[str], pairs: list[tuple[int, int | None]], target: str
) -> tuple[str, str] | None:
    """Find a code cell on this row that is one of the target DRGs.

    Returns (3-digit number, drg_system).
    """
    for code_index, type_index in pairs:
        if code_index >= len(row):
            continue
        code_type = (
            row[type_index] if type_index is not None and type_index < len(row) else None
        )
        hit = is_target_code(row[code_index], code_type)
        if hit:
            system, number = hit
            return number, system
    return None


def _index_of(header_lower: list[str], *names: str) -> int | None:
    for name in names:
        if name in header_lower:
            return header_lower.index(name)
    return None


def _cell(row: list[str], index: int | None) -> str | None:
    if index is None or index >= len(row):
        return None
    value = row[index].strip()
    return value or None


def _parse_wide_payer_columns(header: list[str]) -> dict[int, tuple[str, str, str]]:
    """Map column index -> (payer, plan, measure) for wide-format files.

    Wide files encode the payer in the header itself, e.g.
    `standard_charge|Aetna|PPO|negotiated_dollar`.
    """
    mapping: dict[int, tuple[str, str, str]] = {}
    for index, name in enumerate(header):
        parts = [p.strip() for p in name.strip().split("|")]
        if len(parts) == 4 and parts[0].lower() == "standard_charge":
            _, payer, plan, measure = parts
            mapping[index] = (payer, plan, measure.lower())
    return mapping


def parse_csv_mrf(
    stream: io.TextIOWrapper, facility_label: str, source_url: str, target_drg: str
) -> Iterator[dict]:
    reader = csv.reader(stream)

    preamble: list[list[str]] = []
    for _ in range(10):
        try:
            preamble.append(next(reader))
        except StopIteration:
            break

    header_index = _find_header_row(preamble)
    if header_index is None:
        raise ValueError("Could not locate a CMS column-header row in the first 10 rows")

    header = preamble[header_index]
    metadata = _extract_metadata(preamble, header_index)
    header_lower = [c.strip().lower() for c in header]

    facility_name = metadata.get("hospital_name") or facility_label
    ccn = (
        metadata.get("license_number")
        or metadata.get("cms_certification_number")
        or metadata.get("ccn")
    )

    pairs = _code_column_pairs(header)
    if not pairs:
        raise ValueError("No code columns found in MRF header")

    idx_description = _index_of(header_lower, "description")
    idx_setting = _index_of(header_lower, "setting")
    idx_payer = _index_of(header_lower, "payer_name")
    idx_plan = _index_of(header_lower, "plan_name")
    idx_gross = _index_of(header_lower, "standard_charge|gross", "gross_charge")
    idx_cash = _index_of(
        header_lower, "standard_charge|discounted_cash", "discounted_cash_price"
    )
    # Only the *dollar* column is a price. `'negotiated' in c or 'rate' in c`
    # happily selected standard_charge|negotiated_percentage, writing a number
    # like 65 (percent of billed charges) into a dollar column.
    idx_negotiated = _index_of(
        header_lower, "standard_charge|negotiated_dollar", "standard_charge|negotiated"
    )
    idx_min = _index_of(header_lower, "standard_charge|min")
    idx_max = _index_of(header_lower, "standard_charge|max")

    wide_columns = _parse_wide_payer_columns(header) if idx_payer is None else {}
    is_tall = idx_payer is not None

    log.info(
        "  parsing %s-format CSV (header row %s, %s code column(s))",
        "tall" if is_tall else "wide",
        header_index + 1,
        len(pairs),
    )

    # Rows that appeared after the header inside the sniffed preamble.
    leftover = preamble[header_index + 1:]

    scanned = 0
    for row in _chain(leftover, reader):
        scanned += 1
        if config.MRF_MAX_ROWS_SCANNED and scanned > config.MRF_MAX_ROWS_SCANNED:
            log.warning("  stopping at MRF_MAX_ROWS_SCANNED=%s", config.MRF_MAX_ROWS_SCANNED)
            break

        match = _matching_code(row, pairs, target_drg)
        if match is None:
            continue
        billing_code, billing_code_type = match

        base = {
            "facility_name": facility_name,
            "cms_certification_number": ccn,
            "billing_code": billing_code,
            "billing_code_type": billing_code_type,
            "description": _cell(row, idx_description),
            "setting": _cell(row, idx_setting),
            "gross_charge": clean_currency(_cell(row, idx_gross)),
            "discounted_cash_price": clean_currency(_cell(row, idx_cash)),
            "min_negotiated_charge": clean_currency(_cell(row, idx_min)),
            "max_negotiated_charge": clean_currency(_cell(row, idx_max)),
            "source_url": source_url,
        }

        if is_tall:
            yield {
                **base,
                "payer_name": _cell(row, idx_payer),
                "plan_name": _cell(row, idx_plan),
                "payer_specific_negotiated_charge": clean_currency(
                    _cell(row, idx_negotiated)
                ),
            }
        else:
            emitted = False
            for index, (payer, plan, measure) in wide_columns.items():
                if measure != "negotiated_dollar":
                    continue
                amount = clean_currency(_cell(row, index))
                if amount is None:
                    continue
                emitted = True
                yield {
                    **base,
                    "payer_name": payer,
                    "plan_name": plan,
                    "payer_specific_negotiated_charge": amount,
                }
            if not emitted:
                # Cash-price-only row: still worth keeping as a fallback cost.
                yield {**base, "payer_name": None, "plan_name": None,
                       "payer_specific_negotiated_charge": None}


def _chain(first: list[list[str]], rest: Any) -> Iterator[list[str]]:
    yield from first
    yield from rest


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------
def parse_json_mrf(
    stream: io.TextIOWrapper, facility_label: str, source_url: str, target_drg: str
) -> Iterator[dict]:
    """Stream a CMS v2.0 JSON MRF with ijson (constant memory)."""
    import ijson

    facility_name = facility_label
    ccn = None

    for item in ijson.items(stream, "standard_charge_information.item"):
        codes = item.get("code_information") or []
        matched = None
        for entry in codes:
            hit = is_target_code(entry.get("code"), entry.get("type"))
            if hit:
                system, number = hit
                matched = (number, system)
                break
        if matched is None:
            continue

        billing_code, billing_code_type = matched
        description = item.get("description")

        for charge in item.get("standard_charges") or []:
            base = {
                "facility_name": facility_name,
                "cms_certification_number": ccn,
                "billing_code": billing_code,
                "billing_code_type": billing_code_type,
                "description": description,
                "setting": charge.get("setting"),
                "gross_charge": clean_currency(charge.get("gross_charge")),
                "discounted_cash_price": clean_currency(charge.get("discounted_cash")),
                "min_negotiated_charge": clean_currency(charge.get("minimum")),
                "max_negotiated_charge": clean_currency(charge.get("maximum")),
                "source_url": source_url,
            }

            payers = charge.get("payers_information") or []
            if not payers:
                yield {**base, "payer_name": None, "plan_name": None,
                       "payer_specific_negotiated_charge": None}
                continue

            for payer in payers:
                yield {
                    **base,
                    "payer_name": payer.get("payer_name"),
                    "plan_name": payer.get("plan_name"),
                    "payer_specific_negotiated_charge": clean_currency(
                        payer.get("standard_charge_dollar")
                    ),
                }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def ingest_mrf(facility_label: str, mrf_url: str, target_drg: str | None = None) -> int:
    """Stream one MRF into stg_cms_mrf. Returns rows inserted."""
    target_drg = target_drg or config.TARGET_MS_DRG
    log.info("Streaming MRF for %s", facility_label)
    log.info("  %s", mrf_url)

    stream = None
    response = None
    total = 0
    batch: list[dict] = []
    try:
        stream, response, is_json = _open_stream(mrf_url)
        parser = parse_json_mrf if is_json else parse_csv_mrf

        for record in parser(stream, facility_label, mrf_url, target_drg):
            batch.append(record)
            if len(batch) >= config.MRF_BATCH_ROWS:
                total += db.copy_records("stg_cms_mrf", DB_COLUMNS, batch)
                batch.clear()

        if batch:
            total += db.copy_records("stg_cms_mrf", DB_COLUMNS, batch)
            batch.clear()

        log.info("  inserted %s DRG-%s rows for %s", total, target_drg, facility_label)

    except Exception as exc:  # noqa: BLE001 - one bad MRF must not kill the crawl
        # Multi-gigabyte streams from hospital CDNs do drop mid-read. Keep
        # whatever was parsed before the failure rather than discarding it, and
        # say plainly that the result is partial.
        if batch:
            try:
                total += db.copy_records("stg_cms_mrf", DB_COLUMNS, batch)
            except Exception:  # noqa: BLE001
                pass
        if total:
            log.warning(
                "  PARTIAL: %s failed after %s rows (%s: %s)",
                facility_label, total, type(exc).__name__, str(exc)[:120],
            )
        else:
            log.error(
                "  failed to process MRF for %s (%s: %s)",
                facility_label, type(exc).__name__, str(exc)[:160],
            )
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass
        if response is not None:
            response.close()

    return total


def ingest_all(locations: list, truncate_first: bool = True) -> int:
    if truncate_first:
        db.truncate("stg_cms_mrf")

    total = 0
    for location in locations:
        total += ingest_mrf(location.label, location.mrf_url)

    log.info("Inserted %s pricing rows in total.", total)
    return total
