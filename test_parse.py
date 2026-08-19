"""파서 단위 테스트"""
import os
os.environ.setdefault("WATCH_DATES", "20260530")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "x")

from alarm import parse_schedule, DEFAULT_CONFIG

mock_data = [
    {"scnsNm": "1관(IMAX)", "movNo": "30001210", "scnsrtTm": "1400",
     "scnsNo": "018", "scnSseq": "3", "stcnt": 270},
    {"scnsNm": "1관(IMAX)", "movNo": "30001210", "scnsrtTm": "1900",
     "scnsNo": "018", "scnSseq": "5", "stcnt": 270},
    {"scnsNm": "3관", "movNo": "30001210", "scnsrtTm": "2200",
     "scnsNo": "020", "scnSseq": "7", "stcnt": 150},
]

cfg = dict(DEFAULT_CONFIG)

result = parse_schedule(mock_data, "20260530", cfg)
print(f"파싱 결과: {len(result)}개 (IMAX 아닌 1개 제외 → 2개 예상)")
assert len(result) == 2, f"예상 2개, 실제 {len(result)}개"
for s in result:
    print(f"  {s['time']} | {s['hall']} | 총 {s['total']}석")

# 빈 응답 테스트
empty = parse_schedule([], "20260530", cfg)
assert empty == [], f"빈 응답이어야 함: {empty}"

# watch_times 필터링 테스트
cfg_filtered = dict(DEFAULT_CONFIG)
cfg_filtered["watch_times"] = {"20260530": ["14:00"]}
filtered = parse_schedule(mock_data, "20260530", cfg_filtered)
assert len(filtered) == 1, f"watch_times 필터 후 1개 예상, 실제 {len(filtered)}개"
assert filtered[0]["time"] == "14:00"

print("\n모든 테스트 통과!")
