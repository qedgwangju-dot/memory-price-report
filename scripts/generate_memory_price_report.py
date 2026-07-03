from __future__ import annotations

import json
import os
import re
import textwrap
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

KST = ZoneInfo("Asia/Seoul")
TODAY = datetime.now(KST).strftime("%Y-%m-%d")
QUERY_TIME = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")


REPORT_PROMPT = f"""
오늘자 메모리·저장장치 가격 보고서를 한국어로 작성하라. 실행 기준일은 {TODAY}, 조회 기준 시각은 {QUERY_TIME}이다.
형식은 반드시 '짧은 텍스트 보고 + 마지막 고품질 이미지형 요약판'이다. 장황한 설명, 자료 처리 현황표, 외화 검산표, 관련 기업 지도, 공정 병목표, 긴 기업분석은 넣지 않는다.

최신값 절대 규칙: 최신성이 필요한 값은 모델 기억, 과거 대화, 이전 보고서, 검색 스니펫, 캐시, 추정, 평균 추정, 보간값, 유사 상품 가격으로 답하지 말라. 반드시 현재 접근 가능한 공식·신뢰 소스(웹/API/파일/DB 등)를 직접 열어 조회한 뒤 반영하라. 각 최신값에는 조회 시각(KST), 출처, 원문 Last Update가 있으면 Last Update를 함께 표시하라. 조회 실패, 지연 데이터, 출처 간 불일치, 접근 제한이 있으면 값을 추정하거나 재사용하지 말고 '확인 불가', '직접 확인 실패', '직접치 부족', '지연/불일치 있음' 중 하나로 표시하라. 직접 확인하지 못한 숫자는 절대 쓰지 말라.

기준 환율: 최신 USD/KRW를 직접 조회하고 조회 시각(KST)과 출처를 표시한다. 달러 가격은 원화 환산을 병기한다. 환율 확인 실패 시 원화 환산하지 말고 '환율 직접치 부족'으로 표시한다.

필수 확인 항목:
- 상류 DRAM: DDR3 칩, DDR4 칩, DDR5 칩, DDR4 모듈, DDR5 모듈, DRAM 계약가를 분리한다. DDR3 칩은 TrendForce/DRAMeXchange 공개 표에서 확인 가능한 대표 DDR3 칩 현물가를 사용한다. DDR4 칩은 DDR4 1Gx8 3200MT/s 또는 공개 표 대표 DDR4 칩 현물가를 사용한다. DDR5 칩은 DDR5 16G(2Gx8) 4800/5600을 사용한다. DDR4 모듈은 DDR4 UDIMM 16GB 3200, DDR5 모듈은 DDR5 UDIMM 16GB 4800/5600을 사용한다. DRAM 계약가는 PC DRAM contract price 공개값을 사용한다. 칩, 모듈, 계약가를 합치지 않는다.
- 상류 NAND/SSD: 512Gb TLC 웨이퍼 현물가, NAND 계약가 공개값, PC-client OEM SSD 1TB 계약가, Samsung 990 PRO 1TB street price를 각각 확인한다. NAND 웨이퍼, NAND 계약가, OEM SSD 계약가, SSD street price를 합치지 않는다.
- 소매: 삼성전자 DDR5-5600 32GB, Seagate BarraCuda 8TB, Samsung 990 PRO 1TB를 확인한다. Danawa는 prod.danawa.com 가격비교 페이지만 사용하고 shop.danawa.com, 회원가, 카드혜택가, 네이버포인트 차감가, 현금가는 섞지 않는다. canonical URL은 각각 https://prod.danawa.com/info/?pcode=20644043, https://prod.danawa.com/info/?pcode=5764992, https://prod.danawa.com/info/?pcode=18297002 이다.

비교값 산출 규칙:
- 현재값, 전일 기준값, 전주 기준값, 전월 기준값, 전년 기준값을 각각 확인한다.
- 같은 항목·같은 단위·같은 출처 기준으로만 비교한다.
- 값은 가능하면 '기준값 → 현재값, 변화율'로 쓴다.
- 전년 기준값은 최신 데이터 유효일의 1년 전 같은 날짜를 우선 사용하고, 비거래일·미공개일이면 가장 가까운 직전 공개일을 쓰되 기준일을 명시한다.
- 출처가 변화율만 제공하고 기준값을 공개하지 않으면 변화율만 쓰고 '기준값 미공개'라고 표시한다.
- 동일 기준값을 직접 확인하지 못하면 임의 계산하지 말고 '직접치 부족'으로 표시한다.
- 추세는 전월 대비를 최우선으로 판단하고, 전년 대비는 구조적 사이클 방향으로 따로 표시한다. 전월값이 없고 전년값만 있으면 '전년 기준 장기 상승/장기 하락/장기 보합'으로 쓴다. 전월·전년이 없고 전주값이 있으면 전주 기준, 전월·전년·전주가 없고 전일만 있으면 전일 단기 기준으로만 쓴다.

출력 구조는 반드시 아래 6개 블록만 사용한다.
1) 기준 환율 1줄.
2) 오늘 한눈에 추세판: 표 열은 '구분 / 현재값 / 전일 / 전주 / 전월 / 전년 / 추세 / 판정'으로 고정한다. 필수 행은 DDR3 칩, DDR4 칩, DDR5 칩, DDR4 모듈, DDR5 모듈, DRAM 계약가, NAND 웨이퍼, NAND 계약가, PC-client OEM SSD 계약가, SSD street price, 소매 메모리, 소매 HDD, 소매 SSD다. 판정은 '강함', '약함', '보합', '판단 보류' 네 단어 중 하나만 쓴다.
3) 상승·하락 요약 4줄: '상승:', '하락:', '전년:', '보류:' 네 줄만 쓴다.
4) 가격표: 열은 '항목 / 현재값 / 조회 시각·출처 / 원문 Last Update / 전월 대비 / 전년 대비 / 추세'로 고정한다.
5) 마지막 한 줄: '전월 기준으로 가장 강한 쪽은 ○○, 전년 기준으로 가장 구조적으로 강한 쪽은 ○○, 가장 약한 쪽은 ○○, DRAM은 ○○, NAND는 ○○.' 형식으로 한 줄만 쓴다.
6) 마지막 이미지형 요약판: 보고서 본문에는 이미지형 카드의 핵심 텍스트만 넣어라. 스크립트가 별도 PNG 요약판을 생성해 보고서 마지막에 붙인다.

마지막 이미지형 요약판 디자인 규칙:
- 이전처럼 작은 글씨가 빽빽한 표를 만들지 않는다. 한 장 카드처럼 '큰 제목 → 핵심 판정 3개 → 미니 추세표 → 상승/하락/전년/보류 칩 → 한 줄 결론' 순서로 만든다.
- 제목은 '오늘 메모리·저장장치 추세판'으로 한다.
- 카드 상단에는 기준일, 조회시각, 환율을 작게 넣는다.
- 카드 최상단 3개 핵심 박스에는 ① DRAM ② NAND/SSD ③ 소매를 넣고 각 박스에는 '상승/하락/보합/보류'와 적용 기준(전월·전년·전주·전일)만 크게 표시한다.
- 중앙 미니 추세표는 최대 8행만 넣는다. 우선순위는 DDR4 칩, DDR5 칩, DRAM 계약가, NAND 웨이퍼, NAND 계약가, OEM SSD 계약가, SSD street price, 소매 메모리/SSD다. DDR3·소매 HDD 등은 공간이 부족하면 하단 보조칩에 넣는다.
- 색상·기호는 상승=▲, 하락=▼, 보합=—, 전년 대비=YoY, 판단 보류=보류로 통일한다.
- 숫자는 길게 쓰지 말고 항목명, 적용 기준, 변화율, 판정만 넣는다. 예: 'DDR4 칩 · 전월 +○% · ▲ 강함'.
- 하단에는 '상승 / 하락 / YoY / 보류' 4개 칩 그룹을 넣는다.
- 마지막 줄에는 결론 한 문장만 넣는다.
- 가독성을 최우선으로 하며, 한 카드 안에 12개 이상의 항목을 억지로 넣지 않는다. 상세 숫자는 본문 표에 있고, 이미지는 핵심만 보여주는 용도다.
""".strip()


