"""
CGV 용산IMAX 좌석 오픈 알리미

[인증 방식 완전 해독]
- accessToken: 브라우저 쿠키 'accessToken' 에 저장
- x-signature: HMAC-SHA256("{timestamp}|{path}|{body}", SECRET_KEY) → Base64
- SECRET_KEY: CGV JS 번들(module 74189)에서 추출
- 토큰 만료(401 / statusCode -1001): reissue 엔드포인트로 자동 갱신

[Cloudflare 우회]
- curl_cffi 로 Chrome TLS 핑거프린트 흉내 → Cloudflare 봇 감지 통과
"""

import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from curl_cffi.requests import AsyncSession
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_PROXY     = os.getenv("TELEGRAM_PROXY", "")   # 예) http://127.0.0.1:7890
CHECK_INTERVAL     = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))

MOV_NO        = os.getenv("MOV_NO", "30001210")
SITE_NO       = os.getenv("SITE_NO", "0013")
CO_CD         = os.getenv("CO_CD", "A420")
RTCTL_SCOP_CD = os.getenv("RTCTL_SCOP_CD", "08")
CUST_NO       = os.getenv("CUST_NO", "")
WATCH_DATES   = [d.strip() for d in os.getenv("WATCH_DATES", "").split(",") if d.strip()]

# 브라우저 Application 탭 → Cookies → cgv.co.kr → accessToken 값
ACCESS_TOKEN  = os.getenv("ACCESS_TOKEN", "")
# 브라우저 DevTools → Network 탭 → CGV API 요청 선택 → Headers → cookie 값 전체 복사
# cf_clearance, __cf_bm, accessToken 등이 포함되어야 Cloudflare 통과
CGV_COOKIES   = os.getenv("CGV_COOKIES", "")

TOKEN_FILE    = Path(__file__).parent / "token.json"

CGV_API_BASE   = "https://api.cgv.co.kr"
SCHEDULE_PATH  = "/cnm/atkt/searchSchByMov"
REISSUE_PATH   = "/com/bznsCom/custKeep/reissueToken"

# CGV JS bundle(module 74189)에서 추출한 HMAC 서명 비밀키
_HMAC_SECRET = "ydqXY0ocnFLmJGHr_zNzFcpjwAsXq_8JcBNURAkRscg"

# curl_cffi impersonate 대상 (Chrome 최신 → Cloudflare 통과)
_IMPERSONATE = "chrome124"

notified_sessions: set = set()


# ── 서명 생성 ───────────────────────────────────────────────────────────────

