"""로컬 테스트용 SSO 토큰 발급기.

시크릿을 이 파일에 직접 하드코딩하지 않는다 -- 그러면 깃에 커밋될 때 그대로
노출되고(이 저장소는 공유되므로), .env 쪽 값을 바꿀 때마다 이 파일과 값이
어긋나 서명 불일치로 401이 나는 문제도 반복된다. 대신 .env의
SSO_JWT_ALGORITHM/SSO_JWT_SECRET을 그대로 읽어서 서명한다 -- bootstrap.py와
동일한 방식(load_dotenv()).

HS256만 지원한다 -- RS256은 개인키가 필요해서 로컬 발급기로는 안 맞는다
(RS256 환경에서 테스트하려면 실제 IdP에서 발급받은 토큰을 써야 한다).

사용법:
    python make_token.py [dept] [sub]
    (인자 생략 시 dept=compliance, sub=tester)
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import jwt
from dotenv import load_dotenv

load_dotenv()

algorithm = os.environ.get("SSO_JWT_ALGORITHM")
secret = os.environ.get("SSO_JWT_SECRET")

if algorithm != "HS256" or not secret:
    raise SystemExit(
        ".env에 SSO_JWT_ALGORITHM=HS256과 SSO_JWT_SECRET이 설정돼 있어야 합니다"
        "(RS256은 이 로컬 발급기로 만들 수 없습니다 -- 실제 IdP에서 받은 토큰을 쓰세요)."
    )

dept = sys.argv[1] if len(sys.argv) > 1 else "compliance"
sub = sys.argv[2] if len(sys.argv) > 2 else "tester"

now = datetime.now(timezone.utc)
token = jwt.encode(
    {"sub": sub, "dept": dept, "iat": now, "exp": now + timedelta(hours=1)},
    secret,
    algorithm="HS256",
)
print(token)
