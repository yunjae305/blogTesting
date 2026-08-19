# Windows Server 보안 배포 및 암호화 이관

이 문서는 Blog-it을 현재 Windows PC에서 Windows Server로 옮길 때 로그인 정보와 브라우저
세션을 안전하게 이관하는 기준 절차다. 실제 키·토큰·계정명은 이 문서, 커밋, 콘솔 로그에
기록하지 않는다.

## 먼저 알아둘 결론

Windows Server도 DPAPI를 지원한다. 문제는 현재 기본값이 **사용자 범위 DPAPI**라는 점이다.
암호문은 암호화한 Windows 사용자와 보통 그 PC의 사용자 프로필에 묶인다. 따라서 현재 PC의
`credentials.json`, `session_account`, Chrome `Cookies`를 새 서버나 다른 서비스 계정으로
복사해도 복호화되지 않는 것이 정상이다.

Blog-it 게시 자격증명 v2는 다음 두 방식을 지원한다.

| 방식 | 보호 대상 | 서버 이동 | 사용 시점 |
| --- | --- | --- | --- |
| `dpapi-user` | 식별자(이메일·전화번호·아이디), 비밀번호, 세션 계정·Naver blog id 표식 | 다른 PC/계정으로 복사 불가 | 현재 PC 단독 사용 |
| `aes-256-gcm` | 같은 필드들, 필드별 nonce와 platform/user/field AAD | 같은 키를 안전하게 주입하면 가능 | Windows Server 이전 전 권장 |

Blog-it 자체 계정의 이메일은 `EMAIL_ENC_KEY` 기반 AES-256-GCM, 검색용 값은 별도의
`EMAIL_INDEX_KEY` 기반 blind index로 저장한다. 비밀번호는 복호화 가능한 AES로 암호화하지
않고 scrypt 단방향 해시로 저장한다. 사용자가 말하는 “비밀번호 암호화”의 안전한 구현은
비밀번호를 되찾을 수 있게 저장하는 것이 아니라, 로그인 검증만 가능한 salted hash다.

## 권장 배치

API와 브라우저 발행 작업자를 분리한다.

- API: 외부에는 IIS/Caddy/NGINX의 HTTPS만 공개하고 FastAPI는 loopback에 바인딩한다.
- 발행 작업자: Naver·Threads 전용 최소 권한 Windows 계정으로 실행한다.
- 브라우저 자동화는 보이는 Chrome과 MFA가 필요할 수 있다. Windows Service의 Session 0보다
  전용 계정으로 로그인한 대화형 작업자 또는 “사용자가 로그인한 경우에만 실행”하는 작업
  스케줄러 구성이 적합하다.
- 서비스 계정, `SYSTEM`, `Administrators` 외에는 secret/profile 디렉터리 ACL을 제거한다.
- Chrome 프로필과 자격증명은 저장소 밖(예: `%ProgramData%\BlogIt`)에 둔다.

## 1. 서버용 secret 준비

다음 값은 저장소·이미지·백업 파일과 분리된 vault에서 주입한다. Azure 환경이면 Managed
Identity + Key Vault, 온프레미스면 조직의 비밀 관리 제품이나 서비스 전용 secret injection을
사용한다.

- `POSTING_CREDENTIALS_KEY`: 정확히 32 random bytes를 canonical base64url로 인코딩한 값
- 기존 `EMAIL_ENC_KEY`, `EMAIL_INDEX_KEY`: **현재 값 그대로** 이전. 이전이 끝난 뒤
  `EMAIL_ENC_KEY`는 key id 회전(아래 '키 회전 원칙')으로 새 무작위 키로 바꿀 수 있다.
  `EMAIL_INDEX_KEY`는 회전 절차가 없으므로 계속 그대로 둔다.
- `AUTH_TOKEN_SECRET`: 서버에서 새 32바이트 이상 무작위 값으로 회전 권장
- Mongo URI, AI provider 키 등 (Threads 발행은 브라우저 세션만 쓴다 — API app
  secret/access token은 2026-08-10에 제거돼 더는 없다)

