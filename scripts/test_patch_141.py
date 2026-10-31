"""1.4.1 review regressions. Synthetic data, deterministic interleaving, no API."""
import json
import sqlite3
import unittest
from pathlib import Path

from test_adapter import Base, NOW
from test_memory_engine import event
from memory_engine import MemoryEngine
from adapters.embedding import HashingEmbedding
from adapters.rag_adapter import RAGAdapter
from adapters.chat_path import COMPLETE_NOTICE
from test_chat_path import Base as ChatBase, FakeAdapter, FakeReranker
from adapters.embedding import SiliconFlowEmbedding
from adapters.sync import sync_once


class PurgeTests(Base):
    def prepare(self):
        e = event(weight=10)
        e['actions'][0]['emotion'] = {'valence': -.8, 'arousal': .9,
                                    'confidence': 1, 'sensitive': True}
        self.engine.process_event(e, NOW)
        for n in range(3):
            self.engine.process_event({
                'id': 'appraise-%d' % n, 'kind': 'human_message', 'peer_id': 'peer',
                'sender_id': 'qq:10001', 'content': 'interpretation %d' % n,
                'actions': [{'kind': 'appraise', 'id': 'm1', 'text': 'SECRET-%d' % n}]},
                NOW + n + 1)
        with self.engine.db:
            self.engine.db.execute('UPDATE memories SET deleted_at=? WHERE id=?', (NOW, 'm1'))
            self.engine.db.execute('UPDATE outbox SET ack=1')

    def reminder(self, engine, eid='restore'):
        return engine.process_event({'id': eid, 'kind': 'human_message', 'peer_id': 'peer',
            'sender_id': 'qq:10001', 'content': eid,
            'actions': [{'kind': 'remind', 'id': 'm1'}]}, NOW + 8 * 86400)

    def test_purge_scrubs_all_emotion_history_and_free_text_audit(self):
        self.prepare()
        before = self.engine.row('m1')
        self.assertEqual(len(json.loads(before['emotion'])['appraisal']['history']), 2)
        with self.engine.db:
            self.engine._audit('m1', 'trusted', NOW, reason='SECRET-audit')
        self.assertEqual(self.engine.purge(NOW + 8 * 86400), 1)
        row = self.engine.row('m1')
        self.assertEqual(row['emotion'], '')
        self.assertEqual(row['revision'], before['revision'] + 1)
        self.assertIsNone(self.engine.emotion_view('m1'))
        self.assertFalse(self.engine.injection_allowed('m1', user_initiated=True))
        self.assertEqual(self.engine.outbox(), [])  # prior delete was acknowledged
        for field in ('body', 'text'):
            self.assertEqual(row[field], '')
        for field in ('facts', 'source_ids'):
            self.assertEqual(row[field], '[]')
        audit = self.engine.db.execute('SELECT action,at,detail FROM audit WHERE memory_id=?', ('m1',)).fetchall()
        self.assertTrue(audit)
        self.assertNotIn('SECRET', str([tuple(r) for r in audit]))
        self.assertEqual(self.engine.purge(NOW + 9 * 86400), 0)
        with self.assertRaises(ValueError):
            self.reminder(self.engine)

    def test_purge_selection_blocks_concurrent_reminder_without_sleep(self):
        self.prepare()
        other = MemoryEngine(Path(self.tmp.name) / 'mem.db', self.cfg)
        other.db.execute('PRAGMA busy_timeout=0')
        original = self.engine.db
        outcomes = []
        parent = self

        class Cursor:
            def __init__(self, cursor):
                self.cursor = cursor
            def fetchall(self):
                rows = self.cursor.fetchall()  # exact old select/update gap
                try:
                    parent.reminder(other)
                    outcomes.append('reminder_committed')
                except sqlite3.OperationalError as exc:
                    if 'locked' not in str(exc):
                        raise
                    outcomes.append('writer_locked')
                return rows

        class Connection:
            def __getattr__(self, name):
                return getattr(original, name)
            def __enter__(self):
                original.__enter__()
                return self
            def __exit__(self, *args):
                return original.__exit__(*args)
            def execute(self, sql, args=()):
                cursor = original.execute(sql, args)
                if sql.lstrip().startswith('SELECT id FROM memories WHERE purged=0'):
                    return Cursor(cursor)
                return cursor

        self.engine.db = Connection()
        try:
            self.engine.purge(NOW + 8 * 86400)
        finally:
            self.engine.db = original
            other.close()
        self.assertEqual(outcomes, ['writer_locked'],
                         'old purge destroys the concurrently restored live memory')
        self.assertEqual(self.engine.row('m1')['purged'], 1)
        with self.assertRaises(ValueError):
            self.reminder(self.engine, 'after-purge')

    def test_reminder_committed_before_purge_preserves_history_and_sync(self):
        self.prepare()
        other = MemoryEngine(Path(self.tmp.name) / 'mem.db', self.cfg)
        try:
            self.reminder(other)
        finally:
            other.close()
        before = self.engine.row('m1')
        self.assertEqual(self.engine.purge(NOW + 8 * 86400), 0)
        self.assertEqual(self.engine.row('m1'), before)
        self.assertIsNotNone(self.engine.emotion_view('m1'))
        self.assertEqual(self.engine.outbox()[-1]['operation'], 'upsert')

    def test_purge_failure_rolls_back_content_revision_and_audit(self):
        self.prepare()
        before = self.engine.row('m1')
        original = self.engine._audit
        def fails(*args, **kwargs):
            raise RuntimeError('synthetic failure')
        self.engine._audit = fails
        try:
            with self.assertRaises(RuntimeError):
                self.engine.purge(NOW + 8 * 86400)
        finally:
            self.engine._audit = original
        self.assertEqual(self.engine.row('m1'), before)
        self.assertFalse(self.engine.db.in_transaction)
        self.assertEqual(self.engine.purge(NOW + 8 * 86400), 1)

    def test_legacy_purged_emotion_is_inaccessible_and_scrubbed(self):
        self.prepare()
        with self.engine.db:
            self.engine.db.execute("UPDATE memories SET purged=1,body='',facts='[]',text='',source_ids='[]'")
        self.assertIsNone(self.engine.emotion_view('m1'))
        self.assertEqual(self.engine.purge(NOW + 8 * 86400), 1)
        self.assertEqual(self.engine.row('m1')['emotion'], '')

    def test_unacknowledged_or_dead_delete_still_blocks_purge(self):
        self.prepare()
        for dead in (0, 1):
            with self.engine.db:
                self.engine.db.execute('UPDATE outbox SET ack=0,dead=?', (dead,))
            before = self.engine.row('m1')
            self.assertEqual(self.engine.purge(NOW + 8 * 86400), 0)
            self.assertEqual(self.engine.row('m1'), before)

    def test_live_emotion_history_survives_decay_compression_and_purge_scan(self):
        self.prepare()
        self.reminder(self.engine)
        self.cfg['delete_probability_per_day'] = 0
        before = self.engine.emotion_view('m1', NOW + 8 * 86400)
        far = NOW + 4000 * 86400
        self.engine.apply_plan(self.engine.plan(far))
        self.assertEqual(self.engine.purge(far), 0)
        after = self.engine.emotion_view('m1', far)
        self.assertEqual(after['snapshot'], before['snapshot'])
        self.assertEqual(after['appraisal'], before['appraisal'])
        self.assertAlmostEqual(after['charge'], 0, delta=1e-9)
        self.assertTrue(self.engine.injection_allowed('m1', far, user_initiated=True))
        self.assertNotEqual(self.engine.row('m1')['tier'], 'full')

    def test_context_paging_preserves_emotion_and_relationship_ledger(self):
        from adapters.context_pager import ContextPager, HistoryTurn
        self.prepare()
        # Recycle-window content stays intact even while public emotion view
        # is closed. Paging is disposable context, never a storage deletion.
        before = self.engine.row('m1')
        self.assertTrue(before['emotion'])
        ledger = 'relationship: familiar; open_loop: follow up; veto: do not raise proactively'
        history = [HistoryTurn(str(n), 'user', 'synthetic turn %d' % n,
                               'owner', 'group:1', True) for n in range(40)]
        pager = ContextPager(lambda row, who: True)
        for options in ({}, {'large_context': True, 'large_reason': 'review'}, {}):
            frame = pager.prepare(system='system', ledger=ledger, current='current',
                                  history=history, requester='owner', scope='group:1', **options)
            self.assertIn(ledger, frame.render())
            self.assertEqual(self.engine.row('m1'), before)


