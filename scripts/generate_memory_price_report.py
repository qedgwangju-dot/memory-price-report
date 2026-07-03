from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT / "reports"
REPORTS_DIR.mkdir(exist_ok=True)
SNAPSHOT_DIR = REPORTS_DIR / "snapshots"
SNAPSHOT_DIR.mkdir(exist_ok=True)

KST = ZoneInfo("Asia/Seoul")
TODAY = datetime.now(KST).strftime("%Y-%m-%d")
TODAY_DATE = datetime.now(KST).date()
QUERY_TIME = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
UNAVAILABLE = "확인 불가"
DELAYED_OR_CONFLICT = "지연/불일치 있음"
VERIFIED = "확인"
STALE_REFERENCE_DAYS = 45
REQUEST_TIMEOUT_SECONDS = 15
REQUEST_RETRIES = 2

DRAMEXCHANGE_HOME = "https://www.dramexchange.com/"
DRAMEXCHANGE_HOME_PRICE = "https://www.dramexchange.com/Home/HomePrice"
YAHOO_USDKRW = "https://query1.finance.yahoo.com/v8/finance/chart/USDKRW=X?range=5d&interval=1d"
DANAWA_PRICE_HISTORY = "https://prod.danawa.com/info/ajax/getProductPriceList.ajax.php"
WAYBACK_CDX = "https://web.archive.org/cdx"

DANAWA_PRODUCTS = {
    "소매 메모리": {
        "name": "삼성전자 DDR5-5600 32GB",
        "url": "https://prod.danawa.com/info/?pcode=20644043",
    },
    "소매 HDD": {
        "name": "Seagate BarraCuda 8TB",
        "url": "https://prod.danawa.com/info/?pcode=5764992",
    },
    "소매 SSD": {
        "name": "Samsung 990 PRO 1TB",
        "url": "https://prod.danawa.com/info/?pcode=18297002",
    },
}

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0 Safari/537.36"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    }
)


@dataclass
class ExchangeRate:
    value: float | None
    source: str
    source_url: str
    query_time: str
    last_update: str
    status: str
    change_pct: float | None = None


@dataclass
class SourceRow:
    item: str
    value: float
    change_pct: float | None
    source_url: str
    last_update: str


@dataclass
class ReportRecord:
    label: str
    group: str
    current: str
    query_source: str
    last_update: str
    day: str
    week: str
    month: str
    year: str
    trend: str
    verdict: str
    basis: str
    change_pct: float | None = None
    source_url: str = ""
    certainty_status: str = VERIFIED
    numeric_value: float | None = None
    value_unit: str = ""
    yoy_pct: float | None = None
    yoy_status: str = UNAVAILABLE
    yoy_source: str = ""
    yoy_source_url: str = ""
    yoy_reference: str = ""


@dataclass
class PriorYearValue:
    value: float
    unit: str
    source: str
    source_url: str
    reference: str


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    match = re.search(r"-?\d+(?:,\d{3})*(?:\.\d+)?", str(value))
    if not match:
        return None
    return float(match.group(0).replace(",", ""))


def parse_pct(value: Any) -> float | None:
    text = str(value or "")
    match = re.search(r"[-+]?\d+(?:\.\d+)?\s*%", text)
    if not match:
        return None
    return float(match.group(0).replace("%", "").replace(" ", ""))


def pct_text(value: float | None) -> str:
    if value is None:
        return UNAVAILABLE
    return f"{value:+.2f}%"


def fmt_krw(value: float) -> str:
    return f"{round(value):,}원"


def fmt_usd(value: float, rate: ExchangeRate) -> str:
    base = f"${value:,.3f}"
    if rate.value is None:
        return f"{base} / 환율 {UNAVAILABLE}"
    return f"{base} (약 {fmt_krw(value * rate.value)})"


def kst_from_timestamp(ts: int | str | float | None) -> str:
    if ts in (None, ""):
        return "원문 Last Update 별도 없음"
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
    except Exception:
        return "원문 Last Update 별도 없음"


def certainty_from_last_update(last_update: str) -> str:
    match = re.search(r"\d{4}-\d{2}-\d{2}", last_update or "")
    if not match:
        return VERIFIED
    try:
        ref_date = datetime.strptime(match.group(0), "%Y-%m-%d").date()
    except ValueError:
        return VERIFIED
    age_days = (datetime.now(KST).date() - ref_date).days
    return DELAYED_OR_CONFLICT if age_days > STALE_REFERENCE_DAYS else VERIFIED


def classify(change_pct: float | None) -> tuple[str, str, str]:
    if change_pct is None:
        return UNAVAILABLE, "판단 보류", "보류"
    if change_pct > 0.5:
        return f"공개 변화율 {pct_text(change_pct)} 기준 상승", "강함", "상승"
    if change_pct < -0.5:
        return f"공개 변화율 {pct_text(change_pct)} 기준 하락", "약함", "하락"
    return f"공개 변화율 {pct_text(change_pct)} 기준 보합", "보합", "보합"


