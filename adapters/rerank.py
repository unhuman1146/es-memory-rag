"""Rerankers.

A reranker is optional in the sense that the system must still work without
one -- but it may never *pretend* to have reranked. When no reranker is
configured the adapter reports ``degraded`` (see ``references/integration.md``).
"""
from __future__ import annotations

import urllib.error
import urllib.request

from .embedding import EmbeddingError, resolve_api_key, tokenize


class Reranker:
    backend = 'base'

    def __init__(self, model, timeout=30):
        self.model = model
        self.timeout = timeout

    def rerank(self, query, documents):
        """Return a list of ``{'index': int, 'score': float}`` sorted by score desc."""
        raise NotImplementedError


class SiliconFlowReranker(Reranker):
    """``POST /v1/rerank`` on SiliconFlow (bge-reranker-v2-m3 and friends)."""

    backend = 'siliconflow'

    def __init__(self, model='BAAI/bge-reranker-v2-m3', api_key=None, base_url=None, timeout=30):
        key = resolve_api_key(api_key)
        if not key:
            raise EmbeddingError('no API key for the reranker')
        self._key = key
        self.base_url = (base_url or 'https://api.siliconflow.cn/v1').rstrip('/')
        super().__init__(model, timeout=timeout)

    def rerank(self, query, documents):
        documents = list(documents)
        if not documents:
            return []
        payload = {'model': self.model, 'query': query, 'documents': documents,
                   'return_documents': False, 'top_n': len(documents)}
        data = __import__('json').dumps(payload, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(self.base_url + '/rerank', data=data, method='POST', headers={
            'Authorization': 'Bearer ' + self._key, 'Content-Type': 'application/json'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                body = __import__('json').loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode('utf-8', 'replace')[:400]
            raise EmbeddingError('HTTP %s from %s: %s' % (exc.code, self.base_url.split('/')[2], body)) from exc
        except urllib.error.URLError as exc:
            raise EmbeddingError('network error: %s' % (exc.reason,)) from exc
        out = []
        for item in body.get('results') or []:
            out.append({'index': int(item.get('index', 0)), 'score': float(item.get('relevance_score', item.get('score', 0.0)))})
        out.sort(key=lambda r: r['score'], reverse=True)
        return out


class LexicalReranker(Reranker):
    """Deterministic lexical overlap reranker.

    It is honest about itself: the adapter marks the pipeline ``degraded``
    whenever this class is the active reranker, because a lexical overlap is
    not a learned rerank.
    """

    backend = 'lexical'

    def __init__(self, model='lexical-overlap'):
        super().__init__(model, timeout=5)

    @staticmethod
    def _tokens(text):
        return tokenize(text)

    def rerank(self, query, documents):
        q = set(self._tokens(query))
        out = []
        for index, doc in enumerate(documents):
            d = set(self._tokens(doc))
            score = (len(q & d) / len(q)) if q else 0.0
            out.append({'index': index, 'score': score})
        out.sort(key=lambda r: r['score'], reverse=True)
        return out
