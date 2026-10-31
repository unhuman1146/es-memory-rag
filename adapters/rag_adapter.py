"""The ``RAGAdapter`` implementation from ``references/integration.md``.

The adapter is deliberately dumb about semantics: it embeds, stores, searches
and reranks. It never decides *what is true*. Every retrieval is re-joined
against the authoritative memory store, because the index can be stale and a
stale index must not be able to resurrect a deleted memory.
"""
from __future__ import annotations

import hashlib
import json
import math
import uuid

from .rerank import LexicalReranker

# Starting point from references/integration.md; calibrate against the real
# model before trusting it, and do not mix scales from different vendors.
RELEVANCE_WEIGHT = 0.8
MEMORY_WEIGHT = 0.2
MAX_WEIGHT = 101.0


def chunk_text(text, chunk_chars=400):
    """Split a projection into chunks on paragraph boundaries when possible."""
    text = (text or '').strip()
    if not text:
        return []
    if len(text) <= chunk_chars:
        return [text]
    parts = []
    current = ''
    for para in text.split('\n'):
        piece = para.strip()
        if not piece:
            continue
        if len(piece) > chunk_chars:
            if current:
                parts.append(current)
                current = ''
            for i in range(0, len(piece), chunk_chars):
                parts.append(piece[i:i + chunk_chars])
            continue
        if len(current) + len(piece) + 1 <= chunk_chars:
            current = (current + '\n' + piece) if current else piece
        else:
            parts.append(current)
            current = piece
    if current:
        parts.append(current)
    return parts


def projection_hash(text):
    """Identity of the *projection content*.

    Weight or ACL changes do not change the hash, so the embedding can be
    reused instead of recomputed for every nightly revision.
    """
    return hashlib.sha256((text or '').strip().encode('utf-8')).hexdigest()


