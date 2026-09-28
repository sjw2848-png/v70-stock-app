# V78.13.0 PostgreSQL 자동영구저장 설정

보유종목을 매번 파일로 내려받는 방식이 아니라 개인계정·보유·관심·모으기 데이터를 PostgreSQL에 자동 저장합니다. 앱 코드가 재배포되어도 DB는 별도 자원이라 유지됩니다.

## 1) 포함된 Render Blueprint 사용
`render.yaml`은 Web Service와 `v78-stock-db` PostgreSQL을 함께 선언하고 `DATABASE_URL`을 `fromDatabase.connectionString`으로 자동 연결합니다. Blueprint로 배포하면 DB URL을 매번 복사할 필요가 없습니다.

## 2) 기존 로또 PostgreSQL 재사용
이미 로또용 PostgreSQL을 운영 중이면 Stock Web Service의 `DATABASE_URL`에 그 DB의 Internal URL을 한 번 연결할 수 있습니다. 주식 앱은 `stock_app_state`와 `stock_app_storage_probe` 테이블만 사용해 로또 데이터와 분리합니다.

같은 Render Region/Workspace라면 Internal URL 사용이 권장됩니다. DB URL은 GitHub 코드에 넣지 말고 Render Environment 변수로만 관리하세요.

## 기존 휴대폰 자료
기존 localStorage 키 `v78-portfolio-account-v1:<개인ID>`는 유지됩니다. 새 DB가 비어 있으면 계정 복구 기능으로 로컬 자료를 DB에 올릴 수 있습니다.

정상 상태는 `PostgreSQL 영구저장 연결됨`, `DB 실제 쓰기→읽기 검증 완료`, `서버 N개 / 이 기기 N개`가 모두 일치하는 것입니다.

## V78.13.0 변경
- DB에 일시적으로 접속할 수 없으면 계정/보유종목 API는 `503 STORAGE_UNAVAILABLE`을 반환합니다. 이때 기기 자료는 그대로 보존되며, DB가 복구되면 자동으로 정상 동작합니다. (예전처럼 빈 임시파일을 기준으로 덮어쓰지 않습니다.)
- `render.yaml`에서 `DATA_DIR=/var/data`를 제거했습니다. 무료 플랜에는 영구 디스크가 없고 PostgreSQL이 원본 저장소입니다.
- 임시 휴장일이 생기면 Render 환경변수 `EXTRA_KR_HOLIDAYS=2026-xx-xx` 형식으로 추가할 수 있습니다.
