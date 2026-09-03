"""
CGV IMAX 2연석 자동예매 (좌석선점 → 관람권 적용 → 최종확정)

[흐름] (capture_booking_flow.py로 실제 브라우저 결제를 1석/2석 각각 캡처해서 확인한 순서)
 1. hold_seats              — seatTempPrmp: 좌석 임시선점 (여러 좌석을 한 번에 선점 가능,
                               movAtktNo 하나가 전체 좌석에 공유됨 — 2연석 캡처로 확인)
 2. check_before_pay        — searchAtktBefChkPay: 결제 전 사전체크 (x2, 브라우저와 동일하게 재현)
 3. get_pay_id              — commonGetPayId: 결제세션 발급 (payId → paymNo로 사용).
                               paymVrifyNo는 서버가 안 주고 클라이언트가 임의로 만드는 랜덤
                               문자열이라(2번 캡처 모두 26자 랜덤 alnum) 우리도 그냥 생성한다.
 4. insert_temp_info        — insertIssSalProcTempInfo: 결제정보 임시저장 (관람권 적용 전)
 5. apply_voucher           — searchPrdGiftcRetrnBss + validateGiftcRetrn: 좌석 수만큼,
                               관람권 1장당 1회 호출 (좌석-관람권 매핑은 순서 무관해 보임)
 6. update_temp_info        — updateIssSalProcTempInfo: 결제정보 갱신 (좌석별 관람권 할인 반영)
 7. confirm_booking         — salCreateSal: 최종 예매확정 (좌석별로 sellProductsList 항목 분리,
                               현금영수증(cashrtList)은 전체 합산해서 1건만 발급)
 8. 실패 시 어느 단계에서든 cancel_hold로 롤백 — 다른 고객 좌석 점유 방지

[페이로드 구조]
insertIssSalProcTempInfo/updateIssSalProcTempInfo/salCreateSal은 필드가 매우 많고(고객 프로필이
클라이언트 JS에서 AES로 암호화되어 들어감) 알고리즘을 모르므로, capture_booking_flow.py로 캡처한
*실제 성공한* 요청 바디를 payment_template.json(gitignore됨)에 저장해두고, 이번 구매에서 달라지는
필드(좌석/세션/영화/금액/관람권번호/paymNo 등)만 골라서 덮어쓰는 방식으로 만든다.
암호화된 고객정보(cust/cjOneUser)·카드 placeholder 등은 같은 계정이면 값이 그대로 재사용 가능하므로
템플릿 값을 그대로 둔다.

[2연석 관련 확인된 사실 — 2석 실결제 캡처로 검증]
- searchMovAtktSeatPrcList 응답의 data는 좌석별 가격 리스트: [{seatLocNo, salAmt, ...}, ...]
- seatTempPrmp는 seatPrmpDataList에 좌석 여러 개를 한 번에 넣을 수 있고, 응답 movAtktNo 하나를
  모든 좌석이 공유한다 (좌석별로 따로 나오지 않음).
- 관람권(giftc)은 좌석 1개당 1장 필요. validateGiftcRetrn을 관람권 개수만큼 반복 호출.
- updateIssSalProcTempInfo/salCreateSal의 mov.discountDatas / sellProductsList[].discountDatas는
  좌석 개수만큼 항목이 생기고, 각 항목이 서로 다른 giftcNo 하나씩을 참조한다.
- 현금영수증(cashrtList)은 좌석별이 아니라 구매 전체 합산 금액으로 딱 1건만 생성된다.

[중요한 안전장치]
- 관람권으로 결제금액이 "완전히" 커버되지 않으면(amountPaymTotal != 0) confirm_booking을 호출하지
  않고 즉시 중단한다. 실카드 결제가 필요한 상황을 자동으로 진행시키지 않기 위한 안전장치.
- DRY_RUN=true(기본값)면 좌석선점 직후 바로 취소하고 관람권/결제 단계는 건드리지 않는다.
- 지정된 2연석(alarm.py가 찾은 pair) 중 하나라도 그 사이에 다른 사람이 채갔으면, 그 쌍은 포기하고
  같은 조건의 다른 2연석을 다시 찾는다 (낱개 좌석으로는 절대 진행하지 않음).
"""

import copy
import json
import os
import secrets
import string
from datetime import datetime
from pathlib import Path

from curl_cffi.requests import AsyncSession
from dotenv import load_dotenv

from alarm import fetch_schedule, find_seat_pairs, rank_seat_pairs, make_headers, DEFAULT_CONFIG

