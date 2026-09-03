"""
Playwright 기반 실제 예매 실행 (좌석선점 → 관람권 적용 → 결제확정)

[왜 필요한가]
curl_cffi로는 GET(좌석조회/사전체크 등)은 다 되는데, POST(좌석선점 seatTempPrmp, 가격조회
searchMovAtktSeatPrcList, 결제 관련 전부)는 헤더/referer/쿠키/TLS 지문을 다 맞춰봐도 계속
401 "Unauthorized3"이 난다. 원본 test_seat_hold.py(건드리지 않은 코드)로도 재현되는 걸 보면
프로젝트 버그가 아니라 Cloudflare/CGV가 "쓰기성" 요청만 훨씬 엄격하게 봇 탐지를 하는 것으로
보인다.

그래서 감시(빠른 curl_cffi 폴링)는 그대로 두고, 실제 좌석선점~결제확정만 진짜 브라우저
(Playwright, cgv_session.json 재사용)의 JS 컨텍스트 안에서 fetch()를 실행해서 우회한다.
CDP로 주입한 fetch()라도 실행 자체는 진짜 브라우저 엔진이 하기 때문에, 네트워크 트래픽이
"진짜 사이트 사용"과 구분되지 않는다. 요청 바디 구성 로직(실제 캡처로 검증됨)은 booking.py의
헬퍼를 그대로 재사용하고, 전송 방식만 curl_cffi에서 Playwright 페이지 내 fetch()로 바꾼다.

[속도]
브라우저 기동 + 페이지 이동이 있어서 curl_cffi보다 느리다(대략 수 초~십수 초). 감시 루프
자체는 여전히 curl_cffi라 빠르고, 이 브라우저 실행은 "명당 2연석 감지 후 실제로 잡을 때"
한 번만 일어난다.
"""

import json
import re
from urllib.parse import urlencode

from playwright.async_api import async_playwright, Page

import booking as B
from booking import (
    ACCESS_TOKEN, CUST_NO, GIFTC_NOS, DRY_RUN,
    SITE_BASE, PRMP_PATH, PRMP_CNCL_PATH, PRC_LIST_PATH, GET_PAY_ID_PATH,
    INSERT_TEMP_PATH, UPDATE_TEMP_PATH, GIFTC_FILTER_LIST_PATH, GIFTC_RETRN_BSS_PATH, VALIDATE_GIFTC_PATH,
    CREATE_SAL_PATH, CO_CD, SITE_NO, BZPLC_NO, RTCTL_SCOP_CD, SITE_NM_SHORT,
    REFERER_SEAT_SELECT, REFERER_PAYMENT,
    BookingError, find_seat_pair, fetch_schedule_detail, check_before_pay,
    _load_template, _gen_paym_vrfy_no, _build_sell_product, _build_giftc_discount,
    _fill_mov_common, _cash_receipt_cos_amt,
)
from curl_cffi.requests import AsyncSession

SESSION_FILE = B.Path(__file__).parent / "cgv_session.json"

# ── 예열된 브라우저 (매 시도마다 새로 띄우지 않고 재사용) ────────────────────────
# REFERER_SEAT_SELECT("/cnm/selectVisitorCnt")는 특정 회차에 묶인 URL이 아니라
# 좌석선점 POST의 referer로만 쓰이는 공용 페이지라, 한 번 띄워두면 어떤 세션이든
# (기존 취소표든 신규 오픈이든) 재탐색 없이 그대로 재사용 가능하다.
_warm_pw = None
_warm_browser = None
_warm_context = None
_warm_page: Page | None = None


async def get_warm_page() -> Page:
    """예열된 페이지가 있으면 그대로 반환, 없으면 한 번만 브라우저를 띄워 좌석선택
    화면까지 미리 가있는다. 이후 자동예매 시도는 이 페이지를 그대로 재사용해서
    브라우저 기동(수 초) 지연 없이 바로 좌석선점을 시도할 수 있다."""
    global _warm_pw, _warm_browser, _warm_context, _warm_page

    if _warm_page is not None:
        try:
            if not _warm_page.is_closed():
                return _warm_page
        except Exception:
            pass

    if not SESSION_FILE.exists():
        raise BookingError(f"{SESSION_FILE.name} 없음 — capture_booking_flow.py를 먼저 실행해 로그인하세요.")

    _warm_pw = await async_playwright().start()
    _warm_browser = await _warm_pw.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
    )
    _warm_context = await _warm_browser.new_context(storage_state=str(SESSION_FILE))
    await _warm_context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    )
    _warm_page = await _warm_context.new_page()
    await _warm_page.goto(REFERER_SEAT_SELECT, wait_until="domcontentloaded")
    print("[예열] 자동예매용 브라우저 준비 완료 — 좌석선택 화면 대기중", flush=True)
    return _warm_page


async def close_warm_page() -> None:
    global _warm_pw, _warm_browser, _warm_context, _warm_page
    try:
        if _warm_browser:
            await _warm_browser.close()
    except Exception:
        pass
    try:
        if _warm_pw:
            await _warm_pw.stop()
    except Exception:
        pass
    _warm_pw = _warm_browser = _warm_context = _warm_page = None


