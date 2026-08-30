"""
seatTempPrmp / seatTempPrmpCncl 왕복 테스트 스크립트

[흐름]
1. searchIfSeatData로 실제 예매 가능한 좌석 하나 찾기
2. seatTempPrmp로 그 좌석 임시 선점
3. 응답에서 movAtktNo, 선점 만료시각 확인
4. 몇 초 대기 후 seatTempPrmpCncl로 즉시 해제
5. searchIfSeatData 재조회로 실제 해제됐는지 확인

[주의]
- cgv.co.kr/api/v1/... 경로는 api.cgv.co.kr과 달리 HMAC 서명(x-signature) 불필요.
  Authorization: Bearer {token} + Cookie 세션만 사용.
- 실제 예매 가능한 좌석/회차로 테스트하므로, 사람 적은 회차 골라서 테스트 권장.
- 선점 후 반드시 취소 호출까지 실행되는지 확인할 것 (다른 고객 피해 방지).
"""

import asyncio
import json
import os
import sys
from datetime import datetime

from curl_cffi.requests import AsyncSession
from dotenv import load_dotenv

from alarm import fetch_movie_list, parse_imax_movies, make_headers, DEFAULT_CONFIG

load_dotenv()

ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "")
CGV_COOKIES  = os.getenv("CGV_COOKIES", "")
CUST_NO      = os.getenv("CUST_NO", "")

_IMPERSONATE = "chrome146"

API_BASE      = "https://api.cgv.co.kr"
SITE_BASE     = "https://cgv.co.kr"
SEAT_PATH     = "/cnm/atkt/searchIfSeatData"
PRMP_PATH     = "/api/v1/content/seatTemp/seatTempPrmp"
PRMP_CNCL_PATH = "/api/v1/content/seatTemp/seatTempPrmpCncl"
PRC_LIST_PATH  = "/api/v1/booking/searchMovAtktSeatPrcList"

CO_CD          = "A420"
SITE_NO        = "0013"
RTCTL_SCOP_CD  = "08"


def site_headers() -> dict:
    """cgv.co.kr/api/v1/... 용 헤더 (서명 없음, Bearer+Cookie만)"""
    h = {
        "accept": "application/json",
        "accept-language": "ko-KR",
        "authorization": f"Bearer {ACCESS_TOKEN}",
        "content-type": "application/json",
        "origin": "https://cgv.co.kr",
        "referer": "https://cgv.co.kr/cnm/selectVisitorCnt",
        "priority": "u=1, i",
        "sec-ch-ua": '"Not=A?Brand";v="99", "Microsoft Edge";v="151", "Chromium";v="151"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0"
        ),
    }
    if CGV_COOKIES:
        h["cookie"] = CGV_COOKIES
    return h


async def find_open_seat(client: AsyncSession, date: str, scns_no: str, scn_sseq: str):
    """실제 예매 가능한 좌석 하나 찾기 (searchIfSeatData)"""
    params = {
        "coCd": CO_CD, "siteNo": SITE_NO, "scnYmd": date,
        "scnsNo": scns_no, "scnSseq": scn_sseq,
        "seatAreaNo": "001", "cusgdCd": "01",
    }
    if CUST_NO:
        params["custNo"] = CUST_NO

    resp = await client.get(
        API_BASE + SEAT_PATH, params=params,
        headers=make_headers(ACCESS_TOKEN, SEAT_PATH), timeout=15,
    )
    if resp.status_code != 200:
        print(f"[좌석조회] HTTP {resp.status_code}: {resp.text[:300]}")
        return None
    body = resp.json()
    if body.get("statusCode") != 0:
        print(f"[좌석조회] API 오류: {body.get('statusMessage')}")
        return None
    items = (body.get("data") or {}).get("items") or []
    if not items:
        print("[좌석조회] 좌석 데이터 없음")
        return None
    seats = items[0].get("seats", [])
    for s in seats:
        if s.get("seatSaleYn") == "Y":
            print("[좌석조회] 빈 좌석 원본 필드:")
            print(json.dumps(s, ensure_ascii=False, indent=2))
            return s
    print("[좌석조회] 빈 좌석 없음")
    return None


