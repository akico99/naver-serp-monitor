# -*- coding: utf-8 -*-
"""parser.py를 고친 뒤, data/raw/*.html에 저장해 둔 원본을 현재 parser.py로 다시 파싱해서
같은 이름의 data/*.json을 덮어쓴다. 네이버에 새로 요청하지 않는다 — 파서 버그를 고쳤을 때
이미 수집해 둔 스냅샷 전체에 일괄 반영하기 위한 용도.

주의: parser.py를 고칠 때마다 이 스크립트를 돌리지 않으면, 이미 저장된 data/*.json은
옛날 버전의 파싱 결과로 남아있게 되고(예: 광고 제목이 업체명으로 잘못 들어간 채로,
또는 이번처럼 업체 상세페이지가 가짜 순위표로 잘못 들어간 채로), collector/build_index.py가
그 오래된 값을 그대로 index/by_keyword 파일에 옮기게 된다.

사용례:
    python collector/reparse_raw.py                    # data/raw/*.html 전체
    python collector/reparse_raw.py --keyword 무주리조트  # 이 키워드로 시작하는 파일만
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from parser import parse  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"


def main() -> None:
    ap = argparse.ArgumentParser(description="저장된 원본 HTML을 현재 parser.py로 다시 파싱해 data/*.json을 갱신")
    ap.add_argument("--keyword", help="이 키워드와 정확히 일치하는 스냅샷만 (지정 안 하면 전체)")
    args = ap.parse_args()

    # data/*.json 각각이 observation.raw_html_path로 자기 원본 HTML을 정확히 가리키고
    # 있다 (파일명의 타임스탬프는 raw HTML 저장 시점 / json 저장 시점이 달라서 문자열로는
    # 안 맞는다 — collect_one()이 raw를 먼저 쓰고, 파싱까지 끝난 뒤에 json을 쓰기 때문).
    updated, skipped = 0, 0
    for json_path in sorted(DATA_DIR.glob("*.json")):
        if json_path.name == "run_state.json":
            continue
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        obs = payload.get("observation") or {}
        if args.keyword and obs.get("keyword") != args.keyword:
            continue
        if payload.get("status") not in ("ok", "parsed_with_warnings"):
            continue  # 차단/실패 스냅샷은 HTML이 애초에 결과 페이지가 아니라 재파싱 의미 없음

        raw_rel = obs.get("raw_html_path")
        raw_path = (PROJECT_ROOT / raw_rel) if raw_rel else None
        if not raw_path or not raw_path.exists():
            skipped += 1
            continue

        html = raw_path.read_text(encoding="utf-8")
        parsed = parse(html)
        payload["status"] = "ok" if not parsed.warnings else "parsed_with_warnings"
        payload["blocks"] = parsed.blocks
        payload["competitors"] = parsed.competitors
        payload["place_rankings"] = parsed.place_rankings
        payload["contents"] = parsed.contents
        payload["ads"] = parsed.ads
        payload["warnings"] = parsed.warnings
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        updated += 1

    print(f"재파싱 완료: {updated}개 갱신, {skipped}개 건너뜀(대응 JSON 없음)")
    print("data/latest.js를 쓰는 워크플로였다면 collect.py를, 통합 대시보드라면 collector/build_index.py를 다시 실행하세요.")


if __name__ == "__main__":
    main()
