# -*- coding: utf-8 -*-
"""노출 여부를 추적할 "지정 업체" 목록. 마스터 키워드 시트의 담당 코드(국/스/초/SB)와
1:1로 대응한다. 값(별칭 리스트)은 검색 결과 텍스트(플레이스 상호명 · 검색광고 제목 ·
게시물 제목)에 그대로 포함되어 있는지 부분일치로 검사하는 데 쓴다 — 업체명을 추정하는
게 아니라, 이미 알고 있는 업체명이 문자 그대로 등장하는지만 본다.

새 업체가 생기거나 별칭을 더 추가하고 싶으면 이 파일만 고치면 된다
(collector/build_index.py가 그대로 가져다 쓴다).
"""
from __future__ import annotations

# 코드(마스터 시트의 담당 컬럼 값) -> {업체명, 별칭 리스트, 자사 도메인 리스트}
# 도메인 일치는 제목 텍스트에 이름이 우연히 섞인 것과 달리 사실상 확정 신호라서,
# 노출 등급을 나눌 때(compute_exposure) 별칭 매칭(candidate)보다 위(domain, 더 신뢰도 높음)로 취급한다.
COMPANIES: dict[str, dict] = {
    "국": {"name": "국가대표", "aliases": ["국가대표"], "domains": ["okmuju.com"]},
    "스": {"name": "스카이", "aliases": ["스카이"], "domains": ["mujusky.co.kr"]},
    "초": {"name": "초이스", "aliases": ["초이스"], "domains": ["kgrc.co.kr"]},
    "SB": {"name": "SB아카데미", "aliases": ["SB아카데미"], "domains": ["sbski.co.kr"]},
}