class IdentityTests(Base):
    def test_changed_model_same_dimension_reembeds_identical_text(self):
        self.adapter.upsert('m', 1, 'same text')
        new = HashingEmbedding(dim=64)
        new.model = 'different-model'
        result = RAGAdapter(new, self.store).upsert('m', 2, 'same text')
        self.assertFalse(result['reused_embedding'])
        self.assertEqual(new.calls, 1)

    def test_changed_dimension_reembeds_identical_text(self):
        self.adapter.upsert('m', 1, 'same text')
        new = HashingEmbedding(dim=32)
        RAGAdapter(new, self.store).upsert('m', 2, 'same text')
        self.assertEqual(len(self.store.chunks_of('m')[0]['vector']), 32)

    def test_legacy_unidentified_vectors_are_not_reused(self):
        from adapters.rag_adapter import projection_hash
        self.store.upsert('m', 1, ['same text'], [self.emb.embed(['same text'])[0]],
                          projection_hash=projection_hash('same text'))
        self.assertFalse(self.adapter.upsert('m', 2, 'same text')['reused_embedding'])

    def test_provider_and_version_changes_invalidate_same_instance_cache(self):
        for field in ('backend', 'model_version'):
            with self.subTest(field=field):
                self.adapter.upsert('m', 1, 'same text')
                setattr(self.emb, field, 'changed-' + field)
                self.assertFalse(self.adapter.upsert('m', 2, 'same text')['reused_embedding'])

    def test_same_identity_weight_only_revision_reuses_after_restart(self):
        self.adapter.upsert('m', 1, 'same text', {'weight': 1})
        embedding = HashingEmbedding(dim=64)
        adapter = RAGAdapter(embedding, self.store)
        report = adapter.upsert('m', 2, 'same text', {'weight': 50})
        self.assertTrue(report['reused_embedding'])
        self.assertEqual(embedding.calls, 0)
        self.assertEqual(self.store.chunks_of('m')[0]['metadata']['weight'], 50)

    def test_search_rejects_legacy_and_mixed_model_indexes_until_rebuilt(self):
        self.adapter.upsert('m', 1, 'apple')
        embedding = HashingEmbedding(dim=64)
        embedding.model_version = 'new'
        adapter = RAGAdapter(embedding, self.store)
        adapter.upsert('n', 1, 'apple pear')
        with self.assertRaisesRegex(ValueError, 'rebuild'):
            adapter.search('apple')
        adapter.upsert('m', 1, 'apple')
        self.assertEqual(len(adapter.search('apple')), 2)
        self.store.upsert('legacy', 1, ['apple'], [embedding.embed(['apple'])[0]])
        with self.assertRaisesRegex(ValueError, 'legacy index'):
            adapter.search('apple')

    def test_identity_does_not_store_key_url_credentials_or_query(self):
        a = SiliconFlowEmbedding(api_key='DUMMY-KEY-A',
            base_url='https://user:password@example.invalid/v1?secret=DUMMY-QUERY#fragment')
        b = SiliconFlowEmbedding(api_key='DUMMY-KEY-B', base_url='https://example.invalid/v1')
        self.assertEqual(a.cache_identity(), b.cache_identity())
        identity = json.dumps(a.cache_identity())
        for secret in ('DUMMY', 'password', 'user', 'secret', 'fragment'):
            self.assertNotIn(secret, identity)

    def test_explicit_custom_identity_reuses_and_opaque_backend_fails_closed_on_restart(self):
        class Custom:
            def embed(self, texts):
                return [[1., 0.] for _ in texts]
        identity = {'provider': 'custom-local', 'model': 'toy', 'version': 'v1', 'dim': 2}
        first = RAGAdapter(Custom(), self.store, embedding_identity=identity)
        first.upsert('m', 1, 'apple')
        second = RAGAdapter(Custom(), self.store, embedding_identity=identity)
        self.assertTrue(second.upsert('m', 2, 'apple')['reused_embedding'])
        opaque = RAGAdapter(Custom(), self.store)
        self.assertFalse(opaque.upsert('m', 3, 'apple')['reused_embedding'])
        self.assertEqual(len(opaque.search('apple')), 1)
        with self.assertRaisesRegex(ValueError, 'rebuild'):
            RAGAdapter(Custom(), self.store).search('apple')

    def test_corrupt_vector_dimension_is_not_reused(self):
        self.adapter.upsert('m', 1, 'same text')
        from array import array
        with self.store.db:
            self.store.db.execute('UPDATE chunks SET vector=?,dim=2', (array('f', [1., 0.]).tobytes(),))
        self.assertFalse(self.adapter.upsert('m', 2, 'same text')['reused_embedding'])
        self.assertEqual(len(self.store.chunks_of('m')[0]['vector']), 64)


