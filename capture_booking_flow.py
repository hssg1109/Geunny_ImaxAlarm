"""
CGV 예매 흐름(관람권 적용 → 최종 확정) API 캡처 도구

[용도]
booking.py를 만들기 전에, 관람권 적용/최종 예매확정 API의 정확한 엔드포인트와
페이로드를 모른다. 이 스크립트는 실시간 감시/자동예매에는 관여하지 않고,
브라우저 창을 띄운 뒤 사용자가 직접 한 번 실제로(또는 테스트 결제로) 좌석선택
→ 관람권 적용 → 결제까지 진행하는 동안 모든 XHR/fetch 요청을 가로채서 기록한다.

[흐름]
1. 최초 실행: cgv_session.json 없음 → 로그인 페이지로 이동, 사용자가 직접 로그인.
   로그인 완료 후 Enter를 누르면 storage_state를 cgv_session.json에 저장.
2. 이후 실행: cgv_session.json 재사용 → 로그인 생략, 바로 예매 페이지로 이동.
3. 사용자가 브라우저에서 좌석선택 → 관람권 적용 → 결제를 직접 진행.
4. 그동안 발생한 모든 API 요청/응답을 capture_log.json에 순서대로 저장.
5. 사용자가 완료 후 터미널에서 Enter를 누르면 종료.

[주의]
- 실제 결제가 발생할 수 있으므로, 가급적 저렴하거나 취소 가능한 회차로 테스트할 것.
- capture_log.json에는 세션/토큰 등 민감정보가 그대로 남으므로 커밋 금지 (.gitignore 등록됨).

[중요 — 대기 방식]
Playwright의 동기(sync) API는 응답 이벤트 콜백을, 메인 스레드가 "다시 Playwright API를
호출하는 시점"에만 처리한다. 순수 파이썬 blocking input()으로 기다리면 그동안 발생한 응답이
전혀 처리되지 않고 큐에 쌓이기만 하다가, 다음 Playwright 호출(예: browser.close()) 때 한꺼번에
몰려서 처리되며 이미 브라우저가 닫히는 중이라 대부분 유실된다 (실제로 재현 확인함).
그래서 사용자 입력을 기다릴 때도 input() 대신, 주기적으로 page.wait_for_timeout()을 호출해
이벤트 큐를 계속 비워주면서 Enter 키 입력 여부를 논-블로킹으로 확인한다 (Windows msvcrt 사용).
"""

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

try:
    import msvcrt
    _HAS_MSVCRT = True
except ImportError:
    _HAS_MSVCRT = False

SESSION_FILE = Path(__file__).parent / "cgv_session.json"
LOG_FILE     = Path(__file__).parent / "capture_log.json"

START_URL = "https://cgv.co.kr/"

# 요청 URL에 이 키워드 중 하나라도 포함되면 캡처 대상 (그 외 잡다한 요청은 노이즈라 제외)
INTEREST_KEYWORDS = [
    "atkt", "booking", "seatTemp", "coupon", "voucher", "gift", "prmp",
    "cnfrm", "confirm", "pay", "settle", "movAtkt",
    "netfunnel", "waitroom", "queue",  # 좌석선점 401의 원인으로 의심되는 가상대기실 토큰
]


def is_interesting(url: str) -> bool:
    low = url.lower()
    return any(k.lower() in low for k in INTEREST_KEYWORDS)


def wait_for_enter(page, prompt: str) -> None:
    """
    input()과 달리 Playwright 이벤트 큐를 계속 비워주면서(page.wait_for_timeout) Enter를
    기다린다. msvcrt가 없는 환경(비-Windows)에서는 어쩔 수 없이 input()으로 폴백한다
    (이 경우 대기 중 응답 이벤트가 유실될 수 있음을 감안할 것).
    """
    print(prompt)
    if not _HAS_MSVCRT:
        print("[경고] msvcrt 없음 — input()으로 폴백 (대기 중 이벤트 유실 가능)")
        input()
        return
    while True:
        page.wait_for_timeout(300)
        if msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch in (b"\r", b"\n"):
                break


def main() -> None:
    entries: list[dict] = []
    seen_counter = {"total": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)

        if SESSION_FILE.exists():
            print(f"[세션] {SESSION_FILE.name} 재사용 — 로그인 생략")
            context = browser.new_context(storage_state=str(SESSION_FILE))
        else:
            print("[세션] 저장된 세션 없음 — 로그인 페이지를 엽니다.")
            context = browser.new_context()

        def on_response(response) -> None:
            req = response.request
            if req.resource_type not in ("xhr", "fetch"):
                return
            seen_counter["total"] += 1
            if seen_counter["total"] % 10 == 0:
                print(f"  [관찰중] API 요청 {seen_counter['total']}건 지나감 (캡처 {len(entries)}건)")
            if not is_interesting(req.url):
                return
            try:
                body_text = req.post_data or ""
            except Exception:
                body_text = ""
            try:
                resp_json = response.json()
            except Exception as e_json:
                try:
                    resp_json = response.text()
                except Exception as e_text:
                    resp_json = f"<캡처 실패: json={e_json!r} text={e_text!r}>"
                    print(f"  [경고] 응답 본문 캡처 실패: {req.url}: {e_text!r}")
            entry = {
                "method": req.method,
                "url": req.url,
                "request_headers": dict(req.headers),
                "request_body": body_text,
                "status": response.status,
                "response": resp_json,
            }
            entries.append(entry)
            print(f"[캡처] {req.method} {req.url}  (HTTP {response.status})")

        # 결제 흐름 중 새 탭/팝업이 열려도 놓치지 않도록 컨텍스트의 모든 새 페이지를 감시한다.
        # (context.new_page()로 만든 첫 페이지도 이 이벤트로 잡히므로 따로 또 붙이지 않는다.)
        def attach(new_page) -> None:
            print(f"  [탭 감지] 새 탭/팝업 열림: {new_page.url}")
            new_page.on("response", on_response)

        context.on("page", attach)

        page = context.new_page()
        page.goto(START_URL)

        if not SESSION_FILE.exists():
            wait_for_enter(page, "\n브라우저에서 로그인을 완료한 뒤, 이 터미널에서 Enter를 누르세요...\n")
            context.storage_state(path=str(SESSION_FILE))
            print(f"[세션] 저장 완료 → {SESSION_FILE.name}")

        wait_for_enter(
            page,
            "\n이제 브라우저에서 직접 좌석선택 → 관람권 적용 → 결제(최종확정)까지 진행하세요.\n"
            "진행하는 동안 [관찰중]/[캡처] 로그가 실시간으로 여기 찍혀야 정상입니다.\n"
            "10초 넘게 아무 로그도 안 뜨면 잘못된 창에서 진행 중일 수 있으니 확인해주세요.\n"
            "완료되면 이 터미널로 돌아와 Enter를 누르면 캡처를 종료하고 저장합니다.\n",
        )

        LOG_FILE.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n[완료] API 요청 {seen_counter['total']}건 관찰, {len(entries)}건 캡처 → {LOG_FILE.name}")
        if seen_counter["total"] == 0:
            print(
                "[경고] API 요청이 하나도 관찰되지 않았습니다 — 이 스크립트가 띄운 브라우저 창이 아닌 "
                "다른 브라우저(평소 쓰던 Chrome/Edge)에서 예매를 진행하신 건 아닌지 확인해주세요."
            )

        browser.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n중단됨.")
        sys.exit(1)