CARD_EXTRACTION_PROMPT = """
아래 보고서에서 PNG 카드 생성에 필요한 정보만 추출하라. 새 숫자, 새 판단, 새 출처를 만들지 말고 보고서 안에 있는 내용만 사용하라.
반드시 JSON 객체 하나만 출력하라. Markdown 코드펜스는 쓰지 마라.

스키마:
{
  "title": "오늘 메모리·저장장치 추세판",
  "meta": "기준일 ... · 조회 ... · 환율 ...",
  "boxes": [
    {"label": "DRAM", "status": "상승|하락|보합|보류", "basis": "전월|전년|전주|전일|직접치 부족"},
    {"label": "NAND/SSD", "status": "상승|하락|보합|보류", "basis": "전월|전년|전주|전일|직접치 부족"},
    {"label": "소매", "status": "상승|하락|보합|보류", "basis": "전월|전년|전주|전일|직접치 부족"}
  ],
  "rows": [
    {"item": "DDR4 칩", "basis": "전월", "change": "+0.0% 또는 직접치 부족", "verdict": "▲ 강함|▼ 약함|— 보합|보류"}
  ],
  "chips": {
    "상승": ["항목"],
    "하락": ["항목"],
    "YoY": ["항목"],
    "보류": ["항목"]
  },
  "conclusion": "한 문장 결론"
}

보고서:
""".strip()


