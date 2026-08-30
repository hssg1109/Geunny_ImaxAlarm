"""
CGV_COOKIES 자동 새로고침 도구

[배경]
booking.py의 좌석선점(seatTempPrmp) 같은 쓰기성 호출은 Cloudflare 봇 방어 쿠키
(__cf_bm, _cfuvid 등)를 검사하는데, 이 쿠키들은 수명이 짧다(__cf_bm은 통상 30분).
DevTools에서 수동으로 복사하면 시간 지나면서 만료되거나, 복사 중 줄바꿈이 끼는 실수가
반복되기 쉽다. 이 스크립트는 cgv_session.json(로그인 세션)을 재사용해 브라우저를 잠깐
띄운 뒤, 그 시점의 쿠키를 그대로 읽어서 .env의 CGV_COOKIES= 한 줄로 덮어쓴다.

[사용법]
    venv\\Scripts\\python.exe refresh_cookies.py

cgv_session.json이 없으면 먼저 capture_booking_flow.py를 한 번 실행해 로그인해두세요.
"""

import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

SESSION_FILE = Path(__file__).parent / "cgv_session.json"
ENV_FILE     = Path(__file__).parent / ".env"


def main() -> None:
    if not SESSION_FILE.exists():
        print(f"오류: {SESSION_FILE.name} 없음 — capture_booking_flow.py를 먼저 실행해 로그인하세요.")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(SESSION_FILE))
        page = context.new_page()
        page.goto("https://cgv.co.kr/")
        page.wait_for_timeout(1500)  # Cloudflare 쿠키(cf_bm 등)가 갱신될 시간을 준다

        cookies = context.cookies("https://cgv.co.kr/")
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

        # 로그인 세션도 최신 상태로 다시 저장 (accessToken 갱신 등 반영)
        context.storage_state(path=str(SESSION_FILE))
        browser.close()

    if not cookie_str:
        print("오류: 쿠키를 하나도 못 읽었습니다. cgv_session.json이 만료됐을 수 있어요 — "
              "capture_booking_flow.py를 다시 실행해 로그인해주세요.")
        sys.exit(1)

    if not ENV_FILE.exists():
        print(f"오류: {ENV_FILE.name} 없음")
        sys.exit(1)

    text = ENV_FILE.read_text(encoding="utf-8")
    new_line = f"CGV_COOKIES={cookie_str}"
    if re.search(r"^CGV_COOKIES=.*$", text, flags=re.MULTILINE):
        text = re.sub(r"^CGV_COOKIES=.*$", new_line, text, count=1, flags=re.MULTILINE)
    else:
        text = text.rstrip("\n") + f"\n{new_line}\n"
    ENV_FILE.write_text(text, encoding="utf-8")

    print(f"[완료] CGV_COOKIES 갱신 (길이 {len(cookie_str)}자, 쿠키 {len(cookies)}개, 내용은 출력하지 않음)")


if __name__ == "__main__":
    main()
