from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from PIL import Image, ImageStat
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT / "reports"
AUDIT_DIR = REPORTS_DIR / "audits"
SNAPSHOT_DIR = REPORTS_DIR / "snapshots"
PCPARTPICKER_DIR = REPORTS_DIR / "pcpartpicker"

KST = ZoneInfo("Asia/Seoul")
DEFAULT_DATE = (datetime.now(KST).date() + timedelta(days=1)).isoformat()
VERIFIED = "확인"
Y_DIRECTION_THRESHOLD_PCT = 0.5
RECENT_DIRECTION_THRESHOLD_PCT = 0.5
STRONG_RECENT_THRESHOLD_PCT = 20.0
IMAGE_REQUIRED_ROW_PREFIXES = (
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
)
IMAGE_REQUIRED_TREND_PREFIXES = (
    "DDR3 칩",
    "DDR4 칩",
    "DDR5 칩",
    "DRAM 계약가",
    "NAND 웨이퍼",
    "NAND 계약가",
    "소매 메모리",
    "소매 HDD",
    "소매 SSD",
)
MIN_IMAGE_ROWS = len(IMAGE_REQUIRED_ROW_PREFIXES)
MIN_IMAGE_TREND_ROWS = len(IMAGE_REQUIRED_TREND_PREFIXES)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def relpath(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def as_number(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def positive_points(record: dict[str, Any]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for point in record.get("trend_points") or []:
        value = as_number(point.get("value"))
        if value is not None and value > 0:
            points.append(point)
    return points


def image_label(record: dict[str, Any]) -> str:
    label = str(record.get("label") or "")
    last_update = str(record.get("last_update") or "")
    replacements = {
        "DDR3 칩": "DDR3",
        "DDR4 칩": "DDR4",
        "DDR5 칩": "DDR5",
        "DRAM 계약가": "DRAM 계약",
        "NAND 계약가": "NAND",
        "PC-client OEM SSD 계약가": "OEM SSD",
        "SSD street price": "SSD street",
    }
    if label == "NAND 웨이퍼":
        match = re.search(r"항목\s+((?:SLC|MLC|TLC))\s+(\d+Gb)", last_update)
        if match:
            return f"NAND {match.group(1)}{match.group(2)}"
    return replacements.get(label, label)


def recent_change_pct(record: dict[str, Any]) -> float | None:
    points = positive_points(record)
    if len(points) < 2:
        return None
    previous = as_number(points[-2].get("value"))
    latest = as_number(points[-1].get("value"))
    if previous is None or latest is None or previous <= 0:
        return None
    return ((latest / previous) - 1) * 100


def actionable(record: dict[str, Any]) -> bool:
    return record.get("yoy_status") == VERIFIED and as_number(record.get("yoy_pct")) is not None and len(positive_points(record)) >= 3


def expected_verdict(record: dict[str, Any]) -> str:
    if not actionable(record):
        return "보류"
    yoy_pct = as_number(record.get("yoy_pct")) or 0
    recent_pct = recent_change_pct(record)
    if recent_pct is None:
        return "보류"
    if yoy_pct > Y_DIRECTION_THRESHOLD_PCT:
        if recent_pct > STRONG_RECENT_THRESHOLD_PCT:
            return "강함"
        if recent_pct > RECENT_DIRECTION_THRESHOLD_PCT:
            return "상승"
        return "둔화"
    if yoy_pct < -Y_DIRECTION_THRESHOLD_PCT:
        if recent_pct > RECENT_DIRECTION_THRESHOLD_PCT:
            return "회복"
        return "약함"
    if recent_pct > RECENT_DIRECTION_THRESHOLD_PCT:
        return "상승"
    if recent_pct < -RECENT_DIRECTION_THRESHOLD_PCT:
        return "약함"
    return "보합"


def record_for_prefix(records: list[dict[str, Any]], prefix: str) -> dict[str, Any] | None:
    return next((record for record in records if str(record.get("label") or "").startswith(prefix)), None)


def trend_coverage(records: list[dict[str, Any]], audit_rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    required_rows: list[dict[str, Any]] = []
    required_trends: list[dict[str, Any]] = []
    issues: list[str] = []

    for prefix in IMAGE_REQUIRED_ROW_PREFIXES:
        record = record_for_prefix(records, prefix)
        label = image_label(record) if record else prefix
        audit_row = audit_rows.get(label)
        visible = bool(audit_row and audit_row.get("visible_in_image_table"))
        required_rows.append({"prefix": prefix, "label": label, "visible": visible})
        if not visible:
            issues.append(f"required image row missing: {label}")

    for prefix in IMAGE_REQUIRED_TREND_PREFIXES:
        record = record_for_prefix(records, prefix)
        label = image_label(record) if record else prefix
        audit_row = audit_rows.get(label)
        record_points = len(positive_points(record)) if record else 0
        audit_points = as_number(audit_row.get("trend_points")) if audit_row else None
        audit_point_count = int(audit_points) if audit_points is not None else 0
        visible = bool(audit_row and audit_row.get("visible_in_image_table"))
        has_series = visible and record_points >= 3 and audit_point_count >= 3
        required_trends.append(
            {
                "prefix": prefix,
                "label": label,
                "record_points": record_points,
                "audit_points": audit_point_count,
                "status": "pass" if has_series else "fail",
            }
        )
        if not has_series:
            issues.append(f"required trend line missing or too short: {label} record={record_points}, audit={audit_point_count}")

    visible_rows = sum(1 for item in required_rows if item["visible"])
    trend_line_rows = sum(1 for item in required_trends if item["status"] == "pass")
    if visible_rows < MIN_IMAGE_ROWS:
        issues.append(f"image row coverage too low: {visible_rows}/{MIN_IMAGE_ROWS}")
    if trend_line_rows < MIN_IMAGE_TREND_ROWS:
        issues.append(f"image trend-line coverage too low: {trend_line_rows}/{MIN_IMAGE_TREND_ROWS}")

    return {
        "status": "pass" if not issues else "fail",
        "visible_rows": visible_rows,
        "minimum_visible_rows": MIN_IMAGE_ROWS,
        "trend_line_rows": trend_line_rows,
        "minimum_trend_line_rows": MIN_IMAGE_TREND_ROWS,
        "required_rows": required_rows,
        "required_trends": required_trends,
        "issues": issues,
    }


def group_status(records: list[dict[str, Any]], group: str) -> dict[str, str]:
    counts = {
        "강함": 0,
        "상승": 0,
        "회복": 0,
        "둔화": 0,
        "보합": 0,
        "약함": 0,
        "보류": 0,
    }
    group_records = [record for record in records if record.get("group") == group]
    for record in group_records:
        verdict = expected_verdict(record)
        counts[verdict if verdict in counts else "보류"] += 1

    strong_count = counts["강함"] + counts["상승"] + counts["회복"]
    soft_count = counts["둔화"] + counts["보합"] + counts["약함"]
    pending_count = counts["보류"]
    if not group_records or (strong_count == 0 and soft_count == 0):
        status = "보류"
    elif soft_count > 0:
        status = "혼재" if strong_count > 0 else "둔화"
    elif pending_count > 0:
        status = "일부 상승"
    else:
        status = "상승"

    labels = [
        ("강함", "강"),
        ("상승", "상"),
        ("회복", "회복"),
        ("둔화", "둔"),
        ("보합", "보합"),
        ("약함", "약"),
        ("보류", "보류"),
    ]
    basis = " ".join(f"{short}{counts[key]}" for key, short in labels if counts[key])
    return {"status": status, "basis": basis or "직접치 부족"}


def image_nonblank(path: Path) -> dict[str, Any]:
    with Image.open(path) as image:
        stat = ImageStat.Stat(image.convert("RGB"))
        extrema = image.convert("RGB").getextrema()
        channel_ranges = [high - low for low, high in extrema]
        return {
            "width": image.width,
            "height": image.height,
            "mean": stat.mean,
            "channel_ranges": channel_ranges,
            "nonblank": image.width >= 1000 and image.height >= 1000 and max(channel_ranges) > 10,
        }


def verify(date_text: str) -> tuple[Path, Path, str]:
    AUDIT_DIR.mkdir(exist_ok=True)
    report_path = REPORTS_DIR / f"memory_price_report_{date_text}.md"
    image_path = REPORTS_DIR / f"memory_price_summary_{date_text}.png"
    snapshot_path = SNAPSHOT_DIR / f"memory_price_snapshot_{date_text}.json"
    audit_path = AUDIT_DIR / f"memory_price_audit_{date_text}.json"
    pcpartpicker_path = PCPARTPICKER_DIR / f"pcpartpicker_trends_{date_text}.json"
    crosscheck_json = AUDIT_DIR / f"memory_price_crosscheck_{date_text}.json"
    crosscheck_md = AUDIT_DIR / f"memory_price_crosscheck_{date_text}.md"

    issues: list[str] = []
    required_files = [report_path, image_path, snapshot_path, audit_path, pcpartpicker_path]
    for path in required_files:
        if not path.exists():
            issues.append(f"missing file: {relpath(path)}")

    snapshot = read_json(snapshot_path) if snapshot_path.exists() else {"records": []}
    audit = read_json(audit_path) if audit_path.exists() else {}
    pcpartpicker = read_json(pcpartpicker_path) if pcpartpicker_path.exists() else {}
    report_text = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
    records = list(snapshot.get("records") or [])

    if audit.get("status") != "pass":
        issues.append(f"audit status is not pass: {audit.get('status')}")
    if audit.get("issues"):
        issues.append(f"audit has issues: {audit.get('issues')}")

    thresholds = audit.get("thresholds") or {}
    expected_thresholds = {
        "yoy_direction_pct": Y_DIRECTION_THRESHOLD_PCT,
        "recent_direction_pct": RECENT_DIRECTION_THRESHOLD_PCT,
        "strong_recent_pct": STRONG_RECENT_THRESHOLD_PCT,
        "minimum_image_rows": MIN_IMAGE_ROWS,
        "minimum_image_trend_rows": MIN_IMAGE_TREND_ROWS,
    }
    for key, expected in expected_thresholds.items():
        actual = as_number(thresholds.get(key))
        if actual != expected:
            issues.append(f"threshold mismatch: {key} expected {expected}, got {actual}")

    audit_rows = {
        str(row.get("label")): row
        for row in (((audit.get("checks") or {}).get("row_verdicts")) or [])
    }
    coverage_check = trend_coverage(records, audit_rows)
    issues.extend(str(issue) for issue in coverage_check.get("issues") or [])
    audit_coverage = ((audit.get("checks") or {}).get("image_trend_coverage")) or {}
    if audit_coverage:
        if audit_coverage.get("status") != coverage_check["status"]:
            issues.append(
                f"audit coverage status mismatch: expected {coverage_check['status']}, got {audit_coverage.get('status')}"
            )
        if as_number(audit_coverage.get("visible_rows")) != coverage_check["visible_rows"]:
            issues.append("audit visible-row coverage mismatch")
        if as_number(audit_coverage.get("trend_line_rows")) != coverage_check["trend_line_rows"]:
            issues.append("audit trend-line coverage mismatch")
    else:
        issues.append("audit image trend coverage check missing")

    row_checks: list[dict[str, Any]] = []
    for record in records:
        label = image_label(record)
        expected = expected_verdict(record)
        audit_row = audit_rows.get(label)
        audit_expected = str(audit_row.get("expected_image_verdict")) if audit_row else ""
        audit_actual = str(audit_row.get("actual_image_verdict")) if audit_row else ""
        row_status = "pass"
        if audit_row is None:
            row_status = "fail"
            issues.append(f"missing audit row: {label}")
        elif audit_expected != expected:
            row_status = "fail"
            issues.append(f"audit row expected mismatch: {label} expected {expected}, got {audit_expected}")
        elif audit_actual not in {expected, "not in image table"}:
            row_status = "fail"
            issues.append(f"audit row image mismatch: {label} expected {expected}, got {audit_actual}")
        if audit_actual == "강함" and (recent_change_pct(record) or 0) <= STRONG_RECENT_THRESHOLD_PCT:
            row_status = "fail"
            issues.append(f"strong verdict below strict recent threshold: {label}")
        row_checks.append(
            {
                "label": label,
                "group": record.get("group"),
                "yoy_pct": record.get("yoy_pct"),
                "recent_direct_change_pct": recent_change_pct(record),
                "trend_points": len(positive_points(record)),
                "expected_verdict": expected,
                "audit_expected": audit_expected,
                "audit_actual": audit_actual,
                "status": row_status,
            }
        )

    audit_groups = {
        str(row.get("group")): row
        for row in (((audit.get("checks") or {}).get("group_boxes")) or [])
    }
    group_checks: list[dict[str, Any]] = []
    for group in ("DRAM", "NAND/SSD", "소매"):
        expected = group_status(records, group)
        audit_group = audit_groups.get(group)
        actual_status = str(audit_group.get("actual_status")) if audit_group else ""
        actual_basis = str(audit_group.get("actual_basis")) if audit_group else ""
        status = "pass"
        if audit_group is None:
            status = "fail"
            issues.append(f"missing audit group: {group}")
        elif actual_status != expected["status"] or actual_basis != expected["basis"]:
            status = "fail"
            issues.append(
                f"group mismatch: {group} expected {expected['status']}({expected['basis']}), "
                f"got {actual_status}({actual_basis})"
            )
        if f"{group} 전년+최근 판정" not in report_text and group != "소매":
            issues.append(f"report conclusion missing group wording: {group}")
        group_checks.append(
            {
                "group": group,
                "expected_status": expected["status"],
                "expected_basis": expected["basis"],
                "audit_status": actual_status,
                "audit_basis": actual_basis,
                "status": status,
            }
        )

    image_check: dict[str, Any] = {"path": relpath(image_path), "status": "fail"}
    if image_path.exists():
        image_check.update(image_nonblank(image_path))
        image_check["status"] = "pass" if image_check.get("nonblank") else "fail"
        if image_check["status"] != "pass":
            issues.append("summary image is blank or too small")

    markdown_image_ref = f"![오늘 메모리·저장장치 추세판]({image_path.name})"
    if markdown_image_ref not in report_text:
        issues.append("report markdown image reference is missing or mismatched")

    pcpp_policy = str(pcpartpicker.get("numeric_value_policy") or "")
    pcpartpicker_check = {
        "status": "pass",
        "fetch_status": pcpartpicker.get("status"),
        "image_count": pcpartpicker.get("image_count"),
        "selected_count": pcpartpicker.get("selected_count"),
        "numeric_value_policy": pcpp_policy,
    }
    if "not used as latest values" not in pcpp_policy or "verdict inputs" not in pcpp_policy:
        pcpartpicker_check["status"] = "fail"
        issues.append("PCPartPicker non-use policy missing")

    status = "pass" if not issues else "fail"
    payload = {
        "schema_version": 1,
        "date": date_text,
        "status": status,
        "issues": issues,
        "thresholds": expected_thresholds,
        "files": {
            "report": relpath(report_path),
            "image": relpath(image_path),
            "snapshot": relpath(snapshot_path),
            "audit": relpath(audit_path),
            "pcpartpicker": relpath(pcpartpicker_path),
        },
        "checks": {
            "image_trend_coverage": coverage_check,
            "rows": row_checks,
            "groups": group_checks,
            "image": image_check,
            "pcpartpicker": pcpartpicker_check,
        },
    }
    crosscheck_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        f"# 메모리 가격 보고서 교차 검증 - {date_text}",
        "",
        f"- 상태: {'통과' if status == 'pass' else '실패'}",
        f"- 이슈 수: {len(issues)}",
        f"- 강함 기준 최근 직접 구간: +{STRONG_RECENT_THRESHOLD_PCT:.1f}% 초과",
        "",
        "## 이미지 추세선 커버리지",
        f"- 필수 표 행: {coverage_check['visible_rows']} / {coverage_check['minimum_visible_rows']}",
        f"- 필수 추세선 행: {coverage_check['trend_line_rows']} / {coverage_check['minimum_trend_line_rows']}",
        f"- 상태: {coverage_check['status']}",
        "",
        "## 그룹 재계산",
        "| 그룹 | 재계산 판정 | 재계산 구성 | audit 판정 | audit 구성 | 상태 |",
        "|---|---|---|---|---|---|",
    ]
    for check in group_checks:
        lines.append(
            f"| {check['group']} | {check['expected_status']} | {check['expected_basis']} | "
            f"{check['audit_status']} | {check['audit_basis']} | {check['status']} |"
        )
    lines.extend(
        [
            "",
            "## 행별 재계산",
            "| 항목 | 포인트 | YoY | 최근 직접 구간 | 재계산 | audit 기대 | audit 이미지 | 상태 |",
            "|---|---:|---:|---:|---|---|---|---|",
        ]
    )
    for check in row_checks:
        yoy = "-" if check["yoy_pct"] is None else f"{check['yoy_pct']:.2f}%"
        recent = "-" if check["recent_direct_change_pct"] is None else f"{check['recent_direct_change_pct']:.2f}%"
        lines.append(
            f"| {check['label']} | {check['trend_points']} | {yoy} | {recent} | "
            f"{check['expected_verdict']} | {check['audit_expected']} | {check['audit_actual']} | {check['status']} |"
        )
    lines.extend(["", "## 이슈"])
    lines.extend([f"- {issue}" for issue in issues] if issues else ["- 없음"])
    crosscheck_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return crosscheck_json, crosscheck_md, status


def main() -> int:
    parser = argparse.ArgumentParser(description="Cross-check generated memory price report artifacts.")
    parser.add_argument("--date", default=DEFAULT_DATE, help="Report date in YYYY-MM-DD format")
    args = parser.parse_args()
    json_path, md_path, status = verify(args.date)
    print(f"Wrote {relpath(json_path)}")
    print(f"Wrote {relpath(md_path)}")
    if status != "pass":
        print(f"Cross-check failed. See {relpath(md_path)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
