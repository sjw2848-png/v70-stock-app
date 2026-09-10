# V78.8.2 PostgreSQL 자동영구저장 설정

보유종목을 매번 파일로 내려받는 방식이 아니라 개인계정·보유·관심·모으기 데이터를 PostgreSQL에 자동 저장합니다. 앱 코드가 재배포되어도 DB는 별도 자원이라 유지됩니다.

## 1) 포함된 Render Blueprint 사용
`render.yaml`은 Web Service와 `v78-stock-db` PostgreSQL을 함께 선언하고 `DATABASE_URL`을 `fromDatabase.connectionString`으로 자동 연결합니다. Blueprint로 배포하면 DB URL을 매번 복사할 필요가 없습니다.

## 2) 기존 로또 PostgreSQL 재사용
이미 로또용 PostgreSQL을 운영 중이면 Stock Web Service의 `DATABASE_URL`에 그 DB의 Internal URL을 한 번 연결할 수 있습니다. 주식 앱은 `stock_app_state`와 `stock_app_storage_probe` 테이블만 사용해 로또 데이터와 분리합니다.

같은 Render Region/Workspace라면 Internal URL 사용이 권장됩니다. DB URL은 GitHub 코드에 넣지 말고 Render Environment 변수로만 관리하세요.

## 기존 휴대폰 자료
기존 localStorage 키 `v78-portfolio-account-v1:<개인ID>`는 유지됩니다. 새 DB가 비어 있으면 계정 복구 기능으로 로컬 자료를 DB에 올릴 수 있습니다.

정상 상태는 `PostgreSQL 영구저장 연결됨`, `DB 실제 쓰기→읽기 검증 완료`, `서버 N개 / 이 기기 N개`가 모두 일치하는 것입니다.