_FETCH_JS = """
async ({method, url, body, token}) => {
    const opts = {
        method,
        headers: { 'accept': 'application/json', 'content-type': 'application/json', 'authorization': 'Bearer ' + token },
        credentials: 'include',
    };
    if (body !== null) opts.body = JSON.stringify(body);
    const resp = await fetch(url, opts);
    const text = await resp.text();
    return { status: resp.status, text };
}
"""


async def pw_fetch(page: Page, method: str, url: str, body: dict | None = None) -> dict:
    """페이지 내부 JS fetch()를 실행해서 진짜 브라우저 트래픽으로 요청을 보낸다."""
    result = await page.evaluate(_FETCH_JS, {"method": method, "url": url, "body": body, "token": ACCESS_TOKEN})
    return result


def _check_pw_ok(result: dict, label: str) -> dict:
    status = result.get("status")
    text = result.get("text", "")
    if status != 200:
        raise BookingError(f"{label} HTTP {status}: {text[:500]}")
    try:
        body = json.loads(text) if text else {}
    except Exception as e:
        raise BookingError(f"{label} 응답 JSON 파싱 실패: {e} / {text[:500]}")
    if isinstance(body, dict) and body.get("statusCode") not in (0, None):
        raise BookingError(f"{label} API 오류: {body.get('statusMessage')}")
    return body


async def pw_hold_seats(page: Page, date: str, scns_no: str, scn_sseq: str, seats: list[dict]) -> dict:
    body = {
        "coCd": CO_CD, "custNo": CUST_NO, "siteNo": SITE_NO, "rtctlScopCd": RTCTL_SCOP_CD,
        "scnYmd": date, "scnsNo": scns_no, "scnSseq": scn_sseq,
        "sachlCd": "10", "sachlTypCd": "01", "atktChnlCd": "01", "cusgdCd": "01",
        "bymd": "", "mbltNo": "", "nmbrCrtfNo": "", "movAtktNo": "",
        "seatPrmpDataList": [
            {
                "sbordNo": s.get("sbordNo", "001"), "seatAreaNo": s.get("seatAreaNo", "001"),
                "szoneNo": s.get("szoneNo", "01001"), "seatRowNm": s["seatRowNm"],
                "seatNo": s["seatNo"], "seatLocNo": s.get("seatLocNo", ""),
            }
            for s in seats
        ],
    }
    result = await pw_fetch(page, "POST", SITE_BASE + PRMP_PATH, body)
    return _check_pw_ok(result, "좌석선점")


async def pw_cancel_hold(page: Page, mov_atkt_no: str, seats: list[dict]) -> dict | None:
    body = {
        "coCd": CO_CD, "custNo": CUST_NO, "movAtktNo": mov_atkt_no,
        "rtctlScopCd": RTCTL_SCOP_CD, "sachlTypCd": "01",
        "seatPrmpDataList": [
            {
                "sbordNo": s.get("sbordNo", "001"), "seatAreaNo": s.get("seatAreaNo", "001"),
                "szoneNo": s.get("szoneNo", "01001"), "seatRowNm": s["seatRowNm"], "seatNo": s["seatNo"],
                "seatLocNo": s.get("seatLocNo", ""),
            }
            for s in seats
        ],
    }
    try:
        result = await pw_fetch(page, "POST", SITE_BASE + PRMP_CNCL_PATH, body)
        if result.get("status") != 200:
            print(f"[선점취소] HTTP {result.get('status')}: {result.get('text','')[:300]}", flush=True)
            return None
        return json.loads(result.get("text") or "{}")
    except Exception as e:
        print(f"[선점취소] 오류: {e}", flush=True)
        return None


async def pw_check_prices(page: Page, date: str, scns_no: str, scn_sseq: str, mov_no: str, seats: list[dict]) -> dict[str, int]:
    body = {
        "coCd": CO_CD, "siteNo": SITE_NO, "scnsNo": scns_no, "scnYmd": date,
        "scnSseq": scn_sseq, "movNo": mov_no, "rtctlScopCd": RTCTL_SCOP_CD,
        "prcrulDivCd": "01", "sachlTypCd": "01",
        "prodBnduList": [{"prodBnduCd": "01", "prodBnduQty": len(seats)}],
        "seatList": [
            {
                "seatLocNo": s.get("seatLocNo", ""), "szoneKindCd": s.get("szoneKindCd", "01"),
                "stkndCd": s.get("stkndCd", "01"), "seatSalfrmCd": s.get("seatSalfrmCd", "01"),
                "prodBnduCd": "01",
            }
            for s in seats
        ],
        "zoneGroupYn": "N",
    }
    result = await pw_fetch(page, "POST", SITE_BASE + PRC_LIST_PATH, body)
    price_body = _check_pw_ok(result, "가격조회")

    data = price_body.get("data")
    prices: dict[str, int] = {}
    if isinstance(data, list):
        for item in data:
            loc = item.get("seatLocNo")
            amt = item.get("salAmt")
            if loc and isinstance(amt, (int, float)):
                prices[loc] = int(amt)
    missing = [s.get("seatLocNo", "") for s in seats if s.get("seatLocNo", "") not in prices]
    if missing:
        raise BookingError(f"가격조회 응답에서 일부 좌석 금액을 찾을 수 없음 (누락: {missing})")
    return prices


