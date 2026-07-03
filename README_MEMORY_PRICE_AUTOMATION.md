# 메모리 가격 보고서 자동화

이 폴더는 `메모리 가격 보고서`를 Codex가 꺼져 있어도 실행되도록 GitHub Actions용으로 구성한 패키지입니다.

## 작동 방식

- 실행 시간: 매일 오전 6:30 KST
- GitHub Actions cron: `30 21 * * *` UTC
- 보고서 파일: `reports/memory_price_report_YYYY-MM-DD.md`
- 이미지 요약판: `reports/memory_price_summary_YYYY-MM-DD.png`
- 수동 실행: GitHub Actions 화면에서 `Memory price report` 워크플로우의 `Run workflow`

## GitHub에서 필요한 설정

1. 이 폴더 내용을 GitHub 저장소에 올립니다.
2. 저장소 `Settings > Secrets and variables > Actions > New repository secret`로 이동합니다.
3. 이름은 `OPENAI_API_KEY`, 값은 OpenAI API 키를 넣습니다.
4. 저장소 `Actions` 탭에서 워크플로우가 활성화되어 있는지 확인합니다.

## 현재 상태

일반 PowerShell PATH에서는 `git` 명령이 잡히지 않지만, Codex 번들 git으로 로컬 저장소 초기화와 커밋은 가능합니다. GitHub 원격 저장소만 연결하면 push할 수 있습니다.

기존 GitHub 저장소 이름을 `owner/repo` 형식으로 알려주면, GitHub 커넥터로 이 파일들을 저장소에 바로 올릴 수 있습니다.