async def check_price(client: AsyncSession, date: str, scns_no: str, scn_sseq: str, mov_no: str, seat: dict):
    """searchMovAtktSeatPrcList: 실제 플로우상 선점 직전에 호출되는 좌석 가격조회 (세션 준비 단계로 추정)"""
    body = {
        "coCd": CO_CD,
        "siteNo": SITE_NO,
        "scnsNo": scns_no,
        "scnYmd": date,
        "scnSseq": scn_sseq,
        "movNo": mov_no,
        "rtctlScopCd": RTCTL_SCOP_CD,
        "prcrulDivCd": "01",
        "sachlTypCd": "01",
        "prodBnduList": [{"prodBnduCd": "01", "prodBnduQty": 1}],
        "seatList": [
            {
                "seatLocNo": seat.get("seatLocNo", ""),
                "szoneKindCd": seat.get("szoneKindCd", "01"),
                "stkndCd": seat.get("stkndCd", "01"),
                "seatSalfrmCd": seat.get("seatSalfrmCd", "01"),
                "prodBnduCd": "01",
            }
        ],
        "zoneGroupYn": "N",
    }
    payload = json.dumps(body, ensure_ascii=False)
    resp = await client.post(
        SITE_BASE + PRC_LIST_PATH, content=payload.encode("utf-8"),
        headers=site_headers(), timeout=15,
    )
    print(f"\n[가격조회] HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(resp.text[:500])
        return None
    result = resp.json()
    print(json.dumps(result, ensure_ascii=False, indent=2)[:800])
    return result


async def hold_seat(client: AsyncSession, date: str, scns_no: str, scn_sseq: str, seat: dict):
    """seatTempPrmp: 좌석 임시 선점"""
    body = {
        "coCd": CO_CD,
        "custNo": CUST_NO,
        "siteNo": SITE_NO,
        "rtctlScopCd": RTCTL_SCOP_CD,
        "scnYmd": date,
        "scnsNo": scns_no,
        "scnSseq": scn_sseq,
        "sachlCd": "10",
        "sachlTypCd": "01",
        "atktChnlCd": "01",
        "cusgdCd": "01",
        "bymd": "",
        "mbltNo": "",
        "nmbrCrtfNo": "",
        "movAtktNo": "",
        "seatPrmpDataList": [
            {
                "sbordNo": seat.get("sbordNo", "001"),
                "seatAreaNo": seat.get("seatAreaNo", "001"),
                "szoneNo": seat.get("szoneNo", "01001"),
                "seatRowNm": seat["seatRowNm"],
                "seatNo": seat["seatNo"],
                "seatLocNo": seat.get("seatLocNo", ""),
            }
        ],
    }
    payload = json.dumps(body, ensure_ascii=False)
    resp = await client.post(
        SITE_BASE + PRMP_PATH, content=payload.encode("utf-8"),
        headers=site_headers(), timeout=15,
    )
    print(f"\n[선점] HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(resp.text[:500])
        return None
    result = resp.json()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


async def cancel_hold(client: AsyncSession, mov_atkt_no: str, seat: dict):
    """seatTempPrmpCncl: 좌석 임시 선점 취소"""
    body = {
        "coCd": CO_CD,
        "custNo": CUST_NO,
        "movAtktNo": mov_atkt_no,
        "rtctlScopCd": RTCTL_SCOP_CD,
        "sachlTypCd": "01",
        "seatPrmpDataList": [
            {
                "sbordNo": seat.get("sbordNo", "001"),
                "seatAreaNo": seat.get("seatAreaNo", "001"),
                "szoneNo": seat.get("szoneNo", "01001"),
                "seatRowNm": seat["seatRowNm"],
                "seatNo": seat["seatNo"],
            }
        ],
    }
    payload = json.dumps(body, ensure_ascii=False)
    resp = await client.post(
        SITE_BASE + PRMP_CNCL_PATH, content=payload.encode("utf-8"),
        headers=site_headers(), timeout=15,
    )
    print(f"\n[선점취소] HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(resp.text[:500])
        return None
    result = resp.json()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


async def pick_best_session(client: AsyncSession, date: str):
    """searchMovScnInfo로 해당 날짜 IMAX 회차 중 잔여좌석 제일 많은 회차 자동 선택"""
    cfg = dict(DEFAULT_CONFIG)
    flat, _ = await fetch_movie_list(client, ACCESS_TOKEN, date, cfg)
    if not flat:
        return None
    movies = parse_imax_movies(flat)
    candidates = [
        (m["movNo"], m["movNm"], s)
        for m in movies
        for s in m["sessions"]
        if s["fr_seat"] > 0
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[2]["fr_seat"], reverse=True)
    mov_no, mov_nm, best = candidates[0]
    print(
        f"[자동선택] {mov_nm} | {best['time']} | {best['hall']} | "
        f"잔여 {best['fr_seat']}/{best['total']}석"
    )
    return mov_no, best["scns_no"], best["session_id"]


async def main():
    if len(sys.argv) not in (2, 5):
        print("사용법: python test_seat_hold.py <YYYYMMDD>                             (회차 자동 선택)")
        print("      python test_seat_hold.py <YYYYMMDD> <scnsNo> <scnSseq> <movNo>   (회차 수동 지정)")
        print("예)     python test_seat_hold.py 20260819")
        sys.exit(1)

    date = sys.argv[1]

    if not ACCESS_TOKEN:
        print("오류: .env에 ACCESS_TOKEN 필요")
        sys.exit(1)

    async with AsyncSession(impersonate=_IMPERSONATE) as client:
        if len(sys.argv) == 5:
            scns_no, scn_sseq, mov_no = sys.argv[2], sys.argv[3], sys.argv[4]
        else:
            picked = await pick_best_session(client, date)
            if not picked:
                print(f"{date}에 잔여좌석 있는 IMAX 회차를 못 찾았어요. 수동으로 scnsNo/scnSseq/movNo를 지정해보세요.")
                return
            mov_no, scns_no, scn_sseq = picked

        print(f"[{datetime.now()}] 대상: {date} 상영관{scns_no} 회차{scn_sseq} 영화{mov_no}")

        seat = await find_open_seat(client, date, scns_no, scn_sseq)
        if not seat:
            print("테스트할 빈 좌석을 못 찾았어요. 다른 회차로 시도해보세요.")
            return

        await check_price(client, date, scns_no, scn_sseq, mov_no, seat)

        hold_result = await hold_seat(client, date, scns_no, scn_sseq, seat)
        if not hold_result or hold_result.get("statusCode") != 0:
            print("선점 실패 — 여기서 중단 (취소할 것도 없음)")
            return

        data = hold_result.get("data", {})
        mov_atkt_no = data.get("movAtktNo", "")
        limit_dt = data.get("seatTempPrmpLimitDt", "")
        print(f"\n>>> movAtktNo={mov_atkt_no}  만료시각={limit_dt}")

        print("\n3초 대기 후 즉시 선점 취소...")
        await asyncio.sleep(3)

        cncl_result = await cancel_hold(client, mov_atkt_no, seat)
        if cncl_result and cncl_result.get("statusCode") == 0:
            print("\n[결과] 선점 → 취소 왕복 성공. 좌석 정상 반납됨.")
        else:
            print("\n[경고] 취소 실패! 수동으로 CGV 앱/웹에서 좌석 상태 확인 필요.")

        print("\n재조회로 최종 확인...")
        await find_open_seat(client, date, scns_no, scn_sseq)


if __name__ == "__main__":
    asyncio.run(main())