async def pw_get_pay_id(page: Page, sched: dict, total_amt: int, seat_cnt: int, user_id: str, user_name: str) -> str:
    goods_name = f"{sched.get('expoProdNm') or sched.get('prodNm')} {SITE_NM_SHORT}"
    body = {
        "coCd": CO_CD, "siteCode": SITE_NO, "mrchClsCd": "1001", "sachlTypCd": "01",
        "rvpayYn": "N", "amountTotal": total_amt, "totpayFee": 0, "amountVat": 0,
        "amountTaxFree": 0, "amountTax": 0, "saleDt": sched["scnYmd"],
        "goodsName": goods_name, "goodsCnt": str(seat_cnt), "userId": user_id, "userName": user_name,
    }
    result = await pw_fetch(page, "POST", SITE_BASE + GET_PAY_ID_PATH, body)
    r = _check_pw_ok(result, "결제세션발급")
    paym_no = (r.get("data") or {}).get("payId")
    if not paym_no:
        raise BookingError(f"commonGetPayId 응답에서 payId를 찾을 수 없음: {json.dumps(r, ensure_ascii=False)[:500]}")
    return paym_no


async def pw_insert_temp_info(
    page: Page, sched: dict, seats: list[dict], amts: list[int],
    mov_atkt_no: str, paym_no: str, paym_vrfy_no: str,
) -> None:
    tpl = _load_template()["insertIssSalProcTempInfo_body"]
    cont = B.copy.deepcopy(tpl["paymInfoCont"])
    total = sum(amts)

    cont["saleDt"] = sched["scnYmd"]
    cont["paymNo"] = paym_no
    cont["paymVrifyNo"] = paym_vrfy_no
    cont["amountTotal"] = total
    cont["amountPaymTotal"] = 0
    cont["amountDiscount"] = 0
    cont["custNo"] = CUST_NO
    cont["goodsName"] = f"{sched.get('expoProdNm') or sched.get('prodNm')} {SITE_NM_SHORT}"

    _fill_mov_common(cont, sched, total, seats[0])
    cont["mov"]["bnduQty"] = str(len(seats))
    cont["mov"]["sellProductsList"] = [
        _build_sell_product(sched, seat, mov_atkt_no, amt) for seat, amt in zip(seats, amts)
    ]
    cont["mov"]["discountDatas"] = [None] * len(seats)

    body = {"coCd": CO_CD, "paymNo": paym_no, "paymVrifyNo": paym_vrfy_no, "paymInfoCont": json.dumps(cont, ensure_ascii=False)}
    result = await pw_fetch(page, "POST", SITE_BASE + INSERT_TEMP_PATH, body)
    _check_pw_ok(result, "결제정보임시저장")


# ── /mpy/main 결제화면용 sessionStorage 구성 ────────────────────────────────
# 이 화면은 API가 다 성공해도 sessionStorage의 pid/mov/movStore/query/movieGoers를
# 클라이언트에서 직접 읽어서 렌더링한다 (사용자가 실제 결제화면 DevTools로 전량 확인해줌).
# 안 채우면 "결제 정보를 불러올 수 없습니다" 에러가 뜬다.

def _build_session_mov(
    sched: dict, seats: list[dict], amts: list[int], mov_atkt_no: str, szone_exp_tm: str,
) -> dict:
    tpl = _load_template()["insertIssSalProcTempInfo_body"]
    mov = B.copy.deepcopy(tpl["paymInfoCont"]["mov"])
    total = sum(amts)
    mov["prodNo"] = sched["prodNo"]
    mov["movNo"] = sched["movNo"]
    mov["movNm"] = sched.get("prodNm") or sched.get("expoProdNm") or sched.get("movNm", "")
    mov["orgMovNm"] = sched.get("movNm", "")
    mov["scnYmd"] = sched["scnYmd"]
    mov["scnTm"] = f"{sched['scnsrtTm']}~{sched.get('scnendTm', '')}"
    mov["scnsNo"] = sched["scnsNo"]
    mov["scnSseq"] = sched["scnSseq"]
    mov["szoneKindCd"] = seats[0].get("szoneKindCd", "01")
    mov["stkndCd"] = seats[0].get("stkndCd", "01")
    mov["custNo"] = CUST_NO
    mov["sumSalAmt"] = str(total)
    mov["siteNo"] = SITE_NO
    mov["bnduQty"] = str(len(seats))
    # 템플릿에 박혀있던 과거 시각을 그대로 두면 화면이 "결제 가능 시간이 경과했습니다"로
    # 즉시 튕겨낸다 — 이번 좌석선점(seatTempPrmp) 응답의 실제 만료시각으로 덮어써야 한다.
    if szone_exp_tm:
        mov["szoneExpTm"] = szone_exp_tm
    mov["sellProductsList"] = [_build_sell_product(sched, seat, mov_atkt_no, amt) for seat, amt in zip(seats, amts)]
    return mov