def safe_get(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(REQUEST_RETRIES + 1):
        try:
            response = SESSION.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt < REQUEST_RETRIES:
                time.sleep(1.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def query_usdkrw() -> ExchangeRate:
    try:
        data = safe_get(YAHOO_USDKRW).json()
        result = data["chart"]["result"][0]
        meta = result["meta"]
        rate = float(meta["regularMarketPrice"])
        prev = parse_float(meta.get("chartPreviousClose"))
        change = ((rate / prev) - 1) * 100 if prev else None
        return ExchangeRate(
            value=rate,
            source="Yahoo Finance USDKRW=X",
            source_url="https://finance.yahoo.com/quote/USDKRW=X",
            query_time=QUERY_TIME,
            last_update=kst_from_timestamp(meta.get("regularMarketTime")),
            status=VERIFIED,
            change_pct=change,
        )
    except Exception as exc:
        return ExchangeRate(
            value=None,
            source="Yahoo Finance USDKRW=X",
            source_url="https://finance.yahoo.com/quote/USDKRW=X",
            query_time=QUERY_TIME,
            last_update=UNAVAILABLE,
            status=f"{UNAVAILABLE}: {exc.__class__.__name__}",
        )


def fetch_dramexchange_home() -> BeautifulSoup | None:
    try:
        return BeautifulSoup(safe_get(DRAMEXCHANGE_HOME).text, "html.parser")
    except Exception:
        return None


def parse_spot_table(soup: BeautifulSoup | None, table_id: str) -> list[SourceRow]:
    if soup is None:
        return []
    tbody = soup.find("tbody", id=table_id)
    if tbody is None:
        return []

    rows: list[SourceRow] = []
    for tr in tbody.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 7:
            continue
        item = normalize_text(cells[0].get_text(" ", strip=True))
        if not item or item.lower() == "item":
            continue
        value = parse_float(cells[5].get_text(" ", strip=True))
        if value is None:
            continue
        change = parse_pct(cells[6].get_text(" ", strip=True))
        link = cells[0].find("a", href=True)
        href = link["href"] if link else ""
        source_url = href if href.startswith("http") else f"https://www.dramexchange.com{href}"
        rows.append(
            SourceRow(
                item=item,
                value=value,
                change_pct=change,
                source_url=source_url,
                last_update="홈 공개표, 원문 Last Update 별도 없음",
            )
        )
    return rows


def find_row(rows: list[SourceRow], *keywords: str) -> SourceRow | None:
    lowered = [(row, row.item.lower()) for row in rows]
    for row, item in lowered:
        if all(keyword.lower() in item for keyword in keywords):
            return row
    return None


def record_from_row(label: str, group: str, row: SourceRow | None, rate: ExchangeRate, basis: str) -> ReportRecord:
    if row is None:
        return unavailable_record(label, group, "DRAMeXchange 공개표", DRAMEXCHANGE_HOME, UNAVAILABLE)

    trend, verdict, _ = classify(row.change_pct)
    current = fmt_usd(row.value, rate)
    change = f"공개 변화율 {pct_text(row.change_pct)}" if row.change_pct is not None else UNAVAILABLE
    return ReportRecord(
        label=label,
        group=group,
        current=current,
        query_source=f"{QUERY_TIME} · DRAMeXchange 공개표",
        last_update=row.last_update,
        day=change if basis == "전일" else UNAVAILABLE,
        week=change if basis == "전주" else UNAVAILABLE,
        month=change if basis == "전월" else UNAVAILABLE,
        year=UNAVAILABLE,
        trend=trend,
        verdict=verdict,
        basis=basis,
        change_pct=row.change_pct,
        source_url=row.source_url,
        certainty_status=VERIFIED,
        numeric_value=row.value,
        value_unit="USD",
    )


def fetch_home_price(source: str) -> list[dict[str, Any]]:
    try:
        data = safe_get(DRAMEXCHANGE_HOME_PRICE, params={"Source": source}).json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


def find_dict(rows: list[dict[str, Any]], field: str, *keywords: str) -> dict[str, Any] | None:
    for row in rows:
        value = str(row.get(field) or "").lower()
        if all(keyword.lower() in value for keyword in keywords):
            return row
    return rows[0] if rows else None


def find_dict_strict(rows: list[dict[str, Any]], field: str, *keywords: str) -> dict[str, Any] | None:
    for row in rows:
        value = str(row.get(field) or "").lower()
        if all(keyword.lower() in value for keyword in keywords):
            return row
    return None


def record_from_home_price(
    label: str,
    group: str,
    row: dict[str, Any] | None,
    rate: ExchangeRate,
    source_name: str,
    source_url: str,
) -> ReportRecord:
    if row is None:
        return unavailable_record(label, group, source_name, source_url, UNAVAILABLE)

    value = parse_float(row.get("show_avg", row.get("avg")))
    if value is None:
        return unavailable_record(label, group, source_name, source_url, UNAVAILABLE)

    change = parse_float(row.get("show_avg_change", row.get("change")))
    name = str(row.get("show_name") or row.get("Name") or row.get("Series") or label)
    last_update = row.get("show_day") or row.get("slotTime") or kst_from_timestamp(row.get("updateTime"))
    if isinstance(last_update, str) and "T" in last_update:
        last_update = last_update.replace("T", " ")
    last_update = str(last_update or "원문 Last Update 별도 없음")
    certainty_status = certainty_from_last_update(last_update)

    trend, verdict, _ = classify(change)
    current = fmt_usd(value, rate)
    change_text = f"공개 변화율 {pct_text(change)}, 기준값 확인 불가" if change is not None else UNAVAILABLE
    if certainty_status == DELAYED_OR_CONFLICT:
        current = DELAYED_OR_CONFLICT
        change_text = DELAYED_OR_CONFLICT
        trend = DELAYED_OR_CONFLICT
        verdict = "판단 보류"
        change = None
    return ReportRecord(
        label=f"{label} ({name})" if name and name != label else label,
        group=group,
        current=current,
        query_source=f"{QUERY_TIME} · {source_name}",
        last_update=last_update,
        day=UNAVAILABLE,
        week=UNAVAILABLE,
        month=change_text,
        year=UNAVAILABLE,
        trend=trend,
        verdict=verdict,
        basis="전월",
        change_pct=change,
        source_url=source_url,
        certainty_status=certainty_status,
        numeric_value=value if certainty_status == VERIFIED else None,
        value_unit="USD" if certainty_status == VERIFIED else "",
    )


def parse_danawa_price(html: str) -> float | None:
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        offers = data.get("offers") if isinstance(data, dict) else None
        if isinstance(offers, dict):
            low_price = parse_float(offers.get("lowPrice"))
            if low_price is not None:
                return low_price

    patterns = [
        r'nMinPrice"\s*:\s*"([0-9,]+)"',
        r"nMinPrice:\s*\"([0-9,]+)\"",
        r'"lowPrice"\s*:\s*"?([0-9,]+)"?',
        r"최저가\s*([0-9,]+)원",
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return parse_float(match.group(1))
    return None


def fetch_danawa_record(label: str, product: dict[str, str]) -> ReportRecord:
    url = product["url"]
    source_name = f"Danawa prod 가격비교 · {product['name']}"
    try:
        html = safe_get(url).text
        price = parse_danawa_price(html)
        if price is None:
            return unavailable_record(label, "소매", source_name, url, UNAVAILABLE)
        return ReportRecord(
            label=f"{label} ({product['name']})",
            group="소매",
            current=fmt_krw(price),
            query_source=f"{QUERY_TIME} · {source_name}",
            last_update="상품 페이지 직접 조회, 원문 Last Update 별도 없음",
            day=UNAVAILABLE,
            week=UNAVAILABLE,
            month=UNAVAILABLE,
            year=UNAVAILABLE,
            trend=f"비교 기준값 {UNAVAILABLE}",
            verdict="판단 보류",
            basis=UNAVAILABLE,
            source_url=url,
            certainty_status=VERIFIED,
            numeric_value=price,
            value_unit="KRW",
        )
    except Exception as exc:
        return unavailable_record(label, "소매", source_name, url, f"{UNAVAILABLE}: {exc.__class__.__name__}")


def unavailable_record(label: str, group: str, source: str, source_url: str, reason: str) -> ReportRecord:
    return ReportRecord(
        label=label,
        group=group,
        current=reason,
        query_source=f"{QUERY_TIME} · {source}",
        last_update=reason,
        day=UNAVAILABLE,
        week=UNAVAILABLE,
        month=UNAVAILABLE,
        year=UNAVAILABLE,
        trend=reason,
        verdict="판단 보류",
        basis=UNAVAILABLE,
        source_url=source_url,
        certainty_status=UNAVAILABLE,
    )


def build_records(rate: ExchangeRate) -> list[ReportRecord]:
    soup = fetch_dramexchange_home()
    dram_rows = parse_spot_table(soup, "tb_NationalDramSpotPrice")
    flash_rows = parse_spot_table(soup, "tb_NationalFlashSpotPrice")
    module_rows = parse_spot_table(soup, "tb_ModuleSpotPrice")

    dram_contract = fetch_home_price("NationalDramContract")
    flash_contract = fetch_home_price("NationalFlashContract")
    pcc_contract = fetch_home_price("PCC")
    ssd_street = fetch_home_price("SSD")

    records = [
        record_from_row("DDR3 칩", "DRAM", find_row(dram_rows, "DDR3"), rate, "전일"),
        record_from_row("DDR4 칩", "DRAM", find_row(dram_rows, "DDR4", "2Gx8", "3200"), rate, "전일"),
        record_from_row("DDR5 칩", "DRAM", find_row(dram_rows, "DDR5", "2Gx8", "4800"), rate, "전일"),
        record_from_row("DDR4 모듈", "DRAM", find_row(module_rows, "DDR4", "UDIMM", "16GB", "3200"), rate, "전주"),
        record_from_row("DDR5 모듈", "DRAM", find_row(module_rows, "DDR5", "UDIMM", "16GB"), rate, "전주"),
        record_from_home_price(
            "DRAM 계약가",
            "DRAM",
            find_dict(dram_contract, "show_name", "DDR4", "8Gb"),
            rate,
            "DRAMeXchange HomePrice NationalDramContract",
            "https://www.dramexchange.com/Price/NationalContractDramDetail",
        ),
        record_from_row("NAND 웨이퍼", "NAND/SSD", find_row(flash_rows, "TLC", "512Gb"), rate, "전일"),
        record_from_home_price(
            "NAND 계약가",
            "NAND/SSD",
            find_dict(flash_contract, "show_name", "NAND"),
            rate,
            "DRAMeXchange HomePrice NationalFlashContract",
            "https://www.dramexchange.com/Price/NationalContractFlashDetail",
        ),
        record_from_home_price(
            "PC-client OEM SSD 계약가",
            "NAND/SSD",
            find_dict(pcc_contract, "Name", "1TB"),
            rate,
            "DRAMeXchange HomePrice PCC",
            "https://www.dramexchange.com/Price/PCClientOEMSSD",
        ),
        record_from_home_price(
            "SSD street price",
            "NAND/SSD",
            find_dict(ssd_street, "Series", "990", "pro"),
            rate,
            "DRAMeXchange HomePrice SSD",
            "https://www.dramexchange.com/Price/SSD_Street",
        ),
    ]
    records.extend(fetch_danawa_record(label, product) for label, product in DANAWA_PRODUCTS.items())
    return records


def base_label(record: ReportRecord) -> str:
    return record.label.split(" (", 1)[0]


def previous_year_date(current: date) -> date:
    try:
        return current.replace(year=current.year - 1)
    except ValueError:
        return current.replace(year=current.year - 1, day=28)


def cdx_date(value: date) -> str:
    return value.strftime("%Y%m%d")


def parse_wayback_timestamp(timestamp: str) -> datetime | None:
    try:
        return datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def wayback_kst(timestamp: str) -> str:
    parsed = parse_wayback_timestamp(timestamp)
    if parsed is None:
        return timestamp
    return parsed.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")


def wayback_url(capture: dict[str, str]) -> str:
    return f"https://web.archive.org/web/{capture['timestamp']}id_/{capture['original']}"


def fetch_cdx_captures(url_pattern: str, start: date, end: date, limit: int = 50) -> list[dict[str, str]]:
    try:
        data = safe_get(
            WAYBACK_CDX,
            params={
                "url": url_pattern,
                "from": cdx_date(start),
                "to": cdx_date(end),
                "output": "json",
                "filter": "statuscode:200",
                "collapse": "digest",
                "fl": "timestamp,original,statuscode,digest",
                "limit": str(limit),
            },
        ).json()
    except Exception:
        return []
    if not isinstance(data, list) or len(data) < 2:
        return []
    headers = [str(item) for item in data[0]]
    captures: list[dict[str, str]] = []
    for row in data[1:]:
        if isinstance(row, list) and len(row) == len(headers):
            captures.append({headers[index]: str(value) for index, value in enumerate(row)})
    return captures


def sorted_captures_by_distance(captures: list[dict[str, str]], target: date) -> list[dict[str, str]]:
    target_dt = datetime(target.year, target.month, target.day, 12, tzinfo=timezone.utc)

    def distance(capture: dict[str, str]) -> float:
        parsed = parse_wayback_timestamp(capture.get("timestamp", ""))
        if parsed is None:
            return float("inf")
        return abs((parsed - target_dt).total_seconds())

    return sorted(captures, key=distance)


def add_prior_from_row(
    values: dict[str, PriorYearValue],
    label: str,
    row: SourceRow | None,
    source: str,
    source_url: str,
    reference: str,
) -> None:
    if row is None:
        return
    values[label] = PriorYearValue(row.value, "USD", source, source_url, f"{reference}, 항목 {row.item}")


def fetch_prior_year_dramexchange_spot(prior_date: date) -> dict[str, PriorYearValue]:
    captures = fetch_cdx_captures("www.dramexchange.com/", prior_date - timedelta(days=7), prior_date + timedelta(days=7), 20)
    for capture in sorted_captures_by_distance(captures, prior_date):
        archive_url = wayback_url(capture)
        try:
            soup = BeautifulSoup(safe_get(archive_url).text, "html.parser")
        except Exception:
            continue
        dram_rows = parse_spot_table(soup, "tb_NationalDramSpotPrice")
        flash_rows = parse_spot_table(soup, "tb_NationalFlashSpotPrice")
        module_rows = parse_spot_table(soup, "tb_ModuleSpotPrice")
        if not (dram_rows or flash_rows or module_rows):
            continue
        source = "Internet Archive DRAMeXchange 공개표 캡처"
        reference = f"캡처 {wayback_kst(capture['timestamp'])}, 원문 Last Update 별도 없음"
        values: dict[str, PriorYearValue] = {}
        add_prior_from_row(values, "DDR3 칩", find_row(dram_rows, "DDR3"), source, archive_url, reference)
        add_prior_from_row(values, "DDR4 칩", find_row(dram_rows, "DDR4", "2Gx8", "3200"), source, archive_url, reference)
        add_prior_from_row(values, "DDR5 칩", find_row(dram_rows, "DDR5", "2Gx8", "4800"), source, archive_url, reference)
        add_prior_from_row(values, "DDR4 모듈", find_row(module_rows, "DDR4", "UDIMM", "16GB", "3200"), source, archive_url, reference)
        add_prior_from_row(values, "DDR5 모듈", find_row(module_rows, "DDR5", "UDIMM", "16GB"), source, archive_url, reference)
        add_prior_from_row(values, "NAND 웨이퍼", find_row(flash_rows, "TLC", "512Gb"), source, archive_url, reference)
        return values
    return {}


def normalized_home_price_last_update(row: dict[str, Any]) -> str:
    last_update = row.get("show_day") or row.get("slotTime") or kst_from_timestamp(row.get("updateTime"))
    if isinstance(last_update, str) and "T" in last_update:
        last_update = last_update.replace("T", " ")
    return str(last_update or "원문 Last Update 별도 없음")


def reference_is_compatible(last_update: str, target: date) -> bool:
    match = re.search(r"\d{4}-\d{2}-\d{2}", last_update or "")
    if not match:
        return True
    try:
        ref_date = datetime.strptime(match.group(0), "%Y-%m-%d").date()
    except ValueError:
        return True
    return abs((ref_date - target).days) <= STALE_REFERENCE_DAYS


def fetch_wayback_json(capture: dict[str, str]) -> list[dict[str, Any]]:
    try:
        data = safe_get(wayback_url(capture)).json()
    except Exception:
        return []
    return data if isinstance(data, list) else []


def fetch_prior_year_dramexchange_home_prices(prior_date: date) -> dict[str, PriorYearValue]:
    captures = fetch_cdx_captures(
        "www.dramexchange.com/Home/HomePrice*",
        prior_date - timedelta(days=45),
        prior_date + timedelta(days=45),
        200,
    )
    specs = [
        ("DRAM 계약가", "NationalDramContract", "show_name", ("DDR4", "8Gb"), "USD", "DRAMeXchange HomePrice NationalDramContract"),
        ("NAND 계약가", "NationalFlashContract", "show_name", ("NAND",), "USD", "DRAMeXchange HomePrice NationalFlashContract"),
        ("PC-client OEM SSD 계약가", "PCC", "Name", ("1TB",), "USD", "DRAMeXchange HomePrice PCC"),
        ("SSD street price", "SSD", "Series", ("990", "pro"), "USD", "DRAMeXchange HomePrice SSD"),
    ]
    values: dict[str, PriorYearValue] = {}
    for label, source_key, field, keywords, unit, source_name in specs:
        source_captures = [
            capture
            for capture in captures
            if f"source={source_key.lower()}" in capture.get("original", "").lower()
        ]
        for capture in sorted_captures_by_distance(source_captures, prior_date)[:6]:
            rows = fetch_wayback_json(capture)
            row = find_dict_strict(rows, field, *keywords)
            if row is None:
                continue
            last_update = normalized_home_price_last_update(row)
            if not reference_is_compatible(last_update, prior_date):
                continue
            value = parse_float(row.get("show_avg", row.get("avg")))
            if value is None:
                continue
            values[label] = PriorYearValue(
                value=value,
                unit=unit,
                source=f"Internet Archive {source_name} 캡처",
                source_url=wayback_url(capture),
                reference=f"원문 {last_update}, 캡처 {wayback_kst(capture['timestamp'])}",
            )
            break
    return values


def danawa_product_code(url: str) -> str | None:
    match = re.search(r"pcode=(\d+)", url)
    return match.group(1) if match else None


def fetch_prior_year_danawa(prior_date: date) -> dict[str, PriorYearValue]:
    values: dict[str, PriorYearValue] = {}
    target_month = prior_date.strftime("%y-%m")
    for label, product in DANAWA_PRODUCTS.items():
        product_code = danawa_product_code(product["url"])
        if product_code is None:
            continue
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": product["url"],
        }
        try:
            data = safe_get(DANAWA_PRICE_HISTORY, params={"productCode": product_code}, headers=headers).json()
        except Exception:
            continue
        series = data.get("24") if isinstance(data, dict) else None
        points = series.get("result") if isinstance(series, dict) else None
        if not isinstance(points, list):
            continue
        for point in points:
            if not isinstance(point, dict) or str(point.get("date")) != target_month:
                continue
            price = parse_float(point.get("minPrice"))
            if price is None or price <= 0:
                continue
            values[label] = PriorYearValue(
                value=price,
                unit="KRW",
                source=f"Danawa 가격추이 24개월 · {product['name']}",
                source_url=f"{DANAWA_PRICE_HISTORY}?productCode={product_code}",
                reference=f"{target_month} 월간 가격추이",
            )
            break
    return values


def fetch_prior_year_values(prior_date: date) -> dict[str, PriorYearValue]:
    values: dict[str, PriorYearValue] = {}
    for fetcher in (
        fetch_prior_year_dramexchange_spot,
        fetch_prior_year_dramexchange_home_prices,
        fetch_prior_year_danawa,
    ):
        try:
            values.update(fetcher(prior_date))
        except Exception:
            continue
    return values


def snapshot_path(snapshot_date: date) -> Path:
    return SNAPSHOT_DIR / f"memory_price_snapshot_{snapshot_date.isoformat()}.json"


def load_snapshot(snapshot_date: date) -> dict[str, dict[str, Any]]:
    path = snapshot_path(snapshot_date)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    rows = data.get("records")
    if not isinstance(rows, list):
        return {}
    return {str(row.get("label") or ""): row for row in rows if isinstance(row, dict)}


def prior_value_from_snapshot(record: ReportRecord, prior_date: date, prior_rows: dict[str, dict[str, Any]]) -> PriorYearValue | None:
    prior = prior_rows.get(base_label(record))
    if not prior:
        return None
    if prior.get("certainty_status") != VERIFIED:
        return None
    if prior.get("value_unit") != record.value_unit:
        return None
    prior_value = parse_float(prior.get("numeric_value"))
    if prior_value is None or prior_value <= 0:
        return None
    return PriorYearValue(
        value=prior_value,
        unit=record.value_unit,
        source=f"{prior_date.isoformat()} 검증 스냅샷",
        source_url=str(snapshot_path(prior_date)),
        reference=prior_date.isoformat(),
    )


def apply_yoy(records: list[ReportRecord]) -> None:
    prior_date = previous_year_date(TODAY_DATE)
    prior_values = fetch_prior_year_values(prior_date)
    prior_rows = load_snapshot(prior_date)
    for record in records:
        record.year = "전년 직접치 부족"
        record.yoy_status = "전년 직접치 부족"
        if record.certainty_status != VERIFIED or record.numeric_value is None:
            record.year = "현재 직접치 부족"
            record.yoy_status = "현재 직접치 부족"
            continue
        prior = prior_values.get(base_label(record)) or prior_value_from_snapshot(record, prior_date, prior_rows)
        if prior is None or prior.unit != record.value_unit or prior.value <= 0:
            continue
        record.yoy_pct = ((record.numeric_value / prior.value) - 1) * 100
        record.yoy_status = VERIFIED
        record.yoy_source = prior.source
        record.yoy_source_url = prior.source_url
        record.yoy_reference = prior.reference
        record.year = f"전년 대비 {pct_text(record.yoy_pct)} · 전년값 {prior.reference} · 출처 {prior.source}"


def save_snapshot(records: list[ReportRecord], rate: ExchangeRate) -> Path:
    path = snapshot_path(TODAY_DATE)
    payload = {
        "schema_version": 1,
        "date": TODAY,
        "query_time": QUERY_TIME,
        "exchange_rate": {
            "value": rate.value,
            "source": rate.source,
            "source_url": rate.source_url,
            "last_update": rate.last_update,
            "status": rate.status,
        },
        "records": [
            {
                "label": base_label(record),
                "group": record.group,
                "numeric_value": record.numeric_value,
                "value_unit": record.value_unit,
                "certainty_status": record.certainty_status,
                "current": record.current,
                "query_source": record.query_source,
                "source_url": record.source_url,
                "last_update": record.last_update,
                "yoy_pct": record.yoy_pct,
                "yoy_status": record.yoy_status,
                "yoy_source": record.yoy_source,
                "yoy_source_url": record.yoy_source_url,
                "yoy_reference": record.yoy_reference,
            }
            for record in records
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def format_source(record: ReportRecord) -> str:
    source = record.query_source
    if record.source_url:
        source = f"{source} ({record.source_url})"
    return f"{source} · 상태 {record.certainty_status}"


def summarize(records: list[ReportRecord]) -> dict[str, list[str]]:
    rising: list[str] = []
    falling: list[str] = []
    pending: list[str] = []
    yoy: list[str] = []
    for record in records:
        base_label = record.label.split(" (", 1)[0]
        if record.verdict == "강함":
            rising.append(f"{base_label} {pct_text(record.change_pct)}")
        elif record.verdict == "약함":
            falling.append(f"{base_label} {pct_text(record.change_pct)}")
        elif record.verdict == "판단 보류" and record.yoy_status != VERIFIED:
            pending.append(base_label)
        if record.year == UNAVAILABLE:
            yoy.append(base_label)
    return {
        "상승": rising,
        "하락": falling,
        "YoY": yoy,
        "보류": pending,
    }


def compact_label(label: str) -> str:
    base = label.split(" (", 1)[0]
    replacements = {
        "DDR3 칩": "DDR3",
        "DDR4 칩": "DDR4",
        "DDR5 칩": "DDR5",
        "DDR4 모듈": "DDR4 모듈",
        "DDR5 모듈": "DDR5 모듈",
        "PC-client OEM SSD 계약가": "OEM SSD",
        "SSD street price": "SSD street",
        "DRAM 계약가": "DRAM 계약",
        "NAND 계약가": "NAND",
        "NAND 웨이퍼": "NAND 웨이퍼",
        "소매 메모리": "소매 메모리",
        "소매 HDD": "소매 HDD",
        "소매 SSD": "소매 SSD",
    }
    return replacements.get(base, base)


def compact_source(record: ReportRecord) -> str:
    source_text = record.query_source.lower()
    if "danawa" in source_text:
        return "Danawa"
    if "yahoo" in source_text:
        return "Yahoo"
    if "dramexchange" in source_text:
        return "DX"
    return "출처"


def compact_yoy_source(record: ReportRecord) -> str:
    source_text = (record.yoy_source or record.query_source).lower()
    if "danawa" in source_text:
        return "Danawa 24M"
    if "internet archive" in source_text and "dramexchange" in source_text:
        return "IA+DX"
    if "스냅샷" in record.yoy_source:
        return "스냅샷"
    return compact_source(record)


def compact_status(status: str) -> str:
    if status == VERIFIED:
        return VERIFIED
    if status == DELAYED_OR_CONFLICT:
        return "지연/불일치"
    if status == UNAVAILABLE:
        return UNAVAILABLE
    if status.startswith(UNAVAILABLE):
        return UNAVAILABLE
    return status[:8]


def best_label(records: list[ReportRecord], reverse: bool) -> str:
    comparable = [record for record in records if record.change_pct is not None and record.month != UNAVAILABLE]
    if not comparable:
        return UNAVAILABLE
    chosen = max(comparable, key=lambda item: item.change_pct or 0) if reverse else min(comparable, key=lambda item: item.change_pct or 0)
    return chosen.label.split(" (", 1)[0]


def group_direction(records: list[ReportRecord], group: str) -> str:
    values = [record.change_pct for record in records if record.group == group and record.change_pct is not None]
    if not values:
        return "판단 보류"
    avg = sum(values) / len(values)
    if avg > 0.5:
        return "강함"
    if avg < -0.5:
        return "약함"
    return "보합"


def image_verified_yoy(record: ReportRecord) -> bool:
    return record.yoy_status == VERIFIED and record.yoy_pct is not None


def image_trend_text(record: ReportRecord) -> str:
    if not image_verified_yoy(record):
        return "전년 직접치 부족"
    if (record.yoy_pct or 0) > 0.5:
        direction = "상승"
    elif (record.yoy_pct or 0) < -0.5:
        direction = "하락"
    else:
        direction = "보합"
    return f"전년 {direction}"


def image_yoy_verdict(record: ReportRecord) -> str:
    if not image_verified_yoy(record):
        return "판단 보류"
    _, verdict, _ = classify(record.yoy_pct)
    return verdict


def group_yoy_direction(records: list[ReportRecord], group: str) -> str:
    values = [record.yoy_pct for record in records if record.group == group and image_verified_yoy(record)]
    if not values:
        return "판단 보류"
    avg = sum(values) / len(values)
    if avg > 0.5:
        return "강함"
    if avg < -0.5:
        return "약함"
    return "보합"


def best_yoy_label(records: list[ReportRecord], reverse: bool) -> str:
    comparable = [record for record in records if image_verified_yoy(record)]
    if not comparable:
        return "전년 직접치 부족"
    chosen = max(comparable, key=lambda item: item.yoy_pct or 0) if reverse else min(comparable, key=lambda item: item.yoy_pct or 0)
    return base_label(chosen)


def build_card_data(records: list[ReportRecord], rate: ExchangeRate, conclusion: str) -> dict[str, Any]:
    def box(label: str, group: str) -> dict[str, str]:
        direction = group_yoy_direction(records, group)
        status = {"강함": "상승", "약함": "하락", "보합": "보합"}.get(direction, "보류")
        basis = "전년 대비" if status != "보류" else "전년 직접치 부족"
        return {"label": label, "status": status, "basis": basis}

    def card_change_items(verdict: str, limit: int) -> list[str]:
        candidates = [record for record in records if image_yoy_verdict(record) == verdict and image_verified_yoy(record)]
        candidates.sort(key=lambda record: abs(record.yoy_pct or 0), reverse=True)
        return [compact_label(record.label) for record in candidates[:limit]]

    def short_term_falling_items(limit: int) -> list[str]:
        candidates = [record for record in records if record.change_pct is not None and record.change_pct < 0]
        candidates.sort(key=lambda record: record.change_pct or 0)
        return [compact_label(record.label) for record in candidates[:limit]]

    summaries = summarize(records)
    priorities = [
        "DDR3 칩",
        "DDR4 칩",
        "DDR5 칩",
        "DRAM 계약가",
        "NAND 웨이퍼",
        "NAND 계약가",
        "PC-client OEM SSD 계약가",
        "SSD street price",
        "소매 메모리",
        "소매 HDD",
        "소매 SSD",
    ]
    rows = []
    for wanted in priorities:
        record = next((item for item in records if item.label.startswith(wanted)), None)
        if record is None:
            continue
        verified_yoy = image_verified_yoy(record)
        display_verdict = image_yoy_verdict(record)
        symbol = "▲ " if display_verdict == "강함" else "▼ " if display_verdict == "약함" else "— " if display_verdict == "보합" else ""
        rows.append(
            {
                "item": compact_label(record.label),
                "trend": image_trend_text(record),
                "basis": "전년" if verified_yoy else "전년 부족",
                "trend_pct": record.yoy_pct if verified_yoy else None,
                "change": pct_text(record.yoy_pct) if verified_yoy else "직접치 부족",
                "source_status": f"{compact_yoy_source(record)} · 확인" if verified_yoy else f"{compact_source(record)} · 전년부족",
                "verdict": f"{symbol}{display_verdict if display_verdict != '판단 보류' else '보류'}",
            }
        )
        if len(rows) >= 11:
            break

    rate_status = rate.status if rate.value is None else VERIFIED
    rate_text = f"환율 {UNAVAILABLE} · Yahoo · {rate_status}" if rate.value is None else f"USD/KRW {rate.value:,.2f} · Yahoo · {rate_status}"
    return {
        "title": "오늘 메모리·저장장치 추세판",
        "meta": f"기준일 {TODAY} · 조회 {QUERY_TIME} · {rate_text}",
        "boxes": [box("DRAM", "DRAM"), box("NAND/SSD", "NAND/SSD"), box("소매", "소매")],
        "rows": rows,
        "chips": {
            "YoY 상승": card_change_items("강함", 3),
            "YoY 하락": card_change_items("약함", 3),
            "단기 하락": short_term_falling_items(4),
            "보류": [compact_label(record.label) for record in records if not image_verified_yoy(record)][:4],
        },
        "conclusion": conclusion,
    }


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/malgunbd.ttf" if bold else "C:/Windows/Fonts/malgun.ttf",
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    ]
    for path in candidates:
        if path and Path(path).exists():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def text_width(draw: ImageDraw.ImageDraw, text: str, used_font: ImageFont.ImageFont) -> int:
    box = draw.textbbox((0, 0), text, font=used_font)
    return box[2] - box[0]


def wrap_text(draw: ImageDraw.ImageDraw, text: str, used_font: ImageFont.ImageFont, max_width: int) -> list[str]:
    lines: list[str] = []
    for raw_line in str(text).splitlines() or [""]:
        current = ""
        for char in raw_line:
            test = current + char
            if text_width(draw, test, used_font) <= max_width or not current:
                current = test
            else:
                lines.append(current)
                current = char
        if current:
            lines.append(current)
    return lines


def draw_wrapped(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    used_font: ImageFont.ImageFont,
    fill: str,
    max_width: int,
    line_gap: int = 6,
) -> int:
    x, y = xy
    for line in wrap_text(draw, text, used_font, max_width):
        draw.text((x, y), line, font=used_font, fill=fill)
        y += used_font.size + line_gap
    return y


def status_color(status: str) -> tuple[str, str]:
    if "상승" in status or "강함" in status or "▲" in status:
        return "#0F7B4F", "#E8F6EF"
    if "하락" in status or "약함" in status or "▼" in status:
        return "#B42318", "#FDECEC"
    if "보합" in status or "—" in status:
        return "#475467", "#F2F4F7"
    return "#8A5A00", "#FFF4D6"


def list_text(values: object, limit: int = 4) -> str:
    if isinstance(values, list):
        clean = [str(value) for value in values if str(value).strip()]
        return ", ".join(clean[:limit]) if clean else "-"
    return str(values or "-")


def draw_trend_line(draw: ImageDraw.ImageDraw, x: int, y: int, change_pct: float | None, width: int = 230, height: int = 48) -> None:
    top = y
    bottom = y + height
    left = x
    right = x + width
    mid = y + height // 2
    draw.rounded_rectangle((left, top, right, bottom), radius=10, fill="#F9FAFB", outline="#EAECF0", width=1)
    draw.line((left + 12, mid, right - 12, mid), fill="#D0D5DD", width=2)
    if change_pct is None:
        dash_y = mid
        for start in range(left + 18, right - 18, 18):
            draw.line((start, dash_y, start + 8, dash_y), fill="#8A5A00", width=3)
        return

    magnitude = min(abs(change_pct), 12.0) / 12.0
    amplitude = max(4, int((height // 2 - 7) * magnitude))
    start_x = left + 18
    end_x = right - 18
    if change_pct > 0.5:
        color = "#0F7B4F"
        start_y = mid + amplitude
        end_y = mid - amplitude
    elif change_pct < -0.5:
        color = "#B42318"
        start_y = mid - amplitude
        end_y = mid + amplitude
    else:
        color = "#475467"
        start_y = mid
        end_y = mid
    draw.line((start_x, start_y, end_x, end_y), fill=color, width=5)
    draw.ellipse((start_x - 5, start_y - 5, start_x + 5, start_y + 5), fill=color)
    draw.ellipse((end_x - 6, end_y - 6, end_x + 6, end_y + 6), fill=color)


def create_card_png(card: dict[str, Any], output_path: Path) -> None:
    width, height = 1400, 2100
    image = Image.new("RGB", (width, height), "#F6F7F9")
    draw = ImageDraw.Draw(image)

    title_font = font(58, bold=True)
    meta_font = font(25)
    box_label_font = font(30, bold=True)
    box_status_font = font(42, bold=True)
    table_font = font(26)
    table_bold_font = font(28, bold=True)
    trend_font = font(27, bold=True)
    chip_font = font(25, bold=True)
    conclusion_font = font(30, bold=True)

    margin = 70
    draw.rounded_rectangle((40, 40, width - 40, height - 40), radius=36, fill="#FFFFFF", outline="#E4E7EC", width=2)
    y = 90
    draw.text((margin, y), card.get("title") or "오늘 메모리·저장장치 추세판", font=title_font, fill="#111827")
    y += 78
    y = draw_wrapped(draw, (margin, y), card.get("meta") or f"기준일 {TODAY} · 조회 {QUERY_TIME}", meta_font, "#667085", width - margin * 2)
    y += 30

    boxes = (card.get("boxes") or [])[:3]
    while len(boxes) < 3:
        boxes.append({"label": "-", "status": "보류", "basis": UNAVAILABLE})
    box_gap = 22
    box_w = (width - margin * 2 - box_gap * 2) // 3
    box_h = 190
    for i, box in enumerate(boxes):
        x = margin + i * (box_w + box_gap)
        status = str(box.get("status") or "보류")
        color, bg = status_color(status)
        draw.rounded_rectangle((x, y, x + box_w, y + box_h), radius=24, fill=bg, outline=color, width=2)
        draw.text((x + 28, y + 24), str(box.get("label") or "-"), font=box_label_font, fill="#344054")
        draw.text((x + 28, y + 72), status, font=box_status_font, fill=color)
        draw.text((x + 28, y + 135), str(box.get("basis") or UNAVAILABLE), font=meta_font, fill="#475467")
    y += box_h + 44

    draw.text((margin, y), "전년 대비 추세선", font=table_bold_font, fill="#111827")
    draw.text((margin + 235, y + 4), "전년값 직접 조회 없으면 점선", font=meta_font, fill="#667085")
    y += 52
    header_h = 56
    draw.rounded_rectangle((margin, y, width - margin, y + header_h), radius=14, fill="#111827")
    headers = [("항목", 0), ("기준", 280), ("추세선", 410), ("전년 대비", 665), ("출처·상태", 835), ("판정", 1080)]
    for label, offset in headers:
        draw.text((margin + 24 + offset, y + 14), label, font=chip_font, fill="#FFFFFF")
    y += header_h

    rows = (card.get("rows") or [])[:11]
    row_h = 82
    for idx, row in enumerate(rows):
        bg = "#FFFFFF" if idx % 2 == 0 else "#F9FAFB"
        draw.rectangle((margin, y, width - margin, y + row_h), fill=bg)
        draw.text((margin + 24, y + 23), str(row.get("item") or "-")[:18], font=table_font, fill="#111827")
        trend = str(row.get("trend") or "보류")
        trend_pct = row.get("trend_pct")
        trend_color, _ = status_color(trend)
        draw.text((margin + 304, y + 23), str(row.get("basis") or "-")[:8], font=table_font, fill=trend_color)
        draw_trend_line(draw, margin + 430, y + 17, trend_pct if isinstance(trend_pct, (int, float)) else None)
        draw.text((margin + 689, y + 23), str(row.get("change") or "-")[:10], font=table_bold_font, fill="#111827")
        draw.text((margin + 859, y + 23), str(row.get("source_status") or "-")[:18], font=meta_font, fill="#475467")
        verdict = str(row.get("verdict") or "보류")
        color, _ = status_color(verdict)
        draw.text((margin + 1104, y + 23), verdict[:14], font=table_bold_font, fill=color)
        y += row_h
    y += 24

    chips = card.get("chips") or {}
    chip_labels = ["YoY 상승", "YoY 하락", "단기 하락", "보류"]
    chip_w = (width - margin * 2 - 24) // 2
    chip_h = 110
    for i, label in enumerate(chip_labels):
        x = margin + (i % 2) * (chip_w + 24)
        cy = y + (i // 2) * (chip_h + 20)
        color, bg = status_color(label)
        draw.rounded_rectangle((x, cy, x + chip_w, cy + chip_h), radius=18, fill=bg, outline=color, width=2)
        draw.text((x + 22, cy + 18), label, font=chip_font, fill=color)
        label_width = draw.textbbox((0, 0), label, font=chip_font)[2]
        list_x = x + 22 + label_width + 28
        draw_wrapped(draw, (list_x, cy + 18), list_text(chips.get(label), limit=4), meta_font, "#344054", x + chip_w - list_x - 22, line_gap=2)
    y += chip_h * 2 + 70

    conclusion = card.get("conclusion") or "직접 확인 가능한 핵심값 기준으로 판단한다."
    draw.rounded_rectangle((margin, y, width - margin, height - 95), radius=20, fill="#111827")
    draw_wrapped(draw, (margin + 30, y + 28), conclusion, conclusion_font, "#FFFFFF", width - margin * 2 - 60, line_gap=8)

    image.save(output_path)


def build_report(records: list[ReportRecord], rate: ExchangeRate, card_path: Path) -> str:
    if rate.value is None:
        rate_line = f"USD/KRW: 환율 {UNAVAILABLE} (조회 {rate.query_time}, 출처 {rate.source}, 상태 {rate.status})."
    else:
        rate_line = (
            f"USD/KRW: {rate.value:,.2f}원"
            f" (조회 {rate.query_time}, 출처 {rate.source}, 원문 Last Update {rate.last_update}, "
            f"상태 {rate.status}, 전일 대비 {pct_text(rate.change_pct)})."
        )

    lines = [
        "## 1. 기준 환율 1줄",
        rate_line,
        "",
        "## 2. 오늘 한눈에 추세판",
        "| 구분 | 현재값 | 전일 | 전주 | 전월 | 전년 | 추세 | 판정 |",
        "|---|---:|---|---|---|---|---|---|",
    ]
    for record in records:
        lines.append(
            f"| {record.label} | {record.current} | {record.day} | {record.week} | {record.month} | "
            f"{record.year} | {record.trend} | {record.verdict} |"
        )

    summaries = summarize(records)
    yoy_verified = [record for record in records if image_verified_yoy(record)]
    lines.extend(
        [
            "",
            "## 3. 상승·하락 요약 4줄",
            f"상승: {', '.join(summaries['상승'][:6]) if summaries['상승'] else UNAVAILABLE}",
            f"하락: {', '.join(summaries['하락'][:6]) if summaries['하락'] else UNAVAILABLE}",
            f"전년: {', '.join(f'{base_label(record)} {pct_text(record.yoy_pct)}' for record in yoy_verified[:6]) if yoy_verified else '전년 직접치 부족'}",
            f"보류: {', '.join(summaries['보류'][:8]) if summaries['보류'] else '없음'}",
            "",
            "## 4. 가격표",
            "| 항목 | 현재값 | 조회 시각·출처·상태 | 원문 Last Update | 전월 대비 | 전년 대비 | 추세 |",
            "|---|---:|---|---|---|---|---|",
        ]
    )
    for record in records:
        lines.append(
            f"| {record.label} | {record.current} | {format_source(record)} | {record.last_update} | "
            f"{record.month} | {record.year} | {record.trend} |"
        )

    conclusion = (
        f"전년 기준으로 가장 강한 쪽은 {best_yoy_label(records, True)}, "
        f"가장 약한 쪽은 {best_yoy_label(records, False)}, "
        f"DRAM 전년 추세는 {group_yoy_direction(records, 'DRAM')}, "
        f"NAND/SSD 전년 추세는 {group_yoy_direction(records, 'NAND/SSD')}."
    )
    lines.extend(
        [
            "",
            "## 5. 마지막 한 줄",
            conclusion,
            "",
            "## 6. 마지막 이미지형 요약판",
            f"![오늘 메모리·저장장치 추세판]({card_path.name})",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    rate = query_usdkrw()
    records = build_records(rate)
    apply_yoy(records)

    report_path = REPORTS_DIR / f"memory_price_report_{TODAY}.md"
    card_path = REPORTS_DIR / f"memory_price_summary_{TODAY}.png"
    conclusion = (
        f"DRAM 전년 추세는 {group_yoy_direction(records, 'DRAM')}, "
        f"NAND/SSD 전년 추세는 {group_yoy_direction(records, 'NAND/SSD')}, "
        f"소매 전년 추세는 {group_yoy_direction(records, '소매')}."
    )
    card = build_card_data(records, rate, conclusion)
    create_card_png(card, card_path)
    report_path.write_text(build_report(records, rate, card_path), encoding="utf-8")
    snapshot = save_snapshot(records, rate)

    print(f"Wrote {report_path.relative_to(ROOT)}")
    print(f"Wrote {card_path.relative_to(ROOT)}")
    print(f"Wrote {snapshot.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
