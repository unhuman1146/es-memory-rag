"""Pure-stdlib SQLite vector store.

One row per chunk. The index is a separate database from the authoritative
memory store: the memory store decides *what is true*, the index is only a
projection that can be rebuilt from the outbox at any time.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from array import array


def _pack(vector):
    return array('f', [float(v) for v in vector]).tobytes()


def _unpack(blob):
    buf = array('f')
    buf.frombytes(blob)
    return list(buf)


def cosine(a, b):
    if len(a) != len(b):
        raise ValueError('embedding dimension mismatch; rebuild the index with the current model')
    if not all(math.isfinite(float(x)) for x in a) or not all(
            math.isfinite(float(x)) for x in b):
        raise ValueError('embedding contains non-finite values')
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


class SqliteVectorStore:
    """Chunk store with cosine search. No third-party vector database."""

    def __init__(self, path):
        self.path = str(path)
        self.db = sqlite3.connect(self.path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=15000')
        self.db.executescript('''
CREATE TABLE IF NOT EXISTS chunks(
 memory_id TEXT NOT NULL, revision INTEGER NOT NULL, chunk_index INTEGER NOT NULL,
 text TEXT NOT NULL, vector BLOB NOT NULL, dim INTEGER NOT NULL,
 metadata TEXT NOT NULL DEFAULT '{}', projection_hash TEXT NOT NULL DEFAULT '',
 updated_at REAL NOT NULL DEFAULT 0,
 PRIMARY KEY(memory_id, chunk_index));
CREATE INDEX IF NOT EXISTS chunks_by_memory ON chunks(memory_id);
''')
        self.db.commit()

    # ---------- writes ----------

    def upsert(self, memory_id, revision, chunks, vectors, metadata=None, projection_hash=''):
        """Replace every chunk of this memory with the new revision."""
        chunks, vectors = list(chunks), list(vectors)
        if len(chunks) != len(vectors):
            raise ValueError('embedding batch count does not match chunk count')
        if vectors:
            dim = len(vectors[0])
            if not dim or any(len(v) != dim or not all(math.isfinite(float(x)) for x in v)
                              for v in vectors):
                raise ValueError('invalid or inconsistent embedding dimensions/values')
        metadata = metadata or {}
        now = time.time()
        with self.db:
            self.db.execute('DELETE FROM chunks WHERE memory_id=?', (memory_id,))
            for index, (text, vector) in enumerate(zip(chunks, vectors)):
                self.db.execute(
                    'INSERT INTO chunks(memory_id,revision,chunk_index,text,vector,dim,metadata,projection_hash,updated_at)'
                    ' VALUES(?,?,?,?,?,?,?,?,?)',
                    (memory_id, revision, index, text, _pack(vector), len(vector),
                     json.dumps(metadata, ensure_ascii=False), projection_hash, now))
        return len(chunks)

    def delete(self, memory_id):
        """Remove every chunk of this memory, all revisions included."""
        with self.db:
            cur = self.db.execute('DELETE FROM chunks WHERE memory_id=?', (memory_id,))
        return cur.rowcount

    # ---------- reads ----------

    def count(self, memory_id):
        row = self.db.execute('SELECT COUNT(*) FROM chunks WHERE memory_id=?', (memory_id,)).fetchone()
        return int(row[0])

    def chunks_of(self, memory_id):
        rows = self.db.execute(
            'SELECT memory_id,revision,chunk_index,text,vector,metadata,projection_hash FROM chunks'
            ' WHERE memory_id=? ORDER BY chunk_index', (memory_id,)).fetchall()
        out = []
        for r in rows:
            out.append({
                'memory_id': r[0], 'revision': r[1], 'chunk_index': r[2], 'text': r[3],
                'vector': _unpack(r[4]), 'metadata': json.loads(r[5] or '{}'), 'projection_hash': r[6],
            })
        return out

    def projection_hash(self, memory_id):
        row = self.db.execute('SELECT projection_hash FROM chunks WHERE memory_id=? LIMIT 1', (memory_id,)).fetchone()
        return row[0] if row else ''

    def total(self):
        return int(self.db.execute('SELECT COUNT(*) FROM chunks').fetchone()[0])

    def search(self, vector, limit=10, acl_filter=None):
        """Cosine chunk search; the adapter requests distinct-memory results.

        The authoritative permission check happens in the adapter against the
        memory store, because the index may be stale.
        """
        sql = 'SELECT memory_id,revision,chunk_index,text,vector,metadata,projection_hash FROM chunks'
        args = []
        acl_filter = acl_filter or {}
        owner = acl_filter.get('owner')
        allowed_visibility = acl_filter.get('visibility')
        if owner:
            # A pre-filter must never create a false negative: rows whose
            # metadata does not carry an owner or visibility are kept and
            # judged by the authoritative store in the adapter.
            sql += (" WHERE (json_extract(metadata,'$.owner') IS NULL"
                    " OR json_extract(metadata,'$.owner')=?"
                    " OR json_extract(metadata,'$.visibility')='shared')")
            args.append(owner)
        if allowed_visibility:
            sql += (' AND' if owner else ' WHERE') + (
                " (json_extract(metadata,'$.visibility') IS NULL"
                " OR json_extract(metadata,'$.visibility') IN (%s))" % ','.join('?' * len(allowed_visibility)))
            args.extend(allowed_visibility)
        rows = self.db.execute(sql, args).fetchall()
        scored = []
        for r in rows:
            metadata = json.loads(r[5] or '{}')
            identity = acl_filter.get('_embedding_identity')
            if identity is not None and metadata.get('_embedding_identity') != identity:
                raise ValueError('embedding identity mismatch or legacy index; rebuild the index with the current model')
            vec = _unpack(r[4])
            scored.append({
                'memory_id': r[0], 'revision': r[1], 'chunk_index': r[2], 'text': r[3],
                'metadata': metadata, 'projection_hash': r[6],
                'relevance': cosine(vector, vec),
            })
        scored.sort(key=lambda c: c['relevance'], reverse=True)
        if not acl_filter.get('_distinct_memories'):
            return scored[:limit] if limit else scored
        distinct = {}
        for candidate in scored:
            distinct.setdefault(candidate['memory_id'], candidate)
        result = list(distinct.values())
        return result[:limit] if limit else result

    def close(self):
        self.db.close()
