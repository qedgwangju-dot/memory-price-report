# 메모리 가격 보고서 자동화

이 저장소는 `메모리 가격 보고서`를 매일 오전 6:30 KST에 GitHub Actions로 생성합니다.

OpenAI API를 사용하지 않습니다. 스크립트가 직접 접근 가능한 웹/API만 조회하고, 접근이 막히거나 기준값이 부족한 항목은 숫자를 채우지 않고 `직접 확인 실패` 또는 `직접치 부족`으로 표시합니다.

## 작동 방식

- 실행 시간: 매일 오전 6:30 KST
- GitHub Actions cron: `30 21 * * *` UTC
- 보고서 파일: `reports/memory_price_report_YYYY-MM-DD.md`
- 이미지 요약판: `reports/memory_price_summary_YYYY-MM-DD.png`
- 수동 실행: GitHub Actions 화면에서 `Memory price report` 워크플로우의 `Run workflow`

## 주요 직접 조회 소스

- USD/KRW: Yahoo Finance `USDKRW=X`
- DRAM/NAND/SSD 공개 가격: DRAMeXchange 홈페이지와 공개 `HomePrice` JSON
- 소매 가격: Danawa `prod.danawa.com` 가격비교 상품 페이지

## 기준

- 모델 기억, 과거 보고서, 검색 스니펫, 추정값, 보간값은 사용하지 않습니다.
- 같은 출처에서 직접 확인되지 않은 비교값은 계산하지 않습니다.
- OpenAI API 키나 GitHub Secret은 필요 없습니다.
