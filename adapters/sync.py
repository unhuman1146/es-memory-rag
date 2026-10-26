"""Consume the engine's outbox and drive a real index.

The rules from ``references/integration.md`` are the whole point of this file:

* the authoritative store decides the latest revision -- an event that a newer
  revision has already replaced is acknowledged as *skipped*;
* a real upsert really embeds and writes; a real delete really removes every
  vector chunk of that memory;
* **only** after the index confirms may the event be acknowledged;
* a failed attempt calls ``fail(event_id, error)`` so the queue keeps the event
  and eventually dead-letters it -- no silent loss;
* an event that was never executed is never acknowledged.
"""
from __future__ import annotations

import json


def _metadata_from_row(row, payload):
    meta = {
        'memory_id': row['id'],
        'revision': int(row['revision']),
        'weight': float(row.get('weight') or 0.0),
        'visibility': row.get('visibility') or 'shared',
        'owner': row.get('owner') or '',
        'source_ids': json.loads(row.get('source_ids') or '[]'),
    }
    for key in ('profile', 'pinned', 'tier'):
        if key in row:
            meta[key] = row[key]
    if payload.get('source_ids') and not meta['source_ids']:
        meta['source_ids'] = payload['source_ids']
    return meta


def sync_once(engine, adapter, limit=100):
    """Push pending outbox events into the index.

    Returns a report::

        {'processed': n, 'acked': n, 'skipped': n, 'failed': n, 'dead': n, 'items': [...]}
    """
    report = {'processed': 0, 'acked': 0, 'skipped': 0, 'failed': 0, 'dead': 0, 'items': []}
    events = engine.outbox()
    for row in events:
        if limit and report['processed'] >= limit:
            break
        report['processed'] += 1
        event_id = row['id']
        memory_id = row['memory_id']
        revision = int(row['revision'])
        operation = row['operation']
        try:
            payload = json.loads(row['payload'] or '{}')
        except ValueError:
            payload = {}

        try:
            current = engine.row(memory_id)

            # 1. A newer revision already replaced this event.
            if current is not None and int(current['revision']) > revision:
                engine.acknowledge(event_id)
                report['acked'] += 1
                report['skipped'] += 1
                report['items'].append({'event_id': event_id, 'memory_id': memory_id,
                                        'result': 'skipped_superseded', 'revision': revision})
                continue

            # 2. The memory is gone from the authoritative store: drop its vectors.
            if current is None:
                adapter.delete(memory_id)
                engine.acknowledge(event_id)
                report['acked'] += 1
                report['items'].append({'event_id': event_id, 'memory_id': memory_id,
                                        'result': 'deleted_missing_memory'})
                continue

            gone = (current.get('deleted_at') is not None or current.get('purged')
                    or current.get('profile') == 'archived')
            if operation == 'delete' or gone:
                # 3. A real delete removes every chunk of this memory.
                adapter.delete(memory_id)
                engine.acknowledge(event_id)
                report['acked'] += 1
                report['items'].append({'event_id': event_id, 'memory_id': memory_id,
                                        'result': 'deleted', 'revision': revision})
                continue

            # 4. A real upsert really embeds and writes.
            text = current.get('text') or payload.get('text') or ''
            outcome = adapter.upsert(memory_id, int(current['revision']), text,
                                      _metadata_from_row(current, payload))
            engine.acknowledge(event_id)
            report['acked'] += 1
            report['items'].append({'event_id': event_id, 'memory_id': memory_id,
                                    'result': 'upserted', 'revision': int(current['revision']),
                                    'chunks': outcome.get('chunks'),
                                    'reused_embedding': outcome.get('reused_embedding')})
        except Exception as exc:
            # A real failed attempt must be reported to the queue, not swallowed.
            state = engine.fail(event_id, exc)
            report['failed'] += 1
            if state.get('dead'):
                report['dead'] += 1
            report['items'].append({'event_id': event_id, 'memory_id': memory_id,
                                    'result': 'failed', 'error': str(exc)[:300],
                                    'attempts': state.get('attempts'), 'dead': state.get('dead')})
    return report


def sync_report_line(report):
    """One-line human summary of a sync report."""
    return ('processed=%d acked=%d skipped=%d failed=%d dead=%d'
            % (report['processed'], report['acked'], report['skipped'],
               report['failed'], report['dead']))
