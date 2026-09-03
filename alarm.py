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
from urllib.parse import quote

from curl_cffi.requests import AsyncSession
from dotenv import load_dotenv

load_dotenv()

# ── 환경변수 ─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_PROXY      = os.getenv("TELEGRAM_PROXY", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
CHECK_INTERVAL      = int(os.getenv("CHECK_INTERVAL_SECONDS", "1"))

CUST_NO      = os.getenv("CUST_NO", "")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "")
CGV_COOKIES  = os.getenv("CGV_COOKIES", "")

TOKEN_FILE          = Path(__file__).parent / "token.json"
CONFIG_FILE         = Path(__file__).parent / "config.json"
INTERVAL_STATE_FILE = Path(__file__).parent / "alarm_interval_tune.json"

# ── 적응형 인터벌 튜닝 (429 발생 시 원복) ─────────────────────────────────────
RATE_LIMIT_WAIT           = 90   # 429 발생 시 대기 시간(초)
MIN_CHECK_INTERVAL        = 0.2  # 목표 하한(초) — 실제 레이트리밋에 걸릴 때까지 이 값까지 줄여본다
MAX_CHECK_INTERVAL        = 30   # 안전 상한(초)
DECREASE_STEP             = 0.2  # 안정적일 때 줄이는 폭(초) — 1초 미만 구간도 세밀하게 탐색
SAFETY_MARGIN             = 0.5  # 429 이후 확정값에 얹는 여유분(초)
STABLE_ROUNDS_TO_DECREASE = 20   # 이만큼 연속 무사고면 인터벌 축소 시도


class RateLimitError(Exception):
    pass

CGV_API_BASE  = "https://api.cgv.co.kr"
MOV_SCN_PATH  = "/cnm/atkt/searchMovScnInfo"   # 날짜별 전체 상영 목록
SCHEDULE_PATH = "/cnm/atkt/searchSchByMov"      # 영화별 상영 목록 (감시 루프용)
SEAT_PATH     = "/cnm/atkt/searchIfSeatData"    # 좌석 상세
REISSUE_PATH  = "/com/bznsCom/custKeep/reissueToken"

_HMAC_SECRET = "ydqXY0ocnFLmJGHr_zNzFcpjwAsXq_8JcBNURAkRscg"
_IMPERSONATE = "chrome146"

PAGES_BASE_URL = "https://hssg1109.github.io/Geunny_ImaxAlarm"  # cgv:// 리다이렉트 페이지 (GitHub Pages)

