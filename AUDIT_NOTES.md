# V78.8.1 개인계정 연동·영구저장 감사노트

## 확인한 핵심 원인

1. V78.8.0 `render.yaml`은 `plan: free`이면서 `/var/data` Persistent Disk를 선언했습니다. Render Free Web Service에서는 Persistent Disk를 사용할 수 없어, 파일 기반 `accounts.json` / `portfolio.json`은 재배포 또는 인스턴스 재시작 때 사라질 수 있습니다.
2. `_read_accounts()`의 백업 경로가 잘못되어 `accounts.json` 복구 시 `portfolio.backup.json`을 읽고 있었습니다. 계정 레지스트리와 포트폴리오 백업이 섞이는 실제 코드 오류입니다.
3. 서버 계정이 사라진 경우 UI는 단순히 "등록되지 않은 계정"만 보여주고, 브라우저에 남은 로컬 보유종목으로 안전하게 복구할 경로가 없었습니다.
4. `/var/data`라는 경로명만 보고 영구저장이라고 표시하면 실제 Persistent Disk가 없는 환경을 오판할 수 있었습니다.
5. 계정 ID는 일반 설정 JSON에만 의존했습니다. 버전 이동 시 계정 ID를 더 안정적으로 유지할 별도 고정 키가 필요했습니다.

## V78.8.1 변경

- PostgreSQL 영구저장 백엔드 추가. `DATABASE_URL` 존재 시 `accounts`와 `portfolios` JSON 상태를 DB에 우선 저장.
- 기존 파일 데이터가 남아 있고 DB가 비어 있으면 최초 읽기 때 DB로 자동 이관.
- PostgreSQL 쓰기 실패를 파일 성공으로 숨기지 않고 오류로 처리하여 "영구저장 성공" 오표시 방지.
- `accounts.backup.json` 분리. 계정 백업이 포트폴리오 백업을 읽던 오류 수정.
- 파일 저장도 임시파일 + fsync + atomic replace + 직전본 백업 유지.
- 서버 `/health`, `/api/account/status`, `/api/portfolio`, `/api/storage/status`에 저장소 상태 추가.
- DB / 실제 mount 감지 / 영구저장 미확인 상태를 UI에 명확히 표시.
- 계정 미존재 시 `ACCOUNT_NOT_FOUND` 구조화 오류코드와 서버 orphan 자산 수 진단 추가.
- 현재 기기에 로컬 자산이 남아 있고 서버 계정/자산이 모두 사라진 경우에만 `이 기기 자료로 계정 복구` 허용.
- 서버 자산은 남아 있는데 인증레코드만 없을 때는 자동 재등록을 차단하여 ID 탈취 위험 방지.
- `내 자산 백업` / `자산 백업 복원` JSON 기능 추가. 비밀번호는 백업하지 않음.
- 계정 ID를 `v78-account-id-v1` 고정 로컬 키에도 저장해 앱 버전 설정 이동과 분리.
- 계정 비밀번호 변경 입력 시 기존 포트폴리오 배열을 0개로 비우던 UI 동작 제거.
- 계정 세션 쿠키의 Secure 판정에 `X-Forwarded-Proto: https` 반영.
- PWA 버전/캐시 V78.8.1로 갱신.
- Free Render blueprint에서 지원되지 않는 disk 선언을 제거하고 `DATABASE_URL` 환경변수 슬롯 추가.

## 검증

- `python -m py_compile app.py engine.py state_store.py` 통과
- `node --check static/app.js` 통과
- `python -m json.tool static/manifest.json` 통과
- HTML duplicate id 없음
- 계정 복구/백업/저장소 진단 DOM ID 존재 확인
- `StateStore` 파일 저장 → 교체 → primary 손상 → `accounts.backup.json` 복구 테스트 통과
- 계정 백업과 포트폴리오 백업이 서로 섞이지 않는 테스트 통과
- ZIP 무결성 검사를 최종 패키지에 수행

## 배포 시 필수 확인

Render Free Web Service를 계속 사용할 경우, 계정/자산의 서버 영구보존을 원하면 `DATABASE_URL`을 별도 PostgreSQL에 연결해야 합니다. 유료 Render Web Service에 실제 Persistent Disk를 `/var/data`로 붙이는 방법도 지원합니다.
