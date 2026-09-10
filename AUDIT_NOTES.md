# V78.8.2 DB 자동영구저장 감사노트

- V78.8.1에서 PostgreSQL 지원 코드는 있었지만 실제 Render Web Service에 DATABASE_URL이 없으면 파일 저장으로 떨어지는 구조였음.
- V78.8.2 render.yaml에 관리형 PostgreSQL + fromDatabase 자동주입을 추가해 Blueprint 배포 시 DB가 자동 연결되도록 보강.
- DATABASE_URL / STOCK_DATABASE_URL / LOTTO_DATABASE_URL 별칭 지원.
- `stock_app_state`에 계정/포트폴리오 저장, `stock_app_storage_probe`로 실제 쓰기→읽기 영구저장 검증.
- `/api/storage/verify` 및 UI `DB 영구저장 검증` 추가.
- 기존 계정별 localStorage 영구 키 유지.
- DB 미연결 상태는 절대 '영구저장 정상'으로 표시하지 않음.