def _build_session_movstore(sched: dict) -> dict:
    return {
        "movNo": sched["movNo"], "movNm": sched.get("movNm", ""),
        "scnYmd": sched["scnYmd"], "isScreen": "Y",
        "cratgClsCd": sched.get("cratgClsCd", ""),
        "isMounted": "active", "isSiteMounted": "",
        "siteNo": SITE_NO, "siteNm": SITE_NM_SHORT,
        "isDayMounted": "active",
    }


def _build_session_query(sched: dict) -> dict:
    q = dict(sched)
    q["coCd"] = CO_CD
    q.setdefault("soldierJoinStus", "N")
    return q


def _build_session_moviegoers(sched: dict, seats: list[dict], amts: list[int]) -> list[dict]:
    return [
        {
            "cd": "01", "nm": "일반", "scnYmd": sched["scnYmd"], "siteNo": SITE_NO,
            "scnsNo": sched["scnsNo"], "scnSseq": sched["scnSseq"],
            "seatLocNo": seat.get("seatLocNo", ""), "szoneNo": seat.get("szoneNo", ""),
            "stkndCd": seat.get("stkndCd", "01"), "seatAreaNo": seat.get("seatAreaNo", "001"),
            "szoneKindCd": seat.get("szoneKindCd", "01"), "seatSalfrmCd": seat.get("seatSalfrmCd", "01"),
            "prodBnduCd": "01", "movNo": sched["movNo"],
            "sbordNo": seat.get("sbordNo", "001"), "seatNo": seat["seatNo"], "seatRowNm": seat["seatRowNm"],
            "movAtktNo": None, "amount": 0,
            "szoneNm": seat.get("szoneNm", ""), "stkndNm": seat.get("stkndNm", ""),
            "szoneKindNm": seat.get("szoneKindNm", ""),
            "salAmt": amt, "scnAmt": amt, "tcsvcAmt": 0, "sasvcAmt": 0,
        }
        for seat, amt in zip(seats, amts)
    ]


async def pw_set_session_storage(
    page: Page, sched: dict, seats: list[dict], amts: list[int],
    mov_atkt_no: str, szone_exp_tm: str = "",
) -> None:
    # pid(paymNo/paymVrifyNo)는 일부러 안 채운다 — 페이지가 mov/movStore/query만 보고 자기가
    # 알아서 commonGetPayId + insertIssSalProcTempInfo를 호출해서 결제세션을 새로 만들기
    # 때문에, 우리가 pid를 미리 채워도 무시되고 페이지가 발급한 새 값으로 덮어써진다.
    payload = {
        "com": {"siteNo": SITE_NO, "saleDt": B.datetime.now().strftime("%Y%m%d")},
        "history": {"main": ["167", "", ""], "stoHotdl": ["Y"], "gftHotdl": ["Y"], "mbkTab": ["movieContent"]},
        "mov": _build_session_mov(sched, seats, amts, mov_atkt_no, szone_exp_tm),
        "movStore": _build_session_movstore(sched),
        "query": _build_session_query(sched),
        "movieGoers": _build_session_moviegoers(sched, seats, amts),
    }
    await page.evaluate(
        "(data) => { for (const [k, v] of Object.entries(data)) sessionStorage.setItem(k, JSON.stringify(v)); }",
        payload,
    )


async def pw_list_usable_giftc(page: Page, sched: dict, seat: dict) -> None:
    """
    searchUsepbGiftcFilterList: 이 상품에 쓸 수 있는 보유 관람권 목록 조회.
    실제 캡처에서 이 호출이 validateGiftcRetrn보다 먼저 일어남 — 건너뛰면
    validateGiftcRetrn이 "로그인이 필요합니다"로 거부됨 (관람권 조회 세션이
    먼저 서버에 등록돼야 하는 것으로 보임). 결과 자체는 안 씀, 호출만 필요.
    """
    qs = urlencode({
        "payKndCd": "1058", "coCd": CO_CD, "custNo": CUST_NO, "siteNo": SITE_NO,
        "bzplcNo": BZPLC_NO, "scnYmd": sched["scnYmd"], "scnsNo": sched["scnsNo"],
        "scnSseq": sched["scnSseq"], "prodBnduCd": "01",
        "szoneKindCd": seat.get("szoneKindCd", "01"), "stkndCd": seat.get("stkndCd", "01"),
        "prodNo": sched["prodNo"],
    })
    result = await pw_fetch(page, "GET", f"{SITE_BASE}{GIFTC_FILTER_LIST_PATH}?{qs}")
    _check_pw_ok(result, "관람권목록조회")


async def pw_apply_voucher(page: Page, sched: dict, seat: dict, giftc_no: str) -> None:
    qs = urlencode({"coCd": CO_CD, "giftcNo": giftc_no})
    result1 = await pw_fetch(page, "GET", f"{SITE_BASE}{GIFTC_RETRN_BSS_PATH}?{qs}")
    _check_pw_ok(result1, "관람권조회")

    body = {
        "coCd": CO_CD, "giftcNo": giftc_no, "siteNo": SITE_NO, "bzplcNo": BZPLC_NO,
        "scnYmd": sched["scnYmd"], "scnsNo": sched["scnsNo"], "scnSseq": sched["scnSseq"],
        "prodBnduCd": "01", "szoneKindCd": seat.get("szoneKindCd", "01"),
        "stkndCd": seat.get("stkndCd", "01"), "prodNo": sched["prodNo"], "custNo": CUST_NO,
        "giftcCrtfNo": None,
    }
    result2 = await pw_fetch(page, "POST", SITE_BASE + VALIDATE_GIFTC_PATH, body)
    _check_pw_ok(result2, "관람권검증")