class ResidueTests(Base):
    def add_feeling(self, mid='feeling', visibility='shared', cues=None):
        e = event(eid='source-' + mid, mid=mid, weight=1, visibility=visibility)
        e['actions'][0]['body'] = 'SECRET event details'
        e['actions'][0]['emotion'] = {'valence': -.8, 'arousal': .9, 'confidence': 1,
            'appraisal': 'SECRET personal interpretation', 'cues': ['rain'] if cues is None else cues}
        self.engine.process_event(e, NOW)
        return e

    def forget_naturally(self, mid='feeling', visibility='shared', cues=None):
        self.add_feeling(mid, visibility, cues)
        self.cfg['delete_probability_per_day'] = 1
        self.engine.apply_plan(self.engine.plan(NOW + 86400))
        self.assertEqual(self.engine.row(mid)['deletion_kind'], 'natural')
        self.assertEqual(sync_once(self.engine, self.adapter)['failed'], 0)
        self.engine.purge(NOW + 9 * 86400)

    def test_natural_forgetting_preserves_only_abstract_cue_and_numbers(self):
        self.forget_naturally()
        result = self.engine.emotion_resonance(['rain'], now=NOW + 9 * 86400)
        self.assertEqual(result['feelings'][0]['cue'], 'rain')
        self.assertLess(result['feelings'][0]['valence'], 0)
        self.assertGreater(result['feelings'][0]['charge'], 0)
        self.assertNotIn('memory_id', json.dumps(result))
        self.assertNotIn('feeling', json.dumps(result['feelings'][0]))
        row = self.engine.row('feeling')
        self.assertEqual(row['emotion'], '')
        self.assertEqual(row['emotion_cues'], '[]')
        self.assertIsNone(self.engine.emotion_view('feeling'))
        residue = self.engine.db.execute('SELECT * FROM emotion_residue').fetchone()
        self.assertEqual(set(residue.keys()), {'memory_id', 'cue', 'valence', 'charge', 'updated_at',
                                             'half_life_hours', 'visibility', 'owner'})
        self.assertNotIn('SECRET', str(dict(residue)))
        self.assertNotIn('source-', str(dict(residue)))
        self.assertEqual(self.adapter.search('rain'), [])

    def test_explicit_erase_clears_only_target_residue_and_never_restores(self):
        self.forget_naturally('one')
        self.forget_naturally('two')
        before = self.engine.row('two')
        result = self.engine.erase('one', 'qq:10001', NOW + 10 * 86400)
        self.assertTrue(result['delete_pending'])
        self.assertEqual(self.engine.db.execute('SELECT memory_id FROM emotion_residue').fetchone()[0], 'two')
        self.assertEqual(self.engine.row('two'), before)
        self.assertEqual(self.engine.row('one')['deletion_kind'], 'explicit')
        with self.assertRaises(ValueError):
            self.engine.process_event({'id': 'again', 'kind': 'human_message', 'peer_id': 'p',
                'sender_id': 'qq:10001', 'content': 'restore',
                'actions': [{'kind': 'remind', 'id': 'one'}]}, NOW + 11 * 86400)

    def test_explicit_erase_scrubs_pending_payloads_and_waits_for_index_ack(self):
        self.add_feeling()
        sync_once(self.engine, self.adapter)
        self.engine.set_weight_trusted('feeling', 20, 'SECRET audit reason', NOW + 1)
        pending = self.engine.outbox()[0]
        self.engine.fail(pending['id'], 'SECRET provider error')
        result = self.engine.erase('feeling', 'qq:10001', NOW + 2)
        self.assertTrue(result['delete_pending'])
        self.assertGreater(self.adapter.count('feeling'), 0)
        self.assertEqual(self.adapter.search('SECRET'), [])
        self.assertNotIn('SECRET', json.dumps(self.engine.outbox()))
        self.assertNotIn('SECRET', str([tuple(r) for r in self.engine.db.execute('SELECT * FROM audit')]))
        self.assertEqual(sync_once(self.engine, self.adapter)['failed'], 0)
        self.assertEqual(self.adapter.count('feeling'), 0)
        self.assertFalse(self.engine.erase('feeling', 'qq:10001')['delete_pending'])

    def test_explicit_erase_is_idempotent_and_owner_only_even_for_shared(self):
        self.add_feeling()
        for requester in ('', 'someone-else'):
            with self.assertRaises(ValueError):
                self.engine.erase('feeling', requester)
        first = self.engine.erase('feeling', 'qq:10001', NOW + 1)
        jobs = self.engine.outbox()
        again = self.engine.erase('feeling', 'qq:10001', NOW + 2)
        self.assertEqual(first, again)
        self.assertEqual(jobs, self.engine.outbox())

    def test_ownerless_explicit_erase_fails_closed(self):
        self.add_feeling()
        with self.engine.db:
            self.engine.db.execute("UPDATE memories SET owner='' WHERE id='feeling'")
        with self.assertRaises(ValueError):
            self.engine.erase('feeling', 'admin')
        self.assertFalse(self.engine.row('feeling')['purged'])

    def test_residue_private_acl_and_current_authority_both_apply(self):
        self.forget_naturally(visibility='private')
        now = NOW + 9 * 86400
        for requester in ('', 'other'):
            self.assertEqual(self.engine.emotion_resonance(['rain'], requester, now)['feelings'], [])
        self.assertTrue(self.engine.emotion_resonance(['rain'], 'qq:10001', now)['feelings'])
        with self.engine.db:
            self.engine.db.execute("UPDATE memories SET owner='other' WHERE id='feeling'")
        self.assertEqual(self.engine.emotion_resonance(['rain'], 'qq:10001', now)['feelings'], [])
        self.assertEqual(self.engine.emotion_resonance(['rain'], 'other', now)['feelings'], [])

    def test_residue_decay_is_restart_and_query_frequency_independent(self):
        self.forget_naturally()
        at = NOW + 9 * 86400
        row = self.engine.db.execute('SELECT * FROM emotion_residue').fetchone()
        later = at + row['half_life_hours'] * 3600
        initial = self.engine.emotion_resonance(['rain'], now=at)['feelings'][0]['charge']
        self.engine.emotion_resonance(['rain'], now=(at + later) / 2)
        expected = self.engine.emotion_resonance(['rain'], now=later)['feelings'][0]['charge']
        self.assertAlmostEqual(expected, initial / 2)
        other = MemoryEngine(Path(self.tmp.name) / 'mem.db', self.cfg)
        try:
            self.assertEqual(other.emotion_resonance(['rain'], now=later),
                             self.engine.emotion_resonance(['rain'], now=later))
        finally:
            other.close()

    def test_unknown_deletion_source_drops_residue_and_records_policy(self):
        self.add_feeling()
        with self.engine.db:
            self.engine.db.execute('UPDATE memories SET deleted_at=?', (NOW,))
            self.engine.db.execute('UPDATE outbox SET ack=1')
        self.engine.purge(NOW + 8 * 86400)
        self.assertEqual(self.engine.emotion_resonance(['rain'])['feelings'], [])
        actions = [r[0] for r in self.engine.db.execute('SELECT action FROM audit')]
        self.assertIn('residue_policy_unknown_or_already_purged', actions)

    def test_no_cue_is_inferred_from_old_event_text(self):
        self.forget_naturally(cues=[])
        self.assertEqual(self.engine.emotion_resonance(['rain'])['feelings'], [])

    def test_cues_reject_arbitrary_text_and_match_only_requested_scene(self):
        with self.assertRaises(ValueError):
            self.add_feeling(cues=['SECRET person at specific address'])
        self.assertIsNone(self.engine.row('feeling'))
        self.forget_naturally()
        self.assertEqual(self.engine.emotion_resonance(['music'])['feelings'], [])
        with self.assertRaises(ValueError):
            self.engine.emotion_resonance(['SECRET'])

    def test_erase_failure_rolls_back_residue_source_and_outbox(self):
        self.forget_naturally()
        before = self.engine.row('feeling')
        original = self.engine._queue
        def fails(*args):
            raise RuntimeError('synthetic failure')
        self.engine._queue = fails
        try:
            with self.assertRaises(RuntimeError):
                self.engine.erase('feeling', 'qq:10001')
        finally:
            self.engine._queue = original
        self.assertEqual(self.engine.row('feeling'), before)
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM emotion_residue').fetchone()[0], 1)

    def test_old_schema_upgrade_is_additive_and_defaults_unknown(self):
        self.add_feeling()
        before = self.engine.row('feeling')
        self.engine.close()
        db = sqlite3.connect(Path(self.tmp.name) / 'mem.db')
        db.execute('DROP TABLE emotion_residue')
        db.execute('ALTER TABLE memories DROP COLUMN emotion_cues')
        db.execute('ALTER TABLE memories DROP COLUMN deletion_kind')
        db.commit()
        db.close()
        self.engine = MemoryEngine(Path(self.tmp.name) / 'mem.db', self.cfg)
        self.assertEqual(self.engine.row('feeling')['emotion'], before['emotion'])
        self.assertEqual(self.engine.row('feeling')['deletion_kind'], 'unknown')
        self.assertEqual(self.engine.row('feeling')['emotion_cues'], '[]')


