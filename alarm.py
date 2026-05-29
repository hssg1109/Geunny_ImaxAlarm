"""
CGV IMAX 좌석 오픈 알리미

[흐름]
1. 날짜 입력
2. searchMovScnInfo → IMAX 영화 목록 표시
3. 영화 선택 → 해당 영화 상영 시간 목록 표시
4. 시간 선택 → 명당 조건 확인
5. 명당 좌석이 새로 열릴 때마다 Windows 알림

[인증]
- accessToken: 브라우저 쿠키 'accessToken'
- x-signature: HMAC-SHA256("{timestamp}|{path}|{body}", SECRET_KEY) → Base64

[Cloudflare 우회]
- curl_cffi Chrome TLS 핑거프린트
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

# ── 환경변수 ─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_PROXY      = os.getenv("TELEGRAM_PROXY", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
CHECK_INTERVAL      = int(os.getenv("CHECK_INTERVAL_SECONDS", "3"))

CUST_NO      = os.getenv("CUST_NO", "")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "")
CGV_COOKIES  = os.getenv("CGV_COOKIES", "")

TOKEN_FILE  = Path(__file__).parent / "token.json"
CONFIG_FILE = Path(__file__).parent / "config.json"

CGV_API_BASE  = "https://api.cgv.co.kr"
MOV_SCN_PATH  = "/cnm/atkt/searchMovScnInfo"   # 날짜별 전체 상영 목록
SCHEDULE_PATH = "/cnm/atkt/searchSchByMov"      # 영화별 상영 목록 (감시 루프용)
SEAT_PATH     = "/cnm/atkt/searchIfSeatData"    # 좌석 상세
REISSUE_PATH  = "/com/bznsCom/custKeep/reissueToken"

_HMAC_SECRET = "ydqXY0ocnFLmJGHr_zNzFcpjwAsXq_8JcBNURAkRscg"
_IMPERSONATE = "chrome124"

DEFAULT_CONFIG = {
    "mov_no":         "30001210",
    "mov_name":       "미션 임파서블 8",
    "site_no":        "0013",
    "co_cd":          "A420",
    "rtctl_scop_cd":  "08",
    "watch_dates":    [],
    "watch_times":    [],
    "prime_rows":     ["F", "G", "H", "I", "J", "K", "L"],
    "prime_seat_min": 17,
    "prime_seat_max": 28,
}



# ── config.json ───────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 서명 / 헤더 ──────────────────────────────────────────────────────────────

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


async def send_windows_alert(
    message: str,
    toast_title: str = "",
    toast_body: str = "",
) -> None:
    if platform.system() != "Windows":
        return
    plain = re.sub(r"<[^>]+>", "", message).strip()
    title = toast_title or "🎯 CGV IMAX 명당 오픈!"
    body  = toast_body  or plain
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
            args=(0, f"{title}\n\n{body}"[:400], "🎯 CGV IMAX 명당 오픈!", 0x30 | 0x1000),
            daemon=True,
        ).start()
        print("[윈도우] 팝업 알림 표시", flush=True)
    except Exception as e:
        print(f"[윈도우] 팝업 실패: {e}", flush=True)


async def notify(
    message: str,
    toast_title: str = "",
    toast_body: str = "",
) -> None:
    await asyncio.gather(
        # send_telegram(message),   # 사내망 차단 - 비활성화
        send_discord(message),
        send_windows_alert(message, toast_title, toast_body),
    )


# ── searchMovScnInfo: 날짜별 전체 상영 목록 ──────────────────────────────────

async def fetch_movie_list(
    client: AsyncSession, token: str, date: str, cfg: dict
) -> tuple[list | None, str]:
    params: dict = {
        "coCd":        cfg.get("co_cd", "A420"),
        "siteNo":      cfg.get("site_no", "0013"),
        "scnYmd":      date,
        "rtctlScopCd": cfg.get("rtctl_scop_cd", "08"),
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
        cf = resp.headers.get("cf-ray", "")
        print(f"[상영목록] HTTP {resp.status_code}  cf-ray={cf}", flush=True)
        return None, token

    body = resp.json()
    if body.get("statusCode") != 0:
        print(f"[상영목록] API 오류: {body.get('statusMessage', '')}", flush=True)
        return None, token

    data = body.get("data")
    return (data if isinstance(data, list) else None), token


def _is_imax_hall(hall: str) -> bool:
    return "imax" in hall.lower() or "아이맥스" in hall


def parse_imax_movies(sessions_flat: list) -> list:
    """
    평면 세션 리스트에서 IMAX 세션만 추출하고 영화별로 그룹핑.
    반환: [{movNo, movNm, movEnm, sessions: [{date, time, hall, scns_no, session_id, total, fr_seat}]}]
    """
    movies: dict[str, dict] = {}

    for s in sessions_flat:
        hall = s.get("scnsNm") or s.get("expoScnsNm") or ""
        if not _is_imax_hall(hall):
            continue

        mov_no = s.get("movNo", "")
        if not mov_no:
            continue

        if mov_no not in movies:
            movies[mov_no] = {
                "movNo":  mov_no,
                "movNm":  s.get("movNm") or s.get("expoProdNm") or mov_no,
                "movEnm": s.get("movEnm") or s.get("engProdNm") or "",
                "sessions": [],
            }

        t = s.get("scnsrtTm") or "????"
        time_fmt = f"{t[:2]}:{t[2:]}" if len(t) == 4 else t

        movies[mov_no]["sessions"].append({
            "date":       s.get("scnYmd", ""),
            "time":       time_fmt,
            "hall":       hall,
            "scns_no":    s.get("scnsNo", ""),
            "session_id": s.get("scnSseq", ""),
            "total":      int(s.get("stcnt") or 0),
            "fr_seat":    int(s.get("frSeatCnt") or 0),
        })

    for m in movies.values():
        m["sessions"].sort(key=lambda x: x["time"])

    return sorted(movies.values(), key=lambda x: x["movNm"])


# ── 대화형 설정: 날짜 → 영화 선택 → 시간 선택 → 명당 조건 ─────────────────

def _input(prompt: str, default: str = "") -> str:
    if default:
        val = input(f"{prompt} [{default}]: ").strip()
        return val if val else default
    return input(f"{prompt}: ").strip()


async def interactive_setup(
    client: AsyncSession, token: str, cfg: dict
) -> tuple[dict, str]:
    print("\n" + "=" * 60)
    print("  CGV IMAX 좌석 오픈 알리미")
    print("=" * 60)

    # ── Step 1: 감시 날짜 ────────────────────────────────────────
    print("\n[ 1 ] 날짜 입력")
    print("  YYYYMMDD 형식, 쉼표로 구분")
    print("  예) 20260531,20260601")
    saved_dates = ",".join(cfg.get("watch_dates") or [])
    while True:
        dates_str = _input("  날짜", saved_dates)
        dates = [d.strip() for d in dates_str.split(",") if re.fullmatch(r"\d{8}", d.strip())]
        if dates:
            break
        print("  YYYYMMDD 형식으로 입력해주세요.")

    ref_date = dates[0]

    # ── Step 2: IMAX 영화 목록 조회 ──────────────────────────────
    print(f"\n  [{ref_date}] 상영 목록 조회 중...", flush=True)
    sessions_flat, token = await fetch_movie_list(client, token, ref_date, cfg)

    if sessions_flat is None:
        print("  상영 목록 조회 실패. ACCESS_TOKEN을 확인하세요.")
        sys.exit(1)

    imax_movies = parse_imax_movies(sessions_flat)
    if not imax_movies:
        print(f"  {ref_date}에 IMAX 상영 영화가 없습니다.")
        sys.exit(1)

    # ── Step 3: 영화 선택 ────────────────────────────────────────
    print(f"\n[ 2 ] IMAX 영화 목록 ({ref_date})")
    for i, m in enumerate(imax_movies, 1):
        cnt = len(m["sessions"])
        enm = f"  ({m['movEnm']})" if m["movEnm"] else ""
        print(f"  [{i}] {m['movNm']}{enm}  — {cnt}회차")

    while True:
        raw = input("\n  영화 번호 선택: ").strip()
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(imax_movies):
                break
        except ValueError:
            pass
        print("  올바른 번호를 입력하세요.")

    selected = imax_movies[idx]
    cfg["mov_no"]   = selected["movNo"]
    cfg["mov_name"] = selected["movNm"]

    # ── Step 4: 상영 시간 선택 ───────────────────────────────────
    sessions = selected["sessions"]
    print(f"\n[ 3 ] {selected['movNm']} — 상영 시간")
    for i, s in enumerate(sessions, 1):
        print(f"  [{i}] {s['time']}  {s['hall']}  (잔여 {s['fr_seat']}석 / 총 {s['total']}석)")
    print(f"  [전체] 모든 시간 감시")

    raw_times = input("\n  시간 번호 선택 (쉼표로 구분, 전체면 Enter): ").strip()
    if raw_times:
        watch_times = []
        for t in raw_times.split(","):
            try:
                ti = int(t.strip()) - 1
                if 0 <= ti < len(sessions):
                    tm = sessions[ti]["time"]
                    if tm not in watch_times:
                        watch_times.append(tm)
            except ValueError:
                pass
    else:
        watch_times = []

    cfg["watch_dates"] = dates
    cfg["watch_times"] = watch_times

    # ── Step 5: 명당 조건 확인 ───────────────────────────────────
    rows_disp = ",".join(sorted(cfg["prime_rows"]))
    print(f"\n[ 4 ] 명당 좌석 조건")
    print(f"  현재: {rows_disp}열  {cfg['prime_seat_min']}~{cfg['prime_seat_max']}번")
    change = input("  변경하려면 [c], 그대로면 Enter: ").strip().lower()
    if change == "c":
        rows_str = _input("  감시할 열 (예: F,G,H,I,J)", rows_disp)
        cfg["prime_rows"] = [r.strip().upper() for r in rows_str.split(",") if r.strip()]
        seat_min = _input("  시작 번호", str(cfg["prime_seat_min"]))
        seat_max = _input("  끝 번호",   str(cfg["prime_seat_max"]))
        cfg["prime_seat_min"] = int(seat_min)
        cfg["prime_seat_max"] = int(seat_max)

    save_config(cfg)
    print("\n  설정 저장 완료 (config.json)")
    return cfg, token


# ── 스케줄 파싱 (searchSchByMov 응답용) ──────────────────────────────────────

_debug_logged: set = set()


def parse_schedule(data: list, date: str, cfg: dict) -> list:
    first = date not in _debug_logged
    if first:
        _debug_logged.add(date)
        if data:
            print(f"[{date}] DEBUG 첫 필드: {list(data[0].keys())[:12]}", flush=True)

    watch_times: set = set(cfg.get("watch_times") or [])
    sessions = []
    for s in data:
        hall = s.get("scnsNm") or s.get("expoScnsNm") or ""
        if not _is_imax_hall(hall):
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
    prime_rows = set(cfg.get("prime_rows") or [])
    seat_min   = cfg.get("prime_seat_min", 17)
    seat_max   = cfg.get("prime_seat_max", 28)
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


# ── 명당 감지 + 알림 ─────────────────────────────────────────────────────────

def _format_prime_seats(seats: set) -> str:
    by_row: dict[str, list] = defaultdict(list)
    for row, no in seats:
        by_row[row].append(int(no))
    return "\n".join(
        f"  {row}열: {', '.join(str(n) for n in sorted(nums))}번"
        for row, nums in sorted(by_row.items())
    )


async def process_sessions(
    sessions: list, client: AsyncSession, token: str, cfg: dict
) -> None:
    prime_rows = sorted(cfg.get("prime_rows") or [])
    seat_min   = cfg.get("prime_seat_min", 17)
    seat_max   = cfg.get("prime_seat_max", 28)
    mov_name   = cfg.get("mov_name", "")

    for s in sessions:
        if not s.get("scns_no"):
            continue

        current = await fetch_prime_seats(
            client, token,
            s["date"], s["scns_no"], str(s["session_id"]),
            cfg,
        )
        await asyncio.sleep(0.5)

        if current is None:
            continue

        print(
            f"  [{s['date']} {s['time']}] 명당 {len(current)}석 예매 가능",
            flush=True,
        )

        if not current:
            continue

        seat_str = _format_prime_seats(current)

        # 토스트용 한 줄 좌석 요약: "G열 19·20번 / H열 22번"
        by_row_c: dict[str, list] = defaultdict(list)
        for row, no in current:
            by_row_c[row].append(int(no))
        seat_compact = " / ".join(
            f"{row}열 {'·'.join(str(n) for n in sorted(nums))}번"
            for row, nums in sorted(by_row_c.items())
        )

        toast_title = f"🎯 명당 오픈 — {mov_name}"
        toast_body  = (
            f"📅 {s['date']}  🕐 {s['time']}  |  {s['hall']}\n"
            f"빈 좌석: {seat_compact}\n"
            f"💺 명당 {len(current)}석 예매 가능"
        )

        msg = (
            f"🎯 <b>CGV IMAX 명당!</b>\n\n"
            f"🎬 {mov_name}\n"
            f"📅 {s['date']}  🕐 {s['time']}\n"
            f"🏛 {s['hall']}\n\n"
            f"✨ <b>빈 명당</b> ({','.join(prime_rows)}열 {seat_min}~{seat_max}번)\n"
            f"{seat_str}\n\n"
            f"💺 명당 총 {len(current)}석 예매가능\n\n"
            f"🔗 <a href='https://cgv.co.kr/ticket'>바로 예매</a>"
        )
        await notify(msg, toast_title=toast_title, toast_body=toast_body)
        print(f"  → 명당 알림: {s['date']} {s['time']}", flush=True)


# ── searchSchByMov: 감시 루프용 스케줄 조회 ──────────────────────────────────

async def fetch_schedule(
    client: AsyncSession, token: str, date: str, cfg: dict
):
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


async def check_date(
    client: AsyncSession, token: str, date: str, cfg: dict
) -> tuple[list, str]:
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
        cf_ray = resp.headers.get("cf-ray", "")
        server = resp.headers.get("server", "")
        print(f"[{date}] HTTP {resp.status_code}  server={server}  cf-ray={cf_ray}", flush=True)
        try:
            print(f"[{date}] 응답: {resp.text[:300]}", flush=True)
        except Exception:
            pass
        return [], token

    body = resp.json()
    if body.get("statusCode") != 0:
        print(f"[{date}] API 오류: {body.get('statusMessage', '')}", flush=True)
        return [], token

    data = body.get("data") or []
    if not isinstance(data, list):
        return [], token

    sessions = parse_schedule(data, date, cfg)
    print(f"[{date}] IMAX 세션 {len(sessions)}개", flush=True)
    return sessions, token


# ── 메인 ─────────────────────────────────────────────────────────────────────

async def main():
    token = load_token()
    if not token:
        print("오류: .env에 ACCESS_TOKEN 설정 필요")
        sys.exit(1)

    cfg = load_config()

    async with AsyncSession(impersonate=_IMPERSONATE) as client:
        cfg, token = await interactive_setup(client, token, cfg)

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