async def pw_update_temp_info(
    page: Page, sched: dict, seats: list[dict], amts: list[int],
    mov_atkt_no: str, paym_no: str, paym_vrfy_no: str,
    giftc_nos: list[str], cust_orgnl_no: str,
) -> int:
    tpl = _load_template()["updateIssSalProcTempInfo_body"]
    cont = B.copy.deepcopy(tpl["paymInfoCont"])
    total = sum(amts)

    cash_receipt_cos_amts = [_cash_receipt_cos_amt(a) for a in amts]
    total_cash_receipt = sum(cash_receipt_cos_amts)

    cont["saleDt"] = sched["scnYmd"]
    cont["paymNo"] = paym_no
    cont["paymVrifyNo"] = paym_vrfy_no
    cont["amountTotal"] = total
    cont["amountDiscount"] = sum(amts)
    cont["amountPaymTotal"] = total - cont["amountDiscount"]
    cont["custNo"] = CUST_NO
    cont["goodsName"] = f"{sched.get('expoProdNm') or sched.get('prodNm')} {SITE_NM_SHORT}"
    cont["cashrtList"] = [total_cash_receipt] if total_cash_receipt else []

    _fill_mov_common(cont, sched, total, seats[0])
    cont["mov"]["sellProductsList"] = [
        _build_sell_product(sched, seat, mov_atkt_no, amt) for seat, amt in zip(seats, amts)
    ]
    cont["mov"]["discountDatas"] = [
        _build_giftc_discount(giftc_no, CUST_NO, cust_orgnl_no, amt, cra)
        for giftc_no, amt, cra in zip(giftc_nos, amts, cash_receipt_cos_amts)
    ]

    body = {"coCd": CO_CD, "paymNo": paym_no, "paymVrifyNo": paym_vrfy_no, "paymInfoCont": json.dumps(cont, ensure_ascii=False)}
    result = await pw_fetch(page, "POST", SITE_BASE + UPDATE_TEMP_PATH, body)
    _check_pw_ok(result, "결제정보갱신")
    return cont["amountPaymTotal"]


async def pw_confirm_booking(
    page: Page, sched: dict, seats: list[dict], amts: list[int],
    mov_atkt_no: str, paym_no: str, giftc_nos: list[str], cust_orgnl_no: str, mblt_no: str,
) -> dict:
    tpl = _load_template()["salCreateSal_body"]
    cash_receipt_cos_amts = [_cash_receipt_cos_amt(a) for a in amts]
    total_cash_receipt = sum(cash_receipt_cos_amts)

    sell_products = []
    for i, (seat, amt, giftc_no, cra) in enumerate(zip(seats, amts, giftc_nos, cash_receipt_cos_amts)):
        sp = _build_sell_product(sched, seat, mov_atkt_no, amt)
        sp["tmpSalSeq"] = f"mov-{i}"
        discount = _build_giftc_discount(giftc_no, CUST_NO, cust_orgnl_no, amt, cra)
        sp["discountDatas"] = [{**discount, "payDcSeq": None}]
        sell_products.append(sp)

    sal = B.copy.deepcopy(tpl["sal"])
    sal["custNo"] = CUST_NO
    sal["userNo"] = CUST_NO
    sal["movAtktYmd"] = sched["scnYmd"]
    sal["paymNo"] = paym_no
    sal["mbltNo"] = mblt_no
    sal["sellProductsList"] = sell_products

    pnt = B.copy.deepcopy(tpl.get("pnt") or {})

    cashrt = B.copy.deepcopy((tpl.get("cashrtList") or [{}])[0])
    cashrt["paymNo"] = paym_no
    cashrt["saleDt"] = sched["scnYmd"]
    cashrt["goodsCnt"] = str(len(seats))
    cashrt["amountTotal"] = total_cash_receipt
    cashrt["amountVat"] = round(total_cash_receipt / 11) if total_cash_receipt else 0
    cashrt["amountTax"] = total_cash_receipt - cashrt["amountVat"]
    cashrt["goodsName"] = f"{sched.get('expoProdNm') or sched.get('prodNm')} {SITE_NM_SHORT}"

    body = {"paym": None, "sal": sal, "pnt": pnt, "cashrtList": [cashrt] if total_cash_receipt else []}
    result = await pw_fetch(page, "POST", SITE_BASE + CREATE_SAL_PATH, body)
    return _check_pw_ok(result, "예매확정")


