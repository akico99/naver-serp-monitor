# -*- coding: utf-8 -*-
"""네이버 검색 노출 관측실 수집기 CLI.

사용례:
    python collector/collect.py --keyword "강남 맛집" --region "서울특별시 강남구"
    python collector/collect.py --keyword "해운대 맛집" --region "부산광역시 해운대구" --device pc

    # 다른 지역 관점(예: 대전)에서 보이는 결과를 상용 프록시로 확인
    python collector/collect.py --keyword "무주펜션" --region "전북 무주군" --vantage daejeon
    (프록시 접속정보는 collector/.env 에 NAVER_PROXY_SERVER 등으로 넣어두면 매번 안 쳐도 된다.
     collector/.env.example 참고)

동작:
    1. PC/모바일 각각으로 네이버 통합검색을 관측한다 (기본값 --device both, 완전히 별도 요청).
    2. 원본 HTML을 data/raw/에 스냅샷으로 보관한다 (파서 오류 검증·근거 자료용).
    3. parser.parse()로 블록/자연순위/게시물/광고를 구조화한다.
    4. data/<키워드>__<기기>__<timestamp>.json 로 결과를 저장한다.
    5. data/latest.js 를 갱신해 index.html이 그대로 불러올 수 있게 한다.

비용/차단 관리:
    - 네이버는 과도한 요청에 429(서비스 이용 제한)를 걸고, Place 쪽에는 캡차까지 연결돼
      있다. 이를 우회하는 기능(프록시 로테이션, 탐지 회피, 캡차 우회 등)은 넣지 않는다.
    - 대신 같은 키워드+기기를 MIN_RERUN_MINUTES 안에 다시 요청하면 실제 요청 없이
      가장 최근 저장된 결과를 재사용한다 (--force로 강제 재수집 가능). 불필요한 중복
      요청을 스스로 줄여서 차단 위험과 비용을 같이 낮춘다.
    - PC/모바일, 그리고 여러 키워드를 자동으로 한 번에 도는 배치 기능은 넣지 않았다 —
      사용자가 직접 각 실행 사이에 충분한 시간을 두고 실행해야 한다.

지역(region) 관련:
    - 네이버는 검색어에 포함된 지명 + 요청 IP 위치로 지역을 자동 추론한다. 이 스크립트는
      좌표를 스푸핑하지 않는다 — "지역"을 바꾸려면 --keyword 자체에 지역명을 포함시켜라
      (예: "해운대 맛집", "홍대 맛집"). --region 인자는 기록용 라벨이며, 키워드에 그
      지역명이 없으면 경고를 출력한다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass  # Windows 콘솔 코드페이지(cp949)가 일부 유니코드를 못 담아 죽는 것을 방지

sys.path.insert(0, str(Path(__file__).parent))

from fetch import ProxySpec, check_exit_ip, fetch_with_retry  # noqa: E402
from parser import parse  # noqa: E402

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
STATE_PATH = DATA_DIR / "run_state.json"

KST = timezone(timedelta(hours=9))
MIN_RERUN_MINUTES = 10.0


def _slug(text: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in text).strip("_")


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _find_latest_json(keyword: str, device: str, vantage: str) -> Path | None:
    if not DATA_DIR.exists():
        return None
    candidates = sorted(DATA_DIR.glob(f"{_slug(keyword)}__{device}__{_slug(vantage)}__*.json"))
    return candidates[-1] if candidates else None


def _check_region_hint(keyword: str, region: str) -> None:
    """지역명이 키워드에 없으면, 검색 결과가 의도한 지역과 다를 수 있음을 경고한다."""
    region_tokens = [t for t in region.replace("특별시", " ").replace("광역시", " ").replace("도", " ").split() if len(t) >= 2]
    if region_tokens and not any(tok[:2] in keyword for tok in region_tokens):
        print(
            f"[안내] --region '{region}' 이 키워드 '{keyword}' 안에 보이지 않습니다. "
            f"네이버는 검색어에 포함된 지명과 요청 IP 위치로 지역을 추론하므로, "
            f"'{region_tokens[0]} {keyword}'처럼 지역명을 키워드에 직접 넣는 것을 권장합니다."
        )


async def collect_one(keyword: str, region: str, device: str, *, vantage: str = "direct", proxy: ProxySpec | None = None) -> dict:
    now = datetime.now(KST)
    print(f"[{device}/{vantage}] {keyword} 관측 시작 ({now.isoformat()})")

    # PC 통합검색의 플레이스 캐러셀에는 페이지 이동 없는 "다음" 버튼이 있어, 차단되는
    # m.place.naver.com/restaurant/list로 가지 않고도 최대 수십 개 자연순위를 모을 수
    # 있다 (실측: 클릭 4번으로 30개). 모바일 캡처에는 이 버튼이 없어서 PC에서만 켠다.
    expand_place = device == "pc"
    result = await fetch_with_retry(keyword, device, proxy=proxy, expand_place=expand_place)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_name = f"{_slug(keyword)}__{device}__{_slug(vantage)}__{now.strftime('%Y%m%dT%H%M%S')}.html"
    (RAW_DIR / raw_name).write_text(result.html, encoding="utf-8")

    observation = {
        "keyword": keyword,
        "region": region,
        "device": device,
        "vantage": vantage,
        "observed_at": now.isoformat(),
        "source_url": result.source_url,
        "raw_html_path": f"data/raw/{raw_name}",
    }

    if result.blocked:
        print(f"[{device}] 차단됨: 네이버가 '서비스 이용 제한' 페이지를 반환했습니다. 잠시 후 다시 시도하세요.")
        return {
            "observation": observation,
            "status": "blocked",
            "blocks": [],
            "competitors": [],
            "place_rankings": {},
            "contents": {},
            "ads": [],
            "warnings": ["네이버 요청 제한(429)에 걸렸습니다. 최소 몇 분 이상 대기 후 재시도하세요."],
        }

    if not result.ok:
        print(f"[{device}] 요청 실패: {result.error}")
        return {
            "observation": observation,
            "status": "fetch_error",
            "blocks": [],
            "competitors": [],
            "place_rankings": {},
            "contents": {},
            "ads": [],
            "warnings": [f"요청 실패: {result.error}"],
        }

    parsed = parse(result.html)
    for w in parsed.warnings:
        print(f"[{device}] 파서 경고: {w}")

    return {
        "observation": observation,
        "status": "ok" if not parsed.warnings else "parsed_with_warnings",
        "blocks": parsed.blocks,
        "competitors": parsed.competitors,
        "place_rankings": parsed.place_rankings,
        "contents": parsed.contents,
        "ads": parsed.ads,
        "warnings": parsed.warnings,
    }


def write_json(keyword: str, device: str, vantage: str, payload: dict) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(KST)
    out_path = DATA_DIR / f"{_slug(keyword)}__{device}__{_slug(vantage)}__{now.strftime('%Y%m%dT%H%M%S')}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def to_dataset(payload: dict) -> dict:
    """수집 결과 payload(collect_one()의 반환값 / data/*.json의 내용)를 index.html이 쓰는
    datasets[device][vantage] 항목 형태로 바꾼다. data/latest.js와 data/by_keyword/*.js가
    공유하는 형식이라 collector/build_index.py에서도 그대로 가져다 쓴다."""
    place_rankings = payload.get("place_rankings") or {}
    return {
        "blocks": payload["blocks"],
        "competitors": payload["competitors"],
        "place_rankings": place_rankings,
        "contents": payload["contents"],
        "ads": payload["ads"],
        "pages": max((c[0] for c in payload["competitors"]), default=0),
        "status": payload["status"],
        "observation": payload["observation"],
        "warnings": payload["warnings"],
    }


def update_latest_js(all_results: dict[str, dict]) -> Path:
    """index.html이 바로 로드할 수 있는 window.SERP_DATA 형태로 data/latest.js를 갱신한다.

    datasets 구조: datasets[device][vantage] = {...}
    vantage("direct", "daejeon" 등)가 여러 개면 같은 기기 안에서 지역 관점을 비교할 수 있다.
    all_results의 key는 f"{device}::{vantage}" 이다.
    """
    # 기존 latest.js에 다른 기기/관점의 최신 데이터가 있으면 보존한다.
    # (예: 이번엔 daejeon 관점만 돌렸어도 이전에 수집한 direct 관점 데이터가 사라지지 않게)
    keyword = next(iter(all_results.values()))["observation"]["keyword"]
    region = next(iter(all_results.values()))["observation"]["region"]

    existing_datasets: dict = {}
    latest_path = DATA_DIR / "latest.js"
    if latest_path.exists():
        text = latest_path.read_text(encoding="utf-8")
        m = re.search(r"window\.SERP_DATA\s*=\s*(\{.*\});\s*$", text, re.S)
        if m:
            try:
                prev = json.loads(m.group(1))
                if prev.get("keyword") == keyword:  # 같은 키워드일 때만 다른 기기/관점 데이터를 보존
                    existing_datasets = prev.get("datasets", {})
            except json.JSONDecodeError:
                existing_datasets = {}

    datasets = existing_datasets
    for combo_key, payload in all_results.items():
        device, vantage = combo_key.split("::", 1)
        datasets.setdefault(device, {})[vantage] = to_dataset(payload)

    js = (
        "// 자동 생성 파일: collector/collect.py 가 매 실행마다 덮어씁니다.\n"
        f"window.SERP_DATA = {json.dumps({'keyword': keyword, 'region': region, 'datasets': datasets}, ensure_ascii=False, indent=2)};\n"
    )
    out_path = DATA_DIR / "latest.js"
    out_path.write_text(js, encoding="utf-8")
    return out_path


async def collect_one_cached(keyword: str, region: str, device: str, *, vantage: str, proxy: ProxySpec | None, force: bool) -> dict:
    """MIN_RERUN_MINUTES 안에 같은 키워드+기기+관점을 다시 요청하면 실제 네이버 요청 없이
    가장 최근 저장분을 재사용한다. --force가 켜져 있으면 무조건 새로 수집한다."""
    state = _load_state()
    key = f"{keyword}::{device}::{vantage}"
    last_iso = state.get(key)

    if not force and last_iso:
        last_dt = datetime.fromisoformat(last_iso)
        elapsed_min = (datetime.now(KST) - last_dt).total_seconds() / 60
        if elapsed_min < MIN_RERUN_MINUTES:
            cached_path = _find_latest_json(keyword, device, vantage)
            if cached_path:
                print(
                    f"[{device}/{vantage}] {elapsed_min:.1f}분 전에 같은 키워드를 이미 수집했습니다 "
                    f"(재요청 최소 간격 {MIN_RERUN_MINUTES}분). 네이버에 다시 요청하지 않고 "
                    f"캐시된 결과를 재사용합니다: {cached_path.name} (--force로 강제 재수집 가능)"
                )
                return json.loads(cached_path.read_text(encoding="utf-8"))

    payload = await collect_one(keyword, region, device, vantage=vantage, proxy=proxy)
    if payload["status"] in ("ok", "parsed_with_warnings"):
        state[key] = datetime.now(KST).isoformat()
        _save_state(state)
    return payload


def _build_proxy(args: argparse.Namespace) -> ProxySpec | None:
    """--vantage direct(기본값)면 .env에 프록시가 있어도 절대 쓰지 않는다 — "direct"는
    이 기기의 실제 연결을 의미하므로, 프록시가 조용히 끼어들면 관점 라벨과 실제 경로가
    어긋난다. 프록시를 쓰려면 --vantage 를 direct가 아닌 다른 라벨로 명시해야 한다."""
    if args.vantage == "direct":
        if args.proxy_server or os.environ.get("NAVER_PROXY_SERVER"):
            print("[안내] .env/--proxy-server에 프록시가 설정되어 있지만 --vantage가 'direct'라서 사용하지 않습니다. 프록시를 쓰려면 --vantage에 다른 라벨(예: daejeon)을 지정하세요.")
        return None
    server = args.proxy_server or os.environ.get("NAVER_PROXY_SERVER")
    if not server:
        return None
    username = args.proxy_username or os.environ.get("NAVER_PROXY_USERNAME")
    password = args.proxy_password or os.environ.get("NAVER_PROXY_PASSWORD")
    return ProxySpec(server=server, username=username, password=password, label=args.vantage)


def _ensure_proxy_region(proxy: ProxySpec, expected_country: str, max_attempts: int, retry_delay: float) -> dict | None:
    """레지덴셜 프록시는 연결마다 IP가 랜덤하게 바뀌고, 지역 타겟팅 파라미터가 매번
    정확히 지켜지지 않는 경우가 있다(관측됨: 4번 중 1번만 목표 국가로 나감). 목표 국가가
    아닌 IP로 네이버 요청을 낭비하지 않도록, 실제 수집 전에 새 연결을 반복 확인해서
    맞는 지역이 나올 때까지(또는 시도 횟수를 다 쓸 때까지) 기다린다."""
    for attempt in range(1, max_attempts + 1):
        info = check_exit_ip(proxy)
        if info and info.get("country") == expected_country:
            print(
                f"[{proxy.label}] {attempt}번째 연결에서 목표 국가 확인: {info.get('city')}, "
                f"{info.get('region')}, {info.get('country')} (ip={info.get('ip')})"
            )
            return info
        where = f"{info.get('city')}, {info.get('country')}" if info and "error" not in info else (info or {}).get("error", "알 수 없음")
        print(f"[{proxy.label}] {attempt}/{max_attempts}번째 연결이 목표 국가({expected_country})가 아닙니다 ({where}). 새 연결로 재시도...")
        time.sleep(retry_delay)
    return None


async def main_async(args: argparse.Namespace) -> None:
    _check_region_hint(args.keyword, args.region)
    proxy = _build_proxy(args)

    if proxy:
        print(f"[{args.vantage}] 프록시({proxy.server}) 실제 출구 위치 확인 중 (네이버에는 아직 요청 안 함)...")
        ip_info = _ensure_proxy_region(proxy, args.expect_country, args.max_proxy_attempts, args.delay / 4)
        if ip_info is None:
            print(
                f"[{args.vantage}] {args.max_proxy_attempts}번 시도했지만 목표 국가({args.expect_country})로 나가는 연결을 "
                f"잡지 못했습니다. 네이버 요청은 보내지 않고 중단합니다. 프록시 서비스의 지역 타겟팅이 이 계정/플랜에서 "
                f"신뢰할 수 있게 동작하는지 대시보드나 고객지원에 문의해보세요."
            )
            return
    elif args.vantage != "direct":
        print(f"[안내] --vantage {args.vantage} 를 지정했지만 프록시가 없습니다. 실제로는 이 기기의 직접 연결(direct)로 나갑니다.")

    devices = ["pc", "mobile"] if args.device == "both" else [args.device]
    all_results: dict[str, dict] = {}

    for i, device in enumerate(devices):
        if i > 0:
            wait = args.delay + random.uniform(0, args.delay * 0.5)
            print(f"다음 기기 요청 전 {wait:.1f}초 대기 (네이버 요청 제한 방지)...")
            await asyncio.sleep(wait)
        payload = await collect_one_cached(args.keyword, args.region, device, vantage=args.vantage, proxy=proxy, force=args.force)
        out_path = write_json(args.keyword, device, args.vantage, payload)
        print(f"[{device}/{args.vantage}] 저장 완료: {out_path}")
        all_results[f"{device}::{args.vantage}"] = payload

    latest_path = update_latest_js(all_results)
    print(f"index.html 연동 파일 갱신: {latest_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="네이버 검색 노출 관측실 수집기")
    ap.add_argument("--keyword", required=True, help="검색 키워드 (지역명을 포함하면 그 지역 기준으로 잡힌다, 예: '해운대 맛집')")
    ap.add_argument("--region", required=True, help="지역 라벨 (기록용). 키워드에 같은 지역명이 없으면 경고 출력")
    ap.add_argument("--device", choices=["pc", "mobile", "both"], default="both")
    ap.add_argument("--delay", type=float, default=20.0, help="PC/모바일 두 요청 사이 최소 대기 시간(초, 실제로는 지터가 더해짐)")
    ap.add_argument("--force", action="store_true", help=f"최근 {MIN_RERUN_MINUTES}분 이내 캐시가 있어도 강제로 다시 수집")
    ap.add_argument("--vantage", default="direct", help="관측 관점 라벨 (예: 'daejeon'). 프록시를 쓸 때만 의미 있음, 기본은 이 기기의 직접 연결")
    ap.add_argument("--proxy-server", help="프록시 서버 (예: http://gate.provider.com:8000). 생략 시 .env의 NAVER_PROXY_SERVER 사용")
    ap.add_argument("--proxy-username", help="생략 시 .env의 NAVER_PROXY_USERNAME 사용")
    ap.add_argument("--proxy-password", help="생략 시 .env의 NAVER_PROXY_PASSWORD 사용")
    ap.add_argument("--expect-country", default="KR", help="프록시가 나가야 할 국가 코드 (기본 KR). 실제로 다른 국가로 나가면 네이버 요청 없이 중단")
    ap.add_argument("--max-proxy-attempts", type=int, default=5, help="목표 국가로 나가는 연결을 잡기 위해 재시도할 최대 횟수 (레지덴셜 프록시는 연결마다 IP가 바뀜)")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