새로 만드는 키는 문구가 아니라 32 random bytes의 canonical base64url 값이어야 한다 —
`EMAIL_ENC_KEY_<n>`(회전 키)은 코드가 이 형식만 받아들인다. 기존 `EMAIL_ENC_KEY`가
문구라면 v3 암호화 시 PBKDF2-HMAC-SHA256(600k회)로 늘여 쓰지만, 이는 완충일 뿐이므로
이전 후 무작위 키로 회전하는 것을 권장한다. 키를 잃으면 AES 암호문은 복구할 수 없으므로
vault의 접근 정책·백업·복구 절차를 먼저 검증한다.

## 2. 현재 PC에서 DPAPI를 portable AES로 이관

서버로 복사한 뒤에는 기존 DPAPI를 풀 수 없으므로 반드시 **현재 암호화한 Windows 사용자로**
먼저 실행한다. `POSTING_CREDENTIALS_KEY`를 현재 프로세스에 vault로부터 주입한 뒤 저장소
루트에서 다음 순서로 확인한다.

```powershell
python apps/api/scripts/migrate_posting_credentials.py
python apps/api/scripts/migrate_posting_credentials.py --apply --require-aes
python apps/api/scripts/migrate_posting_credentials.py
```

첫 번째와 세 번째 명령은 계정명·경로·암호문을 출력하지 않고 형식별 개수만 보여 준다.
적용 명령은 모든 `credentials.json`, `session_account`, Naver `blog_id`가 `v2-aes`로
바뀌지 않으면 실패한다.
실패가 하나라도 있으면 서버 복사를 중단하고 해당 계정을 현재 PC에서 다시 저장한다.

프로필 경로의 **부모 드라이브/디렉터리는 바꿔도 되지만 마지막 이름(leaf)과 상대 트리
이름은 바꾸면 안 된다.** 자격증명 AAD와 사용자별 프로필 탐색이 이 이름을 사용한다.

```text
<data>/.naver-profile/                         # 관리 CLI용 base leaf
<data>/.naver-profile-users/<24자리-user-hash>/ # 사용자별 프로필
<data>/.threads-profile/
<data>/.threads-profile-users/<24자리-user-hash>/
```

커스텀 base를 썼다면 `.naver-profile` 같은 기본 이름 대신 그 **정확한 base leaf**와
`<base-leaf>-users/<기존-hash>`를 그대로 보존한다. `<24자리-user-hash>` 디렉터리의 이름을
다시 계산하거나 계정명으로 바꾸거나, 파일을 한 디렉터리로 평탄화하지 않는다.

다음도 값 자체를 출력하지 않는 방식으로 확인한다.

- raw 파일에 실제 이메일·전화번호·아이디·비밀번호 문자열이 없음
- Naver와 Threads 각각 저장 후 재읽기 성공
- 잘못된 키, 변조된 ciphertext, 사용자/플랫폼/필드 교환은 모두 복호 실패
- migration 재실행 시 `migrated=0`, `failures=0`

## 3. 서버에 암호문과 키를 따로 배치

portable AES로 확인된 게시 자격증명 파일만 서버 데이터 경로로 복사한다. 키는 파일과 함께
복사하지 않고 vault에서 실행 프로세스에 주입한다. 예시 ACL은 실제 서비스 계정명으로 바꾼다.

서버 환경변수는 복사한 트리와 같은 base leaf를 가리키도록 **고정**한다. 부모 경로는 달라도
된다. 이후 환경변수의 마지막 이름을 바꾸면 기존 `<base>-users` 트리를 찾지 못한다.

```powershell
$env:NAVER_BROWSER_PROFILE_DIR="C:\ProgramData\BlogIt\.naver-profile"
$env:THREADS_BROWSER_PROFILE_DIR="C:\ProgramData\BlogIt\.threads-profile"
```