DEFAULT_CONFIG = {
    "mov_no":         "30001210",
    "mov_name":       "미션 임파서블 8",
    "site_no":        "0013",
    "co_cd":          "A420",
    "rtctl_scop_cd":  "08",
    "watch_dates":    [],
    "watch_times":    [],
    "watch_new_sessions": {},  # {date: {"mode": "all"} 또는 {"mode": "range", "start": "17:00", "end": "22:00"}}
    "new_session_row_priority": ["G", "H", "F", "I", "J", "K", "L", "M"],  # 신규 회차 전용 열 우선순위
    "new_session_center_seats": [22, 23],  # 신규 회차 전용 — 이 좌석번호 중앙에서 가까운 순으로 시도
    "prime_rows":     ["F", "G", "H", "I", "J", "K", "L"],
    "prime_seat_min": 17,
    "prime_seat_max": 28,
    "auto_book":        False,  # True면 2연석 감지 시 browser_booking.py(Playwright)로 자동예매 시도
    "auto_book_armed":  True,   # 자동예매 1회 성공 시 False로 바뀜 (중복예매 방지)
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


def load_interval_state() -> dict:
    if INTERVAL_STATE_FILE.exists():
        try:
            return json.loads(INTERVAL_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "interval": CHECK_INTERVAL,
        "last_known_good": CHECK_INTERVAL,
        "consecutive_ok": 0,
        "locked": False,
    }


def save_interval_state(state: dict) -> None:
    INTERVAL_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


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
        # send_discord(message),    # 사내망 차단 - 비활성화
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

    # ── Step 0: 저장된 설정 재사용 여부 ─────────────────────────────
    # config.json을 손으로 수정해둔 경우(watch_new_sessions 등) 매번 새로 물어보면서
    # 덮어쓰지 않도록, 기존 설정이 있으면 그대로 쓸지 먼저 물어본다.
    if CONFIG_FILE.exists() and cfg.get("mov_no") and cfg.get("watch_dates"):
        print(f"\n  저장된 설정: {cfg.get('mov_name', '')} ({cfg['mov_no']})")
        wt_disp = cfg.get("watch_times") or {}
        nwt_disp = cfg.get("watch_new_sessions") or {}
        for d in cfg["watch_dates"]:
            times = wt_disp.get(d) if isinstance(wt_disp, dict) else None
            times_str = ", ".join(times) if times else "전체"
            print(f"    {d}: {times_str}", end="")
            nr = nwt_disp.get(d)
            if nr:
                extra = "전체" if nr.get("mode") == "all" else f"{nr.get('start')}~{nr.get('end')}"
                print(f"  (+신규회차: {extra})", end="")
            print()
        use_saved = input("\n  이 설정 그대로 시작할까요? (Y/n): ").strip().lower()
        if use_saved in ("", "y", "yes"):
            print("  저장된 설정으로 시작합니다.")
            return cfg, token

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

    # ── Step 2: IMAX 영화 목록 조회 ──────────────────────────────
    # 입력한 날짜 중 상영 정보가 있는 첫 날짜를 찾는다 (dates[0]으로 고정하면, 신규
    # 오픈만 노리는 미래 날짜를 맨 앞에 입력했을 때 아직 상영정보가 없어서 죽어버린다).
    ref_date = None
    sessions_flat = None
    imax_movies: list = []
    for d in dates:
        print(f"\n  [{d}] 상영 목록 조회 중...", flush=True)
        flat, token = await fetch_movie_list(client, token, d, cfg)
        if flat is None:
            print(f"  [{d}] 조회 실패")
            continue
        movies = parse_imax_movies(flat)
        if movies:
            ref_date, sessions_flat, imax_movies = d, flat, movies
            break
        print(f"  [{d}] IMAX 상영 영화 없음 (아직 미오픈일 수 있음) — 다음 날짜 확인")

    # ── Step 3: 영화 선택 ────────────────────────────────────────
    if imax_movies:
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
    else:
        # 입력한 날짜 전부 아직 상영정보가 없음(전부 신규오픈 전용 미래 날짜인 경우).
        # 영화 목록에서 고를 수가 없으니, 저장된 영화를 재사용하거나 movNo를 직접 입력받는다.
        print("\n[ 2 ] 입력한 날짜 전부 아직 상영정보가 없습니다.")
        if cfg.get("mov_no") and cfg.get("mov_name"):
            use_prev = input(
                f"  저장된 영화 '{cfg['mov_name']}'({cfg['mov_no']})를 그대로 쓸까요? (Y/n): "
            ).strip().lower()
            if use_prev not in ("", "y", "yes"):
                mov_no_in = _input("  영화 번호(movNo) 직접 입력", cfg.get("mov_no", ""))
                cfg["mov_no"] = mov_no_in
                cfg["mov_name"] = mov_no_in
        else:
            mov_no_in = input("  영화 번호(movNo) 직접 입력: ").strip()
            cfg["mov_no"] = mov_no_in
            cfg["mov_name"] = mov_no_in
        selected = {"movNo": cfg["mov_no"], "movNm": cfg["mov_name"]}

    # ── Step 4: 날짜별 상영 시간 선택 ──────────────────────────────
    mov_no = cfg["mov_no"]
    watch_times: dict[str, list] = {}
    watch_new_sessions: dict[str, dict] = {}

    def ask_new_session_rule(date: str, prompt_prefix: str) -> None:
        raw_new = input(
            f"  [{date}] {prompt_prefix} "
            f"(범위: 17:00-22:00 / 전체: all / 안 함: Enter): "
        ).strip()
        if raw_new.lower() == "all":
            watch_new_sessions[date] = {"mode": "all"}
        elif raw_new:
            m = re.fullmatch(r"(\d{2}:\d{2})-(\d{2}:\d{2})", raw_new)
            if m:
                watch_new_sessions[date] = {"mode": "range", "start": m.group(1), "end": m.group(2)}
            else:
                print("  형식이 맞지 않아 무시합니다 (예: 17:00-22:00, all)")

    print(f"\n[ 3 ] {selected['movNm']} — 날짜별 상영 시간 선택")

    for date in dates:
        # 첫 번째 날짜는 이미 조회한 결과 재사용
        if date == ref_date:
            flat = sessions_flat
        else:
            print(f"\n  [{date}] 상영 목록 조회 중...", flush=True)
            flat, token = await fetch_movie_list(client, token, date, cfg)
            if flat is None:
                print(f"  [{date}] 조회 실패 — 전체 시간 감시")
                watch_times[date] = []
                continue

        imax_for_date = parse_imax_movies(flat)
        mov_sessions = next(
            (m["sessions"] for m in imax_for_date if m["movNo"] == mov_no), []
        )

        if not mov_sessions:
            # 아직 이 날짜엔 회차가 하나도 안 열렸을 수 있다 — 취소표 감시 날짜와
            # 별개로, 신규 오픈만 노리는 미래 날짜일 가능성이 높으므로 여기서도 물어본다.
            print(f"\n  [{date}] 해당 영화 IMAX 상영 없음 (아직 회차가 안 열렸을 수 있음)")
            watch_times[date] = []
            ask_new_session_rule(date, "신규 오픈 회차가 뜨면 감시할까요?")
            continue

        print(f"\n  [{date}] 상영 시간:")
        for i, s in enumerate(mov_sessions, 1):
            print(f"    [{i}] {s['time']}  {s['hall']}  (잔여 {s['fr_seat']}석 / 총 {s['total']}석)")
        print(f"    [전체] 모든 시간 감시")

        raw_times = input(f"\n  [{date}] 시간 선택 (번호, 쉼표로 구분 / 전체면 Enter): ").strip()
        if raw_times:
            selected_times = []
            for t in raw_times.split(","):
                try:
                    ti = int(t.strip()) - 1
                    if 0 <= ti < len(mov_sessions):
                        tm = mov_sessions[ti]["time"]
                        if tm not in selected_times:
                            selected_times.append(tm)
                except ValueError:
                    pass
            watch_times[date] = selected_times
        else:
            watch_times[date] = []

        # 특정 시간만 골랐을 때만 물어봄 (전체 선택이면 이미 신규 회차도 다 감시됨)
        if watch_times[date]:
            ask_new_session_rule(date, "위 시간 외에 새로 열리는 회차도 감시할까요?")

    cfg["watch_dates"] = dates
    cfg["watch_times"] = watch_times
    cfg["watch_new_sessions"] = watch_new_sessions

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
_seen_sessions: set = set()  # (date, scnsNo, scnSseq) — 이미 한 번이라도 관측된 세션


def parse_schedule(data: list, date: str, cfg: dict) -> list:
    first = date not in _debug_logged
    if first:
        _debug_logged.add(date)
        if data:
            print(f"[{date}] DEBUG 첫 필드: {list(data[0].keys())[:12]}", flush=True)

    wt = cfg.get("watch_times") or {}
    # dict: {date: [times]} 또는 구버전 list 모두 지원
    if isinstance(wt, dict):
        watch_times: set = set(wt.get(date) or [])
    else:
        watch_times = set(wt)

    new_rule = (cfg.get("watch_new_sessions") or {}).get(date)

    now = datetime.now()
    today_str = now.strftime("%Y%m%d")

    sessions = []
    for s in data:
        hall = s.get("scnsNm") or s.get("expoScnsNm") or ""
        if not _is_imax_hall(hall):
            continue

        t = s.get("scnsrtTm") or s.get("rlMovStartTm") or "?"
        time_fmt = f"{t[:2]}:{t[2:]}" if len(t) == 4 else t

        # 신규 세션 여부는 시간 필터와 무관하게 전체 IMAX 세션 기준으로 추적한다
        # (나중에 watch_new_sessions 조건에 걸리는 세션을 정확히 "처음 보는 세션"으로
        # 판정하려면, 필터에 안 걸려서 건너뛴 세션도 계속 추적해야 하기 때문)
        sess_key = (date, s.get("scnsNo", ""), str(s.get("scnSseq") or t))
        is_new = sess_key not in _seen_sessions
        _seen_sessions.add(sess_key)

        passes_exact = bool(watch_times) and time_fmt in watch_times
        # watch_new_sessions에 이 날짜 규칙이 명시돼 있으면, "watch_times 비어있음 = 전체 감시"
        # 기본 동작을 이 날짜에 한해 끄고 그 규칙만 따른다. 안 그러면 "회차가 아직 하나도 없어서
        # watch_times가 자동으로 비어버린 날짜"에서 range 제한이 항상 무시되고 전부 통과해버린다.
        passes_all   = (not watch_times) and not new_rule
        passes_new   = False
        if is_new and new_rule:
            mode = new_rule.get("mode")
            if mode == "all":
                passes_new = True
            elif mode == "range":
                passes_new = new_rule.get("start", "") <= time_fmt <= new_rule.get("end", "")

        if not (passes_exact or passes_all or passes_new):
            continue

        # 상영 시작 시간이 이미 지난 세션 제외
        if date == today_str and len(t) == 4:
            try:
                show_dt = now.replace(hour=int(t[:2]), minute=int(t[2:]), second=0, microsecond=0)
                if now >= show_dt:
                    continue
            except ValueError:
                pass  # 비정상 시간값(예: 2400)은 필터링하지 않고 통과

        sessions.append({
            "date":       date,
            "time":       time_fmt,
            "hall":       hall,
            "scns_no":    s.get("scnsNo", ""),
            "session_id": s.get("scnSseq") or t,
            "total":      int(s.get("stcnt") or 0),
            "is_new":     is_new,
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
        if resp.status_code == 429:
            raise RateLimitError(date)
        if resp.status_code != 200:
            return None
        body = resp.json()
        if body.get("statusCode") != 0:
            return None
        items = (body.get("data") or {}).get("items") or []
        if not items:
            return None
        seats = items[0].get("seats", [])
        # 반환값은 {(row, no): seat_dict} — set처럼 순회/len() 가능하면서, 좌석 상세(seatLocNo 등)를
        # 자동예매 쪽에 그대로 넘겨서 거기서 또 searchIfSeatData를 다시 조회하는 왕복을 없앤다.
        # no는 반드시 int로 저장 — find_seat_pairs()가 (row, int, int)로 페어를 만들기 때문에
        # 여기 키가 문자열이면 나중에 current[(row, a)] 조회가 항상 실패한다.
        result: dict[tuple[str, int], dict] = {}
        for s in seats:
            if s.get("seatSaleYn") != "Y":
                continue
            row = s.get("seatRowNm", "")
            if not _is_prime(row, s.get("seatNo", ""), cfg):
                continue
            try:
                no = int(s.get("seatNo", ""))
            except (TypeError, ValueError):
                continue
            result[(row, no)] = s
        return result
    except RateLimitError:
        raise
    except Exception as e:
        print(f"[좌석상세] 조회 실패: {e}", flush=True)
        return None


async def fetch_seat_summary(
    client: AsyncSession, token: str,
    date: str, scns_no: str, scn_sseq: str,
    cfg: dict,
) -> dict | None:
    """정시 상태체크용: 전체 잔여석 + 명당 잔여석 요약"""
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
        available = [s for s in seats if s.get("seatSaleYn") == "Y"]
        prime_available = [
            s for s in available
            if _is_prime(s.get("seatRowNm", ""), s.get("seatNo", ""), cfg)
        ]
        return {
            "total": len(seats),
            "available": len(available),
            "prime_available": len(prime_available),
        }
    except Exception as e:
        print(f"[좌석요약] 조회 실패: {e}", flush=True)
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


def find_seat_pairs(seats: set) -> list[tuple[str, int, int]]:
    """같은 열에서 좌석번호가 연속인(n, n+1) 2연석 쌍을 모두 찾는다."""
    by_row: dict[str, list[int]] = defaultdict(list)
    for row, no in seats:
        try:
            by_row[row].append(int(no))
        except (TypeError, ValueError):
            continue

    pairs: list[tuple[str, int, int]] = []
    for row, nums in by_row.items():
        nums_sorted = sorted(set(nums))
        for a, b in zip(nums_sorted, nums_sorted[1:]):
            if b == a + 1:
                pairs.append((row, a, b))
    pairs.sort(key=lambda x: (x[0], x[1]))
    return pairs


def _format_pairs(pairs: list[tuple[str, int, int]]) -> str:
    return " / ".join(f"{row}열 {a}-{b}번" for row, a, b in pairs)


def rank_seat_pairs(
    pairs: list[tuple[str, int, int]],
    row_priority: list[str],
    center_seats: list[int] | None = None,
) -> list[tuple[str, int, int]]:
    """
    신규 회차 자동예매 전용 우선순위 정렬.
    1순위: row_priority 목록 순서 (목록에 없는 열은 맨 뒤로)
    2순위: 같은 열이면 center_seats 중앙값에서 가까운 순
    3순위: 중앙에서 거리가 같으면(양옆 대칭) 시작 좌석번호가 작은 쪽 먼저
    """
    center = sum(center_seats) / len(center_seats) if center_seats else 0.0

    def row_rank(row: str) -> int:
        try:
            return row_priority.index(row)
        except ValueError:
            return len(row_priority)

    def col_dist(a: int, b: int) -> float:
        return abs((a + b) / 2 - center)

    return sorted(pairs, key=lambda p: (row_rank(p[0]), col_dist(p[1], p[2]), p[1]))


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
        await asyncio.sleep(0.15)

        if current is None:
            continue

        print(
            f"  [{s['date']} {s['time']}] 명당 {len(current)}석 예매 가능",
            flush=True,
        )

        if not current:
            continue

        pairs = find_seat_pairs(current)
        if not pairs:
            print(f"    → 2연석 없음 (낱개 {len(current)}석뿐) — 알림 생략", flush=True)
            continue

        seat_str = _format_prime_seats(current)
        pairs_str = _format_pairs(pairs)

        is_new_session = bool(s.get("is_new"))
        headline = "🆕 신규 회차 오픈!" if is_new_session else "🎯 CGV IMAX 2연석 오픈!"

        # 토스트용 한 줄 좌석 요약: "G열 19-20번 / H열 25-26번"
        toast_title = f"{'🆕 신규회차 오픈' if is_new_session else '🎯 2연석 오픈'} — {mov_name}"
        toast_body  = (
            f"📅 {s['date']}  🕐 {s['time']}  |  {s['hall']}\n"
            f"2연석: {pairs_str}\n"
            f"💺 명당 총 {len(current)}석 예매 가능"
        )

        site_no = cfg.get("site_no", "0013")
        mov_no  = cfg.get("mov_no", "")
        web_url = (
            f"https://cgv.co.kr/cnm/movieBook/cinema?siteNo={site_no}"
            f"&siteNm={quote('용산아이파크몰')}"
            f"&scnYmd={s['date']}&movNo={mov_no}"
        )
        app_url = (
            f"{PAGES_BASE_URL}/cgv/?d={s['date']}&site={site_no}"
            f"&siteNm={quote('용산아이파크몰')}&movNo={mov_no}"
        )

        msg = (
            f"<b>{headline}</b>\n\n"
            f"🎬 {mov_name}\n"
            f"📅 {s['date']}  🕐 {s['time']}\n"
            f"🏛 {s['hall']}\n\n"
            f"✨ <b>2연석</b> ({','.join(prime_rows)}열 {seat_min}~{seat_max}번)\n"
            f"  {pairs_str}\n\n"
            f"✨ <b>빈 명당 전체</b>\n{seat_str}\n\n"
            f"💺 명당 총 {len(current)}석 예매가능\n\n"
            f"📱 <a href='{app_url}'>CGV 앱 열기</a>\n"
            f"🔗 <a href='{web_url}'>웹으로 예매</a>"
        )

        booked = None
        auto_book_on = cfg.get("auto_book")
        armed = cfg.get("auto_book_armed", True)

        if is_new_session:
            # 신규 회차는 명당 안에서도 지정한 열 우선순위 + 중앙 좌석 기준으로 순서를 매겨
            # 그 순서대로 시도한다 (해당 좌석이 사라지면 find_seat_pair()가 다음 순위로 자동 재탐색).
            row_priority = cfg.get("new_session_row_priority") or []
            center_seats = cfg.get("new_session_center_seats") or []
            ranked = rank_seat_pairs(pairs, row_priority, center_seats) if row_priority else pairs
            target_pair = ranked[0]
            print(f"    [좌석 우선순위] {_format_pairs(ranked)}", flush=True)
        else:
            row_priority = None
            center_seats = None
            target_pair = pairs[0]

        print(
            f"    [자동예매 게이트] auto_book={auto_book_on} armed={armed} "
            f"신규세션={is_new_session} 대상좌석={target_pair} "
            f"→ {'시도함' if (auto_book_on and armed) else '시도 안 함(알림만)'}",
            flush=True,
        )
        if auto_book_on and armed:
            # 방금 이 라운드에서 이미 받아온 좌석 상세(current)를 그대로 넘겨서, 브라우저 쪽에서
            # searchIfSeatData를 또 조회하는 왕복을 없앤다 — 감지~좌석선점 사이 인터벌 최소화.
            row, a, b = target_pair
            target_seats = [current.get((row, a)), current.get((row, b))]
            booked = await try_auto_book(
                client, token, s, cfg, target_pair,
                row_priority=row_priority, center_seats=center_seats,
                seats=target_seats if all(target_seats) else None,
            )

        if booked:
            msg = (
                f"✅ <b>자동예매 완료!</b>\n\n"
                f"🎬 {mov_name}\n"
                f"📅 {s['date']}  🕐 {s['time']}\n"
                f"🏛 {s['hall']}\n"
                f"💺 좌석: {booked['seat']}\n"
                f"🎫 예매번호: {booked['mov_atkt_no']}"
            )
            toast_title = f"✅ 자동예매 완료 — {mov_name}"
            toast_body  = f"{s['date']} {s['time']} | {booked['seat']}석"

        await notify(msg, toast_title=toast_title, toast_body=toast_body)
        print(f"  → 명당 알림: {s['date']} {s['time']}", flush=True)


async def try_auto_book(
    client: AsyncSession, token: str, session: dict, cfg: dict,
    pair: tuple[str, int, int],
    row_priority: list[str] | None = None,
    center_seats: list[int] | None = None,
    seats: list[dict] | None = None,
) -> dict | None:
    """
    2연석 감지 시 browser_booking.py(Playwright)로 자동예매 시도.
    좌석선점/결제 POST가 curl_cffi에서는 401로 막혀서(Cloudflare가 쓰기성 요청만 엄격히
    봇 탐지하는 것으로 보임, 원본 test_seat_hold.py도 동일 증상), 실제 예매 실행은
    Playwright(cgv_session.json 재사용)로 진행한다. 감시 루프 자체는 여전히 curl_cffi라 빠름.
    성공하면 config.json에 armed=False 저장.

    row_priority/center_seats가 주어지면(신규 회차 케이스) pair가 이미 사라졌을 때
    find_seat_pair()의 대체 탐색이 이 우선순위를 따라 다음 후보로 넘어간다.
    seats가 주어지면 이번 라운드에 이미 조회해둔 좌석 상세를 그대로 써서
    browser_booking.py가 searchIfSeatData를 또 조회하는 걸 생략한다(지연 최소화).
    브라우저는 예열된 걸 재사용하고 매번 새로 띄우지 않는다.
    """
    import browser_booking

    try:
        result = await browser_booking.auto_book(
            token, session["date"], session["scns_no"], str(session["session_id"]),
            cfg.get("mov_no", ""), cfg, pair,
            row_priority=row_priority, center_seats=center_seats, seats=seats,
        )
    except Exception as e:
        print(f"  [자동예매] 오류: {e}", flush=True)
        return None

    if result:
        cfg["auto_book_armed"] = False
        save_config(cfg)
    return result


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

    if resp.status_code == 429:
        raise RateLimitError(date)

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


# ── 정시 상태체크 (07:00~24:00, 1시간마다) ────────────────────────────────────

async def send_hourly_status(client: AsyncSession, token: str, cfg: dict) -> str:
    mov_name = cfg.get("mov_name", "")
    lines = []

    for date in cfg["watch_dates"]:
        sessions, token = await check_date(client, token, date, cfg)
        for s in sessions:
            if not s.get("scns_no"):
                continue
            summary = await fetch_seat_summary(
                client, token, s["date"], s["scns_no"], str(s["session_id"]), cfg
            )
            await asyncio.sleep(0.5)
            if summary is None:
                lines.append(f"  {s['date']} {s['time']} — 조회 실패")
                continue
            lines.append(
                f"  {s['date']} {s['time']} — 잔여 {summary['available']}/{summary['total']}석"
                f" (명당 {summary['prime_available']}석)"
            )

    now_str = datetime.now().strftime("%H:%M")
    body = "\n".join(lines) if lines else "  감시 대상 회차 없음"
    print(f"\n[정시 상태체크 {now_str}]\n{body}", flush=True)

    msg = f"📊 <b>[정시 상태체크 {now_str}]</b>\n🎬 {mov_name}\n{body}"
    # await send_discord(msg)   # 사내망 차단 - 비활성화
    return token


# ── 메인 ─────────────────────────────────────────────────────────────────────

async def main():
    token = load_token()
    if not token:
        print("오류: .env에 ACCESS_TOKEN 설정 필요")
        sys.exit(1)

    cfg = load_config()

    async with AsyncSession(impersonate=_IMPERSONATE) as client:
        cfg, token = await interactive_setup(client, token, cfg)

        # 신규 세션 감지 기준선 기록 — 지금 이미 존재하는 회차들을 "신규"로
        # 오탐하지 않도록, 감시 루프 시작 전에 한 번 조회해서 _seen_sessions를 채운다.
        print("\n  [초기화] 기존 회차 기준선 기록 중...", flush=True)
        for d in cfg["watch_dates"]:
            try:
                _, token = await check_date(client, token, d, cfg)
            except RateLimitError:
                print(f"  [초기화] [{d}] 429 레이트 리밋 — {RATE_LIMIT_WAIT}초 대기 후 계속", flush=True)
                await asyncio.sleep(RATE_LIMIT_WAIT)
            except Exception as e:
                print(f"  [초기화] [{d}] 조회 실패: {e}", flush=True)
            await asyncio.sleep(0.3)
        print("  [초기화] 완료 — 이제부터 새로 뜨는 회차만 신규로 판정합니다.", flush=True)

        if cfg.get("auto_book"):
            print("  [예열] 자동예매용 브라우저 미리 띄우는 중...", flush=True)
            try:
                import browser_booking
                await browser_booking.get_warm_page()
            except Exception as e:
                print(f"  [예열] 실패({e}) — 예매 시도 시점에 새로 띄웁니다.", flush=True)

        rows_disp = ", ".join(sorted(cfg["prime_rows"]))
        wt = cfg.get("watch_times") or {}
        nwt = cfg.get("watch_new_sessions") or {}

        print("\n" + "=" * 60)
        print(f"  영화    : {cfg['mov_name']} ({cfg['mov_no']})")
        for d in cfg["watch_dates"]:
            times = wt.get(d) if isinstance(wt, dict) else None
            times_disp = ", ".join(times) if times else "전체"
            print(f"  날짜    : {d}  ({times_disp})")
            new_rule = nwt.get(d)
            if new_rule:
                if new_rule.get("mode") == "all":
                    print(f"    ↳ 신규 회차: 전체 감시")
                else:
                    print(f"    ↳ 신규 회차: {new_rule.get('start')}~{new_rule.get('end')} 범위 감시")
        print(f"  명당 조건: {rows_disp}열  {cfg['prime_seat_min']}~{cfg['prime_seat_max']}번")
        ivl = load_interval_state()
        if ivl["locked"]:
            print(f"  확인 주기: {ivl['interval']}초 (429 이후 안전값 고정)")
        else:
            print(
                f"  확인 주기: {ivl['interval']}초 "
                f"(적응형 튜닝 중, 연속 무사고 {ivl['consecutive_ok']}/{STABLE_ROUNDS_TO_DECREASE})"
            )
        if platform.system() == "Windows":
            try:
                import win11toast  # noqa: F401
                print("  윈도우 알림: 토스트 + 경고음")
            except ImportError:
                print("  윈도우 알림: 팝업 + 경고음")
        print("=" * 60)

        dates_summary = "\n".join(
            f"  {d}: {', '.join(wt.get(d)) if isinstance(wt, dict) and wt.get(d) else '전체'}"
            for d in cfg["watch_dates"]
        )
        await notify(
            f"🔔 CGV IMAX 알리미 시작\n"
            f"🎬 {cfg['mov_name']}\n"
            f"📅 감시 날짜/시간:\n{dates_summary}\n"
            f"🎯 명당: {rows_disp}열 {cfg['prime_seat_min']}~{cfg['prime_seat_max']}번"
        )

        round_num = 0
        last_status_hour = None
        while True:
            round_num += 1
            now_dt = datetime.now()
            now = now_dt.strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n[{now}] 라운드 {round_num}", flush=True)

            rate_limited = False
            for date in cfg["watch_dates"]:
                try:
                    sessions, token = await check_date(client, token, date, cfg)
                    if sessions:
                        await process_sessions(sessions, client, token, cfg)
                    else:
                        print(f"  [{date}] 해당 세션 없음", flush=True)
                except RateLimitError:
                    print(f"  [{date}] 429 레이트 리밋 — 라운드 중단, {RATE_LIMIT_WAIT}초 대기 후 재시작", flush=True)
                    await asyncio.sleep(RATE_LIMIT_WAIT)
                    rate_limited = True
                    break
                except Exception as e:
                    print(f"  [{date}] 오류: {e}", flush=True)
                await asyncio.sleep(0.3)

            if rate_limited:
                if not ivl["locked"]:
                    ivl["interval"] = round(min(MAX_CHECK_INTERVAL, ivl["last_known_good"] + SAFETY_MARGIN), 2)
                    ivl["locked"] = True
                    ivl["consecutive_ok"] = 0
                    save_interval_state(ivl)
                    print(
                        f"  [적응형 인터벌] 안전값으로 원복 및 고정: {ivl['interval']}초 "
                        f"(마지막 안정값 {ivl['last_known_good']}초 + 안전마진 {SAFETY_MARGIN}초)",
                        flush=True,
                    )
                continue

            if not ivl["locked"]:
                ivl["consecutive_ok"] += 1
                if ivl["consecutive_ok"] >= STABLE_ROUNDS_TO_DECREASE:
                    ivl["last_known_good"] = ivl["interval"]
                    ivl["interval"] = round(max(MIN_CHECK_INTERVAL, ivl["interval"] - DECREASE_STEP), 2)
                    ivl["consecutive_ok"] = 0
                    print(f"  [적응형 인터벌] {STABLE_ROUNDS_TO_DECREASE}라운드 무사고 — {ivl['interval']}초로 축소 시도", flush=True)
                save_interval_state(ivl)

            if 7 <= now_dt.hour <= 23 and now_dt.hour != last_status_hour:
                try:
                    token = await send_hourly_status(client, token, cfg)
                except Exception as e:
                    print(f"[정시 상태체크] 오류: {e}", flush=True)
                last_status_hour = now_dt.hour

            print(f"[대기] {ivl['interval']}초 후 재확인...", flush=True)
            await asyncio.sleep(ivl["interval"])


if __name__ == "__main__":
    # 콘솔 codepage가 cp949 등 UTF-8이 아니면 자동예매 로그의 특수문자(—) 출력에서
    # UnicodeEncodeError로 죽으면서 취소(롤백) 호출까지 같이 못 하게 되는 사고가 있었다
    # (2연석 홀드가 안 풀리는 원인). 프로세스 전체 stdout/stderr을 UTF-8로 강제한다.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n종료합니다.")
