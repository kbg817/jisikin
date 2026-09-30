"""질문을 제품/카테고리에 매칭하고 관련도 점수를 계산한다.

점수 규칙 (제품별로 따로 계산):
  제품 키워드  : 제목에 있으면 3점, 본문에만 있으면 2점
  카테고리     : 제목에 있으면 2점, 본문에만 있으면 1점
  score >= min_score 이면 그 제품의 '관련 질문'.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import Product

W_KEYWORD_TITLE = 3
W_KEYWORD_BODY = 2
W_CATEGORY_TITLE = 2
W_CATEGORY_BODY = 1


def compile_pattern(raw: str) -> re.Pattern:
    """키워드 → 정규식. 키워드에 쓴 띄어쓰기는 있어도/없어도 매칭된다."""
    if raw.startswith("re:"):
        return re.compile(raw[3:], re.IGNORECASE)
    parts = raw.split()
    return re.compile(r"\s*".join(re.escape(p) for p in parts), re.IGNORECASE)


def normalize(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


@dataclass
class Match:
    product_id: str
    score: float
    relevant: bool
    keywords: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)  # 실제로 매칭된 문구 (화면 강조용)

    def to_dict(self) -> dict:
        return {
            "product_id": self.product_id,
            "score": self.score,
            "relevant": self.relevant,
            "keywords": self.keywords,
            "categories": self.categories,
            "terms": self.terms,
        }


@dataclass
class _CompiledProduct:
    product: Product
    keywords: list[tuple[str, re.Pattern]]
    categories: list[tuple[str, list[re.Pattern]]]
    masks: list[re.Pattern]
    blocks: list[re.Pattern]


class Matcher:
    def __init__(self, products: list[Product]):
        self._products = []
        for p in products:
            masks, blocks = [], []
            for raw in p.exclude:
                if raw.startswith("!"):
                    blocks.append(compile_pattern(raw[1:].strip()))
                else:
                    masks.append(compile_pattern(raw))
            self._products.append(
                _CompiledProduct(
                    product=p,
                    keywords=[(k, compile_pattern(k)) for k in p.keywords],
                    categories=[(c.name, [compile_pattern(k) for k in c.keywords]) for c in p.categories],
                    masks=masks,
                    blocks=blocks,
                )
            )

    def classify(self, title: str, body: str = "") -> list[Match]:
        """점수 > 0 인 제품들을 (관련 여부, 점수) 높은 순으로 돌려준다."""
        title_n, body_n = normalize(title), normalize(body)
        results: list[Match] = []
        for cp in self._products:
            m = self._match_product(cp, title_n, body_n)
            if m and m.score > 0:
                results.append(m)
        results.sort(key=lambda m: (m.relevant, m.score), reverse=True)
        return results

    @staticmethod
    def best(matches: list[Match]) -> Match | None:
        return next((m for m in matches if m.relevant), None)

    @staticmethod
    def _match_product(cp: _CompiledProduct, title: str, body: str) -> Match | None:
        full = f"{title} {body}"
        if any(b.search(full) for b in cp.blocks):
            return None
        for mask in cp.masks:
            title = mask.sub(" ", title)
            body = mask.sub(" ", body)

        score = 0.0
        keywords: list[str] = []
        terms: list[str] = []

        def find(pat: re.Pattern) -> tuple[bool, bool]:
            mt = pat.search(title)
            if mt:
                _add_term(terms, mt.group(0))
                return True, False
            mb = pat.search(body)
            if mb:
                _add_term(terms, mb.group(0))
                return False, True
            return False, False

        for raw, pat in cp.keywords:
            in_title, in_body = find(pat)
            if in_title:
                score += W_KEYWORD_TITLE
                keywords.append(raw)
            elif in_body:
                score += W_KEYWORD_BODY
                keywords.append(raw)

        title_cats, body_cats = [], []
        for name, pats in cp.categories:
            hits = [find(p) for p in pats]
            if any(t for t, _ in hits):
                score += W_CATEGORY_TITLE
                title_cats.append(name)
            elif any(b for _, b in hits):
                score += W_CATEGORY_BODY
                body_cats.append(name)

        return Match(
            product_id=cp.product.id,
            score=score,
            relevant=score >= cp.product.min_score and (bool(keywords) or not cp.product.require_keyword),
            keywords=keywords,
            categories=title_cats + body_cats,
            terms=terms[:12],
        )


def _add_term(terms: list[str], term: str) -> None:
    term = term.strip()
    if term and term not in terms:
        terms.append(term)