def make_signature(path: str, body: str, timestamp: str) -> str:
    """
    CGV API x-signature 생성
    알고리즘: HMAC-SHA256("{timestamp}|{path}|{body}", SECRET) → Base64
    """
    message = f"{timestamp}|{path}|{body}"
    raw = hmac.new(
        _HMAC_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(raw).decode("utf-8")


def current_timestamp() -> str:
    return str(math.floor(time.time()))


# ── HTTP 헤더 ────────────────────────────────────────────────────────────────

def make_headers(token: str, path: str, body: str = "") -> dict:
    ts  = current_timestamp()
    sig = make_signature(path, body, ts)
    headers = {
        "accept":           "application/json",
        "accept-language":  "ko-KR,ko;q=0.9",
        "authorization":    f"Bearer {token}",
        "origin":           "https://cgv.co.kr",
        "referer":          "https://cgv.co.kr/",
        "x-timestamp":      ts,
        "x-signature":      sig,
    }
    if CGV_COOKIES:
        headers["cookie"] = CGV_COOKIES
    return headers


# ── 토큰 저장/로드 ─────────────────────────────────────────────────────────

def save_token(token: str) -> None:
    TOKEN_FILE.write_text(json.dumps({"accessToken": token}))
    print("[토큰] 저장 완료", flush=True)


def load_token() -> str:
    """저장된 토큰 → 없으면 .env ACCESS_TOKEN 사용"""
    if TOKEN_FILE.exists():
        try:
            t = json.loads(TOKEN_FILE.read_text()).get("accessToken", "")
            if t:
                return t
        except Exception:
            pass
    return ACCESS_TOKEN


# ── 토큰 갱신 ─────────────────────────────────────────────────────────────

async def reissue_token(client: AsyncSession, current_token: str) -> str:
    """
    CGV 토큰 갱신 (401 / statusCode -1001 발생 시 호출)
    JS 분석 결과: POST {accessToken} → data.accessToken
    """
    print("[토큰] 갱신 시도...", flush=True)
    payload = json.dumps({"accessToken": current_token})
    try:
        resp = await client.post(
            CGV_API_BASE + REISSUE_PATH,
            content=payload,
            headers={
                **make_headers(current_token, REISSUE_PATH, payload),
                "content-type": "application/json",
            },
            timeout=10,
        )
        body = resp.json()
        new_token = (body.get("data") or {}).get("accessToken")
        if new_token:
            print("[토큰] 갱신 성공!", flush=True)
            save_token(new_token)
            return new_token
        print(f"[토큰] 갱신 실패: {body.get('statusMessage')}", flush=True)
    except Exception as e:
        print(f"[토큰] 갱신 오류: {e}", flush=True)

    print("[토큰] .env의 ACCESS_TOKEN을 새로 복사해주세요.", flush=True)
    return current_token


# ── Telegram ────────────────────────────────────────────────────────────────

async def send_telegram(message: str) -> None:
    """curl_cffi 로 Telegram Bot API 직접 호출"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[텔레그램 미설정] {message}", flush=True)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    kwargs: dict = {"timeout": 10}
    if TELEGRAM_PROXY:
        kwargs["proxies"] = {"https": TELEGRAM_PROXY, "http": TELEGRAM_PROXY}
    try:
        async with AsyncSession(impersonate=_IMPERSONATE) as s:
            resp = await s.post(url, json=payload, **kwargs)
        if resp.status_code == 200:
            print("[텔레그램] 전송 완료", flush=True)
        else:
            print(f"[텔레그램] 전송 실패: HTTP {resp.status_code} → {resp.text[:200]}", flush=True)
    except Exception as e:
        print(f"[텔레그램] 전송 실패: {type(e).__name__}: {e}", flush=True)


# ── 파싱 ────────────────────────────────────────────────────────────────────

_debug_logged: set = set()


def parse_schedule(data: list, date: str) -> list:
    """
    CGV API 응답 data 배열에서 IMAX 잔여석 세션 추출

    확인된 필드명:
      frSeatCnt  → 잔여(예매가능)석
      stcnt      → 총좌석
      scnsrtTm   → 시작시간 (HHMM)
      scnsNm     → 상영관명
    """
    first = date not in _debug_logged
    if first:
        _debug_logged.add(date)
        if data:
            print(f"[{date}] DEBUG 첫 응답 필드: {list(data[0].keys())[:15]}", flush=True)

    available = []
    for s in data:
        hall = s.get("scnsNm") or s.get("expoScnsNm") or ""
        # IMAX 상영관 필터
        if "imax" not in hall.lower() and "아이맥스" not in hall:
            continue

        remain = int(s.get("frSeatCnt") or s.get("cpSeatCnt") or 0)
        total  = int(s.get("stcnt") or 0)
        if remain <= 0:
            continue

        t = s.get("scnsrtTm") or s.get("rlMovStartTm") or "?"
        time_fmt = f"{t[:2]}:{t[2:]}" if len(t) == 4 else t

        available.append({
            "date":       date,
            "time":       time_fmt,
            "remain":     remain,
            "total":      total,
            "hall":       hall,
            "session_id": s.get("scnSseq") or t,
        })
    return available


# ── API 호출 ────────────────────────────────────────────────────────────────

async def fetch_schedule(client: AsyncSession, token: str, date: str):
    params: dict = {
        "coCd":        CO_CD,
        "siteNo":      SITE_NO,
        "scnYmd":      date,
        "movNo":       MOV_NO,
        "rtctlScopCd": RTCTL_SCOP_CD,
    }
    if CUST_NO:
        params["custNo"] = CUST_NO

    return await client.get(
        CGV_API_BASE + SCHEDULE_PATH,
        params=params,
        headers=make_headers(token, SCHEDULE_PATH),
        timeout=15,
    )


async def check_date(client: AsyncSession, token: str, date: str) -> tuple[list, str]:
    resp = await fetch_schedule(client, token, date)

    # 401: 토큰 만료 → 갱신 후 재시도
    if resp.status_code == 401:
        try:
            body = resp.json()
            if body.get("statusCode") in (-1001, "-1001"):
                token = await reissue_token(client, token)
                resp  = await fetch_schedule(client, token, date)
        except Exception:
            pass

    if resp.status_code != 200:
        try:
            body_text = resp.text[:600]
        except Exception:
            body_text = "(응답 본문 읽기 실패)"
        cf_ray = resp.headers.get("cf-ray", "")
        server = resp.headers.get("server", "")
        ct     = resp.headers.get("content-type", "")
        print(f"[{date}] HTTP {resp.status_code}  server={server}  cf-ray={cf_ray}  content-type={ct}", flush=True)
        print(f"[{date}] 응답 본문: {body_text}", flush=True)
        return [], token

    body = resp.json()
    if body.get("statusCode") != 0:
        print(f"[{date}] API 오류 {body.get('statusCode')}: {body.get('statusMessage','')}", flush=True)
        return [], token

    data = body.get("data") or []
    if not isinstance(data, list):
        return [], token

    print(f"[{date}] 전체 {len(data)}개 세션 (IMAX 필터 전)", flush=True)
    return parse_schedule(data, date), token


# ── 알림 ─────────────────────────────────────────────────────────────────────

async def process_results(sessions: list) -> None:
    for s in sessions:
        sid = f"{s['date']}_{s['session_id']}_{s['time']}"
        if sid in notified_sessions:
            continue
        notified_sessions.add(sid)
        msg = (
            f"🎬 <b>CGV 용산IMAX 오픈!</b>\n\n"
            f"📅 {s['date']}  🕐 {s['time']}\n"
            f"🏛 {s['hall']}\n"
            f"💺 잔여 {s['remain']}석 / 총 {s['total']}석\n\n"
            f"🔗 <a href='https://cgv.co.kr/ticket'>바로 예매</a>"
        )
        await send_telegram(msg)
        print(f"  → 알림: {s['date']} {s['time']} 잔여 {s['remain']}석", flush=True)


# ── 메인 ─────────────────────────────────────────────────────────────────────

async def main():
    if not WATCH_DATES:
        print("오류: .env에 WATCH_DATES 설정 필요  예) WATCH_DATES=20260530,20260531")
        sys.exit(1)

    token = load_token()
    if not token:
        print("오류: .env에 ACCESS_TOKEN 설정 필요")
        print("  브라우저 DevTools → Application → Cookies → cgv.co.kr → accessToken 값 복사")
        sys.exit(1)

    print("=" * 60)
    print("  CGV 용산IMAX 좌석 오픈 알리미")
    print(f"  영화: {MOV_NO} | 상영관 siteNo: {SITE_NO}")
    print(f"  감시 날짜: {', '.join(WATCH_DATES)}")
    print(f"  확인 주기: {CHECK_INTERVAL}초")
    print(f"  TLS 핑거프린트: {_IMPERSONATE}")
    if TELEGRAM_BOT_TOKEN:
        print(f"  텔레그램 봇: ...{TELEGRAM_BOT_TOKEN[-6:]}")
        print(f"  텔레그램 채팅ID: {TELEGRAM_CHAT_ID}")
        if TELEGRAM_PROXY:
            print(f"  텔레그램 프록시: {TELEGRAM_PROXY}")
    else:
        print("  텔레그램: 미설정 (콘솔 출력만)")
    print("=" * 60)

    ts  = current_timestamp()
    sig = make_signature(SCHEDULE_PATH, "", ts)
    print(f"[서명 테스트] ts={ts}  sig={sig[:20]}...", flush=True)

    await send_telegram(
        f"🔔 CGV 용산IMAX 알리미 시작\n감시 날짜: {', '.join(WATCH_DATES)}"
    )

    async with AsyncSession(impersonate=_IMPERSONATE) as client:
        round_num = 0
        while True:
            round_num += 1
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n[{now}] 라운드 {round_num}", flush=True)

            for date in WATCH_DATES:
                try:
                    sessions, token = await check_date(client, token, date)
                    if sessions:
                        await process_results(sessions)
                    else:
                        print(f"  [{date}] IMAX 예매 없음", flush=True)
                except Exception as e:
                    print(f"  [{date}] 오류: {e}", flush=True)
                await asyncio.sleep(1)

            print(f"[대기] {CHECK_INTERVAL}초 후 재확인...", flush=True)
            await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n종료합니다.")
