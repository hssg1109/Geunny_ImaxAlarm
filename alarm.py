"""
CGV 용산IMAX 좌석 오픈 알리미

[인증 방식 완전 해독]
- accessToken: 브라우저 쿠키 'accessToken' 에 저장
- x-signature: HMAC-SHA256("{timestamp}|{path}|{body}", SECRET_KEY) → Base64
- SECRET_KEY: CGV JS 번들(module 74189)에서 추출
- 토큰 만료(401 / statusCode -1001): reissue 엔드포인트로 자동 갱신

[Cloudflare 우회]
- curl_cffi 로 Chrome TLS 핑거프린트 흉내 → Cloudflare 봇 감지 통과

[명당 알림 조건]
- F~J열, 17~28번 좌석 중 빈 좌석이 새로 생겼을 때만 알림
"""

import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import platform
import re
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from curl_cffi.requests import AsyncSession
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_PROXY      = os.getenv("TELEGRAM_PROXY", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
CHECK_INTERVAL      = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))

MOV_NO        = os.getenv("MOV_NO", "30001210")
SITE_NO       = os.getenv("SITE_NO", "0013")
CO_CD         = os.getenv("CO_CD", "A420")
RTCTL_SCOP_CD = os.getenv("RTCTL_SCOP_CD", "08")
CUST_NO       = os.getenv("CUST_NO", "")
WATCH_DATES   = [d.strip() for d in os.getenv("WATCH_DATES", "").split(",") if d.strip()]

ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "")
CGV_COOKIES  = os.getenv("CGV_COOKIES", "")

TOKEN_FILE = Path(__file__).parent / "token.json"

CGV_API_BASE  = "https://api.cgv.co.kr"
SCHEDULE_PATH = "/cnm/atkt/searchSchByMov"
SEAT_PATH     = "/cnm/atkt/searchIfSeatData"
REISSUE_PATH  = "/com/bznsCom/custKeep/reissueToken"

_HMAC_SECRET = "ydqXY0ocnFLmJGHr_zNzFcpjwAsXq_8JcBNURAkRscg"
_IMPERSONATE = "chrome124"

# ── 명당 좌석 조건 ────────────────────────────────────────────────────────────
PRIME_ROWS     = {"F", "G", "H", "I", "J"}
PRIME_SEAT_MIN = 17
PRIME_SEAT_MAX = 28

# session_key → 이전 라운드의 예매가능 명당 좌석 set{(row, seat_no)}
# None = 아직 한 번도 조회 안 함
prime_seat_state: dict[str, set | None] = defaultdict(lambda: None)


# ── 서명 / 헤더 ─────────────────────────────────────────────────────────────

def make_signature(path: str, body: str, timestamp: str) -> str:
    message = f"{timestamp}|{path}|{body}"
    raw = hmac.new(
        _HMAC_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(raw).decode("utf-8")


def current_timestamp() -> str:
    return str(math.floor(time.time()))


def make_headers(token: str, path: str, body: str = "") -> dict:
    ts  = current_timestamp()
    sig = make_signature(path, body, ts)
    headers = {
        "accept":          "application/json",
        "accept-language": "ko-KR,ko;q=0.9",
        "authorization":   f"Bearer {token}",
        "origin":          "https://cgv.co.kr",
        "referer":         "https://cgv.co.kr/",
        "x-timestamp":     ts,
        "x-signature":     sig,
    }
    if CGV_COOKIES:
        headers["cookie"] = CGV_COOKIES
    return headers


# ── 토큰 저장/로드/갱신 ───────────────────────────────────────────────────────

def save_token(token: str) -> None:
    TOKEN_FILE.write_text(json.dumps({"accessToken": token}))
    print("[토큰] 저장 완료", flush=True)


def load_token() -> str:
    if TOKEN_FILE.exists():
        try:
            t = json.loads(TOKEN_FILE.read_text()).get("accessToken", "")
            if t:
                return t
        except Exception:
            pass
    return ACCESS_TOKEN


async def reissue_token(client: AsyncSession, current_token: str) -> str:
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


# ── 알림 채널 ────────────────────────────────────────────────────────────────

async def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    kwargs: dict = {"timeout": 10}
    if TELEGRAM_PROXY:
        kwargs["proxies"] = {"https": TELEGRAM_PROXY, "http": TELEGRAM_PROXY}
    try:
        async with AsyncSession(impersonate=_IMPERSONATE) as s:
            resp = await s.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, **kwargs)
        if resp.status_code == 200:
            print("[텔레그램] 전송 완료", flush=True)
        else:
            print(f"[텔레그램] 전송 실패: HTTP {resp.status_code}", flush=True)
    except Exception as e:
        print(f"[텔레그램] 전송 실패: {type(e).__name__}: {e}", flush=True)


