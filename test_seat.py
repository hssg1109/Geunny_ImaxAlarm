"""
searchIfSeatData API 응답 구조 확인용 테스트 스크립트
"""

import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import time

from curl_cffi.requests import AsyncSession
from dotenv import load_dotenv

load_dotenv()

_HMAC_SECRET = "ydqXY0ocnFLmJGHr_zNzFcpjwAsXq_8JcBNURAkRscg"
CGV_API_BASE = "https://api.cgv.co.kr"
SEAT_PATH    = "/cnm/atkt/searchIfSeatData"

ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "")
CGV_COOKIES  = os.getenv("CGV_COOKIES", "")
CUST_NO      = os.getenv("CUST_NO", "286863252")  # 캡처된 요청의 custNo


def make_signature(path: str, body: str, timestamp: str) -> str:
    message = f"{timestamp}|{path}|{body}"
    raw = hmac.new(_HMAC_SECRET.encode(), message.encode(), hashlib.sha256).digest()
    return base64.b64encode(raw).decode()


def make_headers(token: str, path: str) -> dict:
    ts  = str(math.floor(time.time()))
    sig = make_signature(path, "", ts)
    h = {
        "accept":          "application/json",
        "accept-language": "ko-KR,ko;q=0.9",
        "authorization":   f"Bearer {token}",
        "origin":          "https://cgv.co.kr",
        "referer":         "https://cgv.co.kr/",
        "x-timestamp":     ts,
        "x-signature":     sig,
    }
    if CGV_COOKIES:
        h["cookie"] = CGV_COOKIES
    return h


async def fetch_seat_data(scn_ymd: str, scns_no: str, scn_sseq: str, seat_area_no: str = "001"):
    params = {
        "coCd":       "A420",
        "siteNo":     "0013",
        "scnYmd":     scn_ymd,
        "scnsNo":     scns_no,
        "scnSseq":    scn_sseq,
        "seatAreaNo": seat_area_no,
        "cusgdCd":    "01",
    }
    if CUST_NO:
        params["custNo"] = CUST_NO

    async with AsyncSession(impersonate="chrome124") as client:
        resp = await client.get(
            CGV_API_BASE + SEAT_PATH,
            params=params,
            headers=make_headers(ACCESS_TOKEN, SEAT_PATH),
            timeout=15,
        )
    return resp


async def main():
    # 캡처된 요청 파라미터로 테스트
    scn_ymd   = "20260529"
    scns_no   = "018"
    scn_sseq  = "5"

    print(f"요청: {scn_ymd} 상영관{scns_no} 회차{scn_sseq}")
    print("-" * 60)

    resp = await fetch_seat_data(scn_ymd, scns_no, scn_sseq)
    print(f"HTTP {resp.status_code}")

    if resp.status_code != 200:
        print(f"server={resp.headers.get('server')}  cf-ray={resp.headers.get('cf-ray')}")
        print(resp.text[:500])
        return

    body = resp.json()
    print(f"statusCode: {body.get('statusCode')}")
    print(f"statusMessage: {body.get('statusMessage')}")

    data = body.get("data")
    print(f"\ndata 타입: {type(data).__name__}")

    if isinstance(data, list):
        print(f"data 길이: {len(data)}개")
        if data:
            print(f"\n첫 번째 항목 전체 필드:")
            print(json.dumps(data[0], ensure_ascii=False, indent=2))
    elif isinstance(data, dict):
        print(f"data 키: {list(data.keys())}")
        # 하위 리스트 탐색
        for k, v in data.items():
            if isinstance(v, list) and v:
                print(f"\ndata['{k}'] 길이: {len(v)}개, 첫 항목:")
                print(json.dumps(v[0], ensure_ascii=False, indent=2))
                break
    else:
        print(f"data 전체:")
        print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