복사 대상은 각 보존된 프로필 디렉터리 안의 관리 파일(`credentials.json`,
`session_account`, Naver의 `blog_id`)이다. Chrome `Cookies`, `Local State`, 캐시 등은 복사하지
않고 4절에 따라 서버에서 새로 만든다. 복사 후 원본을 지우기 전에 서버의 같은 환경변수와
vault 키로 migration dry-run의 발견 개수와 전 파일 복호화 성공을 확인한다.

```powershell
icacls "C:\ProgramData\BlogIt" /inheritance:r
icacls "C:\ProgramData\BlogIt" /grant:r "DOMAIN\BlogItPublisher:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F"
```

Instagram 설정 스크립트가 기존 `.env`를 갱신할 때는 같은 디렉터리 임시 파일을
만든 직후 기존 `.env`의 DACL을 먼저 복제하고, 그 뒤 값을 써서 flush/fsync한 다음 Windows
`ReplaceFileW`로 교체한다. 이 API는 기존 파일의 DACL을 보존하며, 임시 파일 DACL 복제나
최종 보존에 실패하면 기존 파일을 둔 채 실패한다. Windows에서 `.env`가 없으면 스크립트는 넓은
상속 ACL로 secret 파일을 만들 위험을 피하려고 생성을 거부한다. 먼저 빈 `.env`를 만들고
위와 같이 서비스 계정·SYSTEM·Administrators만 허용하는 DACL을 적용한 뒤 스크립트를 실행한다.

서버의 서비스 계정에서 migration dry-run과 자격증명 재읽기를 다시 수행한다. 다른 계정에서는
읽히지 않게 하는 것은 파일 ACL의 역할이고, 키가 없는 프로세스에서 읽히지 않게 하는 것은
AES/vault의 역할이다.

## 4. Chrome 세션은 서버에서 새로 만든다

Chrome 쿠키 DB는 별도로 DPAPI 보호되며 Chrome의 App-Bound Encryption은 쿠키를 머신에 더
강하게 묶을 수 있다. 현재 PC의 Chrome 프로필을 신뢰해 복사하지 않는다.

1. 서버의 전용 발행 계정으로 로그인한다.
2. 새 Chrome 프로필을 만든다.
3. Naver와 Threads에 각각 로그인하고 MFA를 완료한다.
4. 시험용 비공개 글로 발행·이미지 가져오기·로그아웃 후 재로그인을 확인한다.
5. RDP 연결을 끊고 재부팅한 뒤에도 예약 작업자가 같은 Windows 계정으로 동작하는지 확인한다.

## 5. Blog-it 계정과 웹 보안

기존 평문 이메일 이관은 API와 가입 쓰기를 완전히 중단하고 백업을 검증한 유지보수 창에서만
실행한다. `--apply`만으로는 거부되며 다음 순서를 지킨다.

```powershell
python apps/api/scripts/migrate_email_encryption.py
python apps/api/scripts/migrate_email_encryption.py --apply --maintenance-confirm
```

스크립트는 스냅샷의 원문 email/userId를 CAS로 확인해 암호문을 기록하고, 모든 대상의 정확한
`emailHash`/`emailEnc`와 복호 결과를 다시 검증한 뒤 같은 값이 유지된 행만 평문을 제거한다.
불일치가 하나라도 있으면 검증 단계에서 평문 삭제 없이 중단한다.

- `EMAIL_INDEX_KEY`를 바꾸거나 잃으면 기존 계정 조회·유니크가 깨진다 — 그대로 이전한다.
  `EMAIL_ENC_KEY`는 key id 회전을 지원한다(2026-08-09): 값을 덮어쓰지 말고
  `EMAIL_ENC_KEY_<n>`을 추가한 뒤 아래 '키 회전 원칙'의 절차로 재암호화한다.
- `AUTH_TOKEN_SECRET`을 새로 만들면 기존 로그인 세션은 무효화된다. 서버 이전 때 의도적으로
  전원 재로그인시키는 편이 안전하다.
