"""
CGV 용산IMAX 좌석 오픈 알리미 (직접 HTTP 호출 버전)

Playwright 없이 httpx로 CGV API를 직접 폴링합니다.
브라우저 DevTools에서 복사한 Bearer 토큰을 .env에 넣으면 바로 동작합니다.
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL     = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))

MOV_NO         = os.getenv("MOV_NO", "30001210")
SITE_NO        = os.getenv("SITE_NO", "0013")
CO_CD          = os.getenv("CO_CD", "A420")
RTCTL_SCOP_CD  = os.getenv("RTCTL_SCOP_CD", "08")
CUST_NO        = os.getenv("CUST_NO", "")
WATCH_DATES    = [d.strip() for d in os.getenv("WATCH_DATES", "").split(",") if d.strip()]

# 브라우저 DevTools → Network 탭 → searchSchByMov 요청 → Headers → authorization 값
BEARER_TOKEN   = os.getenv("BEARER_TOKEN", "")

TOKEN_FILE = Path(__file__).parent / "token.json"

CGV_API_BASE = "https://api.cgv.co.kr"
SCHEDULE_PATH = "/cnm/atkt/searchSchByMov"

notified_sessions: set = set()


# ── HTTP 클라이언트 ─────────────────────────────────────────────────────────

def make_headers(token: str) -> dict:
    return {
        "accept": "application/json",
        "accept-language": "ko-KR",
        "authorization": f"Bearer {token}",
        "origin": "https://cgv.co.kr",
        "referer": "https://cgv.co.kr/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
        ),
    }


async def fetch_schedule(client: httpx.AsyncClient, token: str, date: str) -> httpx.Response:
    params = {
        "coCd": CO_CD,
        "siteNo": SITE_NO,
        "scnYmd": date,
        "movNo": MOV_NO,
        "rtctlScopCd": RTCTL_SCOP_CD,
    }
    if CUST_NO:
        params["custNo"] = CUST_NO

    return await client.get(
        CGV_API_BASE + SCHEDULE_PATH,
        params=params,
        headers=make_headers(token),
        timeout=15.0,
    )


# ── 토큰 저장/로드 ─────────────────────────────────────────────────────────

def save_token(token: str) -> None:
    TOKEN_FILE.write_text(json.dumps({"bearer": token}))


def load_token() -> str:
    """저장된 토큰 로드. 없으면 .env 값 사용."""
    if TOKEN_FILE.exists():
        try:
            return json.loads(TOKEN_FILE.read_text()).get("bearer", "")
        except Exception:
            pass
    return BEARER_TOKEN


# ── 토큰 갱신 ─────────────────────────────────────────────────────────────

async def try_refresh_token(client: httpx.AsyncClient, current_token: str) -> str:
    """
    refresh_token 쿠키로 새 Bearer 토큰 요청 시도.
    CGV 갱신 엔드포인트가 확인되면 이 함수를 업데이트하세요.
    """
    # 알려진 후보 엔드포인트들 순서대로 시도
    candidates = [
        ("POST", "/com/bznsCom/custKeep/reissueToken"),
        ("POST", "/com/auth/reissue"),
        ("GET",  "/com/bznsCom/custKeep/reissueToken"),
    ]
    for method, path in candidates:
        try:
            resp = await client.request(
                method,
                CGV_API_BASE + path,
                headers=make_headers(current_token),
                timeout=10.0,
            )
            if resp.status_code == 200:
                body = resp.json()
                new_token = (
                    body.get("data", {}).get("accessToken")
                    or body.get("data", {}).get("token")
                    or body.get("accessToken")
                )
                if new_token:
                    print(f"[토큰] 갱신 성공 (엔드포인트: {path})", flush=True)
                    save_token(new_token)
                    return new_token
        except Exception:
            pass

    print("[토큰] 자동 갱신 실패 — .env의 BEARER_TOKEN을 새로 복사해주세요.", flush=True)
    return current_token


# ── Telegram ────────────────────────────────────────────────────────────────

async def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[텔레그램 미설정] {message}", flush=True)
        return
    try:
        import telegram
        bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        await asyncio.wait_for(
            bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="HTML"),
            timeout=10.0,
        )
        print("[텔레그램] 전송 완료", flush=True)
    except asyncio.TimeoutError:
        print("[텔레그램] 전송 타임아웃 — 계속 진행", flush=True)
    except Exception as e:
        print(f"[텔레그램] 전송 실패: {e}", flush=True)


# ── 파싱 ────────────────────────────────────────────────────────────────────

def parse_schedule(data: list, date: str, debug: bool = False) -> list:
    """
    API 응답 data 배열에서 IMAX 잔여석 세션 추출.

    실제 필드명 (응답에서 확인):
      frSeatCnt  → 잔여석
      stcnt      → 총좌석
      scnsrtTm   → 시작시간 (HHMM)
      scnsNm     → 상영관명
      scnYmd     → 날짜
    """
    if debug and data:
        print(f"[DEBUG] 첫 번째 세션 키: {list(data[0].keys())}", flush=True)

    available = []
    for s in data:
        hall = s.get("scnsNm") or s.get("expoScnsNm") or ""
        # IMAX 상영관 필터 (아이맥스관, IMAX 등)
        if "imax" not in hall.lower() and "아이맥스" not in hall:
            continue

        remain = int(s.get("frSeatCnt") or s.get("cpSeatCnt") or 0)
        total  = int(s.get("stcnt") or 0)

        if remain <= 0:
            continue

        time_raw = s.get("scnsrtTm") or s.get("rlMovStartTm") or "?"
        # "0800" → "08:00"
        time_fmt = f"{time_raw[:2]}:{time_raw[2:]}" if len(time_raw) == 4 else time_raw

        available.append({
            "date": date,
            "time": time_fmt,
            "remain": remain,
            "total": total,
            "hall": hall,
            "session_id": s.get("scnSseq") or s.get("prodNo") or time_raw,
        })
    return available


# ── 날짜 확인 ───────────────────────────────────────────────────────────────

_debug_logged: set = set()


async def check_date(client: httpx.AsyncClient, token: str, date: str) -> tuple[list, str]:
    """
    해당 날짜 스케줄 조회. 토큰 만료 시 갱신 후 재시도.
    Returns (sessions, current_token)
    """
    resp = await fetch_schedule(client, token, date)

    if resp.status_code == 401:
        print(f"[{date}] 401 → 토큰 갱신 시도...", flush=True)
        token = await try_refresh_token(client, token)
        resp = await fetch_schedule(client, token, date)

    if resp.status_code != 200:
        print(f"[{date}] HTTP {resp.status_code}", flush=True)
        return [], token

    body = resp.json()
    status_code = body.get("statusCode")

    # CGV API 성공 코드: 0 (정수)
    if status_code != 0:
        print(f"[{date}] API 오류: {status_code} — {body.get('statusMessage', '')}", flush=True)
        return [], token

    data = body.get("data") or []
    if not isinstance(data, list):
        print(f"[{date}] data 형식 이상: {type(data)}", flush=True)
        return [], token

    first_time = date not in _debug_logged
    if first_time:
        _debug_logged.add(date)
        print(f"[{date}] 응답 {len(data)}개 세션 (전체, IMAX 필터 전)", flush=True)

    sessions = parse_schedule(data, date, debug=first_time)
    return sessions, token


# ── 알림 전송 ───────────────────────────────────────────────────────────────

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


# ── 메인 ────────────────────────────────────────────────────────────────────

async def main():
    if not WATCH_DATES:
        print("오류: .env에 WATCH_DATES를 설정하세요.  예) WATCH_DATES=20260530,20260531")
        sys.exit(1)

    token = load_token()
    if not token:
        print("오류: .env에 BEARER_TOKEN을 설정하세요.")
        print("  브라우저 DevTools → Network → searchSchByMov 요청 → Headers → authorization 값 복사")
        sys.exit(1)

    print("=" * 60)
    print("  CGV 용산IMAX 좌석 오픈 알리미  (직접 API 버전)")
    print(f"  영화: {MOV_NO} | 상영관 siteNo: {SITE_NO}")
    print(f"  감시 날짜: {', '.join(WATCH_DATES)}")
    print(f"  확인 주기: {CHECK_INTERVAL}초")
    print("=" * 60)

    await send_telegram(
        f"🔔 CGV 용산IMAX 알리미 시작\n"
        f"감시 날짜: {', '.join(WATCH_DATES)}"
    )

    async with httpx.AsyncClient() as client:
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
