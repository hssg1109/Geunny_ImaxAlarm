"""
CGV 용산IMAX 좌석 오픈 알리미

첫 실행 시 브라우저 창이 열리면 CGV에 직접 로그인하세요.
로그인 후 쿠키가 저장되어 이후 실행은 자동으로 진행됩니다.
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, BrowserContext, Page

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))

MOV_NO = os.getenv("MOV_NO", "30001210")
SITE_NO = os.getenv("SITE_NO", "0013")
CO_CD = os.getenv("CO_CD", "A420")
RTCTL_SCOP_CD = os.getenv("RTCTL_SCOP_CD", "08")
WATCH_DATES = [d.strip() for d in os.getenv("WATCH_DATES", "").split(",") if d.strip()]

COOKIE_FILE = Path(__file__).parent / "cgv_session.json"
SCHEDULE_API_PATH = "/cnm/atkt/searchSchByMov"

notified_sessions: set = set()


# ── Telegram ────────────────────────────────────────────────────────────────

async def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[텔레그램 미설정] {message}", flush=True)
        return
    try:
        import telegram
        bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        # 10초 타임아웃: 네트워크 문제 시 무한 대기 방지
        await asyncio.wait_for(
            bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="HTML"),
            timeout=10.0
        )
        print("[텔레그램] 전송 완료", flush=True)
    except asyncio.TimeoutError:
        print("[텔레그램] 전송 타임아웃 (10초) — 계속 진행", flush=True)
    except Exception as e:
        print(f"[텔레그램] 전송 실패: {e}", flush=True)


# ── 쿠키 저장/로드 ─────────────────────────────────────────────────────────

async def save_cookies(context: BrowserContext) -> None:
    cookies = await context.cookies()
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)
    print(f"[쿠키] 저장 완료: {COOKIE_FILE}")


async def load_cookies(context: BrowserContext) -> bool:
    if not COOKIE_FILE.exists():
        return False
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        await context.add_cookies(cookies)
        print(f"[쿠키] 로드 완료 ({len(cookies)}개)")
        return True
    except Exception as e:
        print(f"[쿠키] 로드 실패: {e}")
        return False


# ── 로그인 ─────────────────────────────────────────────────────────────────

async def auto_login(context: BrowserContext) -> bool:
    """env의 CGV_ID/CGV_PW로 자동 로그인합니다."""
    page = await context.new_page()
    try:
        print(f"[로그인] 자동 로그인 시도 (ID: {CGV_ID})")
        await page.goto("https://cgv.co.kr/login", wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(1000)

        # ID 입력
        await page.fill("input[name='username']", CGV_ID)
        await page.wait_for_timeout(300)

        # PW 입력
        await page.fill("input[name='password']", CGV_PW)
        await page.wait_for_timeout(300)

        # 로그인 버튼 클릭: 같은 클래스의 버튼이 2개(뒤로가기/로그인)이므로 마지막 버튼 선택
        await page.locator("button[class*='loginButton']").last().click()

        # 로그인 완료 대기 (최대 10초)
        await page.wait_for_url(lambda url: "login" not in url, timeout=10000)
        print("[로그인] 자동 로그인 성공!")
        await save_cookies(context)
        return True

    except Exception as e:
        print(f"[로그인] 자동 로그인 실패: {e}")
        return False
    finally:
        await page.close()


async def manual_login(context: BrowserContext) -> None:
    """브라우저 창을 열고 사용자가 직접 로그인할 때까지 대기합니다 (fallback)."""
    page = await context.new_page()
    print("\n" + "="*60)
    print("브라우저에서 CGV에 직접 로그인해주세요.")
    print("로그인 완료 후 메인 페이지로 이동하면 자동 진행됩니다.")
    print("="*60)
    await page.goto("https://cgv.co.kr/login", wait_until="domcontentloaded")

    for _ in range(180):  # 최대 3분 대기
        await asyncio.sleep(1)
        if "login" not in page.url:
            print("[로그인] 수동 로그인 성공 감지!")
            break
    else:
        print("[로그인] 시간 초과. 그래도 계속 진행합니다.")

    await save_cookies(context)
    await page.close()


async def check_session_valid(page: Page) -> bool:
    """현재 세션이 유효한지 확인 (로그인 상태인지)"""
    try:
        await page.goto("https://cgv.co.kr/mypage", wait_until="domcontentloaded", timeout=15000)
        is_valid = "login" not in page.url
        print(f"[세션] {'유효한 로그인 세션 확인' if is_valid else '로그인 필요'}")
        return is_valid
    except Exception as e:
        print(f"[세션] 확인 오류: {e}")
        return False


# ── 스케줄 확인 ────────────────────────────────────────────────────────────

def parse_schedule(body: dict, date: str, debug: bool = False) -> list:
    """API 응답에서 예매 가능 세션 추출 (필드명 fallback 처리)"""
    available = []
    try:
        data = body.get("data") or {}

        if debug:
            print(f"[DEBUG] 응답 최상위 키: {list(body.keys())}")
            if isinstance(data, dict):
                print(f"[DEBUG] data 키: {list(data.keys())}")
            elif isinstance(data, list):
                print(f"[DEBUG] data = list({len(data)}개), 첫 항목 키: {list(data[0].keys()) if data else '비어있음'}")

        # CGV API 응답 구조: data.schList 또는 data 자체가 list
        sessions = (
            data.get("schList")
            or data.get("scheduleList")
            or data.get("list")
            or (data if isinstance(data, list) else [])
        )

        if debug and sessions:
            print(f"[DEBUG] 세션 {len(sessions)}개, 첫 번째 필드: {list(sessions[0].keys())}")

        for s in sessions:
            # 잔여석 필드명 후보
            remain = int(
                s.get("rmndSeatCnt")
                or s.get("remainSeatCnt")
                or s.get("leftSeatCnt")
                or 0
            )
            total = int(
                s.get("totSeatCnt")
                or s.get("totalSeatCnt")
                or s.get("seatCnt")
                or 0
            )
            # 예매 불가 상태 코드 체크
            status = s.get("schSttsCd") or s.get("status") or ""
            if status in ("SOLDOUT", "CLOSED", "N"):
                continue

            if remain > 0 or (not remain and status in ("", "OPEN", "Y")):
                available.append({
                    "date": date,
                    "time": s.get("scrnStartDttm") or s.get("startTime") or s.get("schDttm") or "?",
                    "remain": remain,
                    "total": total,
                    "hall": s.get("scrnNm") or s.get("hallName") or s.get("theatreName") or "IMAX",
                    "session_id": s.get("schNo") or s.get("sessionId") or s.get("schId") or str(s),
                })
    except Exception as e:
        print(f"[파싱] 오류: {e}")
    return available


_debug_logged: set = set()  # 날짜별 최초 1회만 debug 출력
_page_ready: bool = False   # CGV 페이지 초기 로드 여부


async def ensure_cgv_loaded(page: Page) -> None:
    """CGV 메인 페이지를 한 번 로드해 JS fetch interceptor를 활성화합니다."""
    global _page_ready
    if not _page_ready or "cgv.co.kr" not in page.url or "login" in page.url:
        print("[페이지] CGV 메인 로드 중...", flush=True)
        await page.goto("https://cgv.co.kr/", wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(2000)
        _page_ready = True


async def check_date(page: Page, date: str) -> list:
    """CGV 페이지 JS 컨텍스트에서 직접 fetch로 스케줄 API 호출.

    /ticket URL이 404를 반환하는 문제를 우회합니다.
    accessToken 쿠키를 Bearer 토큰으로 자동 사용하며,
    페이지 내 CGV fetch 인터셉터가 x-signature를 자동으로 추가합니다.
    """
    await ensure_cgv_loaded(page)

    api_url = (
        f"https://api.cgv.co.kr/cnm/atkt/searchSchByMov"
        f"?coCd={CO_CD}&siteNo={SITE_NO}&scnYmd={date}"
        f"&movNo={MOV_NO}&rtctlScopCd={RTCTL_SCOP_CD}"
    )

    # page.evaluate(): CGV 페이지 컨텍스트에서 fetch 실행
    # → 페이지 JS가 Authorization / x-signature 헤더를 자동 부착
    result = await page.evaluate(f"""
        async () => {{
            try {{
                const r = await fetch("{api_url}", {{ credentials: "include" }});
                const body = await r.json();
                return {{ status: r.status, body }};
            }} catch(e) {{
                return {{ error: String(e) }};
            }}
        }}
    """)

    if result.get("error"):
        print(f"[{date}] fetch 오류: {result['error']}", flush=True)
        return []

    http_status = result.get("status", 0)
    body = result.get("body", {})
    api_status = str(body.get("statusCode", ""))

    if http_status == 401 or api_status == "401":
        print(f"[{date}] 401 — 세션 만료. 재로그인 필요", flush=True)
        global _page_ready
        _page_ready = False   # 다음 라운드에 재로드 유도
        return []

    if api_status == "200":
        first_time = date not in _debug_logged
        if first_time:
            _debug_logged.add(date)
        return parse_schedule(body, date, debug=first_time)

    # 스케줄 없음 (아직 오픈 전) 또는 다른 상태
    msg = body.get("statusMessage", "")
    print(f"[{date}] 스케줄 없음 (API {api_status or http_status} {msg})", flush=True)
    return []


async def process_results(sessions: list, date: str) -> int:
    """새로 발견된 세션에 대해 알림 전송, 알림 수 반환"""
    new_count = 0
    for s in sessions:
        sid = f"{date}_{s['session_id']}_{s['time']}"
        if sid in notified_sessions:
            continue
        notified_sessions.add(sid)
        new_count += 1

        remain_str = f"{s['remain']}석" if s['remain'] else "좌석 있음"
        msg = (
            f"🎬 <b>CGV 용산IMAX 오픈!</b>\n\n"
            f"📅 {s['date']} {s['time']}\n"
            f"🏛 {s['hall']}\n"
            f"💺 잔여: {remain_str}"
            + (f" / 총 {s['total']}석" if s['total'] else "")
            + f"\n\n🔗 <a href='https://cgv.co.kr/ticket'>바로 예매</a>"
        )
        await send_telegram(msg)
        print(f"  → 알림 전송: {s['time']} ({remain_str})")

    return new_count


# ── 메인 루프 ──────────────────────────────────────────────────────────────

async def main():
    if not WATCH_DATES:
        print("오류: .env 파일에 WATCH_DATES를 설정해주세요.")
        print("예) WATCH_DATES=20260530,20260531")
        sys.exit(1)

    print("=" * 60)
    print("  CGV 용산IMAX 좌석 오픈 알리미")
    print(f"  영화: {MOV_NO} | 상영관 siteNo: {SITE_NO}")
    print(f"  감시 날짜: {', '.join(WATCH_DATES)}")
    print(f"  확인 주기: {CHECK_INTERVAL}초")
    print("=" * 60)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,  # 로그인 확인을 위해 처음엔 창 표시
            args=["--window-size=1200,800"]
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
            ),
            viewport={"width": 1200, "height": 800},
            locale="ko-KR",
        )

        # 1) 저장된 쿠키로 세션 복원 시도
        has_cookies = await load_cookies(context)
        session_ok = False
        if has_cookies:
            check_page = await context.new_page()
            session_ok = await check_session_valid(check_page)
            await check_page.close()

        # 2) 세션 없으면 로그인
        if not session_ok:
            if CGV_ID and CGV_PW:
                # env에 계정 정보 있으면 자동 로그인
                session_ok = await auto_login(context)
            if not session_ok:
                # 자동 로그인 실패 or 계정 정보 없으면 수동 로그인
                await manual_login(context)

        print("\n[시작] 감시를 시작합니다. 브라우저 창을 닫지 마세요 (최소화는 OK).", flush=True)
        print("[텔레그램] 시작 알림 전송 중...", flush=True)
        await send_telegram(
            f"🔔 CGV 용산IMAX 알리미 시작\n"
            f"감시 날짜: {', '.join(WATCH_DATES)}\n"
            f"확인 주기: {CHECK_INTERVAL}초"
        )
        print("[루프] 감시 루프 진입...", flush=True)

        page = await context.new_page()

        try:
            round_num = 0
            while True:
                round_num += 1
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"\n[{now}] 라운드 {round_num} 확인 중...", flush=True)

                # 세션 만료 감지: 로그인 페이지로 리다이렉트됐으면 재로그인
                if "login" in page.url:
                    print("[세션 만료] 재로그인 시도...")
                    if CGV_ID and CGV_PW:
                        await auto_login(context)
                    else:
                        await manual_login(context)

                total_found = 0
                for date in WATCH_DATES:
                    sessions = await check_date(page, date)
                    if sessions:
                        print(f"  [{date}] {len(sessions)}개 세션 발견!")
                        found = await process_results(sessions, date)
                        total_found += found
                    else:
                        print(f"  [{date}] 예매 없음")
                    await asyncio.sleep(1)

                print(f"[완료] {CHECK_INTERVAL}초 후 재확인...", flush=True)
                await asyncio.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            print("\n\n종료합니다.")
        finally:
            await save_cookies(context)
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
