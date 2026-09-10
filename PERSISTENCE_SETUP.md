# V78.8.1 개인계정·자산 영구저장 설정

## 왜 계정이 "없음"으로 나왔나

이 앱의 V78.8.0까지는 `accounts.json`과 `portfolio.json`을 서버 파일에 저장했습니다. `render.yaml`은 Web Service를 `plan: free`로 두면서 `/var/data` 디스크를 선언했지만, Render Free Web Service는 Persistent Disk를 사용할 수 없습니다. 따라서 실제 서비스가 무료 인스턴스라면 재배포/재시작 뒤 서버 파일이 초기화될 수 있고, 그 결과 개인 ID가 "등록되지 않은 계정"으로 보일 수 있습니다.

또 V78.8.0의 `_read_accounts()`에는 `accounts.json`이 없거나 손상됐을 때 잘못해서 `portfolio.backup.json`을 계정 백업으로 읽는 오류가 있었습니다. V78.8.1에서는 `accounts.backup.json`을 별도로 사용하도록 수정했습니다.

## 권장: DATABASE_URL 연결

V78.8.1은 `DATABASE_URL`이 설정되어 있으면 개인계정과 전체 포트폴리오 저장소를 PostgreSQL의 `stock_app_state` 테이블에 우선 저장합니다. 서버 코드를 다시 배포해도 DB가 유지되는 한 계정과 자산은 유지됩니다.

1. 지속적으로 사용할 PostgreSQL 데이터베이스를 준비합니다.
2. Render Web Service의 Environment에서 `DATABASE_URL`에 PostgreSQL 연결 문자열을 등록합니다.
3. 앱을 다시 배포합니다.
4. 앱의 **내 종목 센터 → 서버 저장소 상태**가 `PostgreSQL 영구저장 연결됨`으로 나오는지 확인합니다.
5. 기존 계정이 서버에서 사라졌지만 특정 PC/휴대폰에 보유종목이 남아 있다면, 그 기기에서 기존 ID와 비밀번호를 입력한 뒤 **이 기기 자료로 계정 복구**를 누릅니다.

주의: Render의 무료 PostgreSQL은 장기 영구저장 용도로 적합하지 않을 수 있으므로 사용하는 DB 서비스의 보존 정책을 확인하세요. 유료 Render Persistent Disk를 사용하는 경우에는 `DATABASE_URL` 없이 `/var/data` 디스크 방식도 사용할 수 있습니다.

## V78.8.1의 복구 안전장치

- 서버 계정은 사라졌고 서버 자산도 비어 있으며, 현재 기기에 로컬 자산이 남아 있을 때만 로컬 계정 복구를 허용합니다.
- 서버 자산은 남아 있는데 인증 계정만 사라진 경우에는 다른 사람이 ID만 알고 계정을 탈취하지 못하도록 자동 재등록을 차단합니다.
- `내 자산 백업`으로 JSON 백업을 내려받을 수 있습니다. 백업에는 계정 비밀번호가 포함되지 않습니다.
- `자산 백업 복원`은 기존 자료와 병합하며, 계정 연결 상태라면 서버에도 다시 동기화합니다.
