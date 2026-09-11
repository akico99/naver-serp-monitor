# -*- coding: utf-8 -*-
"""data/*.json (collect.py / batch_collect.py가 쌓아둔 수집 스냅샷)을 스캔해서

  - data/by_keyword/<slug>.js  키워드별 상세 데이터 (index.html?keyword=... 이 이 파일을 불러온다)
  - data/keywords_index.js     키워드별 최신 수집 요약 (keywords.html 목록 화면이 읽는다)

를 다시 만든다. 네이버에 새로 요청하지 않고 이미 디스크에 저장된 JSON만 사용하므로 언제든
다시 실행해도 안전하다 (예: parser.py를 고친 뒤 원본 raw HTML을 재파싱해서 data/*.json을
갱신했다면, 이 스크립트도 다시 돌려야 by_keyword/*.js와 keywords_index.js에 반영된다).

사용례:
    python collector/build_index.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from collect import to_dataset, _slug  # noqa: E402
from companies import COMPANIES  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
BY_KEYWORD_DIR = DATA_DIR / "by_keyword"
INDEX_PATH = DATA_DIR / "keywords_index.js"

EXCLUDE_NAMES = {"run_state.json"}

# "함께 가볼만한 장소"는 사용자별로 다른 업체가 뜨는 개인화 추천 위젯이라(사용자 확인,
# 2026-09-10), 이 안의 업체 노출은 재현되지 않는 관측이라 노출 매트릭스에 넣지 않는다.
# 상세 화면(index.html)에는 원본 그대로 계속 보여준다 — 여기서만 집계 대상에서 뺀다.
PERSONALIZED_PLACE_AREAS = {"함께 가볼만한 장소"}


def _iter_snapshot_files():
    for p in sorted(DATA_DIR.glob("*.json")):
        if p.name in EXCLUDE_NAMES:
            continue
        yield p


def _place_top(dataset: dict) -> str | None:
    for area, rows in (dataset.get("place_rankings") or {}).items():
        if area in PERSONALIZED_PLACE_AREAS:
            continue
        if rows:
            return rows[0][-1]
    return None


def _text_matches(text: str, aliases: list[str]) -> bool:
    return any(alias in (text or "") for alias in aliases)


def _matching_domain(url: str, domains: list[str]) -> str | None:
    """url의 호스트가 domains 중 하나와 정확히 같거나 그 하위 도메인이면 그 도메인을
    반환한다. 게시물 제목에 업체명이 우연히 섞인 것과 달리, 링크가 업체 자사 도메인으로
    바로 연결되면 사실상 확정 신호로 봐도 된다."""
    if not url:
        return None
    host = (urlparse(url).netloc or "").lower()
    for d in domains:
        d = d.lower()
        if host == d or host.endswith("." + d):
            return d
    return None


def compute_exposure(device: str, vantage: str, dataset: dict) -> list[dict]:
    """collector/companies.py에 등록된 "지정 업체"가 이 dataset(플레이스 순위 · 게시물) 안에
    실제로 노출됐는지 찾는다. 신뢰도 순으로 3가지 kind를 매긴다:
      - place  : 플레이스 상호명 자체와 일치 (확정)
      - domain : 게시물 링크가 업체 자사 도메인 (거의 확정)
      - content: 게시물 제목에 업체명(별칭)이 텍스트로 포함 (후보 — 오탐 가능, 직접 확인 필요)
    검색광고는 실시간 입찰이라 노출 추적 의미가 없다고 판단해 제외한다(사용자 확인, 2026-09-10).
    """
    hits: list[dict] = []

    for area, rows in (dataset.get("place_rankings") or {}).items():
        if area in PERSONALIZED_PLACE_AREAS:
            continue
        for row in rows:
            name = row[-1]
            for code, info in COMPANIES.items():
                if _text_matches(name, info["aliases"]):
                    hits.append({"company": code, "kind": "place", "area": area, "rank": row[2], "text": name, "device": device, "vantage": vantage})

    for area, items in (dataset.get("contents") or {}).items():
        for item in items:
            rank, title = item[0], item[1]
            url = item[3] if len(item) > 3 else ""
            domain_hit_companies = set()
            for code, info in COMPANIES.items():
                domain = _matching_domain(url, info.get("domains") or [])
                if domain:
                    domain_hit_companies.add(code)
                    hits.append({"company": code, "kind": "domain", "area": area, "rank": rank, "text": title, "url": url, "domain": domain, "device": device, "vantage": vantage})
            for code, info in COMPANIES.items():
                if code in domain_hit_companies:
                    continue  # 이미 도메인으로 확정됐으면 같은 항목을 후보로 중복 표시하지 않는다
                if _text_matches(title, info["aliases"]):
                    hits.append({"company": code, "kind": "content", "area": area, "rank": rank, "text": title, "url": url, "device": device, "vantage": vantage})

    return hits


def main() -> None:
    # keyword -> {"region": ..., "combos": {(device, vantage): payload}}  (콤보별 최신 것만 남김)
    by_keyword: dict[str, dict] = {}
    skipped = 0
    for path in _iter_snapshot_files():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"[건너뜀] JSON 파싱 실패: {path.name}")
            skipped += 1
            continue
        obs = payload.get("observation") or {}
        keyword, device, vantage, observed_at = obs.get("keyword"), obs.get("device"), obs.get("vantage"), obs.get("observed_at")
        if not (keyword and device and vantage and observed_at):
            skipped += 1
            continue

        entry = by_keyword.setdefault(keyword, {"region": obs.get("region"), "combos": {}})
        combo_key = (device, vantage)
        prev = entry["combos"].get(combo_key)
        if prev is None or prev["observation"]["observed_at"] < observed_at:
            entry["combos"][combo_key] = payload

    BY_KEYWORD_DIR.mkdir(parents=True, exist_ok=True)
    index: dict[str, dict] = {}

    for keyword, entry in by_keyword.items():
        slug = _slug(keyword)
        datasets: dict[str, dict] = {}
        devices_summary: dict[str, dict] = {}
        exposure: list[dict] = []
        place_areas: dict[str, None] = {}  # 순서를 보존하는 집합 용도(dict 키)
        latest_observed_at = None

        for (device, vantage), payload in entry["combos"].items():
            dataset = to_dataset(payload)
            datasets.setdefault(device, {})[vantage] = dataset
            obs = payload["observation"]
            areas = list((dataset.get("place_rankings") or {}).keys())
            devices_summary.setdefault(device, {})[vantage] = {
                "status": payload["status"],
                "observed_at": obs["observed_at"],
                "place_top": _place_top(dataset),
                "place_areas": areas,
                "ad_count": len(payload.get("ads") or []),
            }
            for a in areas:
                if a not in PERSONALIZED_PLACE_AREAS:
                    place_areas[a] = None
            if payload["status"] in ("ok", "parsed_with_warnings"):
                exposure.extend(compute_exposure(device, vantage, dataset))
            if latest_observed_at is None or obs["observed_at"] > latest_observed_at:
                latest_observed_at = obs["observed_at"]

        out_path = BY_KEYWORD_DIR / f"{slug}.js"
        js = (
            "// 자동 생성 파일: collector/build_index.py 가 data/*.json 에서 다시 만듭니다. 직접 수정하지 마세요.\n"
            f"window.SERP_DATA = {json.dumps({'keyword': keyword, 'region': entry['region'], 'datasets': datasets}, ensure_ascii=False, indent=2)};\n"
        )
        out_path.write_text(js, encoding="utf-8")

        index[keyword] = {
            "slug": slug,
            "region": entry["region"],
            "devices": devices_summary,
            "exposure": exposure,
            "place_areas": list(place_areas.keys()),
            "latest_observed_at": latest_observed_at,
            "file": f"data/by_keyword/{slug}.js",
        }

    INDEX_PATH.write_text(
        "// 자동 생성 파일: collector/build_index.py 가 data/*.json 에서 다시 만듭니다. 직접 수정하지 마세요.\n"
        f"window.KEYWORDS_INDEX = {json.dumps(index, ensure_ascii=False, indent=2)};\n",
        encoding="utf-8",
    )
    print(f"{len(index)}개 키워드 집계 -> {INDEX_PATH} (+ data/by_keyword/*.js {len(index)}개, 건너뜀 {skipped}개 파일)")


if __name__ == "__main__":
    main()