def call_openai(input_text: str, use_web_search: bool) -> str:
    client = OpenAI()
    tools = [{"type": "web_search"}] if use_web_search else None
    response = client.responses.create(
        model=os.getenv("OPENAI_MODEL", "gpt-5.5"),
        input=input_text,
        tools=tools,
        max_output_tokens=12000,
    )
    text = getattr(response, "output_text", None)
    if text:
        return text.strip()
    chunks: list[str] = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            value = getattr(content, "text", None)
            if value:
                chunks.append(value)
    return "\n".join(chunks).strip()


def extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("JSON object was not found in model output.")
    return json.loads(match.group(0))


def fallback_card_data(report_text: str) -> dict:
    conclusion = ""
    for line in reversed(report_text.splitlines()):
        if line.strip():
            conclusion = line.strip()
            break
    return {
        "title": "오늘 메모리·저장장치 추세판",
        "meta": f"기준일 {TODAY} · 조회 {QUERY_TIME}",
        "boxes": [
            {"label": "DRAM", "status": "보류", "basis": "직접치 부족"},
            {"label": "NAND/SSD", "status": "보류", "basis": "직접치 부족"},
            {"label": "소매", "status": "보류", "basis": "직접치 부족"},
        ],
        "rows": [
            {"item": "DDR4 칩", "basis": "전월", "change": "직접치 부족", "verdict": "보류"},
            {"item": "DDR5 칩", "basis": "전월", "change": "직접치 부족", "verdict": "보류"},
            {"item": "DRAM 계약가", "basis": "전월", "change": "직접치 부족", "verdict": "보류"},
            {"item": "NAND 웨이퍼", "basis": "전월", "change": "직접치 부족", "verdict": "보류"},
        ],
        "chips": {"상승": [], "하락": [], "YoY": [], "보류": ["직접치 부족"]},
        "conclusion": conclusion or "직접 확인 가능한 핵심값이 부족해 판단 보류.",
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
        values = [str(v) for v in values if str(v).strip()]
        return ", ".join(values[:limit]) if values else "-"
    return str(values or "-")


def create_card_png(card: dict, output_path: Path) -> None:
    width, height = 1400, 1700
    image = Image.new("RGB", (width, height), "#F6F7F9")
    draw = ImageDraw.Draw(image)

    title_font = font(58, bold=True)
    meta_font = font(25)
    box_label_font = font(30, bold=True)
    box_status_font = font(42, bold=True)
    table_font = font(27)
    table_bold_font = font(28, bold=True)
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
        boxes.append({"label": "-", "status": "보류", "basis": "직접치 부족"})
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
        basis = str(box.get("basis") or "직접치 부족")
        draw.text((x + 28, y + 135), basis, font=meta_font, fill="#475467")
    y += box_h + 44

    draw.text((margin, y), "미니 추세표", font=table_bold_font, fill="#111827")
    y += 48
    header_h = 56
    draw.rounded_rectangle((margin, y, width - margin, y + header_h), radius=14, fill="#111827")
    headers = [("항목", 0), ("기준", 430), ("변화율", 650), ("판정", 920)]
    for label, offset in headers:
        draw.text((margin + 24 + offset, y + 14), label, font=chip_font, fill="#FFFFFF")
    y += header_h

    rows = (card.get("rows") or [])[:8]
    row_h = 72
    for idx, row in enumerate(rows):
        bg = "#FFFFFF" if idx % 2 == 0 else "#F9FAFB"
        draw.rectangle((margin, y, width - margin, y + row_h), fill=bg)
        draw.text((margin + 24, y + 18), str(row.get("item") or "-")[:24], font=table_font, fill="#111827")
        draw.text((margin + 454, y + 18), str(row.get("basis") or "-")[:10], font=table_font, fill="#475467")
        draw.text((margin + 674, y + 18), str(row.get("change") or "-")[:18], font=table_font, fill="#111827")
        verdict = str(row.get("verdict") or "보류")
        color, _ = status_color(verdict)
        draw.text((margin + 944, y + 18), verdict[:16], font=table_bold_font, fill=color)
        y += row_h
    y += 34

    chips = card.get("chips") or {}
    chip_labels = ["상승", "하락", "YoY", "보류"]
    chip_w = (width - margin * 2 - 24) // 2
    chip_h = 110
    for i, label in enumerate(chip_labels):
        x = margin + (i % 2) * (chip_w + 24)
        cy = y + (i // 2) * (chip_h + 20)
        color, bg = status_color(label)
        if label == "YoY":
            color, bg = "#175CD3", "#EFF8FF"
        draw.rounded_rectangle((x, cy, x + chip_w, cy + chip_h), radius=18, fill=bg, outline=color, width=2)
        draw.text((x + 22, cy + 18), label, font=chip_font, fill=color)
        value = list_text(chips.get(label), limit=4)
        draw_wrapped(draw, (x + 120, cy + 18), value, meta_font, "#344054", chip_w - 145, line_gap=2)
    y += chip_h * 2 + 70

    conclusion = card.get("conclusion") or "직접 확인 가능한 핵심값 기준으로 판단한다."
    draw.rounded_rectangle((margin, y, width - margin, height - 95), radius=20, fill="#111827")
    draw_wrapped(draw, (margin + 30, y + 28), conclusion, conclusion_font, "#FFFFFF", width - margin * 2 - 60, line_gap=8)

    image.save(output_path)


def main() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY secret is required.")

    report_text = call_openai(REPORT_PROMPT, use_web_search=True)

    try:
        card_json_text = call_openai(CARD_EXTRACTION_PROMPT + "\n\n" + report_text, use_web_search=False)
        card = extract_json(card_json_text)
    except Exception:
        card = fallback_card_data(report_text)

    report_path = REPORTS_DIR / f"memory_price_report_{TODAY}.md"
    card_path = REPORTS_DIR / f"memory_price_summary_{TODAY}.png"
    create_card_png(card, card_path)

    image_markdown = f"![오늘 메모리·저장장치 추세판]({card_path.name})"
    if image_markdown not in report_text:
        report_text = report_text.rstrip() + "\n\n" + image_markdown + "\n"
    report_path.write_text(report_text, encoding="utf-8")

    print(f"Wrote {report_path.relative_to(ROOT)}")
    print(f"Wrote {card_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