class RAGAdapter:
    """Reference adapter over an embedding client and a vector store.

    Parameters
    ----------
    embedding:
        Object with ``embed(texts) -> list[list[float]]``.
    store:
        Object with the ``SqliteVectorStore`` interface.
    engine:
        Optional authoritative ``MemoryEngine`` (or anything exposing
        ``row(memory_id) -> dict``). When present, every search is joined
        against it and stale or forbidden rows are dropped.
    reranker:
        Optional object with ``rerank(query, documents)``. When absent the
        pipeline reports ``degraded`` instead of pretending to rerank.
    """

    def __init__(self, embedding, store, engine=None, reranker=None, chunk_chars=400,
                 require_rerank=False, relevance_weight=RELEVANCE_WEIGHT, memory_weight=MEMORY_WEIGHT,
                 min_relevance=None, embedding_identity=None, search_scan_limit=4096):
        self.embedding = embedding
        self.store = store
        self.engine = engine
        self.reranker = reranker
        self.chunk_chars = chunk_chars
        self.require_rerank = require_rerank
        self.relevance_weight = relevance_weight
        self.memory_weight = memory_weight
        # A high-weight but unrelated memory must never be forced into the
        # context. The threshold is per model and per corpus -- calibrate it,
        # do not hardcode a vendor's raw score.
        self.min_relevance = min_relevance
        self.search_scan_limit = max(1, int(search_scan_limit))
        identity = embedding_identity
        self._identity_from_client = identity is None and callable(getattr(embedding, 'cache_identity', None))
        if identity is None and callable(getattr(embedding, 'cache_identity', None)):
            identity = embedding.cache_identity()
        if identity is None:
            # Opaque custom backends remain usable, but cannot claim persistent
            # cache compatibility across adapter instances. Supply an explicit
            # identity to opt into safe reuse across restarts.
            identity = {'provider': 'custom', 'model': 'opaque',
                        'version': uuid.uuid4().hex, 'dim': getattr(embedding, 'dim', None)}
        self.embedding_identity = dict(identity)
        if set(self.embedding_identity) != {'provider', 'model', 'version', 'dim'} or any(
                not isinstance(self.embedding_identity[k], str) or not self.embedding_identity[k]
                for k in ('provider', 'model', 'version')):
            raise ValueError('embedding_identity requires non-secret provider/model/version/dim')
        dim = self.embedding_identity['dim']
        if dim is None and embedding_identity is not None:
            raise ValueError('explicit embedding identity requires a dimension')
        if dim is not None and (type(dim) is not int or dim <= 0):
            raise ValueError('embedding identity dimension must be a positive integer')

    # ---------- contract ----------

    def upsert(self, memory_id, revision, text, metadata=None):
        """Write the projection of one memory into the index.

        Returns a report dict. ``reused_embedding`` is True when the
        projection text did not change and the stored vector was reused --
        a weight-only change must not cost an embedding call.
        """
        self._refresh_identity()
        metadata = dict(metadata or {})
        metadata.setdefault('memory_id', memory_id)
        metadata['revision'] = revision
        chunks = chunk_text(text, self.chunk_chars)
        phash = projection_hash(text)

        existing = self.store.chunks_of(memory_id)
        reused = (bool(existing) and self.embedding_identity['dim'] is not None
                  and all(c.get('projection_hash') == phash
                          and (c.get('metadata') or {}).get('_embedding_identity') == self.embedding_identity
                          and len(c['vector']) == self.embedding_identity['dim']
                          for c in existing)
                  and [c['text'] for c in existing] == chunks)
        if reused:
            vectors = [c['vector'] for c in existing]
            chunks = [c['text'] for c in existing]
        else:
            if not chunks:
                self.store.delete(memory_id)
                return {'ok': True, 'memory_id': memory_id, 'revision': revision, 'chunks': 0,
                        'reused_embedding': False, 'embedded': 0}
            vectors = self.embedding.embed(chunks)
        self._validate_vectors(vectors, len(chunks))
        metadata['_embedding_identity'] = dict(self.embedding_identity)
        self.store.upsert(memory_id, revision, chunks, vectors, metadata, phash)
        return {'ok': True, 'memory_id': memory_id, 'revision': revision, 'chunks': len(chunks),
                'reused_embedding': reused, 'embedded': 0 if reused else len(chunks)}

    def delete(self, memory_id):
        """Remove every chunk of this memory, all revisions included."""
        removed = self.store.delete(memory_id)
        return {'ok': True, 'memory_id': memory_id, 'removed': removed}

    def count(self, memory_id):
        """Number of indexed chunks for this memory. Zero after a delete."""
        return self.store.count(memory_id)

    def search(self, query, acl_filter=None, limit=10):
        """Vector candidates, re-joined against the authoritative store.

        Drops deleted / purged / archived / superseded / forbidden rows, then
        blends relevance with memory weight.
        """
        self._refresh_identity()
        acl_filter = acl_filter or {}
        requester = acl_filter.get('requester') or acl_filter.get('owner')
        store_filter = dict(acl_filter)
        if 'requester' in store_filter and 'owner' not in store_filter:
            store_filter['owner'] = store_filter['requester']
        vectors = self.embedding.embed([query])
        self._validate_vectors(vectors, 1)
        vector = vectors[0]
        store_filter['_embedding_identity'] = dict(self.embedding_identity)
        store_filter['_distinct_memories'] = True

        # Refill after authoritative filtering. The reference store limits
        # distinct memories, not chunks; legacy stores can still return chunks.
        # A bounded widening window and no-progress guard support both.
        window = min(self.search_scan_limit, max(1, limit * 3 if limit else self.search_scan_limit))
        previous = set()
        while True:
            raw = self.store.search(vector, limit=window, acl_filter=store_filter)
            out = self._search_candidates(raw, requester)
            if (limit and len(out) >= limit) or len(raw) < window or window >= self.search_scan_limit:
                break
            keys = {(c['memory_id'], c['revision'], c.get('chunk_index')) for c in raw}
            if keys == previous:
                break
            previous = keys
            window = min(self.search_scan_limit, window * 2)
        return out[:limit] if limit else out

    def _search_candidates(self, raw, requester):
        out = []
        for cand in raw:
            if (cand.get('metadata') or {}).get('_embedding_identity') != self.embedding_identity:
                raise ValueError('embedding identity mismatch or legacy index; rebuild the index with the current model')
            row = self._authoritative_row(cand['memory_id'])
            if row is not None:
                if row.get('deleted_at') is not None or row.get('purged'):
                    continue
                if row.get('profile') == 'archived':
                    continue
                if int(row.get('revision', -1)) != int(cand['revision']):
                    continue
                visibility = row.get('visibility')
                owner = row.get('owner')
                # Fail closed: a private memory is only visible to its owner,
                # and a search without a requester context sees nothing private.
                if visibility != 'shared' and (not requester or owner != requester):
                    continue
                weight = float(row.get('weight') or 0.0)
            else:
                # No authoritative row: the memory is gone from the source of
                # truth, so its projection must not be served.
                if self.engine is not None:
                    continue
                meta = cand.get('metadata') or {}
                if meta.get('visibility', 'shared') != 'shared' and (
                        not requester or meta.get('owner') != requester):
                    continue
                weight = float(meta.get('weight') or 0.0)

            relevance = float(cand['relevance'])
            if self.min_relevance is not None and relevance < self.min_relevance:
                continue
            score = self.relevance_weight * relevance + self.memory_weight * (weight / MAX_WEIGHT)
            item = dict(cand)
            item['weight'] = weight
            item['score'] = score
            item['relevance'] = relevance
            out.append(item)
        out.sort(key=lambda c: c['score'], reverse=True)
        distinct = {}
        for item in out:
            distinct.setdefault(item['memory_id'], item)
        return list(distinct.values())

    def rerank(self, query, candidates):
        """Rerank candidates.

        Returns ``{'status': 'ok', ...}`` or ``{'status': 'degraded', ...}``.
        A missing reranker is reported, never silently treated as a rerank.
        """
        candidates = list(candidates or [])
        if not candidates:
            return {'status': 'ok' if self.reranker else self._degraded_status(), 'candidates': [], 'reason': 'no candidates'}
        if self.reranker is None:
            status = self._degraded_status()
            ordered = sorted(candidates, key=lambda c: c.get('score', 0.0), reverse=True)
            return {'status': status, 'candidates': ordered,
                    'reason': 'no reranker configured; results are vector+weight order only'}

        documents = [c.get('text', '') for c in candidates]
        try:
            ranked = list(self.reranker.rerank(query, documents))
            if not ranked:
                raise ValueError('reranker returned no results for nonempty candidates')
            for item in ranked:
                index, score = item['index'], item['score']
                if (type(index) is not int or not 0 <= index < len(candidates)
                        or type(score) not in (int, float) or not math.isfinite(score)
                        or not 0 <= score <= 1):
                    raise ValueError('reranker must return valid indices and finite scores in 0..1')
        except Exception as exc:  # a broken reranker must not be silent
            ordered = sorted(candidates, key=lambda c: c.get('score', 0.0), reverse=True)
            return {'status': 'degraded', 'candidates': ordered,
                    'reason': 'reranker failed: %s' % (exc,), 'reranker_backend': getattr(self.reranker, 'backend', 'unknown')}

        ordered = []
        used = set()
        for item in ranked:
            index = item['index']
            if 0 <= index < len(candidates) and index not in used:
                used.add(index)
                cand = dict(candidates[index])
                cand['rerank_score'] = item['score']
                ordered.append(cand)
        # A reranker may answer with a subset (many vendors return only the
        # top-n). Append what it did not rank, once, in arrival order. The
        # previous version compared dicts against already-ranked dicts that
        # carry an extra ``rerank_score`` key, so nothing ever matched and
        # every candidate was appended a second time.
        for index, cand in enumerate(candidates):
            if index not in used:
                ordered.append(dict(cand))
        status = 'degraded' if getattr(self.reranker, 'backend', '') == 'lexical' else 'ok'
        return {'status': status, 'candidates': ordered,
                'reranker_backend': getattr(self.reranker, 'backend', 'unknown')}

    # ---------- helpers ----------

    def _refresh_identity(self):
        if self._identity_from_client:
            identity = dict(self.embedding.cache_identity())
            if (set(identity) != {'provider', 'model', 'version', 'dim'}
                    or type(identity['dim']) is not int or identity['dim'] <= 0
                    or any(not isinstance(identity[k], str) or not identity[k]
                           for k in ('provider', 'model', 'version'))):
                raise ValueError('invalid embedding cache identity')
            self.embedding_identity = identity

    def _validate_vectors(self, vectors, expected):
        if len(vectors) != expected or not vectors:
            raise ValueError('embedding batch count mismatch')
        dim = self.embedding_identity['dim']
        if dim is None:
            dim = len(vectors[0])
            self.embedding_identity['dim'] = dim
        if not dim or any(len(v) != dim or not all(math.isfinite(float(x)) for x in v)
                          for v in vectors):
            raise ValueError('embedding identity dimension/values mismatch; rebuild the index')

    def _authoritative_row(self, memory_id):
        if self.engine is None:
            return None
        row = self.engine.row(memory_id)
        return row

    def _degraded_status(self):
        if self.require_rerank:
            return 'degraded'
        return 'degraded'