load_dotenv()

ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "")
CGV_COOKIES  = os.getenv("CGV_COOKIES", "")
CUST_NO      = os.getenv("CUST_NO", "")
# 사용할 영화관람권(기프트카드) 번호들, 쉼표 구분. 2연석이면 정확히 2개 필요.
GIFTC_NOS    = [x.strip() for x in os.getenv("GIFTC_NOS", "").split(",") if x.strip()]
DRY_RUN      = os.getenv("DRY_RUN", "true").lower() != "false"

_IMPERSONATE = "chrome146"

API_BASE   = "https://api.cgv.co.kr"
SITE_BASE  = "https://cgv.co.kr"

SEAT_PATH            = "/cnm/atkt/searchIfSeatData"
PRMP_PATH            = "/api/v1/content/seatTemp/seatTempPrmp"
PRMP_CNCL_PATH       = "/api/v1/content/seatTemp/seatTempPrmpCncl"
PRC_LIST_PATH        = "/api/v1/booking/searchMovAtktSeatPrcList"
BEF_CHK_PAY_PATH     = "/api/v1/booking/searchAtktBefChkPay"
GET_PAY_ID_PATH      = "/api/v1/payment/pay/commonGetPayId"
INSERT_TEMP_PATH     = "/api/v1/payment/mpy/proc/insertIssSalProcTempInfo"
UPDATE_TEMP_PATH     = "/api/v1/payment/mpy/proc/updateIssSalProcTempInfo"
GIFTC_FILTER_LIST_PATH = "/api/v1/payment/cif/searchUsepbGiftcFilterList"
GIFTC_RETRN_BSS_PATH   = "/api/v1/store/prd/searchPrdGiftcRetrnBss"
VALIDATE_GIFTC_PATH  = "/api/v1/payment/cif/validateGiftcRetrn"
CREATE_SAL_PATH      = "/api/v1/payment/iss/salCreateSal"

CO_CD         = "A420"
SITE_NO       = "0013"
BZPLC_NO      = "0013001"
RTCTL_SCOP_CD = "08"
SITE_NM_SHORT = "용산아이파크몰"

TEMPLATE_FILE = Path(__file__).parent / "payment_template.json"

# 주의: 이 파일(booking.py, curl_cffi 기반)의 좌석선점/결제 POST는 Cloudflare가 막는다
# (원본 test_seat_hold.py도 동일 증상 — 프로젝트 버그 아님). 실제 alarm.py 자동예매는
# browser_booking.py(Playwright)를 쓴다. 이 파일은 curl_cffi로 되는 부분(좌석조회 등)의
# 재사용 소스이자 독립 CLI 테스트용으로 남겨둔다.

_template_cache: dict | None = None


class BookingError(Exception):
    pass


def _load_template() -> dict:
    global _template_cache
    if _template_cache is None:
        if not TEMPLATE_FILE.exists():
            raise BookingError(
                f"{TEMPLATE_FILE.name} 없음 — capture_booking_flow.py로 실제 결제 흐름을 "
                f"먼저 캡처한 뒤, 캡처 로그에서 요청 바디를 추출해 저장해두세요."
            )
        _template_cache = json.loads(TEMPLATE_FILE.read_text(encoding="utf-8"))
    return _template_cache


