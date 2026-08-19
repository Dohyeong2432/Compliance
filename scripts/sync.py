"""API 서버 프로세스와 분리된 독립 재색인 스크립트.

api/main.py의 lifespan()은 기본적으로 서버가 뜰 때마다, 그리고
SYNC_INTERVAL_SECONDS마다 크롤링+임베딩을 반복한다. 운영 규모가 커지면
(법령이 한두 개가 아니면) 이게 매 재시작마다 오래 걸리는 작업이 되어
서버 기동 자체를 느리게 만든다.

이 스크립트는 그 작업을 서버 프로세스 밖으로 완전히 빼서, cron이나
작업 스케줄러가 원하는 시각에 독립적으로 실행하도록 만든다. 서버는
SYNC_ON_STARTUP=false로 띄우면 이 스크립트가 채워둔 영속 데이터를
그대로 읽어 즉시 서빙을 시작한다.

전제 조건 (SYNC_ON_STARTUP=false와 함께 쓸 때):
  - VECTOR_STORE_BACKEND=chroma, GRAPH_STORE_BACKEND=kuzu -- 인메모리
    백엔드는 이 스크립트가 만든 데이터를 서버 프로세스가 전혀 못 본다
    (서로 다른 프로세스라 메모리를 공유하지 않으므로).
  - LEXICAL_INDEX_PATH가 설정되어 있어야 BM25 채널도 함께 영속화된다
    (안 그러면 서버가 시작할 때 어휘 검색만 매번 비어 있게 된다).
  - 이 스크립트와 서버가 같은 .env(같은 CHROMA_PERSIST_DIR/KUZU_DB_PATH/
    LEXICAL_INDEX_PATH)를 봐야 한다.

사용법:
    python scripts/sync.py

종료 코드는 전체 소스 중 하나라도 실패하면 1, 전부 성공하면 0이다 --
cron/스케줄러가 실패를 감지해 알림을 보내도록 하는 용도.
"""

from __future__ import annotations

import sys
from pathlib import Path

# 리포 루트를 sys.path에 넣는다 -- 이 파일은 scripts/ 하위에 있어서
# `python scripts/sync.py`로 실행하면 파이썬이 scripts/만 sys.path[0]에
# 넣고 리포 루트(bootstrap.py가 있는 곳)는 넣어주지 않는다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bootstrap import build_components  # noqa: E402 -- sys.path 조정 이후에만 import 가능


def main() -> int:
    components = build_components()

    print("재색인 시작 (크롤링 포함 시 시간이 걸릴 수 있습니다)...")
    report = components.syncer.sync_once()

    ok = True
    for r in report.results:
        status = "OK" if r.ok else "FAIL"
        print(f"  [{status}] {r.name}: ingested={r.ingested} removed={r.removed} errors={len(r.errors)}")
        for err in r.errors:
            print(f"      - {err}")
        ok = ok and r.ok

    print("전체 결과:", "성공" if ok else "일부 실패")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