# ── 실제 클릭 기반 관람권 적용 / 결제확정 ────────────────────────────────────
# /api/v1/payment/cif/* (관람권 조회·검증)는 페이지에 주입한 fetch()로는 계속
# "인증된 사용자의 접근이 아닙니다"로 거부됐다 (다른 모든 fetch() 기반 호출은 성공).
# 실제 화면에서 체크박스 클릭 → 적용 버튼 클릭으로 관람권 UI를 직접 조작하면 이 검사를
# 우회할 필요가 없다 — 진짜 신뢰된 클릭 이벤트라 CGV 자체 JS가 알아서 처리한다.
# (사용자가 DevTools로 확인해준 실제 구조: input[name="movGft"] 체크박스들 + "적용하기" 버튼,
# 최종 결제 버튼은 .botFix 안의 "N원 결제하기" 버튼.)

_AMOUNT_RE = re.compile(r"([\d,]+)\s*원")


def _parse_amount(text: str) -> int | None:
    m = _AMOUNT_RE.search(text or "")
    return int(m.group(1).replace(",", "")) if m else None


async def pw_raise_if_blocking_modal(page: Page) -> None:
    """
    좌석선점 시간이 다 되면 "결제 가능시간이 지났습니다" 같은 alert 모달이 뜨는데, 이걸
    모르고 다른 버튼을 클릭하려 하면 모달 배경이 클릭을 가로채서 Playwright가 30초씩
    타임아웃날 때까지 재시도만 한다. 클릭하기 전에 먼저 확인해서 바로 실패 처리한다.
    """
    modal = page.locator(".cgv-modal.modal-alert.active")
    if await modal.count() > 0 and await modal.first.is_visible():
        text = (await modal.first.inner_text()).strip().replace("\n", " ")
        try:
            storage = await page.evaluate(
                "() => ({"
                "  session: Object.fromEntries(Object.keys(sessionStorage).map(k => [k, sessionStorage.getItem(k)])),"
                "  local: Object.fromEntries(Object.keys(localStorage).map(k => [k, localStorage.getItem(k)])),"
                "  cookie: document.cookie,"
                "  url: location.href"
                "})"
            )
            debug_path = B.Path(__file__).parent / "debug_sessionstorage.json"
            debug_path.write_text(json.dumps(storage, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[디버그] storage 덤프 저장(session+local+cookie+url): {debug_path}", flush=True)
        except Exception as e:
            print(f"[디버그] storage 덤프 실패: {e}", flush=True)
        raise BookingError(f"결제 화면에 안내 모달 뜸(시간 초과 등으로 추정): {text[:200]}")


async def pw_apply_vouchers_ui(page: Page, seat_count: int) -> None:
    """관람권/기프트콘 섹션을 열고 체크박스를 전부 선택한 뒤 적용 버튼을 클릭한다."""
    await pw_raise_if_blocking_modal(page)
    await page.get_by_text("CGV영화관람권/기프트콘", exact=True).click()
    await page.wait_for_timeout(500)

    checkboxes = page.locator('input[name="movGft"]')
    count = await checkboxes.count()
    if count < seat_count:
        debug_path = B.Path(__file__).parent / "debug_screenshot.png"
        try:
            await page.screenshot(path=str(debug_path), full_page=True)
            print(f"[디버그] 관람권 체크박스 부족 스크린샷 저장: {debug_path}", flush=True)
        except Exception as e:
            print(f"[디버그] 스크린샷 저장 실패: {e}", flush=True)
        raise BookingError(f"관람권 체크박스가 좌석 수보다 적음 (체크박스 {count}개, 좌석 {seat_count}개)")
    for i in range(count):
        cb = checkboxes.nth(i)
        if await cb.is_checked():
            continue
        try:
            # force=True로 input을 직접 체크하면 React onChange가 안 붙는 것으로 보여서(state가
            # 안 바뀜), 실제 사용자처럼 연결된 <label for="..">를 클릭한다. 체크된 상태가 될
            # 때까지 최대 5초 기다린다.
            cb_id = await cb.get_attribute("id")
            if cb_id:
                await page.locator(f'label[for="{cb_id}"]').click()
            else:
                await cb.click(force=True)
            for _ in range(10):
                if await cb.is_checked():
                    break
                await page.wait_for_timeout(500)
            else:
                raise BookingError("클릭 후 5초 내에 체크 상태로 안 바뀜")
        except Exception as e:
            debug_path = B.Path(__file__).parent / "debug_screenshot.png"
            try:
                await page.screenshot(path=str(debug_path), full_page=True)
                print(f"[디버그] 체크박스({i}) 실패 스크린샷 저장: {debug_path}", flush=True)
            except Exception:
                pass
            raise BookingError(f"관람권 체크박스({i}) 선택 실패: {e}")
        # 체크 한 번마다 서버 검증(validateGiftcRetrn 등)이 비동기로 붙는 것으로 보여,
        # 다음 체크박스를 누르기 전에 짧게 기다려준다.
        await page.wait_for_timeout(1200)

    await page.get_by_role("button", name=re.compile("적용하기")).click()
    await page.wait_for_timeout(1500)


async def pw_read_payable_amount(page: Page) -> int:
    """하단 결제 버튼에 표시된 실제 결제금액을 읽는다 (관람권 적용이 서버에 실제로 반영됐는지 확인용)."""
    btn = page.locator(".botFix button", has_text="결제하기")
    text = await btn.inner_text()
    amt = _parse_amount(text)
    if amt is None:
        raise BookingError(f"결제 버튼에서 금액을 못 읽음: {text!r}")
    return amt


async def pw_wait_payable_amount(page: Page, expect: int | None, timeout: float = 8.0) -> int:
    """
    결제 버튼 금액을 읽되, 새로고침/적용 직후 화면이 아직 안 그려져서 0원 같은 placeholder가
    보일 수 있어 값이 안정될 때까지(=expect와 같아질 때까지, expect 없으면 0이 아닐 때까지)
    짧게 폴링한다.
    """
    deadline = 0.0
    last = 0
    while deadline < timeout:
        last = await pw_read_payable_amount(page)
        if expect is not None:
            if last == expect:
                return last
        elif last != 0:
            return last
        await page.wait_for_timeout(500)
        deadline += 0.5
    return last


async def pw_agree_terms(page: Page) -> None:
    """"전체 약관 동의하기" 체크 안 하면 결제 버튼 클릭이 "약관을 체크해주세요"로 막힌다."""
    label = page.get_by_text("전체 약관 동의하기", exact=True)
    if await label.count() == 0:
        return
    checkbox = page.locator('input[type="checkbox"]').filter(
        has=page.locator("xpath=following-sibling::*[contains(., '전체 약관 동의하기')]")
    )
    # 체크박스를 직접 못 찾으면 라벨 텍스트 자체를 클릭 (커스텀 체크박스는 보통 라벨이 토글을 겸함)
    try:
        if await checkbox.count() > 0 and not await checkbox.first.is_checked():
            await checkbox.first.check(force=True)
        else:
            await label.click()
    except Exception:
        await label.click()
    await page.wait_for_timeout(500)


async def pw_click_confirm_payment(page: Page) -> None:
    """최종 결제 버튼 클릭 (호출 전 pw_read_payable_amount로 0원인지 반드시 확인할 것)."""
    await pw_raise_if_blocking_modal(page)
    await pw_agree_terms(page)
    btn = page.locator(".botFix button", has_text="결제하기")
    await btn.click()
    await page.wait_for_timeout(2000)


async def auto_book(
    token: str, date: str, scns_no: str, scn_sseq: str, mov_no: str,
    cfg: dict, target_pair: tuple[str, int, int] | None = None,
    row_priority: list[str] | None = None,
    center_seats: list[int] | None = None,
    seats: list[dict] | None = None,
) -> dict | None:
    """
    2연석 선점 → 관람권 적용 → 최종확정. 브라우저는 예열된 걸 재사용(매번 새로 안 띄움),
    좌석찾기는 curl_cffi로 빠르게(가능하면 그마저도 생략). 성공 시 예매 정보 dict 반환,
    실패/DRY_RUN/카드결제 필요 시 None 반환.

    row_priority/center_seats가 주어지면(신규 회차 케이스) target_pair가 이미 사라졌을 때
    find_seat_pair()가 이 우선순위를 따라 다음 후보 2연석을 찾는다.
    seats가 이미 주어지면(alarm.py가 이번 라운드에 조회해둔 좌석 상세) searchIfSeatData를
    또 조회하지 않고 바로 그 좌석으로 좌석선점을 시도한다 — 감지~선점 사이 지연을 없앤다.
    """
    async with AsyncSession(impersonate=B._IMPERSONATE) as client:
        if seats is None:
            seats = await find_seat_pair(
                client, date, scns_no, scn_sseq, cfg, target_pair,
                row_priority=row_priority, center_seats=center_seats,
            )
            if not seats:
                print("[자동예매] 명당 조건에 맞는 2연석을 찾지 못함", flush=True)
                return None
        sched = await fetch_schedule_detail(client, token, date, scns_no, scn_sseq, cfg)

    seat_label = " ".join(f"{s.get('seatRowNm')}{s.get('seatNo')}" for s in seats)
    print(f"[자동예매] (예열된 브라우저) 2연석 선점 시도: {seat_label}", flush=True)

    import time as _time
    _t0 = _time.monotonic()

    def _mark(label: str) -> None:
        print(f"[타이밍] {label}: +{_time.monotonic() - _t0:.1f}s", flush=True)

    page = await get_warm_page()
    if page.url != REFERER_SEAT_SELECT:
        await page.goto(REFERER_SEAT_SELECT, wait_until="domcontentloaded")

    mov_atkt_no = ""
    try:
        hold_result = await pw_hold_seats(page, date, scns_no, scn_sseq, seats)
        _mark("좌석선점 완료")
        data = hold_result.get("data", {})
        mov_atkt_no = data.get("movAtktNo", "")
        if not mov_atkt_no:
            raise BookingError(f"좌석선점 응답에 movAtktNo 없음: {json.dumps(hold_result, ensure_ascii=False)[:300]}")
        szone_exp_tm = data.get("seatTempPrmpLimitDt", "")
        print(f"[자동예매] 선점 성공 movAtktNo={mov_atkt_no} 만료={szone_exp_tm}", flush=True)

        if DRY_RUN:
            print("[자동예매] DRY_RUN 모드 — 관람권 적용/확정 생략 후 즉시 취소", flush=True)
            await pw_cancel_hold(page, mov_atkt_no, seats)
            return None

        if len(GIFTC_NOS) != len(seats):
            raise BookingError(
                f".env의 GIFTC_NOS에 좌석 수({len(seats)})만큼 관람권 번호가 설정되지 않음 "
                f"(현재 {len(GIFTC_NOS)}개). 예: GIFTC_NOS=코드1,코드2"
            )

        prices = await pw_check_prices(page, date, scns_no, scn_sseq, mov_no, seats)
        amts = [prices[s.get("seatLocNo", "")] for s in seats]
        _mark("가격조회 완료")

        # 네트워크 로그로 확인한 사실: /mpy/main 페이지는 sessionStorage의 mov/movStore/
        # query/movieGoers만 보고 자기가 알아서 commonGetPayId + insertIssSalProcTempInfo를
        # 직접 호출해서 결제세션을 새로 만든다 — 우리가 미리 만든 paymNo/paymVrifyNo(pid)는
        # 무시되고 버려진다. 그래서 이제 그 두 호출은 하지 않고 페이지에 맡긴다.
        await pw_set_session_storage(page, sched, seats, amts, mov_atkt_no, szone_exp_tm)

        # networkidle은 GA 등 트래킹 요청이 계속 붙어서 느리다 — domcontentloaded로
        # 빠르게 넘어가고, 실제 렌더 완료 여부는 아래 금액 폴링으로 확인한다.
        await page.goto(REFERER_PAYMENT, wait_until="domcontentloaded")
        _mark("/mpy/main 이동 완료")
        initial_amt = await pw_wait_payable_amount(page, expect=sum(amts))
        _mark(f"결제금액 확인 완료 ({initial_amt}원)")
        if initial_amt != sum(amts):
            debug_path = B.Path(__file__).parent / "debug_screenshot.png"
            try:
                await page.screenshot(path=str(debug_path), full_page=True)
                print(f"[디버그] 스크린샷 저장: {debug_path}", flush=True)
            except Exception as e:
                print(f"[디버그] 스크린샷 저장 실패: {e}", flush=True)
            raise BookingError(
                f"결제 화면 금액이 예상과 다름 (화면={initial_amt}원, 예상={sum(amts)}원) — "
                f"잘못된 세션/화면일 수 있어 중단합니다."
            )

        # /api/v1/payment/cif/*(관람권 조회·검증)는 fetch() 주입으로는 "인증된 사용자의
        # 접근이 아닙니다"로 거부돼서(신뢰된 클릭 이벤트가 필요한 것으로 추정), 실제
        # 체크박스 클릭 → 적용 버튼 클릭으로 우회한다.
        await pw_apply_vouchers_ui(page, len(seats))
        _mark("관람권 적용 완료")

        amount_paym_total = await pw_wait_payable_amount(page, expect=0)
        if amount_paym_total != 0:
            raise BookingError(
                f"관람권으로 전액 커버되지 않음 (남은 결제금액={amount_paym_total}원) — "
                f"실카드 결제가 필요해 자동확정을 중단합니다. 수동으로 완료해주세요."
            )

        await pw_click_confirm_payment(page)
        _mark("결제확정 클릭 완료")
        result = {"final_url": page.url}
        print(f"[자동예매] 예매확정 클릭 완료. 최종 URL: {page.url}", flush=True)
        return {
            "mov_atkt_no": mov_atkt_no,
            "seat": seat_label,
            "confirmed_at": B.datetime.now().isoformat(),
            "result": result,
        }
    except Exception:
        print("[자동예매] 실패 — 좌석선점 롤백 시도", flush=True)
        if mov_atkt_no:
            try:
                await pw_cancel_hold(page, mov_atkt_no, seats)
            except Exception as e:
                print(f"[자동예매] 롤백 실패(무시하고 계속): {e}", flush=True)
        # 다음 시도를 위해 좌석선택 화면으로 되돌려놓는다(결제화면 등으로 넘어가 있었을 수 있음).
        # 브라우저는 절대 닫지 않는다 — 예열 상태를 유지해야 다음 감지 때 지연이 없다.
        try:
            await page.goto(REFERER_SEAT_SELECT, wait_until="domcontentloaded")
        except Exception:
            pass
        raise


async def main() -> None:
    import sys
    import asyncio
    import alarm as A

    if len(sys.argv) != 5:
        print("사용법: python browser_booking.py <YYYYMMDD> <scnsNo> <scnSseq> <movNo>")
        sys.exit(1)

    date, scns_no, scn_sseq, mov_no = sys.argv[1:5]
    cfg = dict(A.DEFAULT_CONFIG)
    token = A.load_token()

    try:
        result = await auto_book(token, date, scns_no, scn_sseq, mov_no, cfg)
    except Exception as e:
        print(f"[자동예매] 오류: {e}", flush=True)
        return
    finally:
        # CLI 단독 실행일 때는 예열 브라우저를 계속 띄워둘 이유가 없으니 정리한다.
        await close_warm_page()

    if result:
        print(f"\n[결과] 예매 완료: {result}")
    else:
        print("\n[결과] 예매 확정 없음 (DRY_RUN이거나 2연석 없음/카드결제 필요)")


if __name__ == "__main__":
    import asyncio
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    asyncio.run(main())