def _gen_paym_vrfy_no(length: int = 26) -> str:
    """paymVrifyNo는 서버 응답에 없고 클라이언트가 임의로 만드는 상관관계용 랜덤 문자열
    (실제 캡처 2건 모두 26자 영숫자 랜덤값)이라 우리도 그냥 생성한다."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


# referer는 호출 단계별로 다르게 검증되는 것으로 확인됨(2연석 캡처에서 401로 실측):
# 좌석선택 단계(좌석조회/가격조회/좌석선점/선점취소)는 REFERER_SEAT_SELECT를 써야 하고,
# 여기에 REFERER_PAYMENT("/mpy/main")를 쓰면 seatTempPrmp가 401을 반환한다.
REFERER_MOVIE_BOOK  = "https://cgv.co.kr/cnm/movieBook/movie"
REFERER_SEAT_SELECT = "https://cgv.co.kr/cnm/selectVisitorCnt"
REFERER_PAYMENT     = "https://cgv.co.kr/mpy/main"


def site_headers(referer: str = REFERER_SEAT_SELECT) -> dict:
    """
    cgv.co.kr/api/v1/... 용 헤더 (서명 없음, Bearer+Cookie만).

    user-agent/sec-ch-ua* 는 일부러 안 넣는다 — curl_cffi의 impersonate(_IMPERSONATE)가
    TLS/HTTP2 지문에 맞춰 자동으로 채워주는데, 예전에 여기서 수동으로 "Chrome/151 Windows"를
    박아놨더니 실제 impersonate 지문(chrome146, macOS)과 안 맞아서 Cloudflare가 좌석선점
    같은 민감한 쓰기 요청만 401로 걸러내는 원인이 됐다 (읽기 요청은 검사가 느슨해서 통과됨).
    """
    h = {
        "accept": "application/json",
        "accept-language": "ko-KR",
        "authorization": f"Bearer {ACCESS_TOKEN}",
        "content-type": "application/json",
        "origin": "https://cgv.co.kr",
        "referer": referer,
        "priority": "u=1, i",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    if CGV_COOKIES:
        h["cookie"] = CGV_COOKIES
    return h


def _check_ok(resp, label: str) -> dict:
    if resp.status_code != 200:
        raise BookingError(f"{label} HTTP {resp.status_code}: {resp.text[:500]}")
    try:
        body = resp.json()
    except Exception as e:
        raise BookingError(f"{label} 응답 JSON 파싱 실패: {e} / {resp.text[:500]}")
    if isinstance(body, dict) and body.get("statusCode") not in (0, None):
        raise BookingError(f"{label} API 오류: {body.get('statusMessage')}")
    return body


# ── 좌석 조회 / 선점 / 취소 ──────────────────────────────────────────────────

async def find_seat_pair(
    client: AsyncSession, date: str, scns_no: str, scn_sseq: str, cfg: dict,
    target_pair: tuple[str, int, int] | None = None,
    row_priority: list[str] | None = None,
    center_seats: list[int] | None = None,
) -> list[dict] | None:
    """
    searchIfSeatData에서 명당 조건에 맞는 2연석(같은 열, 연속 좌석번호)을 찾는다.
    target_pair=(row, a, b)가 주어지면 그 쌍이 아직 살아있는지 먼저 확인하고,
    없어졌으면(다른 사람이 채감) 같은 조건의 다른 2연석을 새로 찾는다.
    row_priority가 주어지면(신규 회차 케이스) 대체 탐색도 이 우선순위 순서를 따른다 —
    없으면 기존처럼 find_seat_pairs()의 기본 순서(열 알파벳순 → 번호순)를 그대로 쓴다.
    """
    params = {
        "coCd": CO_CD, "siteNo": SITE_NO, "scnYmd": date,
        "scnsNo": scns_no, "scnSseq": scn_sseq,
        "seatAreaNo": "001", "cusgdCd": "01",
    }
    if CUST_NO:
        params["custNo"] = CUST_NO

    resp = await client.get(
        API_BASE + SEAT_PATH, params=params,
        headers=make_headers(ACCESS_TOKEN, SEAT_PATH), timeout=15,
    )
    body = _check_ok(resp, "좌석조회")
    items = (body.get("data") or {}).get("items") or []
    if not items:
        return None

    prime_rows = set(cfg.get("prime_rows") or [])
    seat_min   = cfg.get("prime_seat_min", 17)
    seat_max   = cfg.get("prime_seat_max", 28)

    seats_by_key: dict[tuple[str, int], dict] = {}
    for s in items[0].get("seats", []):
        if s.get("seatSaleYn") != "Y":
            continue
        row = s.get("seatRowNm", "")
        try:
            no = int(s.get("seatNo", ""))
        except (TypeError, ValueError):
            continue
        if row in prime_rows and seat_min <= no <= seat_max:
            seats_by_key[(row, no)] = s

    def pair_from(row: str, a: int, b: int) -> list[dict] | None:
        s1, s2 = seats_by_key.get((row, a)), seats_by_key.get((row, b))
        return [s1, s2] if s1 and s2 else None

    if target_pair:
        found = pair_from(*target_pair)
        if found:
            return found
        print(f"[자동예매] 지정된 2연석 {target_pair}이 이미 사라짐 — 다른 2연석 재탐색", flush=True)

    candidates = find_seat_pairs(set(seats_by_key.keys()))
    if row_priority:
        candidates = rank_seat_pairs(candidates, row_priority, center_seats or [])
    for row, a, b in candidates:
        found = pair_from(row, a, b)
        if found:
            return found
    return None


async def hold_seats(client: AsyncSession, date: str, scns_no: str, scn_sseq: str, seats: list[dict]) -> dict:
    """seatTempPrmp: 좌석 여러 개를 한 번에 임시 선점 (응답 movAtktNo 하나를 전체가 공유)"""
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
    payload = json.dumps(body, ensure_ascii=False)
    resp = await client.post(SITE_BASE + PRMP_PATH, content=payload.encode("utf-8"), headers=site_headers(), timeout=15)
    return _check_ok(resp, "좌석선점")


async def cancel_hold(client: AsyncSession, mov_atkt_no: str, seats: list[dict]) -> dict | None:
    """seatTempPrmpCncl: 좌석 임시 선점 취소 (롤백용, 실패해도 예외를 던지지 않음)"""
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
    payload = json.dumps(body, ensure_ascii=False)
    try:
        resp = await client.post(SITE_BASE + PRMP_CNCL_PATH, content=payload.encode("utf-8"), headers=site_headers(), timeout=15)
        if resp.status_code != 200:
            print(f"[선점취소] HTTP {resp.status_code}: {resp.text[:300]}", flush=True)
            return None
        return resp.json()
    except Exception as e:
        print(f"[선점취소] 오류: {e}", flush=True)
        return None


# ── 스케줄 상세 조회 (prodNo/movfNo 등 결제 페이로드에 필요한 필드) ────────────

async def fetch_schedule_detail(client: AsyncSession, token: str, date: str, scns_no: str, scn_sseq: str, cfg: dict) -> dict:
    resp = await fetch_schedule(client, token, date, cfg)
    body = _check_ok(resp, "스케줄상세")
    data = body.get("data") or []
    for d in data:
        if d.get("scnsNo") == scns_no and str(d.get("scnSseq")) == str(scn_sseq):
            return d
    raise BookingError(f"스케줄 상세 정보를 찾지 못함 (scnsNo={scns_no}, scnSseq={scn_sseq})")


# ── 결제 전 사전체크 / 가격조회 ────────────────────────────────────────────

async def check_before_pay(client: AsyncSession, date: str, scns_no: str, scn_sseq: str, szone_kind_cd: str, sascns_grad_cd: str) -> None:
    """searchAtktBefChkPay: 실제 브라우저 플로우에서 결제 진입 직전 호출되는 사전체크 (x2)"""
    base_params = {
        "coCd": CO_CD, "siteNo": SITE_NO, "scnYmd": date,
        "scnsNo": scns_no, "scnSseq": scn_sseq, "cxprdYn": "N",
    }
    if CUST_NO:
        base_params["custNo"] = CUST_NO

    calls = [
        ({**base_params, "dblfrRpsntYn": "N"}, REFERER_MOVIE_BOOK),
        ({**base_params, "szoneKindCd": szone_kind_cd, "sascnsGradCd": sascns_grad_cd}, REFERER_SEAT_SELECT),
    ]
    for params, referer in calls:
        resp = await client.get(SITE_BASE + BEF_CHK_PAY_PATH, params=params, headers=site_headers(referer), timeout=15)
        _check_ok(resp, "결제사전체크")


async def check_prices(client: AsyncSession, date: str, scns_no: str, scn_sseq: str, mov_no: str, seats: list[dict]) -> dict[str, int]:
    """searchMovAtktSeatPrcList: 좌석별 가격조회. 응답 data는 [{seatLocNo, salAmt, ...}, ...] 리스트."""
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
    payload = json.dumps(body, ensure_ascii=False)
    resp = await client.post(SITE_BASE + PRC_LIST_PATH, content=payload.encode("utf-8"), headers=site_headers(), timeout=15)
    price_body = _check_ok(resp, "가격조회")

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
        raise BookingError(f"가격조회 응답에서 일부 좌석 금액을 찾을 수 없음 (누락: {missing}): {json.dumps(price_body, ensure_ascii=False)[:500]}")
    return prices


# ── 결제세션 발급 ────────────────────────────────────────────────────────────

async def get_pay_id(client: AsyncSession, sched: dict, total_amt: int, seat_cnt: int, user_id: str, user_name: str) -> str:
    """commonGetPayId: 결제세션 발급 → paymNo(=payId) 반환"""
    goods_name = f"{sched.get('expoProdNm') or sched.get('prodNm')} {SITE_NM_SHORT}"
    body = {
        "coCd": CO_CD, "siteCode": SITE_NO, "mrchClsCd": "1001", "sachlTypCd": "01",
        "rvpayYn": "N", "amountTotal": total_amt, "totpayFee": 0, "amountVat": 0,
        "amountTaxFree": 0, "amountTax": 0, "saleDt": sched["scnYmd"],
        "goodsName": goods_name, "goodsCnt": str(seat_cnt), "userId": user_id, "userName": user_name,
    }
    payload = json.dumps(body, ensure_ascii=False)
    resp = await client.post(SITE_BASE + GET_PAY_ID_PATH, content=payload.encode("utf-8"), headers=site_headers(REFERER_PAYMENT), timeout=15)
    result = _check_ok(resp, "결제세션발급")
    paym_no = (result.get("data") or {}).get("payId")
    if not paym_no:
        raise BookingError(f"commonGetPayId 응답에서 payId를 찾을 수 없음: {json.dumps(result, ensure_ascii=False)[:500]}")
    return paym_no


# ── 결제정보 임시저장 / 관람권 적용 / 최종확정 ──────────────────────────────

def _build_ticket_products(sched: dict, seat: dict, mov_atkt_no: str, sal_amt: int) -> dict:
    return {
        "scnYmd": sched["scnYmd"], "scnTm": sched["scnsrtTm"], "siteNo": SITE_NO,
        "scnsNo": sched["scnsNo"], "scnsNm": sched.get("scnsNm") or sched.get("expoScnsNm") or "",
        "scnSseq": sched["scnSseq"], "seatLocNo": seat.get("seatLocNo", ""),
        "szoneCd": seat.get("szoneNo", ""), "szoneNm": seat.get("szoneNm", ""),
        "szoneNo": seat.get("szoneNo", ""), "stkndCd": seat.get("stkndCd", "01"),
        "stkndNm": seat.get("stkndNm", ""), "seatAreaNo": seat.get("seatAreaNo", "001"),
        "szoneKindCd": seat.get("szoneKindCd", "01"), "szoneKindNm": seat.get("szoneKindNm", ""),
        "seatSalfrmCd": seat.get("seatSalfrmCd", "01"), "siteGradCd": sched.get("siteGradCd", "01"),
        "tcscnsGradCd": sched.get("tcscnsGradCd", ""), "videoAddexpCd": sched.get("videoAddexpCd"),
        "prodBnduCd": "01", "prodBnduNm": "일반", "scnsrtTm": sched["scnsrtTm"],
        "scnendTm": sched.get("scnendTm", ""), "movNo": sched["movNo"],
        "movNm": sched.get("prodNm") or sched.get("expoProdNm") or sched.get("movNm", ""),
        "movTirCd": sched.get("movTirCd", "01"), "movfNo": sched.get("movfNo", ""),
        "movkndCd": sched.get("movkndCd", ""), "rlsYmd": sched.get("rlsYmd") or sched.get("scnYmd", ""),
        "rtctlScopCd": sched.get("rtctlScopCd", RTCTL_SCOP_CD), "salsTznCd": sched.get("salsTznCd", ""),
        "sascnsGradCd": sched.get("sascnsGradCd", "01"), "rtktAmt": sal_amt,
        "sbordNo": seat.get("sbordNo", "001"), "seatNo": seat["seatNo"], "seatRowNm": seat["seatRowNm"],
        "movAtktNo": mov_atkt_no, "salAmt": sal_amt, "scnAmt": sal_amt, "tcsvcAmt": 0, "sasvcAmt": 0,
        "smtScnRepYn": None, "smtScnNo": None, "smtScnYn": None, "hrzoneCd": sched.get("salsTznCd", ""),
        "speclIndctTypCd": sched.get("speclIndctTypCd", "01"), "vatincYn": "Y",
        "prcrulDivCd": sched.get("prcrulDivCd", "01"), "itgrScnsGradCd": sched.get("scnsGradCd", ""),
        "movEtcAttrCd": sched.get("movEtcAttrCd"),
    }


def _build_sell_product(sched: dict, seat: dict, mov_atkt_no: str, sal_amt: int) -> dict:
    return {
        "bzplcTypCd": "01", "dblfrNo": None, "dblfrYn": None, "cxprdYn": "N",
        "prodImg": sched.get("prodImg"), "dblfrProducts": None, "dcAmt": 0, "giftYn": "N",
        "movAtktNo": mov_atkt_no, "parntGrpProdNo": None, "prcrulDivCd": sched.get("prcrulDivCd", "01"),
        "prdcmpTypCd": sched.get("prdcmpTypCd", "01"), "prddtlTypCd": sched.get("prddtlTypCd", "0101"),
        "prdtypCd": sched.get("prdtypCd", "01"),
        "prodNm": sched.get("prodNm") or sched.get("expoProdNm") or "",
        "prodNo": sched["prodNo"], "prodPrc": sal_amt, "salAmt": sal_amt, "salQty": 1,
        "selBzplcNo": BZPLC_NO, "selSiteNo": SITE_NO, "selStoNo": f"{SITE_NO}021",
        "ticketProducts": _build_ticket_products(sched, seat, mov_atkt_no, sal_amt),
        "generalProducts": None, "cmpProductsList": None,
        "speclIndctTypCd": sched.get("speclIndctTypCd", "01"), "vatincYn": "Y",
        "hotdlNo": None, "hotdlTypCd": sched.get("hotdlTypCd", "02"),
    }


def _build_giftc_discount(giftc_no: str, cust_no: str, cust_orgnl_no: str, amt: int, cash_receipt_cos_amt: int) -> dict:
    return {
        "dcTyp": "0", "dcDtlTyp": "cgvGft", "dcPrty": 4, "dcPaymdCd": "movieMoney", "paykndCd": "1058",
        "dcNo": None, "giftcNo": giftc_no, "dctgtNo": giftc_no, "dcCustNo": cust_no, "useCustNo": cust_no,
        "dcAmt": amt, "giftcUseAmt": amt, "custOrgnlNo": cust_orgnl_no, "fardivCd": "03",
        "dcRate": None, "dcVal": None, "cpnCrtfNo": None, "cashrtIssdYn": None,
        "cashReceiptCosAmt": cash_receipt_cos_amt,
    }


def _cash_receipt_cos_amt(sal_amt: int) -> int:
    """추정치(좌석 1매당 예매수수료 2000원 제외) — 2건의 실캡처로 확인된 값(21000→19000)."""
    return max(sal_amt - 2000, 0)


def _fill_mov_common(cont: dict, sched: dict, sum_sal_amt: int, first_seat: dict) -> None:
    cont["mov"]["prodNo"] = sched["prodNo"]
    cont["mov"]["movNo"] = sched["movNo"]
    cont["mov"]["movNm"] = sched.get("prodNm") or sched.get("expoProdNm") or sched.get("movNm", "")
    cont["mov"]["orgMovNm"] = sched.get("movNm", "")
    cont["mov"]["scnYmd"] = sched["scnYmd"]
    cont["mov"]["scnTm"] = f"{sched['scnsrtTm']}~{sched.get('scnendTm', '')}"
    cont["mov"]["scnsNo"] = sched["scnsNo"]
    cont["mov"]["scnSseq"] = sched["scnSseq"]
    cont["mov"]["szoneKindCd"] = first_seat.get("szoneKindCd", "01")
    cont["mov"]["stkndCd"] = first_seat.get("stkndCd", "01")
    cont["mov"]["custNo"] = CUST_NO
    cont["mov"]["sumSalAmt"] = sum_sal_amt
    cont["mov"]["siteNo"] = SITE_NO


async def insert_temp_info(
    client: AsyncSession, sched: dict, seats: list[dict], amts: list[int],
    mov_atkt_no: str, paym_no: str, paym_vrfy_no: str,
) -> None:
    """insertIssSalProcTempInfo: 결제정보 임시저장 (관람권 적용 전 상태)"""
    tpl = _load_template()["insertIssSalProcTempInfo_body"]
    cont = copy.deepcopy(tpl["paymInfoCont"])
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
    cont["mov"]["sellProductsList"] = [
        _build_sell_product(sched, seat, mov_atkt_no, amt) for seat, amt in zip(seats, amts)
    ]
    cont["mov"]["discountDatas"] = [None] * len(seats)

    body = {"coCd": CO_CD, "paymNo": paym_no, "paymVrifyNo": paym_vrfy_no, "paymInfoCont": json.dumps(cont, ensure_ascii=False)}
    payload = json.dumps(body, ensure_ascii=False)
    resp = await client.post(SITE_BASE + INSERT_TEMP_PATH, content=payload.encode("utf-8"), headers=site_headers(REFERER_PAYMENT), timeout=15)
    _check_ok(resp, "결제정보임시저장")


async def apply_voucher(client: AsyncSession, sched: dict, seat: dict, giftc_no: str) -> None:
    """searchPrdGiftcRetrnBss(조회) + validateGiftcRetrn(검증): 관람권 1장 적용 가능 여부 확인"""
    resp1 = await client.get(
        SITE_BASE + GIFTC_RETRN_BSS_PATH,
        params={"coCd": CO_CD, "giftcNo": giftc_no},
        headers=site_headers(REFERER_PAYMENT), timeout=15,
    )
    _check_ok(resp1, "관람권조회")

    body = {
        "coCd": CO_CD, "giftcNo": giftc_no, "siteNo": SITE_NO, "bzplcNo": BZPLC_NO,
        "scnYmd": sched["scnYmd"], "scnsNo": sched["scnsNo"], "scnSseq": sched["scnSseq"],
        "prodBnduCd": "01", "szoneKindCd": seat.get("szoneKindCd", "01"),
        "stkndCd": seat.get("stkndCd", "01"), "prodNo": sched["prodNo"], "custNo": CUST_NO,
        "giftcCrtfNo": None,
    }
    payload = json.dumps(body, ensure_ascii=False)
    resp2 = await client.post(SITE_BASE + VALIDATE_GIFTC_PATH, content=payload.encode("utf-8"), headers=site_headers(REFERER_PAYMENT), timeout=15)
    _check_ok(resp2, "관람권검증")


async def update_temp_info(
    client: AsyncSession, sched: dict, seats: list[dict], amts: list[int],
    mov_atkt_no: str, paym_no: str, paym_vrfy_no: str,
    giftc_nos: list[str], cust_orgnl_no: str,
) -> int:
    """
    updateIssSalProcTempInfo: 좌석별 관람권 할인 반영해서 갱신.
    반환값: amountPaymTotal (0이 아니면 실카드 결제가 필요하다는 뜻 — 자동확정 금지 신호)
    """
    tpl = _load_template()["updateIssSalProcTempInfo_body"]
    cont = copy.deepcopy(tpl["paymInfoCont"])
    total = sum(amts)

    cash_receipt_cos_amts = [_cash_receipt_cos_amt(a) for a in amts]
    total_cash_receipt = sum(cash_receipt_cos_amts)

    cont["saleDt"] = sched["scnYmd"]
    cont["paymNo"] = paym_no
    cont["paymVrifyNo"] = paym_vrfy_no
    cont["amountTotal"] = total
    cont["amountDiscount"] = sum(amts)  # 좌석별 관람권이 각 좌석 가격을 전액 커버한다고 가정
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
    payload = json.dumps(body, ensure_ascii=False)
    resp = await client.post(SITE_BASE + UPDATE_TEMP_PATH, content=payload.encode("utf-8"), headers=site_headers(REFERER_PAYMENT), timeout=15)
    _check_ok(resp, "결제정보갱신")

    return cont["amountPaymTotal"]


async def confirm_booking(
    client: AsyncSession, sched: dict, seats: list[dict], amts: list[int],
    mov_atkt_no: str, paym_no: str, giftc_nos: list[str], cust_orgnl_no: str, mblt_no: str,
) -> dict:
    """salCreateSal: 최종 예매확정 (관람권으로 전액 커버된 경우에만 호출할 것)"""
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

    sal = copy.deepcopy(tpl["sal"])
    sal["custNo"] = CUST_NO
    sal["userNo"] = CUST_NO
    sal["movAtktYmd"] = sched["scnYmd"]
    sal["paymNo"] = paym_no
    sal["mbltNo"] = mblt_no
    sal["sellProductsList"] = sell_products

    pnt = copy.deepcopy(tpl.get("pnt") or {})

    cashrt = copy.deepcopy((tpl.get("cashrtList") or [{}])[0])
    cashrt["paymNo"] = paym_no
    cashrt["saleDt"] = sched["scnYmd"]
    cashrt["goodsCnt"] = str(len(seats))
    cashrt["amountTotal"] = total_cash_receipt
    cashrt["amountVat"] = round(total_cash_receipt / 11) if total_cash_receipt else 0
    cashrt["amountTax"] = total_cash_receipt - cashrt["amountVat"]
    cashrt["goodsName"] = f"{sched.get('expoProdNm') or sched.get('prodNm')} {SITE_NM_SHORT}"

    body = {"paym": None, "sal": sal, "pnt": pnt, "cashrtList": [cashrt] if total_cash_receipt else []}
    payload = json.dumps(body, ensure_ascii=False)
    resp = await client.post(SITE_BASE + CREATE_SAL_PATH, content=payload.encode("utf-8"), headers=site_headers(REFERER_PAYMENT), timeout=15)
    return _check_ok(resp, "예매확정")


# ── 전체 흐름 ─────────────────────────────────────────────────────────────────

async def auto_book(
    client: AsyncSession, token: str, date: str, scns_no: str, scn_sseq: str, mov_no: str,
    cfg: dict, target_pair: tuple[str, int, int] | None = None,
) -> dict | None:
    """
    2연석 선점 → 관람권 적용 → 최종확정 전체 흐름.
    성공 시 예매 정보 dict 반환, 실패/DRY_RUN/카드결제 필요 시 None 반환 (좌석은 항상 정리됨).
    """
    seats = await find_seat_pair(client, date, scns_no, scn_sseq, cfg, target_pair)
    if not seats:
        print("[자동예매] 명당 조건에 맞는 2연석을 찾지 못함", flush=True)
        return None

    seat_label = " ".join(f"{s.get('seatRowNm')}{s.get('seatNo')}" for s in seats)
    print(f"[자동예매] 2연석 선점 시도: {seat_label}", flush=True)
    hold_result = await hold_seats(client, date, scns_no, scn_sseq, seats)
    data = hold_result.get("data", {})
    mov_atkt_no = data.get("movAtktNo", "")
    if not mov_atkt_no:
        raise BookingError(f"좌석선점 응답에 movAtktNo 없음: {json.dumps(hold_result, ensure_ascii=False)[:300]}")

    print(f"[자동예매] 선점 성공 movAtktNo={mov_atkt_no}", flush=True)

    try:
        if DRY_RUN:
            print("[자동예매] DRY_RUN 모드 — 관람권 적용/확정 생략 후 즉시 취소", flush=True)
            await cancel_hold(client, mov_atkt_no, seats)
            return None

        if len(GIFTC_NOS) != len(seats):
            raise BookingError(
                f".env의 GIFTC_NOS에 좌석 수({len(seats)})만큼 관람권 번호가 설정되지 않음 "
                f"(현재 {len(GIFTC_NOS)}개). 예: GIFTC_NOS=코드1,코드2"
            )

        sched = await fetch_schedule_detail(client, token, date, scns_no, scn_sseq, cfg)

        prices = await check_prices(client, date, scns_no, scn_sseq, mov_no, seats)
        amts = [prices[s.get("seatLocNo", "")] for s in seats]

        await check_before_pay(client, date, scns_no, scn_sseq, seats[0].get("szoneKindCd", "01"), sched.get("sascnsGradCd", "01"))

        tpl = _load_template()
        tpl_cont = tpl["insertIssSalProcTempInfo_body"]["paymInfoCont"]
        cust_orgnl_no = ((tpl_cont.get("cjOneUser") or {}).get("memberNo")) or ""
        # userId/userName/mbltNo는 클라이언트에서 암호화되기 전 평문이 필요한데 우리는 암호화 전
        # 원문을 모르므로, 같은 계정으로 실제 캡처됐던 값을 그대로 재사용한다 (계정 고정값이라 안전).
        user_id   = tpl["commonGetPayId_body"].get("userId", CUST_NO)
        user_name = tpl["commonGetPayId_body"].get("userName", "")
        mblt_no   = tpl_cont.get("cashReceiptInfo", "")

        paym_no = await get_pay_id(client, sched, sum(amts), len(seats), user_id=user_id, user_name=user_name)
        paym_vrfy_no = _gen_paym_vrfy_no()

        await insert_temp_info(client, sched, seats, amts, mov_atkt_no, paym_no, paym_vrfy_no)
        for seat, giftc_no in zip(seats, GIFTC_NOS):
            await apply_voucher(client, sched, seat, giftc_no)
        amount_paym_total = await update_temp_info(
            client, sched, seats, amts, mov_atkt_no, paym_no, paym_vrfy_no, GIFTC_NOS, cust_orgnl_no,
        )

        if amount_paym_total != 0:
            raise BookingError(
                f"관람권으로 전액 커버되지 않음 (남은 결제금액={amount_paym_total}원) — "
                f"실카드 결제가 필요해 자동확정을 중단합니다. 수동으로 완료해주세요."
            )

        result = await confirm_booking(
            client, sched, seats, amts, mov_atkt_no, paym_no, GIFTC_NOS, cust_orgnl_no, mblt_no,
        )
        print(f"[자동예매] 예매확정 성공: {json.dumps(result, ensure_ascii=False)[:300]}", flush=True)
        return {
            "mov_atkt_no": mov_atkt_no,
            "seat": seat_label,
            "confirmed_at": datetime.now().isoformat(),
            "result": result,
        }
    except Exception:
        print("[자동예매] 실패 — 좌석선점 롤백 시도", flush=True)
        await cancel_hold(client, mov_atkt_no, seats)
        raise


async def main() -> None:
    import sys

    if len(sys.argv) != 5:
        print("사용법: python booking.py <YYYYMMDD> <scnsNo> <scnSseq> <movNo>")
        sys.exit(1)

    if not ACCESS_TOKEN:
        print("오류: .env에 ACCESS_TOKEN 필요")
        sys.exit(1)

    date, scns_no, scn_sseq, mov_no = sys.argv[1:5]
    cfg = dict(DEFAULT_CONFIG)

    async with AsyncSession(impersonate=_IMPERSONATE) as client:
        try:
            result = await auto_book(client, ACCESS_TOKEN, date, scns_no, scn_sseq, mov_no, cfg)
        except Exception as e:
            print(f"[자동예매] 오류: {e}", flush=True)
            return

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
