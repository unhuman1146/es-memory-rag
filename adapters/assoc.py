"""One-hop association candidates, derived from data that already exists.

No embeddings, no model call: an edge here is a structural fact already stored
in the database, and the caller decides whether to admit the neighbour.

Three edge types, strongest first:

``source``
    The two memories were built from the same event. Effectively the same
    moment, recorded twice.
``conversation``
    Their source events share a ``peer`` (same account / thread) and lie within
    ``window_days`` of each other. This is the "we were talking about that
    around then" edge -- weak on purpose, which is why the caller caps how many
    neighbours it admits.
``topic``
    One atom's object string appears in the other's (``猫`` vs ``猫粮``). Exact
    substring, length >= 2, so it does not fire on single common characters.

What this deliberately does **not** do: it does not invent semantic links. A
birthday memory and a cake message have no edge here even though a person
would connect them instantly -- that connection comes from the model's world
knowledge, not from the store. ``scripts/demo_140_alpha.py`` shows both sides.
"""
from __future__ import annotations

import json

DEFAULT_WINDOW_DAYS = 45
MIN_TOKEN_LEN = 2
MAX_SCAN = 500


def _events(engine, source_ids):
    out = []
    for sid in source_ids or []:
        row = engine.db.execute('SELECT peer, at FROM events WHERE id=?', (sid,)).fetchone()
        if row is not None:
            out.append({'peer': row[0], 'at': float(row[1])})
    return out


def _source_ids(row):
    try:
        return [str(s) for s in json.loads(row.get('source_ids') or '[]')]
    except (TypeError, ValueError):
        return []


def _objects(row):
    """Object strings. Single characters are kept: 「猫」 and 「猫粮」 only link
    through the one-character overlap, and dropping it removes the useful edge.
    The cost is that a one-character object can match by accident, which is why
    the caller caps how many neighbours it admits and marks them as guesses."""
    try:
        facts = json.loads(row.get('facts') or '[]')
    except (TypeError, ValueError):
        return set()
    out = set()
    for fact in facts:
        value = str(fact.get('object') or '').strip()
        if value:
            out.add(value)
    return out


def _live_ids(engine, requester):
    rows = engine.db.execute(
        """SELECT id FROM memories
           WHERE deleted_at IS NULL AND purged=0 AND profile!='archived'
             AND (visibility='shared' OR owner=?)
           ORDER BY last_decay DESC LIMIT ?""",
        (requester or '', MAX_SCAN)).fetchall()
    return [r[0] for r in rows]


def _topic_overlap(a, b):
    """Shared object string. Equality needs two characters, containment one."""
    for left in a:
        for right in b:
            if left == right and len(left) >= MIN_TOKEN_LEN:
                return left
            if len(left) < len(right) and left in right and len(right) >= MIN_TOKEN_LEN:
                return left
            if len(right) < len(left) and right in left and len(left) >= MIN_TOKEN_LEN:
                return right
    return ''


def neighbors(engine, seed_ids, requester='', limit=2, window_days=DEFAULT_WINDOW_DAYS,
              now=None, exclude=None):
    """Return at most ``limit`` one-hop neighbours of ``seed_ids``.

    Each item is ``{'memory_id', 'strength', 'via', 'evidence'}`` where ``via``
    is ``source`` / ``conversation`` / ``topic`` and ``evidence`` is a short
    human-readable reason the caller can log. Order is strength descending.
    """
    seeds = [sid for sid in (seed_ids or []) if sid]
    if not seeds or engine is None:
        return []
    exclude = set(exclude or []) | set(seeds)

    seed_meta = {}
    for sid in seeds:
        row = engine.row(sid)
        if row is not None:
            seed_meta[sid] = {'events': _events(engine, _source_ids(row)),
                              'objects': _objects(row),
                              'sources': set(_source_ids(row))}

    scored = {}
    for mid in _live_ids(engine, requester):
        if mid in exclude:
            continue
        row = engine.row(mid)
        if row is None:
            continue
        mine_events = _events(engine, _source_ids(row))
        mine_objects = _objects(row)
        mine_sources = set(_source_ids(row))
        best = None
        for sid, meta in seed_meta.items():
            if sid == mid:
                continue
            if mine_sources & meta['sources']:
                cand = (1.0, 'source', 'same source event')
            else:
                cand = None
                for a in meta['events']:
                    for b in mine_events:
                        if a['peer'] != b['peer']:
                            continue
                        days = abs(a['at'] - b['at']) / 86400.0
                        if days <= window_days:
                            strength = round(1.0 - days / float(window_days), 3)
                            cand = (max(0.05, strength), 'conversation',
                                    'same thread, %.0f days apart' % days)
                            break
                    if cand:
                        break
                if cand is None:
                    token = _topic_overlap(meta['objects'], mine_objects)
                    if token:
                        cand = (0.6, 'topic', 'shared topic 「%s」' % token)
            if cand and (best is None or cand[0] > best[0]):
                best = cand
        if best:
            scored[mid] = {'memory_id': mid, 'strength': best[0], 'via': best[1],
                           'evidence': best[2]}

    ordered = sorted(scored.values(), key=lambda item: item['strength'], reverse=True)
    return ordered[:max(0, int(limit))]
