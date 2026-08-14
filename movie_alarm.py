"""
CGV 용산아이파크몰 IMAX 신규 영화/회차 오픈 알리미

오늘 ~ 14일 후까지 전체 IMAX 상영 목록을 감시합니다.
새로운 영화 또는 새로운 날짜/시간 회차가 열리면 즉시 알림을 보냅니다.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import platform
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from curl_cffi.requests import AsyncSession
from dotenv import load_dotenv

load_dotenv()

# ── 환경변수 ─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_PROXY      = os.getenv("TELEGRAM_PROXY", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
CHECK_INTERVAL      = int(os.getenv("NEW_MOVIE_CHECK_INTERVAL_SECONDS", "5"))

ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "")
CGV_COOKIES  = os.getenv("CGV_COOKIES", "")
CUST_NO      = os.getenv("CUST_NO", "")

TOKEN_FILE  = Path(__file__).parent / "token.json"
STATE_FILE  = Path(__file__).parent / "movie_state.json"

CGV_API_BASE  = "https://api.cgv.co.kr"
MOV_SCN_PATH  = "/cnm/atkt/searchMovScnInfo"
REISSUE_PATH  = "/com/bznsCom/custKeep/reissueToken"

_HMAC_SECRET = "ydqXY0ocnFLmJGHr_zNzFcpjwAsXq_8JcBNURAkRscg"
_IMPERSONATE = "chrome124"

# 용산아이파크몰 CGV 고정값
CO_CD         = "A420"
SITE_NO       = "0013"
RTCTL_SCOP_CD = "08"

WATCH_DAYS = 20  # 오늘부터 감시할 일수


# ── 날짜 목록 생성 ────────────────────────────────────────────────────────────

def get_date_range(days: int) -> list[str]:
    today = datetime.now()
    return [(today + timedelta(days=i)).strftime("%Y%m%d") for i in range(days)]


# ── 상태 저장/로드 ────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 서명 / 헤더 ──────────────────────────────────────────────────────────────

def make_signature(path: str, body: str, timestamp: str) -> str:
    message = f"{timestamp}|{path}|{body}"
    raw = hmac.new(
        _HMAC_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(raw).decode("utf-8")


def make_headers(token: str, path: str) -> dict:
    ts  = str(math.floor(time.time()))
    sig = make_signature(path, "", ts)
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


# ── 토큰 로드/갱신 ────────────────────────────────────────────────────────────

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
                **make_headers(current_token, REISSUE_PATH),
                "content-type": "application/json",
            },
            timeout=10,
        )
        body = resp.json()
        new_token = (body.get("data") or {}).get("accessToken")
        if new_token:
            TOKEN_FILE.write_text(json.dumps({"accessToken": new_token}))
            print("[토큰] 갱신 성공!", flush=True)
            return new_token
    except Exception as e:
        print(f"[토큰] 갱신 실패: {e}", flush=True)
    return current_token


# ── 알림 채널 ────────────────────────────────────────────────────────────────

async def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        kwargs: dict = {"timeout": 10}
        if TELEGRAM_PROXY:
            kwargs["proxies"] = {"https": TELEGRAM_PROXY, "http": TELEGRAM_PROXY}
        async with AsyncSession(impersonate=_IMPERSONATE) as s:
            resp = await s.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
                **kwargs,
            )
        if resp.status_code == 200:
            print("[텔레그램] 전송 완료", flush=True)
        else:
            print(f"[텔레그램] 전송 실패: HTTP {resp.status_code}", flush=True)
    except Exception as e:
        print(f"[텔레그램] 전송 실패: {e}", flush=True)


async def send_windows_alert(title: str, body: str) -> None:
    if platform.system() != "Windows":
        return
    try:
        import winsound
        loop = asyncio.get_event_loop()
        for _ in range(3):
            await loop.run_in_executor(None, winsound.Beep, 880, 300)
            await asyncio.sleep(0.2)
    except Exception:
        pass
    try:
        from win11toast import toast_async
        await toast_async(title, body)
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
            args=(0, f"{title}\n\n{body}"[:400], "🎬 CGV IMAX 신규 오픈!", 0x30 | 0x1000),
            daemon=True,
        ).start()
    except Exception:
        pass


async def notify_initial_status(current_all: dict) -> None:
    """첫 실행 시 오늘자 IMAX 현황 알림"""
    today = datetime.now().strftime("%Y%m%d")

    # 오늘 날짜 세션만 필터링, 영화별 그룹핑
    by_movie: dict[str, list] = {}
    for info in current_all.values():
        if info["date"] == today:
            by_movie.setdefault(info["movNm"], []).append(info)

    dow = ["월","화","수","목","금","토","일"][datetime.now().weekday()]

    if not by_movie:
        toast_body = f"오늘({today}) IMAX 상영 없음\n신규 오픈 감시 중..."
        await asyncio.gather(
            # send_telegram(...)  # 사내망 차단 - 비활성화
            send_windows_alert("🎬 IMAX 알리미 시작", toast_body),
        )
        return

    # 토스트: 영화별 "영화명\nHH:MM 잔여 N석 / HH:MM 잔여 N석" 한 블록씩
    toast_lines = []
    for mov_nm, sessions in sorted(by_movie.items()):
        times = " / ".join(
            f"{s['time']} 잔여 {s['fr_seat']}석"
            for s in sorted(sessions, key=lambda x: x["time"])
        )
        toast_lines.append(f"{mov_nm}\n{times}")
    toast_body = "\n\n".join(toast_lines)

    await asyncio.gather(
        # send_telegram(msg)  # 사내망 차단 - 비활성화
        send_windows_alert(f"🎬 IMAX 현황 — 오늘 {today} ({dow})", toast_body),
    )


async def notify_new_movie(mov_nm: str, new_sessions: list) -> None:
    """새 영화/회차 알림"""
    lines = []
    for s in sorted(new_sessions, key=lambda x: (x["date"], x["time"])):
        dow = ["월","화","수","목","금","토","일"][
            datetime.strptime(s["date"], "%Y%m%d").weekday()
        ]
        lines.append(
            f"  📅 {s['date']} ({dow}) 🕐 {s['time']}"
            f"  |  잔여 <b>{s['fr_seat']}석</b> / 총 {s['total']}석"
        )

    sessions_text = "\n".join(lines)
    msg = (
        f"🎬 <b>IMAX 신규 오픈!</b>\n\n"
        f"<b>{mov_nm}</b>\n"
        f"🏛 CGV 용산아이파크몰 IMAX\n\n"
        f"{sessions_text}"
    )

    # 토스트용 요약
    first = new_sessions[0]
    toast_body = (
        f"{first['date']} {first['time']} 외 {len(new_sessions)-1}회차\n"
        f"잔여 {first['fr_seat']}석"
    ) if len(new_sessions) > 1 else (
        f"{first['date']} {first['time']}  잔여 {first['fr_seat']}석"
    )

    await asyncio.gather(
        # send_telegram(msg),  # 사내망 차단 - 비활성화
        send_windows_alert(f"🎬 IMAX 신규 오픈 — {mov_nm}", toast_body),
    )


# ── 상영 목록 조회 ────────────────────────────────────────────────────────────

async def fetch_movie_list(
    client: AsyncSession, token: str, date: str
) -> tuple[list | None, str]:
    params: dict = {
        "coCd":        CO_CD,
        "siteNo":      SITE_NO,
        "scnYmd":      date,
        "rtctlScopCd": RTCTL_SCOP_CD,
    }
    if CUST_NO:
        params["custNo"] = CUST_NO

    resp = await client.get(
        CGV_API_BASE + MOV_SCN_PATH,
        params=params,
        headers=make_headers(token, MOV_SCN_PATH),
        timeout=15,
    )

    if resp.status_code == 401:
        token = await reissue_token(client, token)
        resp = await client.get(
            CGV_API_BASE + MOV_SCN_PATH,
            params=params,
            headers=make_headers(token, MOV_SCN_PATH),
            timeout=15,
        )

    if resp.status_code != 200:
        print(f"[{date}] HTTP {resp.status_code}  cf-ray={resp.headers.get('cf-ray','')}", flush=True)
        return None, token

    body = resp.json()
    if body.get("statusCode") != 0:
        return None, token

    data = body.get("data")
    return (data if isinstance(data, list) else None), token


def parse_imax_sessions(sessions_flat: list) -> dict[str, dict]:
    """
    IMAX 세션만 추출.
    반환: {session_key: {movNo, movNm, date, time, hall, fr_seat, total}}
    session_key = "{movNo}_{date}_{time}"
    """
    result = {}
    for s in sessions_flat:
        hall = s.get("scnsNm") or s.get("expoScnsNm") or ""
        if "imax" not in hall.lower() and "아이맥스" not in hall:
            continue

        mov_no = s.get("movNo", "")
        if not mov_no:
            continue

        t = s.get("scnsrtTm") or "????"
        time_fmt = f"{t[:2]}:{t[2:]}" if len(t) == 4 else t
        date = s.get("scnYmd", "")
        key = f"{mov_no}_{date}_{time_fmt}"

        result[key] = {
            "movNo":   mov_no,
            "movNm":   s.get("movNm") or s.get("expoProdNm") or mov_no,
            "date":    date,
            "time":    time_fmt,
            "hall":    hall,
            "fr_seat": int(s.get("frSeatCnt") or 0),
            "total":   int(s.get("stcnt") or 0),
        }
    return result


# ── 메인 루프 ─────────────────────────────────────────────────────────────────

async def main():
    token = load_token()
    if not token:
        print("오류: .env에 ACCESS_TOKEN 설정 필요")
        return

    # 매 실행 시 상태 초기화 (오늘 현황 알림 → 이후 신규 오픈만 감지)
    state: dict = {}
    if STATE_FILE.exists():
        STATE_FILE.unlink()

    print("\n" + "=" * 60)
    print("  CGV 용산아이파크몰 IMAX 신규 영화 알리미")
    print(f"  감시 범위: 오늘 ~ {WATCH_DAYS}일 후")
    print(f"  확인 주기: {CHECK_INTERVAL}초")
    print("=" * 60)
    print("\n  상태 초기화 완료. 오늘자 현황 알림 후 감시 시작.\n", flush=True)

    async with AsyncSession(impersonate=_IMPERSONATE) as client:
        round_num = 0
        while True:
            round_num += 1
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{now}] 라운드 {round_num}", flush=True)

            dates = get_date_range(WATCH_DAYS)
            current_all: dict[str, dict] = {}

            for date in dates:
                flat, token = await fetch_movie_list(client, token, date)
                if flat is None:
                    print(f"  [{date}] 조회 실패", flush=True)
                    await asyncio.sleep(1)
                    continue
                sessions = parse_imax_sessions(flat)
                current_all.update(sessions)
                await asyncio.sleep(0.3)

            print(f"  전체 IMAX 세션: {len(current_all)}개", flush=True)

            if not state:
                # 첫 라운드: 오늘 현황 알림 후 상태 기록
                await notify_initial_status(current_all)
                state = {k: True for k in current_all}
                save_state(state)
                print("  초기화 완료. 이제부터 신규 오픈 감시 시작.\n", flush=True)
            else:
                # 새로 등장한 세션 찾기
                new_keys = set(current_all.keys()) - set(state.keys())

                if new_keys:
                    # 영화별로 그룹핑
                    by_movie: dict[str, list] = {}
                    for key in new_keys:
                        info = current_all[key]
                        mov_nm = info["movNm"]
                        by_movie.setdefault(mov_nm, []).append(info)

                    for mov_nm, sessions in by_movie.items():
                        print(f"  🎬 신규 오픈: {mov_nm} ({len(sessions)}회차)", flush=True)
                        for s in sessions:
                            print(f"    {s['date']} {s['time']}  잔여 {s['fr_seat']}석", flush=True)
                        await notify_new_movie(mov_nm, sessions)

                    # 상태 업데이트
                    for key in new_keys:
                        state[key] = True
                    save_state(state)
                else:
                    print("  신규 오픈 없음", flush=True)

            print(f"[대기] {CHECK_INTERVAL}초 후 재확인...\n", flush=True)
            await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n종료합니다.")
