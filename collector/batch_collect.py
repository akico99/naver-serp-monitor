# -*- coding: utf-8 -*-
"""마스터 키워드 목록(data/키워드_통합정리.xlsx, 행유형=대표)을 평균검색량 내림차순으로
훑으면서, 아직 수집된 적 없거나 --stale-days 보다 오래된 키워드만 PC/direct로 수집한다.

왜 PC/direct만 기본인가:
    - 통합 대시보드(keywords.html) 목록 화면은 "최근 수집됐는지 + 대략적인 노출 현황"만
      보여주면 충분하고, PC 쪽이 자연순위를 최대 30개까지 모아서 정보량이 더 많다.
    - 모바일/지역(프록시) 비교는 특정 키워드를 깊이 볼 때만 필요하므로, 목록 화면에서
      해당 키워드를 골라 상세 화면(index.html)에 들어간 뒤 "수집 명령어 복사"로 개별
      실행하는 편이 비용(시간·프록시 대역폭) 대비 합리적이다.

비용/차단 관리:
    - collect.py와 동일하게, 키워드 사이에도 정중한 대기시간을 둔다.
    - --stale-days로 "얼마나 오래된 데이터까지 재사용할지" 조절한다 (기본 0.4일≈10시간 —
      하루 2회 정도 돌리는 운영 주기에 맞춘 값. 하루 1회만 돌린다면 --stale-days 1 처럼
      늘려서 불필요한 재수집을 줄일 수 있다). 이미 그 안에 수집된 키워드는 네이버에
      다시 요청하지 않고 건너뛴다.
    - Ctrl+C로 중단해도 이미 저장된 키워드는 data/*.json에 남아있으니, 다시 실행하면
      이어서 진행된다 (멱등적 — 몇 번을 나눠 돌려도 결과가 같다).

사용례:
    python collector/batch_collect.py                 # 대표 키워드 전체, 3일 이상 지난 것만
    python collector/batch_collect.py --limit 3        # 동작 확인용으로 앞 3개만
    python collector/batch_collect.py --force          # 전부 강제로 다시 수집
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).parent))
from collect import DATA_DIR, KST, _slug, collect_one_cached, write_json  # noqa: E402
import build_index  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PROJECT_ROOT = Path(__file__).parent.parent
XLSX_PATH = PROJECT_ROOT / "data" / "키워드_통합정리.xlsx"

DEVICE = "pc"
VANTAGE = "direct"


def load_representative_keywords() -> list[tuple[str, float]]:
    wb = openpyxl.load_workbook(XLSX_PATH, data_only=True)
    ws = wb["통합_키워드"]
    headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    idx = {h: i for i, h in enumerate(headers) if h}

    def get(row, name):
        i = idx.get(name)
        return row[i] if i is not None else None

    out = []
    seen = set()
    for row in ws.iter_rows(min_row=2, values_only=True):
        if get(row, "행유형") != "대표":
            continue
        keyword = get(row, "키워드")
        if not keyword or not str(keyword).strip():
            continue
        keyword = str(keyword).strip()
        if keyword in seen:  # 같은 대표 키워드가 여러 행에 걸쳐 있을 수 있음
            continue
        seen.add(keyword)

        volume = get(row, "평균검색량")
        try:
            volume = float(volume) if volume not in (None, "") else 0.0
        except (TypeError, ValueError):
            volume = 0.0
        out.append((keyword, volume))

    out.sort(key=lambda kv: kv[1], reverse=True)
    return out


def _latest_observed_at(keyword: str) -> datetime | None:
    candidates = sorted(DATA_DIR.glob(f"{_slug(keyword)}__{DEVICE}__{_slug(VANTAGE)}__*.json"))
    if not candidates:
        return None
    ts = candidates[-1].stem.split("__")[-1]
    return datetime.strptime(ts, "%Y%m%dT%H%M%S").replace(tzinfo=KST)


async def main_async(args: argparse.Namespace) -> None:
    keywords = load_representative_keywords()
    print(f"대표 키워드 {len(keywords)}개 로드 (평균검색량 내림차순)")

    stale_cutoff = datetime.now(KST) - timedelta(days=args.stale_days)
    todo = []
    for keyword, volume in keywords:
        last = _latest_observed_at(keyword)
        if last is not None and last >= stale_cutoff and not args.force:
            continue
        todo.append((keyword, volume, last))

    if args.limit:
        todo = todo[: args.limit]

    skipped = len(keywords) - len(todo)
    print(f"수집 대상 {len(todo)}개 (최근 {args.stale_days:g}일 이내 수집분 등 {skipped}개는 건너뜀)")

    ok, blocked, failed = 0, 0, 0
    for i, (keyword, volume, last) in enumerate(todo):
        if i > 0:
            wait = args.delay + random.uniform(0, args.delay * 0.5)
            print(f"다음 키워드 요청 전 {wait:.1f}초 대기 (네이버 요청 제한 방지)...")
            await asyncio.sleep(wait)

        last_label = last.isoformat() if last else "없음"
        print(f"[{i + 1}/{len(todo)}] {keyword} (평균검색량 {volume:.0f}, 이전 수집 {last_label})")
        payload = await collect_one_cached(keyword, keyword, DEVICE, vantage=VANTAGE, proxy=None, force=args.force)
        out_path = write_json(keyword, DEVICE, VANTAGE, payload)
        print(f"  -> {out_path.name} (status={payload['status']})")

        if payload["status"] in ("ok", "parsed_with_warnings"):
            ok += 1
        elif payload["status"] == "blocked":
            blocked += 1
            print("  네이버 요청 제한에 걸렸습니다. 배치를 중단합니다 — 잠시 후 다시 실행하면 남은 키워드부터 이어집니다.")
            break
        else:
            failed += 1

    print(f"완료: 성공 {ok} · 차단 {blocked} · 실패 {failed} (총 {len(todo)}개 시도)")
    print("data/by_keyword/*.js 및 data/keywords_index.js 재생성 중...")
    build_index.main()


def main() -> None:
    ap = argparse.ArgumentParser(description="대표 키워드 일괄 수집 (PC/direct 전용)")
    ap.add_argument("--stale-days", type=float, default=0.4, help="이보다 오래됐거나 없는 키워드만 수집 (기본 0.4일=약 10시간 — 하루 2회 실행 기준)")
    ap.add_argument("--delay", type=float, default=20.0, help="키워드 사이 최소 대기 시간(초, 실제로는 지터가 더해짐)")
    ap.add_argument("--limit", type=int, default=0, help="테스트용: 대상 중 앞에서부터 N개만 처리 (0=전체)")
    ap.add_argument("--force", action="store_true", help="최신 여부와 관계없이 전부 다시 수집")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
