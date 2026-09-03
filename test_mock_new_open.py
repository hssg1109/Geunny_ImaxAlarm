"""
모의 신규오픈 시나리오 테스트.

실제로 CGV가 새 회차를 여는 타이밍은 통제할 수 없으므로, 지금 실제로 상영정보가
잡혀있는 회차를 "방금 막 신규오픈된 세션"인 것처럼 취급해서 전체 파이프라인을
그대로 흘려본다:

  스케줄 조회 → is_new 판정(이 프로세스는 방금 떴으니 처음 보는 세션 = 신규) →
  watch_new_sessions 필터 통과 → 명당 좌석 조회 → 우선순위(G,H,F... / 22,23 중앙) 랭킹 →
  자동예매 게이트 → (예열된 브라우저) 좌석선점 → DRY_RUN이면 즉시취소, 아니면 실제 진행 →
  윈도우 토스트 알림

주의: 이 스크립트는 실제 CGV 좌석을 몇 초간 홀드했다가 DRY_RUN이면 취소합니다.
.env의 DRY_RUN이 true인지 꼭 확인 후 실행하세요. 이미 돌고 있는 alarm.py 프로세스와는
별도 프로세스라 서로 config.json/interval 상태를 건드리지 않습니다 (아래 cfg는 이 스크립트
안에서만 쓰는 테스트 전용 값이고, 실제 config.json은 전혀 읽거나 쓰지 않습니다).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import alarm as A
from curl_cffi.requests import AsyncSession
import booking as B

# ── 테스트 대상: 지금 실제로 상영정보가 있는 날짜/영화로 바꿔서 쓰세요 ──────────────
DATE   = "20260903"
MOV_NO = "30001323"   # 오디세이


async def main():
    print("=" * 70)
    print("  모의 신규오픈 시나리오")
    print(f"  DRY_RUN = {B.DRY_RUN}  (False면 실제 결제까지 진행되니 꼭 확인!)")
    print("=" * 70)

    if not B.DRY_RUN:
        ans = input("\n  DRY_RUN이 False입니다. 실제로 결제까지 진행될 수 있습니다. 계속할까요? (y/N): ").strip().lower()
        if ans != "y":
            print("  중단합니다.")
            return

    token = A.load_token()

    # 이 스크립트만의 테스트 전용 cfg — config.json은 안 건드림
    cfg = dict(A.DEFAULT_CONFIG)
    cfg.update({
        "mov_no": MOV_NO,
        "mov_name": "오디세이",
        "site_no": "0013",
        "co_cd": "A420",
        "rtctl_scop_cd": "08",
        "watch_dates": [DATE],
        "watch_times": {DATE: []},                       # 특정 시간 지정 없음
        "watch_new_sessions": {DATE: {"mode": "all"}},    # 이 날짜는 "신규 오픈만" 감시한다고 가정
        # 명당 구역을 넓게 잡아서 실제로 빈 좌석을 확실히 찾도록 함(파이프라인 끝까지 검증 목적).
        # 그래도 new_session_row_priority(G,H,F...)/center_seats(22,23)는 좁게 유지해서
        # "넓은 풀 안에서 우선순위에 맞는 걸 고르는지"까지 같이 검증한다.
        "prime_rows": list("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
        "prime_seat_min": 1,
        "prime_seat_max": 99,
        "new_session_row_priority": ["G", "H", "F", "I", "J", "K", "L", "M"],
        "new_session_center_seats": [22, 23],
        "auto_book": True,
        "auto_book_armed": True,
    })

    async with AsyncSession(impersonate=A._IMPERSONATE) as client:
        print(f"\n[1] {DATE} 스케줄 조회 중 (이 프로세스는 방금 시작해서 _seen_sessions가 "
              f"비어있으므로, 지금 보이는 세션은 전부 '신규'로 판정됩니다)")
        sessions, token = await A.check_date(client, token, DATE, cfg)

        if not sessions:
            print(f"  {DATE}에 조건에 맞는 IMAX 세션이 없습니다. 스크립트 상단의 DATE/MOV_NO를 확인하세요.")
            return

        for s in sessions:
            print(f"    {s['date']} {s['time']}  scns_no={s['scns_no']}  is_new={s['is_new']}")

        print(f"\n[2] process_sessions() 실행 — 실제 alarm.py 라이브 루프와 동일한 함수 호출")
        print("    (명당 조회 → 신규세션이면 우선순위 랭킹 → 자동예매 게이트 → 예열 브라우저로 선점 시도)\n")
        await A.process_sessions(sessions, client, token, cfg)

    print("\n완료.")


asyncio.run(main())
