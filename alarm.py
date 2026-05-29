"""
CGV IMAX 좌석 오픈 알리미

[인증 방식]
- accessToken: 브라우저 쿠키 'accessToken' 에 저장
- x-signature: HMAC-SHA256("{timestamp}|{path}|{body}", SECRET_KEY) → Base64

[Cloudflare 우회]
- curl_cffi 로 Chrome TLS 핑거프린트 흉내

[명당 알림 조건]
- 사용자 지정 열/번호 중 빈 좌석이 새로 생겼을 때만 알림
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

# ── 환경변수 (알림/인증 관련만) ───────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_PROXY      = os.getenv("TELEGRAM_PROXY", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
CHECK_INTERVAL      = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))

CUST_NO      = os.getenv("CUST_NO", "")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "")
CGV_COOKIES  = os.getenv("CGV_COOKIES", "")

TOKEN_FILE  = Path(__file__).parent / "token.json"
CONFIG_FILE = Path(__file__).parent / "config.json"

CGV_API_BASE  = "https://api.cgv.co.kr"
SCHEDULE_PATH = "/cnm/atkt/searchSchByMov"
SEAT_PATH     = "/cnm/atkt/searchIfSeatData"
REISSUE_PATH  = "/com/bznsCom/custKeep/reissueToken"

_HMAC_SECRET = "ydqXY0ocnFLmJGHr_zNzFcpjwAsXq_8JcBNURAkRscg"
_IMPERSONATE = "chrome124"

# 명당 조건 (설정에서 덮어씀)
PRIME_ROWS     = {"F", "G", "H", "I", "J"}
PRIME_SEAT_MIN = 17
PRIME_SEAT_MAX = 28

prime_seat_state: dict[str, set | None] = defaultdict(lambda: None)


# ── config.json 관리 ─────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "mov_no":       "30001210",
    "mov_name":     "미션 임파서블 8",
    "site_no":      "0013",
    "co_cd":        "A420",
    "rtctl_scop_cd":"08",
    "watch_dates":  [],           # ["20260531", "20260601"]
    "watch_times":  [],           # ["08:00", "11:00"] — 비어있으면 전체
    "prime_rows":   ["F","G","H","I","J"],
    "prime_seat_min": 17,
    "prime_seat_max": 28,
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            # 누락된 키는 기본값으로 채움
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 대화형 설정 마법사 ────────────────────────────────────────────────────────

def _input(prompt: str, default: str = "") -> str:
    """입력 받기. Enter만 치면 default 반환"""
    if default:
        val = input(f"{prompt} [{default}]: ").strip()
        return val if val else default
    return input(f"{prompt}: ").strip()


def setup_wizard(cfg: dict) -> dict:
    print("\n" + "─" * 50)
    print("  설정 마법사")
    print("─" * 50)

    # ── 영화 ──────────────────────────────────────────
    print("\n[ 1 ] 영화")
    print("  CGV 예매 페이지 URL에서 movNo= 값을 확인하세요")
    print("  예) cgv.co.kr/ticket?movNo=30001210")
    mov_no   = _input("  영화 번호 (movNo)", cfg["mov_no"])
    mov_name = _input("  영화 이름 (표시용)", cfg["mov_name"])

    # ── 날짜 ──────────────────────────────────────────
    print("\n[ 2 ] 감시 날짜")
    print("  YYYYMMDD 형식, 쉼표로 구분")
    print("  예) 20260531,20260601,20260602")
    default_dates = ",".join(cfg["watch_dates"])
    dates_str = _input("  날짜", default_dates)
    watch_dates = [d.strip() for d in dates_str.split(",") if d.strip()]

    # ── 시간 ──────────────────────────────────────────
    print("\n[ 3 ] 원하는 상영 시간")
    print("  HH:MM 형식, 쉼표로 구분 / 전체 감시는 Enter")
    print("  예) 08:00,11:00,14:00")
    default_times = ",".join(cfg["watch_times"])
    times_str = _input("  시간 (전체면 Enter)", default_times)
    watch_times = [t.strip() for t in times_str.split(",") if t.strip()]

    # ── 명당 좌석 ──────────────────────────────────────
    print("\n[ 4 ] 명당 좌석 조건")
    default_rows = ",".join(sorted(cfg["prime_rows"]))
    rows_str = _input("  감시할 열 (예: F,G,H,I,J)", default_rows)
    prime_rows = [r.strip().upper() for r in rows_str.split(",") if r.strip()]

    seat_min = _input("  시작 번호", str(cfg["prime_seat_min"]))
    seat_max = _input("  끝 번호",   str(cfg["prime_seat_max"]))

    cfg.update({
        "mov_no":         mov_no,
        "mov_name":       mov_name,
        "watch_dates":    watch_dates,
        "watch_times":    watch_times,
        "prime_rows":     prime_rows,
        "prime_seat_min": int(seat_min),
        "prime_seat_max": int(seat_max),
    })

    save_config(cfg)
    print("\n  설정이 저장되었습니다. (config.json)")
    return cfg


def confirm_or_setup(cfg: dict) -> dict:
    """저장된 설정을 보여주고 그대로 쓸지 재설정할지 선택"""
    print("\n" + "=" * 60)
    print("  CGV IMAX 좌석 오픈 알리미")
    print("=" * 60)
    print(f"\n현재 설정:")
    print(f"  영화    : {cfg['mov_name']} ({cfg['mov_no']})")
    dates_disp = ", ".join(cfg["watch_dates"]) if cfg["watch_dates"] else "(없음)"
    times_disp = ", ".join(cfg["watch_times"]) if cfg["watch_times"] else "전체"
    rows_disp  = ", ".join(sorted(cfg["prime_rows"]))
    print(f"  날짜    : {dates_disp}")
    print(f"  시간    : {times_disp}")
    print(f"  명당 조건: {rows_disp}열  {cfg['prime_seat_min']}~{cfg['prime_seat_max']}번")

    print("\n[Enter] 시작  /  [c] 설정 변경: ", end="", flush=True)
    choice = input().strip().lower()
    if choice == "c":
        cfg = setup_wizard(cfg)
    return cfg


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
    kwargs: dict = {"timeout": 10}
    if TELEGRAM_PROXY:
        kwargs["proxies"] = {"https": TELEGRAM_PROXY, "http": TELEGRAM_PROXY}
    try:
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
        await toast_async("🎯 CGV IMAX 명당 오픈!", plain[:150])
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
            args=(0, plain[:300], "🎯 CGV IMAX 명당 오픈!", 0x30 | 0x1000),
            daemon=True,
        ).start()
        print("[윈도우] 팝업 알림 표시", flush=True)
    except Exception as e:
        print(f"[윈도우] 팝업 실패: {e}", flush=True)


async def notify(message: str) -> None:
    await asyncio.gather(
        # send_telegram(message),   # 사내망 차단 - 비활성화
        # send_discord(message),    # 사내망 차단 - 비활성화
        send_windows_alert(message),
    )


# ── 스케줄 파싱 ──────────────────────────────────────────────────────────────

_debug_logged: set = set()


def parse_schedule(data: list, date: str, cfg: dict) -> list:
    """IMAX 세션 반환. watch_times 설정 시 해당 시간만 필터."""
    first = date not in _debug_logged
    if first:
        _debug_logged.add(date)
        if data:
            print(f"[{date}] DEBUG 첫 필드: {list(data[0].keys())[:12]}", flush=True)

    watch_times: set = set(cfg.get("watch_times") or [])
    sessions = []
    for s in data:
        hall = s.get("scnsNm") or s.get("expoScnsNm") or ""
        if "imax" not in hall.lower() and "아이맥스" not in hall:
            continue

        t = s.get("scnsrtTm") or s.get("rlMovStartTm") or "?"
        time_fmt = f"{t[:2]}:{t[2:]}" if len(t) == 4 else t

        if watch_times and time_fmt not in watch_times:
            continue

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

def _is_prime(row: str, seat_no_str: str, cfg: dict) -> bool:
    prime_rows = set(cfg.get("prime_rows") or PRIME_ROWS)
    seat_min   = cfg.get("prime_seat_min", PRIME_SEAT_MIN)
    seat_max   = cfg.get("prime_seat_max", PRIME_SEAT_MAX)
    try:
        return row in prime_rows and seat_min <= int(seat_no_str) <= seat_max
    except (ValueError, TypeError):
        return False


async def fetch_prime_seats(
    client: AsyncSession, token: str,
    date: str, scns_no: str, scn_sseq: str,
    cfg: dict,
) -> set | None:
    params = {
        "coCd":       cfg.get("co_cd", "A420"),
        "siteNo":     cfg.get("site_no", "0013"),
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
            and _is_prime(s.get("seatRowNm", ""), s.get("seatNo", ""), cfg)
        }
    except Exception as e:
        print(f"[좌석상세] 조회 실패: {e}", flush=True)
        return None


# ── 명당 변화 감지 + 알림 ────────────────────────────────────────────────────

def _format_prime_seats(seats: set) -> str:
    by_row: dict[str, list] = defaultdict(list)
    for row, no in seats:
        by_row[row].append(int(no))
    return "\n".join(
        f"  {row}열: {', '.join(str(n) for n in sorted(nums))}번"
        for row, nums in sorted(by_row.items())
    )


async def process_sessions(sessions: list, client: AsyncSession, token: str, cfg: dict) -> None:
    prime_rows = sorted(cfg.get("prime_rows") or PRIME_ROWS)
    seat_min   = cfg.get("prime_seat_min", PRIME_SEAT_MIN)
    seat_max   = cfg.get("prime_seat_max", PRIME_SEAT_MAX)
    mov_name   = cfg.get("mov_name", "")

    for s in sessions:
        if not s.get("scns_no"):
            continue
        session_key = f"{s['date']}_{s['session_id']}"

        current = await fetch_prime_seats(
            client, token,
            s["date"], s["scns_no"], str(s["session_id"]),
            cfg,
        )
        await asyncio.sleep(0.5)

        if current is None:
            continue

        prev    = prime_seat_state[session_key]
        newly   = current if prev is None else (current - prev)
        prime_seat_state[session_key] = current

        print(
            f"  [{s['date']} {s['time']}] 명당 {len(current)}석 (새로 열림: {len(newly)}석)",
            flush=True,
        )

        if not newly:
            continue

        seat_str = _format_prime_seats(newly)
        msg = (
            f"🎯 <b>CGV IMAX 명당 오픈!</b>\n\n"
            f"🎬 {mov_name}\n"
            f"📅 {s['date']}  🕐 {s['time']}\n"
            f"🏛 {s['hall']}\n\n"
            f"✨ <b>새로 열린 명당</b> ({','.join(prime_rows)}열 {seat_min}~{seat_max}번)\n"
            f"{seat_str}\n\n"
            f"💺 명당 총 {len(current)}석 예매가능\n\n"
            f"🔗 <a href='https://cgv.co.kr/ticket'>바로 예매</a>"
        )
        await notify(msg)
        print(f"  → 명당 알림: {s['date']} {s['time']}", flush=True)


# ── 스케줄 API 호출 ──────────────────────────────────────────────────────────

async def fetch_schedule(client: AsyncSession, token: str, date: str, cfg: dict):
    params: dict = {
        "coCd":        cfg.get("co_cd", "A420"),
        "siteNo":      cfg.get("site_no", "0013"),
        "scnYmd":      date,
        "movNo":       cfg.get("mov_no", "30001210"),
        "rtctlScopCd": cfg.get("rtctl_scop_cd", "08"),
    }
    if CUST_NO:
        params["custNo"] = CUST_NO
    return await client.get(
        CGV_API_BASE + SCHEDULE_PATH,
        params=params,
        headers=make_headers(token, SCHEDULE_PATH),
        timeout=15,
    )


async def check_date(client: AsyncSession, token: str, date: str, cfg: dict) -> tuple[list, str]:
    resp = await fetch_schedule(client, token, date, cfg)

    if resp.status_code == 401:
        try:
            body = resp.json()
            if body.get("statusCode") in (-1001, "-1001"):
                token = await reissue_token(client, token)
                resp  = await fetch_schedule(client, token, date, cfg)
        except Exception:
            pass

    if resp.status_code != 200:
        try:
            body_text = resp.text[:400]
        except Exception:
            body_text = ""
        cf_ray = resp.headers.get("cf-ray", "")
        server = resp.headers.get("server", "")
        print(f"[{date}] HTTP {resp.status_code}  server={server}  cf-ray={cf_ray}", flush=True)
        print(f"[{date}] 응답: {body_text}", flush=True)
        return [], token

    body = resp.json()
    if body.get("statusCode") != 0:
        print(f"[{date}] API 오류: {body.get('statusMessage','')}", flush=True)
        return [], token

    data = body.get("data") or []
    if not isinstance(data, list):
        return [], token

    sessions = parse_schedule(data, date, cfg)
    print(f"[{date}] IMAX 세션 {len(sessions)}개", flush=True)
    return sessions, token


# ── 메인 ─────────────────────────────────────────────────────────────────────

async def main():
    # 설정 로드 → 확인/변경
    cfg = load_config()
    cfg = confirm_or_setup(cfg)

    if not cfg["watch_dates"]:
        print("\n오류: 날짜를 설정해주세요. [c]로 재설정 후 실행하세요.")
        sys.exit(1)

    token = load_token()
    if not token:
        print("오류: .env에 ACCESS_TOKEN 설정 필요")
        sys.exit(1)

    rows_disp  = ", ".join(sorted(cfg["prime_rows"]))
    times_disp = ", ".join(cfg["watch_times"]) if cfg["watch_times"] else "전체"

    print("\n" + "=" * 60)
    print(f"  영화    : {cfg['mov_name']} ({cfg['mov_no']})")
    print(f"  날짜    : {', '.join(cfg['watch_dates'])}")
    print(f"  시간    : {times_disp}")
    print(f"  명당 조건: {rows_disp}열  {cfg['prime_seat_min']}~{cfg['prime_seat_max']}번")
    print(f"  확인 주기: {CHECK_INTERVAL}초")
    if platform.system() == "Windows":
        try:
            import win11toast  # noqa: F401
            print("  윈도우 알림: 토스트 + 경고음")
        except ImportError:
            print("  윈도우 알림: 팝업 + 경고음")
    print("=" * 60)

    await notify(
        f"🔔 CGV IMAX 알리미 시작\n"
        f"🎬 {cfg['mov_name']}\n"
        f"📅 날짜: {', '.join(cfg['watch_dates'])}\n"
        f"🕐 시간: {times_disp}\n"
        f"🎯 명당: {rows_disp}열 {cfg['prime_seat_min']}~{cfg['prime_seat_max']}번"
    )

    async with AsyncSession(impersonate=_IMPERSONATE) as client:
        round_num = 0
        while True:
            round_num += 1
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n[{now}] 라운드 {round_num}", flush=True)

            for date in cfg["watch_dates"]:
                try:
                    sessions, token = await check_date(client, token, date, cfg)
                    if sessions:
                        await process_sessions(sessions, client, token, cfg)
                    else:
                        print(f"  [{date}] 해당 세션 없음", flush=True)
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
