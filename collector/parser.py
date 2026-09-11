# -*- coding: utf-8 -*-
"""네이버 통합검색 HTML을 문서 규칙에 따라 블록/자연순위/게시물/광고로 분해한다.

이 파서는 네이버 DOM의 해시된 클래스명이 아니라 비교적 안정적인 신호에 의존한다:
  - place-app-root  : 플레이스 / 새로 오픈했어요
  - 클래스가 없는 <section> : 검색광고(파워링크 또는 플레이스형 광고 카드)
  - sc_new / sc     : 네이버 클립, 뉴스, 이미지, 함께 많이 찾는 (h2/h3 제목으로 이름 확정)
  - _fe_view_root   : 브랜드 콘텐츠
  - spw_fsolid      : 관련문서 + 관련문서 이후 게시물 (같은 컨테이너 안에 섞여 있음)

네이버가 DOM을 바꾸면 이 규칙이 깨질 수 있다. 그 경우 parse()는 blocks가 비정상적으로
적거나 competitors/contents가 비어 있는 결과를 반환하므로, 호출측에서 parser_warning으로
표시해야 한다.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from bs4 import BeautifulSoup

EXCLUDED_DOMAINS = {"help.naver.com", "m.keep.naver.com", "keep.naver.com"}
SKIP_CHILD_CLASS_HINTS = (
    "_scrollLog",
    "ban_whale_download",
    "_fe_whale_banner_bottom",
    "api_sc_page_wrap",
    "sp_page",
    "ct_feed_wrap",
)


_UI_NOISE = ("새 창 열림", "새창 열림")


def _norm(text: str | None) -> str:
    text = text or ""
    for noise in _UI_NOISE:
        text = text.replace(noise, "")
    return re.sub(r"\s+", " ", text).strip()


def _title_only_text(h) -> str:
    """헤딩 안에 class에 'title'이 들어간 요소가 있으면 그 요소의 직접 텍스트만 취한다.
    네이버는 실제 제목 뒤에 '안내' 툴팁 버튼이나 'MY' 링크를 같은 헤딩 안에 중첩시켜
    두는데, get_text()로 통째로 읽으면 그 버튼 텍스트까지 다 섞여 들어온다."""
    for el in [h] + h.find_all(True):
        classes = el.get("class") or []
        if any("title" in c for c in classes):
            direct = el.find(string=True, recursive=False)
            if direct and direct.strip():
                return _norm(direct)
    return ""


def _first_heading_text(container) -> str:
    for tag in ("h2", "h3", "strong"):
        h = container.find(tag)
        if h:
            t = _title_only_text(h) or _norm(h.get_text())
            if t:
                return t
    return ""


def find_root(soup: BeautifulSoup):
    main_pack = soup.select_one("#main_pack")
    if main_pack:
        return main_pack
    place_root = soup.select_one(".place-app-root, [id='place-app-root']")
    if place_root:
        return place_root.parent
    return soup.body


def top_level_blocks(root):
    """root의 직계 자식 중 실제 콘텐츠 블록만 DOM 순서대로 반환한다."""
    out = []
    for child in root.find_all(recursive=False):
        if child.name in ("script", "link", "style"):
            continue
        classes = child.get("class") or []
        if any(hint in classes for hint in SKIP_CHILD_CLASS_HINTS):
            continue
        if child.get("id") == "snb":  # 숨겨진 검색옵션 패널
            continue
        text = _norm(child.get_text())
        if not text:
            continue
        out.append(child)
    return out


def _is_place_detail_panel(container) -> bool:
    """검색어가 특정 업체 하나와 정확히 일치하면, 네이버는 경쟁업체 캐러셀 대신 그
    업체 하나의 상세정보 카드(주소/전화번호 라벨, AI 브리핑, 리뷰, 사진, 인근 다른
    업체 목록 등)를 place-app-root 안에 보여준다. 이 카드의 <li>들은 전부 그 업체에
    딸린 부속 콘텐츠(리뷰·사진·가격비교·인근매장)라서 _place_cards()로 그대로 읽으면
    "전체 상품 가격비교"처럼 업체명이 아닌 잡음이 순위표에 섞여 들어간다. "주소"와
    "전화번호"가 라벨(strong)로 같이 나오는 건 이 상세 카드에서만 보이는 조합이라
    신뢰할 수 있는 구분 신호로 쓴다."""
    labels = {_norm(el.get_text()) for el in container.find_all("strong")}
    return {"주소", "전화번호"}.issubset(labels)


def classify_block(child) -> tuple[str, str]:
    """(block_name, block_kind) 반환. block_kind는 파서 내부 처리용 태그.

    place-app-root가 한 페이지에 2개 이상 나올 수 있다 (예: '무주펜션'처럼 숙박류
    검색은 '새로 오픈했어요' 외에도 '펜션 실시간 예약'처럼 실제 "플레이스"와는 다른
    전용 캐러셀을 별도로 보여준다). 제목을 그대로 살려서 서로 다른 자연순위로 구분한다.
    """
    classes = child.get("class") or []
    cid = child.get("id") or ""

    if "place-app-root" in classes or cid == "place-app-root":
        if _is_place_detail_panel(child):
            return "업체 상세정보", "place_detail"
        header = _first_heading_text(child)
        if header.startswith("새로 오픈했어요"):
            return "새로 오픈했어요", "place_new"
        return header or "플레이스", "place_main"

    if child.name == "section" and not classes:
        return "검색광고", "ad_section"

    if "spw_fsolid" in classes:
        return "관련문서", "doc_posts"

    if "_fe_view_root" in classes:
        header = _first_heading_text(child) or "브랜드 콘텐츠"
        return header, "content_generic"

    header = _first_heading_text(child)
    if header:
        return header, "content_generic"
    return "기타", "unknown"


def _card_anchor_text(a) -> str:
    """카드 링크(<a>) 안의 상호명 텍스트만 뽑는다. 일부 카드(예: '플레이스 플러스'
    배지가 붙은 썸네일 링크)는 화면에 안 보이는 스크린리더용 배지 라벨을
    <span class="place_blind">에 넣어두는데, 이걸 그대로 읽으면 상호명 대신
    "플레이스 플러스" 같은 배지 텍스트가 잡힌다. 복제본에서 그 라벨을 먼저 지운 뒤
    span(있으면)·전체 텍스트 순으로 읽는다."""
    clone = copy.deepcopy(a)
    for blind in clone.find_all(class_="place_blind"):
        blind.decompose()
    span = clone.find("span")
    return _norm(span.get_text() if span else clone.get_text())


def _place_cards(container):
    cards = []
    for li in container.select("li"):
        chosen = None
        for a in li.find_all("a", href=True):
            text = _card_anchor_text(a)
            if text:
                chosen = (a, text)
                break
        if not chosen:
            continue
        a, name = chosen
        href = a["href"]
        is_ad = href.startswith("https://ader.naver.com")
        cards.append({"name": name, "href": href, "is_ad": is_ad})
    return cards


def _place_cards_from_marker(soup: BeautifulSoup, index: int):
    """fetch.py의 PLACE_EXPAND_JS가 "다음" 버튼을 클릭하며 모아 둔 결과를 읽는다.
    있으면 DOM에서 바로 보이는 카드보다 훨씬 많은(최대 수십 개) 업체를 담고 있다.
    없으면 None을 반환해서 호출측이 기본 _place_cards()로 대체하게 한다."""
    marker = soup.select_one(f"#__collected_place_cards_{index}__")
    if not marker:
        return None
    try:
        data = json.loads(marker.get_text())
    except (ValueError, TypeError):
        return None
    return [{"name": d.get("name", ""), "href": "", "is_ad": bool(d.get("isAd"))} for d in data if d.get("name")]


def _is_reply_text(text: str) -> bool:
    return text.startswith("RE") and len(text) > 2 and not text[2].isspace() and not text[2].isdigit()


def _content_items(container, limit: int | None = None):
    # data-heatmap-target=".sublink" 는 한 결과 카드 안에 곁다리로 붙는 "사이트링크"
    # (예: 예약신청·셀프견적·견적신청·예약확인)다. 같은 결과를 가리키는 빠른 이동 버튼일
    # 뿐 별개 게시물이 아닌데, 그대로 두면 카드 하나가 서로 다른 게시물 4개로 부풀려진다.
    anchors = [
        a
        for a in container.select("a[href]")
        if a["href"].startswith("http") and a.get("data-heatmap-target") != ".sublink"
    ]

    href_text = {}
    href_has_mark = {}
    order = []
    for a in anchors:
        href = a["href"]
        netloc = urlparse(href).netloc
        if netloc in EXCLUDED_DOMAINS:
            continue
        text = _norm(a.get_text())
        if not text or _is_reply_text(text):
            continue
        has_mark = bool(a.find("mark"))
        if href not in href_text:
            href_text[href] = text
            href_has_mark[href] = has_mark
            order.append(href)
        elif has_mark and not href_has_mark[href]:
            # 같은 링크를 프로필 바이라인 앵커와 실제 제목 앵커가 동시에 가리키는 경우가
            # 있다(예: "kgrc.co.kr" 바이라인 + "무주 초이스스키샵" 제목이 둘 다 사이트
            # 루트로 연결). 검색어가 <mark>로 강조된 쪽이 진짜 제목이니 그걸 우선한다.
            href_text[href] = text
            href_has_mark[href] = True

    items = []
    current_source = None
    seen_titles = set()
    for href in order:
        text = href_text[href]
        parsed = urlparse(href)
        depth = len([p for p in parsed.path.split("/") if p])
        # <mark> 강조가 있으면(=검색어와 일치해 실제 제목으로 뽑힌 것) 아무리 짧고 루트
        # 경로라도 "출처 라벨"이 아니라 그 자체로 완결된 결과(예: 공식 홈페이지)다.
        is_root_like = depth <= 1 and len(text) <= 20 and not parsed.query and not href_has_mark[href]
        if is_root_like:
            current_source = {"name": text, "domain": parsed.netloc}
            continue
        if text in seen_titles:
            continue
        seen_titles.add(text)
        items.append(
            {
                "title": text,
                "source": (current_source or {}).get("name") or parsed.netloc,
                "source_domain": parsed.netloc,
                "url": href,
                "mentioned_business": guess_mentioned_business(text),
            }
        )
        if limit and len(items) >= limit:
            break
    return items


_QUOTE_PATTERN = re.compile(r"[\'\"「『（(\[]([^\'\"「」『』（）()\[\]]{2,20})[\'\"」』）)\]]")


def guess_mentioned_business(text: str) -> str:
    m = _QUOTE_PATTERN.search(text)
    if m:
        candidate = m.group(1).strip()
        if 2 <= len(candidate) <= 20:
            return candidate
    return "확인 불가"


def _ad_section_items(container):
    """검색광고(파워링크) 카드에서 실제로 크게 노출되는 광고 제목(헤드라인)을 그대로
    가져온다. 업체명을 추정하지 않는다 — 어떤 업체를 가리키는지는 사용자가 제목과
    링크를 보고 직접 판단한다.

    - PC: <a class="lnk_head"> 가 제목(강조 키워드 포함)만 감싸고 있고, 브랜드명
      (<a class="site">)·도메인은 형제 요소라 섞이지 않는다.
    - 모바일: <a class="txt_link"> 안에 브랜드명(.site)·도메인(.url)·제목이 같이
      들어있어서, 앞부분의 브랜드명+도메인 텍스트를 잘라내고 남은 부분만 제목으로 쓴다.
    """
    area = "검색광고 블록" if container.select_one("#power_link_body") else "플레이스 광고"
    items = []
    seen = set()

    def add(title, href):
        title = _norm(title)
        if not title:
            return
        key = (title, href)
        if key in seen:
            return
        seen.add(key)
        items.append({"business_name": title, "display_label": "광고", "ad_area": area, "url": href})

    for head in container.select("a.lnk_head"):  # PC
        add(head.get_text(" "), head.get("href", ""))

    if not items:
        for link in container.select("a.txt_link"):  # 모바일
            site_el = link.select_one(".site")
            url_el = link.select_one(".url")
            full_text = _norm(link.get_text(" "))
            prefix = _norm((site_el.get_text(" ") if site_el else "") + " " + (url_el.get_text(" ") if url_el else ""))
            title = full_text[len(prefix):].strip() if prefix and full_text.startswith(prefix) else full_text
            add(title, link.get("href", ""))

    if not items:  # 그 외 광고 카드 형태(예: 플레이스형 단일 배너) — 브랜드명 요소로 대체
        for site_el in container.select(".site"):
            link_el = site_el if site_el.name == "a" and site_el.get("href") else site_el.find_parent("a", href=True)
            add(site_el.get_text(" "), link_el["href"] if link_el else "")

    return items


@dataclass
class ParseResult:
    blocks: list = field(default_factory=list)
    competitors: list = field(default_factory=list)  # 대표(자연결과가 가장 많은) 캐러셀 — 하위호환용
    place_rankings: dict = field(default_factory=dict)  # {블록이름: [[page,pageRank,overallRank,name],...]} 전체 플레이스 캐러셀
    contents: dict = field(default_factory=dict)
    ads: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def parse(html: str) -> ParseResult:
    soup = BeautifulSoup(html, "lxml")
    root = find_root(soup)
    result = ParseResult()

    if root is None:
        result.warnings.append("검색 결과 루트를 찾지 못했습니다 (네이버 DOM 변경 가능성).")
        return result

    children = top_level_blocks(root)
    if len(children) < 3:
        result.warnings.append(f"최상위 블록이 {len(children)}개뿐입니다. 차단 페이지이거나 DOM이 변경되었을 수 있습니다.")

    order = 0
    natural_rank = 0
    ad_rank_counters: dict[str, int] = {}
    place_root_index = 0  # fetch.py의 PLACE_EXPAND_JS가 매긴 마커 번호와 순서를 맞춘다
    place_main_candidates: list[dict] = []

    for child in children:
        name, kind = classify_block(child)
        order += 1

        if kind == "place_main":
            # 숙박류 등 일부 검색어는 place-app-root가 2개 이상 나온다 (예: '펜션 실시간
            # 예약' + 일반 '플레이스'). 자연순위 열은 문서 스펙상 4열(페이지/페이지내순위/
            # 전체자연순위/상호명)로 고정이라 카테고리 열을 추가할 수 없으므로, 순위표는
            # 한 캐러셀만 대표로 쓴다 — 아래 루프가 끝난 뒤 "다음" 버튼 클릭으로 더 많은
            # 업체를 확보한(=실제로 더 완전한) 캐러셀을 골라서 채운다.
            cards = _place_cards_from_marker(soup, place_root_index)
            if cards is None:
                cards = _place_cards(child)
            place_root_index += 1
            block_ref = {"order": order, "name": name, "meta": "", "type": "자연 + 광고 혼합"}
            result.blocks.append(block_ref)
            place_main_candidates.append({"block": block_ref, "cards": cards})

        elif kind == "place_detail":
            # 이 블록도 fetch.py 쪽에서는 여전히 "다음" 버튼 확장 대상 place-app-root로
            # 세어지므로(마커는 DOM에 나온 place-app-root 순서대로 0,1,2...번이 매겨진다),
            # 순위표로 쓰지는 않아도 place_root_index는 반드시 같이 올려야 다음 캐러셀이
            # 엉뚱한 마커(예: 이 상세페이지의 리뷰 카드)를 읽어오지 않는다.
            place_root_index += 1
            result.blocks.append(
                {"order": order, "name": name, "meta": "검색어와 정확히 일치하는 단일 업체의 상세페이지 — 경쟁업체 순위 없음", "type": "업체 상세"}
            )

        elif kind == "place_new":
            cards = _place_cards_from_marker(soup, place_root_index)
            if cards is None:
                cards = _place_cards(child)
            place_root_index += 1
            result.blocks.append({"order": order, "name": name, "meta": f"업체 카드 {len(cards)}개 · 혼합 노출", "type": "업체"})

        elif kind == "ad_section":
            ad_items = _ad_section_items(child)
            result.blocks.append({"order": order, "name": name, "meta": "검색광고 전용 블록", "type": "광고", "ad": True})
            for it in ad_items:
                area = it["ad_area"]
                ad_rank_counters[area] = ad_rank_counters.get(area, 0) + 1
                result.ads.append([area, ad_rank_counters[area], it["business_name"], it["display_label"], "별도 검색광고 영역", it.get("url", "")])

        elif kind == "doc_posts":
            items = _content_items(child)
            result.blocks.append({"order": order, "name": name, "meta": f"게시물 {len(items)}개", "type": "콘텐츠"})
            bucket = result.contents.setdefault(name, [])
            start = len(bucket) + 1
            for i, it in enumerate(items):
                bucket.append([start + i, it["title"], it["source"], it["url"], it["mentioned_business"]])

        elif kind == "content_generic":
            if name in ("함께 많이 찾는", "이미지"):
                result.blocks.append({"order": order, "name": name, "meta": "탐색 보조 영역", "type": "탐색" if name == "함께 많이 찾는" else "콘텐츠"})
                continue
            items = _content_items(child)
            result.blocks.append({"order": order, "name": name, "meta": f"{len(items)}개 노출", "type": "콘텐츠"})
            bucket = result.contents.setdefault(name, [])
            for i, it in enumerate(items):
                bucket.append([i + 1, it["title"], it["source"], it["url"], it["mentioned_business"]])

        else:
            order -= 1  # 인식하지 못한 블록은 순서를 소비하지 않음
            continue

    if place_main_candidates:
        # 캐러셀별로 각자 자연순위를 매긴다 (사용자가 실제로 보는 위젯이 자연결과 수가
        # 가장 많은 쪽이 아닐 수 있어서 — 예: '펜션 실시간 예약'이 화면상 더 먼저/눈에
        # 띄게 나오는데도 '플레이스'보다 업체 수가 적을 수 있다). competitors는 하위
        # 호환을 위해 자연결과가 가장 많은 캐러셀로 채운다.
        primary = max(place_main_candidates, key=lambda c: sum(1 for card in c["cards"] if not card["is_ad"]))
        used_names: dict[str, int] = {}
        for cand in place_main_candidates:
            cards = cand["cards"]
            ad_count = sum(1 for c in cards if c["is_ad"])
            organic_count = len(cards) - ad_count
            is_primary = cand is primary
            meta = f"광고 {ad_count}개 · 자연결과 {organic_count}개"
            cand["block"]["meta"] = meta

            block_name = cand["block"]["name"]
            used_names[block_name] = used_names.get(block_name, 0) + 1
            ranking_key = block_name if used_names[block_name] == 1 else f"{block_name} ({used_names[block_name]})"

            rank = 0
            ranking_rows = []
            for card in cards:
                if card["is_ad"]:
                    ad_rank_counters["플레이스 광고"] = ad_rank_counters.get("플레이스 광고", 0) + 1
                    result.ads.append(
                        ["플레이스 광고", ad_rank_counters["플레이스 광고"], card["name"], "광고", "플레이스 블록 내 노출", card.get("href", "")]
                    )
                else:
                    rank += 1
                    page_no = (rank - 1) // 20 + 1
                    page_rank = (rank - 1) % 20 + 1
                    ranking_rows.append([page_no, page_rank, rank, card["name"]])
            result.place_rankings[ranking_key] = ranking_rows
            if is_primary:
                result.competitors = ranking_rows

        # 표시 순서를 키워드와 무관하게 고정한다: "실시간예약"류(있으면) → 일반 "플레이스" →
        # 그 외. DOM에 나온 순서(검색광고가 맨 위에 오는 경우 등)를 그대로 따르면 키워드마다
        # 패널 순서가 들쭉날쭉해진다.
        def _place_priority(name: str) -> int:
            if "실시간" in name:
                return 0
            if name == "플레이스":
                return 1
            return 2

        result.place_rankings = dict(sorted(result.place_rankings.items(), key=lambda kv: _place_priority(kv[0])))

    if not result.blocks:
        result.warnings.append("블록을 하나도 추출하지 못했습니다. 차단(429) 페이지인지 확인하세요.")

    return result