class RecallTests(Base):
    def test_long_memory_does_not_crowd_out_distinct_matches(self):
        adapter = RAGAdapter(self.emb, self.store, chunk_chars=6)
        adapter.upsert('long', 1, 'apple\n' * 80)
        adapter.upsert('short', 1, 'apple pear')
        self.assertEqual({c['memory_id'] for c in adapter.search('apple', limit=2)}, {'long', 'short'})

    def test_stale_candidates_are_replaced_by_live_candidates(self):
        for n in range(12):
            self.adapter.upsert('stale-%d' % n, 1, 'apple')
        self.add(mid='live')
        self.adapter.upsert('live', 1, 'apple pear')
        self.assertEqual([c['memory_id'] for c in self.adapter.search('apple', limit=1)], ['live'])

    def test_complete_notice_does_not_claim_whole_library_recall(self):
        self.assertNotIn('无遗漏', COMPLETE_NOTICE)
        self.assertIn('本轮', COMPLETE_NOTICE)

    def test_forbidden_candidates_are_replaced_before_rerank(self):
        for n in range(10):
            mid = 'private-%d' % n
            self.add(mid=mid, eid=mid, content=mid, visibility='private', sender='other')
            # Stale coarse metadata says shared: authoritative ACL must win.
            self.adapter.upsert(mid, 1, 'apple', {'visibility': 'shared'})
        self.add(mid='live', eid='live', content='live')
        self.adapter.upsert('live', 1, 'apple pear')
        self.assertEqual([c['memory_id'] for c in self.adapter.search('apple', {'requester': 'owner'}, 1)], ['live'])