- `APP_ENV=production`, `ALLOW_IN_MEMORY_STORAGE=false`를 지정한다.
- `CORS_ALLOWED_ORIGINS`는 실제 프런트 origin만 허용하고 `*`를 쓰지 않는다.
- HTTPS, HSTS, 허용 Host, 방화벽 규칙을 reverse proxy에서 강제한다.
- `BLOGIT_CHROME_NO_SANDBOX=false`를 유지한다.
- 로그에 `Authorization`, `Cookie`, 비밀번호, 이메일, OAuth query/fragment, access token을
  남기지 않는다.
- 애플리케이션의 로그인 실패 제한, 가입 제한, 비밀번호 해시 작업 큐는 한 프로세스 안의
  급격한 폭주를 막는 1차 방어다. 여러 Uvicorn worker/서버 사이에는 공유되지 않고 재시작하면
  초기화되므로, 운영에서는 reverse proxy의 IP별 요청 제한과 Redis 같은 공유 제한기를 함께
  둔다. proxy가 전달한 주소를 쓸 때는 임의 클라이언트의 위조 헤더를 신뢰하지 말고 loopback의
  reverse proxy만 trusted proxy로 지정해 `request.client.host`가 실제 클라이언트를 가리키게 한다.

## 6. 운영 전 합격 기준

- 새 서비스 계정 + vault 주입 상태에서 `v2-aes` 자격증명 재읽기 성공
- 이전 DPAPI blob을 서버에서 읽으려 하면 실패
- DB 사용자 문서에 평문 `email` 필드 0건, `emailEnc`/`emailHash` 일치
- 기존 scrypt 비밀번호가 성공 로그인 뒤 새 정책으로 자동 승격되고 오답은 업데이트하지 않음
- 로그인 속도 제한, CORS, HTTPS, `Cache-Control: no-store` 확인
- 게시 이미지 URL은 만료·변조 시 거부되고, Threads 임시 이미지가 성공/실패 모두 삭제됨
- `npm audit`과 전체 API/web 테스트·프로덕션 빌드 통과
- 백업 복원과 vault 장애 시 fail-closed 동작을 별도 staging에서 확인

## 키 회전 원칙

암호화 키 회전은 “새 키 설정 후 옛 키 즉시 삭제”가 아니다. 새 key id를 활성화하고 옛 키로
읽은 값을 새 키로 원자 재암호화한 뒤, 전체 개수와 재복호 검증이 끝났을 때만 옛 키 접근을
제거한다.

**이메일 암호화 키(`EMAIL_ENC_KEY`)는 이 절차를 지원한다(2026-08-09, key ring).**

```powershell
# 1) vault/.env에 새 키를 추가한다(기존 EMAIL_ENC_KEY는 지우지 않는다).
#    값: 정확히 32 random bytes의 canonical base64url
#    생성: python -c "import base64,secrets;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip('='))"
#    EMAIL_ENC_KEY_2=<생성한 값>
# 2) 서버를 재기동한다. 이제 새 암호화는 v3:2:로 저장되고 옛 암호문도 계속 읽힌다.
python apps/api/scripts/rotate_email_key.py            # dry-run: 대상 수 확인
python apps/api/scripts/rotate_email_key.py --apply    # 행별 CAS 재암호화
python apps/api/scripts/rotate_email_key.py            # 재암호화 대상 0건 확인
# 3) 잔여 0을 확인한 뒤에만 옛 EMAIL_ENC_KEY 값을 설정에서 비운다.
```

스크립트는 쓰기 전에 전체 문서의 복호화와 `emailHash` 일치(현재 `EMAIL_INDEX_KEY` 검증)를
확인하고, 하나라도 어긋나면 아무것도 바꾸지 않고 중단한다. `EMAIL_INDEX_KEY`는 결정적
블라인드 인덱스라 이 회전의 대상이 아니다 — 바꾸려면 전체 emailHash 재계산 마이그레이션이
따로 필요하다.

`POSTING_CREDENTIALS_KEY` 형식은 여전히 active key 하나를 사용하므로 key ring을 추가하기
전에는 무계획 회전을 하지 않는다.
