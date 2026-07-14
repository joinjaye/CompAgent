"""Zoomex（我方基线）近 90 天公告的轻量全文检索：纯 Python TF-IDF，不引入 sklearn
（项目运行时依赖目前只有 PyYAML/certifi，见 requirements.txt；语料规模是"单个
category×locale 的近 90 天公告"，通常几十到几百条，用不到 sklearn 那套向量化管线）。

用法：
    index = build_index(conn, category="campaign", locale="EN")
    hits = index.search(query_text, top_k=5)

Zoomex 各 locale/menu_id 的历史数据可能很薄（甚至为空，见 CLAUDE.md「Phase 3 之后
补丁」全量建仓记录），search() 只返回真正有词面重叠的文档（similarity_score > 0），
命中数量本身就是"基线数据是否充分"的信号，不在这里做任何补全。
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _term_counts(tokens: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for t in tokens:
        counts[t] = counts.get(t, 0) + 1
    return counts


@dataclass
class ZmxArticle:
    uid: str
    title: Optional[str]
    content_preview: str
    post_time: Optional[str]
    similarity_score: float


@dataclass
class _Doc:
    uid: str
    title: Optional[str]
    content: str
    post_time: Optional[str]
    tfidf: dict[str, float]
    norm: float


class ZmxIndex:
    """一个 (category, locale) 组合的 TF-IDF 索引。"""

    def __init__(self, docs: list[_Doc], idf: dict[str, float], preview_chars: int = 400):
        self._docs = docs
        self._idf = idf
        self._preview_chars = preview_chars

    def __len__(self) -> int:
        return len(self._docs)

    def search(self, query_text: str, top_k: int = 5) -> list[ZmxArticle]:
        if not self._docs:
            return []

        query_tf = _term_counts(_tokenize(query_text))
        query_vec: dict[str, float] = {}
        for term, count in query_tf.items():
            idf = self._idf.get(term)
            if idf is None:
                continue
            query_vec[term] = count * idf
        query_norm = math.sqrt(sum(w * w for w in query_vec.values()))
        if query_norm == 0:
            return []

        scored: list[tuple[float, _Doc]] = []
        for doc in self._docs:
            if doc.norm == 0:
                continue
            dot = sum(w * doc.tfidf.get(term, 0.0) for term, w in query_vec.items())
            if dot <= 0:
                continue
            similarity = dot / (query_norm * doc.norm)
            if similarity > 0:
                scored.append((similarity, doc))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        results = []
        for similarity, doc in scored[:top_k]:
            results.append(
                ZmxArticle(
                    uid=doc.uid,
                    title=doc.title,
                    content_preview=doc.content[: self._preview_chars],
                    post_time=doc.post_time,
                    similarity_score=similarity,
                )
            )
        return results


def build_index(
    conn: sqlite3.Connection,
    category: str,
    locale: str,
    lookback_days: int = 90,
    preview_chars: int = 400,
    reference_date: Optional[datetime] = None,
) -> ZmxIndex:
    """按 category + locale 过滤 source='Zoomex' 的近 lookback_days 天数据建索引，
    只索引 content 非空的行。reference_date 默认当前 UTC 时间，测试里可传固定值
    让"近 90 天"这个窗口可复现。
    """
    reference_date = reference_date or datetime.now(timezone.utc)
    cutoff = (reference_date - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = conn.execute(
        """
        SELECT uid, title, content, post_time
        FROM announcements
        WHERE source = 'Zoomex' AND category = ? AND locale = ?
              AND content IS NOT NULL AND content != ''
              AND post_time IS NOT NULL AND post_time >= ?
        """,
        (category, locale, cutoff),
    ).fetchall()

    doc_tokens: list[list[str]] = []
    doc_meta: list[tuple[str, Optional[str], str, Optional[str]]] = []
    for uid, title, content, post_time in rows:
        text = f"{title or ''} {content or ''}"
        tokens = _tokenize(text)
        doc_tokens.append(tokens)
        doc_meta.append((uid, title, content, post_time))

    n_docs = len(doc_tokens)
    df: dict[str, int] = {}
    for tokens in doc_tokens:
        for term in set(tokens):
            df[term] = df.get(term, 0) + 1

    idf: dict[str, float] = {
        term: math.log((1 + n_docs) / (1 + count)) + 1.0 for term, count in df.items()
    }

    docs: list[_Doc] = []
    for tokens, (uid, title, content, post_time) in zip(doc_tokens, doc_meta):
        tf = _term_counts(tokens)
        tfidf = {term: count * idf[term] for term, count in tf.items()}
        norm = math.sqrt(sum(w * w for w in tfidf.values()))
        docs.append(_Doc(uid=uid, title=title, content=content, post_time=post_time, tfidf=tfidf, norm=norm))

    return ZmxIndex(docs, idf, preview_chars=preview_chars)