class ChatRefillTests(ChatBase):
    def candidates(self, count=12):
        self.add(mid='live', eid='live')
        live = self.engine.row('live')
        raw = [{'memory_id': 'gone-%d' % n, 'revision': 1,
                'text': 'UNAUTHORIZED', 'relevance': 1.0} for n in range(count)]
        raw.append({'memory_id': 'live', 'revision': live['revision'],
                    'text': live['text'], 'relevance': .8})
        return raw

    def test_chat_refills_legacy_adapter_and_checks_acl_before_and_after_rerank(self):
        raw = self.candidates()
        parent = self
        class Paged(FakeAdapter):
            def search(self, query, acl_filter=None, limit=10):
                return self.candidates[:limit]
            def rerank(self, query, candidates):
                parent.assertEqual([c['memory_id'] for c in candidates], ['live'])
                with parent.engine.db:
                    parent.engine.db.execute("UPDATE memories SET visibility='private',owner='other' WHERE id='live'")
                return super().rerank(query, candidates)
        result = self.ask(path=self.path(adapter=Paged(raw), top_k=1))
        self.assertEqual(result['injected'], [])
        self.assertNotIn('UNAUTHORIZED', result['block'])

    def test_chat_refills_after_already_loaded_and_sensitive_candidates(self):
        raw = []
        for n in range(8):
            mid = 'sensitive-%d' % n
            self.add(mid=mid, eid=mid)
            row = self.engine.row(mid)
            emotion = json.loads(row['emotion'])
            emotion['sensitive'] = True
            with self.engine.db:
                self.engine.db.execute('UPDATE memories SET emotion=? WHERE id=?', (json.dumps(emotion), mid))
            raw.append({'memory_id': mid, 'revision': 1, 'text': row['text'], 'relevance': .9})
        raw.extend(self.candidates(0))
        class Paged(FakeAdapter):
            def search(self, query, acl_filter=None, limit=10):
                return self.candidates[:limit]
        path = self.path(adapter=Paged(raw), top_k=1)
        result = self.ask(path=path)
        self.assertEqual([c['memory_id'] for c in result['injected']], ['live'])
        budget = path.new_turn_budget()
        budget.injected_ids.add('live')
        again = self.ask(path=path, turn_budget=budget)
        self.assertEqual(again['injected'], [])

    def test_nonprogressing_adapter_terminates_and_budget_still_holds(self):
        class IgnoresLimit(FakeAdapter):
            calls = 0
            def search(self, *args, **kwargs):
                self.calls += 1
                return self.candidates
        adapter = IgnoresLimit(self.candidates()[:-1])
        result = self.ask(path=self.path(adapter=adapter, top_k=1, token_budget=20))
        self.assertLessEqual(adapter.calls, 2)
        self.assertEqual(result['injected'], [])
        self.assertLessEqual(result['tokens'], 20)

    def test_long_memory_window_keeps_other_memory_and_budget(self):
        for mid in ('long', 'short'):
            self.add(mid=mid, eid=mid)
        self.adapter.chunk_chars = 6
        self.adapter.upsert('long', 1, 'apple\n' * 100)
        self.adapter.upsert('short', 1, 'apple pear')
        path = self.path(top_k=2, token_budget=1200)
        result = self.ask(text='apple', path=path)
        self.assertEqual({c['memory_id'] for c in result['injected']}, {'long', 'short'})
        self.assertLessEqual(result['tokens'], 1200)


if __name__ == '__main__':
    unittest.main(verbosity=2)
