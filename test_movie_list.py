"""
searchMovScnInfo API 응답 구조 확인용 테스트 스크립트
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
CGV_API_BASE  = "https://api.cgv.co.kr"
MOV_SCN_PATH  = "/cnm/atkt/searchMovScnInfo"

ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "")
CGV_COOKIES  = os.getenv("CGV_COOKIES", "")
CUST_NO      = os.getenv("CUST_NO", "")


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


async def main():
    scn_ymd = "20260529"
    params = {
        "coCd":        "A420",
        "siteNo":      "0013",
        "scnYmd":      scn_ymd,
        "rtctlScopCd": "08",
    }
    if CUST_NO:
        params["custNo"] = CUST_NO

    print(f"요청: {scn_ymd} 전체 상영 목록")
    print("-" * 60)

    async with AsyncSession(impersonate="chrome124") as client:
        resp = await client.get(
            CGV_API_BASE + MOV_SCN_PATH,
            params=params,
            headers=make_headers(ACCESS_TOKEN, MOV_SCN_PATH),
            timeout=15,
        )

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
            print(f"\n▶ 첫 번째 항목 전체 필드:")
            print(json.dumps(data[0], ensure_ascii=False, indent=2))
            if len(data) > 1:
                print(f"\n▶ 두 번째 항목 전체 필드:")
                print(json.dumps(data[1], ensure_ascii=False, indent=2))

    elif isinstance(data, dict):
        print(f"data 최상위 키: {list(data.keys())}")
        for k, v in data.items():
            if isinstance(v, list):
                print(f"\n▶ data['{k}'] 길이: {len(v)}개")
                if v:
                    print(f"  첫 항목:")
                    print(json.dumps(v[0], ensure_ascii=False, indent=2))
            else:
                print(f"  data['{k}']: {v}")
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
