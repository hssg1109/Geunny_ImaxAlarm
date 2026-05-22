"""파서 단위 테스트"""
import os
os.environ.setdefault("WATCH_DATES", "20260530")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "x")

from alarm import parse_schedule

mock_response = {
    "statusCode": "200",
    "statusMessage": "success",
    "data": {
        "schList": [
            {"schNo": "A001", "scrnStartDttm": "20260530140000", "scrnNm": "IMAX관", "rmndSeatCnt": 45, "totSeatCnt": 270},
            {"schNo": "A002", "scrnStartDttm": "20260530190000", "scrnNm": "IMAX관", "rmndSeatCnt": 0, "totSeatCnt": 270, "schSttsCd": "SOLDOUT"},
            {"schNo": "A003", "scrnStartDttm": "20260530220000", "scrnNm": "IMAX관", "rmndSeatCnt": 120, "totSeatCnt": 270},
        ]
    }
}

result = parse_schedule(mock_response, "20260530")
print(f"파싱 결과: {len(result)}개 (SOLDOUT 1개 제외 → 2개 예상)")
assert len(result) == 2, f"예상 2개, 실제 {len(result)}개"
for s in result:
    print(f"  {s['time']} | 잔여 {s['remain']}석 | {s['hall']}")

# 빈 응답 테스트
empty = parse_schedule({"statusCode": "200", "data": {"schList": []}}, "20260530")
assert empty == [], f"빈 응답이어야 함: {empty}"

print("\n모든 테스트 통과!")