async def send_discord(message: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        return
    text = re.sub(r"<a href='([^']+)'>([^<]+)</a>", r"[\2](\1)",
           message.replace("<b>", "**").replace("</b>", "**"))
    try:
        async with AsyncSession(impersonate=_IMPERSONATE) as s:
            resp = await s.post(DISCORD_WEBHOOK_URL, json={"content": text}, timeout=10)
        if resp.status_code in (200, 204):
            print("[디스코드] 전송 완료", flush=True)
        else:
            print(f"[디스코드] 전송 실패: HTTP {resp.status_code}", flush=True)
    except Exception as e:
        print(f"[디스코드] 전송 실패: {type(e).__name__}: {e}", flush=True)


async def send_windows_alert(message: str) -> None:
    """Windows 전용: 경고음 + 토스트 알림 (네트워크 불필요)"""
    if platform.system() != "Windows":
        return

    plain = re.sub(r"<[^>]+>", "", message).strip()

    try:
        import winsound
        loop = asyncio.get_event_loop()
        for _ in range(5):
            await loop.run_in_executor(None, winsound.Beep, 1000, 300)
            await asyncio.sleep(0.2)
    except Exception as e:
        print(f"[윈도우] 경고음 실패: {e}", flush=True)

    try:
        from win11toast import toast_async
        await toast_async("🎬 CGV 용산IMAX 명당 오픈!", plain[:150])
        print("[윈도우] 토스트 알림 전송", flush=True)
        return
    except ImportError:
        pass
    except Exception as e:
        print(f"[윈도우] 토스트 실패: {e}", flush=True)

    try:
        import ctypes
        threading.Thread(
            target=ctypes.windll.user32.MessageBoxW,
            args=(0, plain[:300], "🎬 CGV 용산IMAX 명당 오픈!", 0x30 | 0x1000),
            daemon=True,
        ).start()
        print("[윈도우] 팝업 알림 표시", flush=True)
    except Exception as e:
        print(f"[윈도우] 팝업 실패: {e}", flush=True)


async def notify(message: str) -> None:
    """설정된 모든 채널로 알림 전송"""
    await asyncio.gather(
        # send_telegram(message),   # 사내망 차단 - 비활성화
        # send_discord(message),    # 사내망 차단 - 비활성화
        send_windows_alert(message),
    )


# ── 스케줄 파싱 ──────────────────────────────────────────────────────────────

_debug_logged: set = set()


def parse_schedule(data: list, date: str) -> list:
    """IMAX 세션 전체 반환 (frSeatCnt 무관 — 명당 변화 감지를 위해)"""
    first = date not in _debug_logged
    if first:
        _debug_logged.add(date)
        if data:
            print(f"[{date}] DEBUG 첫 응답 필드: {list(data[0].keys())[:15]}", flush=True)

    sessions = []
    for s in data:
        hall = s.get("scnsNm") or s.get("expoScnsNm") or ""
        if "imax" not in hall.lower() and "아이맥스" not in hall:
            continue

        t = s.get("scnsrtTm") or s.get("rlMovStartTm") or "?"
        time_fmt = f"{t[:2]}:{t[2:]}" if len(t) == 4 else t

        sessions.append({
            "date":       date,
            "time":       time_fmt,
            "hall":       hall,
            "scns_no":    s.get("scnsNo", ""),
            "session_id": s.get("scnSseq") or t,
            "total":      int(s.get("stcnt") or 0),
        })
    return sessions


# ── 좌석 상세 조회 + 명당 필터 ───────────────────────────────────────────────

def _is_prime(row: str, seat_no_str: str) -> bool:
    try:
        return row in PRIME_ROWS and PRIME_SEAT_MIN <= int(seat_no_str) <= PRIME_SEAT_MAX
    except (ValueError, TypeError):
        return False


async def fetch_prime_seats(
    client: AsyncSession, token: str,
    date: str, scns_no: str, scn_sseq: str,
) -> set | None:
    """
    searchIfSeatData → 예매가능 명당 좌석 set{(row, seat_no)} 반환
    API 실패 시 None 반환 (상태 미업데이트)
    """
    params = {
        "coCd":       CO_CD,
        "siteNo":     SITE_NO,
        "scnYmd":     date,
        "scnsNo":     scns_no,
        "scnSseq":    scn_sseq,
        "seatAreaNo": "001",
        "cusgdCd":    "01",
    }
    if CUST_NO:
        params["custNo"] = CUST_NO

    try:
        resp = await client.get(
            CGV_API_BASE + SEAT_PATH,
            params=params,
            headers=make_headers(token, SEAT_PATH),
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        body = resp.json()
        if body.get("statusCode") != 0:
            return None

        items = (body.get("data") or {}).get("items") or []
        if not items:
            return None

        seats = items[0].get("seats", [])
        return {
            (s["seatRowNm"], s["seatNo"])
            for s in seats
            if s.get("seatSaleYn") == "Y"
            and _is_prime(s.get("seatRowNm", ""), s.get("seatNo", ""))
        }
    except Exception as e:
        print(f"[좌석상세] 조회 실패: {e}", flush=True)
        return None


# ── 명당 변화 감지 + 알림 ────────────────────────────────────────────────────

def _format_prime_seats(seats: set) -> str:
    """명당 좌석을 열별로 정렬해서 문자열로 변환"""
    by_row: dict[str, list] = defaultdict(list)
    for row, no in seats:
        by_row[row].append(int(no))
    lines = []
    for row in sorted(by_row):
        nums = sorted(by_row[row])
        lines.append(f"  {row}열: {', '.join(str(n) for n in nums)}번")
    return "\n".join(lines)


async def process_sessions(sessions: list, client: AsyncSession, token: str) -> None:
    for s in sessions:
        if not s.get("scns_no"):
            continue

        session_key = f"{s['date']}_{s['session_id']}"

        current = await fetch_prime_seats(
            client, token,
            s["date"], s["scns_no"], str(s["session_id"])
        )
        await asyncio.sleep(0.5)

        if current is None:
            # API 실패 → 상태 유지, 알림 없음
            continue

        prev = prime_seat_state[session_key]  # None = 첫 조회

        if prev is None:
            # 첫 조회: 명당 있으면 바로 알림
            newly = current
        else:
            # 이전에 없었다가 새로 생긴 명당만
            newly = current - prev

        prime_seat_state[session_key] = current

        # 콘솔에 현황 출력
        print(
            f"  [{s['date']} {s['time']}] 명당 {len(current)}석 "
            f"(새로 열림: {len(newly)}석)",
            flush=True,
        )

        if not newly:
            continue

        seat_str = _format_prime_seats(newly)
        total_str = _format_prime_seats(current) if current != newly else seat_str

        msg = (
            f"🎯 <b>CGV 용산IMAX 명당 오픈!</b>\n\n"
            f"📅 {s['date']}  🕐 {s['time']}\n"
            f"🏛 {s['hall']}\n\n"
            f"✨ <b>새로 열린 명당 좌석</b> (F~J열 17~28번)\n"
            f"{seat_str}\n\n"
            f"💺 현재 명당 총 {len(current)}석 예매가능\n\n"
            f"🔗 <a href='https://cgv.co.kr/ticket'>바로 예매</a>"
        )
        await notify(msg)
        print(f"  → 명당 알림 전송: {s['date']} {s['time']}", flush=True)


# ── 스케줄 API 호출 ──────────────────────────────────────────────────────────

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
        print(f"[{date}] HTTP {resp.status_code}  server={server}  cf-ray={cf_ray}", flush=True)
        print(f"[{date}] 응답: {body_text}", flush=True)
        return [], token

    body = resp.json()
    if body.get("statusCode") != 0:
        print(f"[{date}] API 오류 {body.get('statusCode')}: {body.get('statusMessage','')}", flush=True)
        return [], token

    data = body.get("data") or []
    if not isinstance(data, list):
        return [], token

    sessions = parse_schedule(data, date)
    print(f"[{date}] IMAX 세션 {len(sessions)}개", flush=True)
    return sessions, token


# ── 메인 ─────────────────────────────────────────────────────────────────────

async def main():
    if not WATCH_DATES:
        print("오류: .env에 WATCH_DATES 설정 필요  예) WATCH_DATES=20260530,20260531")
        sys.exit(1)

    token = load_token()
    if not token:
        print("오류: .env에 ACCESS_TOKEN 설정 필요")
        sys.exit(1)

    print("=" * 60)
    print("  CGV 용산IMAX 좌석 오픈 알리미")
    print(f"  영화: {MOV_NO} | 상영관 siteNo: {SITE_NO}")
    print(f"  감시 날짜: {', '.join(WATCH_DATES)}")
    print(f"  확인 주기: {CHECK_INTERVAL}초")
    print(f"  명당 조건: {sorted(PRIME_ROWS)}열  {PRIME_SEAT_MIN}~{PRIME_SEAT_MAX}번")
    if DISCORD_WEBHOOK_URL:
        print(f"  디스코드 Webhook: ...{DISCORD_WEBHOOK_URL[-12:]}")
    if TELEGRAM_BOT_TOKEN:
        print(f"  텔레그램 봇: ...{TELEGRAM_BOT_TOKEN[-6:]}")
    if platform.system() == "Windows":
        try:
            import win11toast  # noqa: F401
            print("  윈도우 알림: 토스트 + 경고음")
        except ImportError:
            print("  윈도우 알림: 팝업 + 경고음")
    print("=" * 60)

    ts  = current_timestamp()
    sig = make_signature(SCHEDULE_PATH, "", ts)
    print(f"[서명 테스트] ts={ts}  sig={sig[:20]}...", flush=True)

    await notify(
        f"🔔 CGV 용산IMAX 알리미 시작\n"
        f"감시 날짜: {', '.join(WATCH_DATES)}\n"
        f"명당 조건: {sorted(PRIME_ROWS)}열 {PRIME_SEAT_MIN}~{PRIME_SEAT_MAX}번"
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
                        await process_sessions(sessions, client, token)
                    else:
                        print(f"  [{date}] IMAX 세션 없음", flush=True)
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
