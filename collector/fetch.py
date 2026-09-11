# -*- coding: utf-8 -*-
"""Crawl4AI/Playwright로 네이버 검색 결과 HTML을 가져온다.

주의: 네이버는 과도한 요청에 IP 단위로 "서비스 이용이 제한되었습니다"(429)를 반환한다.
이 모듈은 그 차단을 우회하지 않는다 — 차단되면 그대로 보고하고, 호출측이 시간을 두고
다시 시도하도록 한다. 요청 사이 지연(delay)을 항상 두어 차단 가능성을 낮춘다.

지역별 관측(예: "대전에서 보면 이 광고가 뜨는가")이 필요하면 ProxySpec으로 실제
그 지역에 있는 정당한 프록시/서버를 지정해서 연결한다 — 이건 차단 우회가 아니라
"실제로 그 지역에서 보내는 요청"을 만드는 것이다. IP를 숨기거나 탐지를 피하려는
목적이 아니라, 광고 지역 타겟팅이 정말 의도대로 동작하는지 확인하기 위함이다.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from crawl4ai.async_configs import ProxyConfig

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 10; SM-G981B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)
PC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

BLOCK_MARKERS = ("서비스 이용이 제한되었습니다", "과도한 접근 요청")

# PC 통합검색의 플레이스 캐러셀에는 href="#"(JS 버튼, 실제 이동 없음)인 "다음"
# 버튼(a.cmm_pg_next)이 있다. m.place.naver.com/restaurant/list(반복 차단됨)로
# 이동하지 않고도 같은 화면 안에서 클릭만으로 더 많은 업체를 볼 수 있다 — 실제로
# 클릭 4번으로 광고 없는 업체 30개를 확인했다. place-app-root가 여러 개일 수 있어
# (예: "펜션 실시간 예약" + 일반 "플레이스") 각각 별도로 펼쳐서 마커에 기록한다.
PLACE_EXPAND_JS = r"""
window.scrollTo(0, document.body.scrollHeight/4);
await new Promise(r=>setTimeout(r,1200));
const roots = document.querySelectorAll('.place-app-root, [id="place-app-root"]');
function cardAnchorText(a) {
  // 일부 카드(예: '플레이스 플러스' 배지가 붙은 썸네일 링크)는 화면에 안 보이는
  // 스크린리더용 배지 라벨을 .place_blind 안에 넣어둔다. 그대로 읽으면 상호명 대신
  // "플레이스 플러스" 같은 배지 텍스트가 잡히므로, 복제본에서 먼저 지운다.
  const clone = a.cloneNode(true);
  clone.querySelectorAll('.place_blind').forEach(el => el.remove());
  const span = clone.querySelector('span');
  return (span ? span.textContent : clone.textContent).trim();
}
function extractCards(container) {
  const out = [];
  container.querySelectorAll('li').forEach(li => {
    const anchors = li.querySelectorAll('a[href]');
    let chosen = null;
    for (const a of anchors) {
      const text = cardAnchorText(a);
      if (text) { chosen = {a, text}; break; }
    }
    if (!chosen) return;
    out.push({name: chosen.text, isAd: chosen.a.href.startsWith('https://ader.naver.com')});
  });
  return out;
}
for (let ri = 0; ri < roots.length; ri++) {
  const target = roots[ri];
  const allCards = [];
  const seen = new Set();
  function addUnique(cards) {
    let added = 0;
    cards.forEach(c => {
      const key = c.name + '|' + c.isAd;
      if (!seen.has(key)) { seen.add(key); allCards.push(c); added++; }
    });
    return added;
  }
  addUnique(extractCards(target));
  for (let i=0;i<10;i++){
    const nextBtn = target.querySelector('a.cmm_pg_next');
    if (!nextBtn) break;
    const disabled = nextBtn.getAttribute('aria-disabled') === 'true' || nextBtn.classList.contains('dimmed');
    if (disabled) break;
    nextBtn.click();
    await new Promise(r=>setTimeout(r,1100));
    const added = addUnique(extractCards(target));
    if (added === 0) break;
  }
  const marker = document.createElement('div');
  marker.id = '__collected_place_cards_' + ri + '__';
  marker.style.display = 'none';
  marker.textContent = JSON.stringify(allCards);
  document.body.appendChild(marker);
}
"""


@dataclass
class ProxySpec:
    """상용 지역별 프록시 접속 정보. 자격증명은 코드/저장 파일에 남기지 말고
    환경변수(.env)로 주입한다 — collect.py가 로드한다."""

    server: str  # 예: "http://gate.provider.com:8000"
    username: str | None = None
    password: str | None = None
    label: str = "proxy"  # 기록용 별칭, 예: "daejeon"


@dataclass
class FetchResult:
    ok: bool
    html: str
    status_code: int | None
    blocked: bool
    error: str | None = None
    source_url: str = ""
    exit_ip_info: dict | None = None  # 프록시 사용 시 실제 관측된 위치 (있으면)


def _search_url(keyword: str, device: str) -> str:
    from urllib.parse import quote

    q = quote(keyword)
    if device == "mobile":
        return f"https://m.search.naver.com/search.naver?query={q}"
    return f"https://search.naver.com/search.naver?query={q}"


def _proxy_requests_dict(proxy: "ProxySpec | None") -> dict | None:
    if not proxy:
        return None
    proxy_url = proxy.server
    if proxy.username:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(proxy.server)
        auth = f"{proxy.username}:{proxy.password or ''}"
        proxy_url = urlunsplit((parts.scheme, f"{auth}@{parts.netloc}", parts.path, parts.query, parts.fragment))
    return {"http": proxy_url, "https": proxy_url}


def check_exit_ip(proxy: "ProxySpec | None" = None, *, timeout: float = 8.0) -> dict | None:
    """프록시가 실제로 의도한 지역으로 나가는지 확인한다 (naver.com이 아닌 IP 조회 서비스만 이용).
    프록시 설정 실수로 엉뚱한 지역 IP를 쓰면서 네이버 요청을 낭비하는 것을 막기 위함.

    ipinfo.io 무료 조회는 일부 ASN(특히 연구망 계열)에서 실제 위치와 다르게 나오는 것을
    확인해서, ip-api.com을 기본으로 쓰고 ipinfo.io는 참고용 보조 결과로만 덧붙인다.
    """
    import requests

    proxies = _proxy_requests_dict(proxy)
    result: dict = {}
    try:
        resp = requests.get("http://ip-api.com/json/", proxies=proxies, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "success":
            result = {
                "ip": data.get("query"),
                "city": data.get("city"),
                "region": data.get("regionName"),
                "country": data.get("countryCode"),
                "org": data.get("org") or data.get("isp"),
            }
        else:
            result = {"error": data.get("message", "ip-api lookup failed")}
    except Exception as e:  # noqa: BLE001
        result = {"error": str(e)}

    if "error" in result:
        try:
            resp = requests.get("https://ipinfo.io/json", proxies=proxies, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            result = {"ip": data.get("ip"), "city": data.get("city"), "region": data.get("region"), "country": data.get("country"), "org": data.get("org")}
        except Exception as e:  # noqa: BLE001
            result = {"error": str(e)}
    return result


async def fetch_serp(
    keyword: str,
    device: str,
    *,
    proxy: ProxySpec | None = None,
    page_timeout_ms: int | None = None,
    expand_place: bool = False,
) -> FetchResult:
    ua = MOBILE_UA if device == "mobile" else PC_UA
    url = _search_url(keyword, device)
    proxy_config = ProxyConfig(server=proxy.server, username=proxy.username, password=proxy.password) if proxy else None
    browser = BrowserConfig(headless=True, user_agent=ua, proxy_config=proxy_config)
    # 레지덴셜 프록시는 실제 가정용 회선을 경유해서 직접 연결보다 훨씬 느리고,
    # 백그라운드 트래픽 때문에 networkidle에 끝까지 도달하지 못하는 경우가 있다.
    if page_timeout_ms is None:
        page_timeout_ms = 60000 if proxy else 30000
    js_steps = ["window.scrollTo(0, document.body.scrollHeight);", "await new Promise(r=>setTimeout(r,1200));"]
    if expand_place and device == "pc":  # 모바일 캡처에는 이 "다음" 버튼이 없었다
        js_steps.append(PLACE_EXPAND_JS)
        page_timeout_ms = max(page_timeout_ms, 45000)  # 클릭 반복만큼 시간이 더 걸린다
    # expand_place는 클릭 시점에만 나가는 백그라운드 요청이 있어 networkidle이
    # 불안정하게 오래 걸릴 수 있어 load로 고정한다 (실측 테스트에서 이게 안정적이었음).
    run = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        js_code=js_steps,
        wait_until="load" if (proxy or expand_place) else "networkidle",
        page_timeout=page_timeout_ms,
    )
    async with AsyncWebCrawler(config=browser) as crawler:
        result = await crawler.arun(url=url, config=run)
        html = result.html or ""
        blocked = any(marker in html for marker in BLOCK_MARKERS) or result.status_code == 429
        return FetchResult(
            ok=bool(result.success) and not blocked,
            html=html,
            status_code=result.status_code,
            blocked=blocked,
            error=None if result.success else str(getattr(result, "error_message", "unknown error")),
            source_url=url,
        )


async def fetch_with_retry(
    keyword: str,
    device: str,
    *,
    proxy: ProxySpec | None = None,
    expand_place: bool = False,
    retries: int = 1,
    backoff_seconds: float = 20.0,
) -> FetchResult:
    last: FetchResult | None = None
    for attempt in range(retries + 1):
        last = await fetch_serp(keyword, device, proxy=proxy, expand_place=expand_place)
        if last.ok:
            return last
        if last.blocked:
            # 차단 상태에서는 재시도해도 같은 IP로는 대부분 동일하게 막힌다.
            # 무리하게 재시도하지 않고 즉시 반환한다 (재시도 폭주 방지).
            return last
        if attempt < retries:
            await asyncio.sleep(backoff_seconds)
    return last
