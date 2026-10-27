"""Release regressions: temporary stores, deterministic providers, no network."""
import json
import unittest

from test_adapter import Base as AdapterBase
from test_chat_path import AlphaBase, FakeAdapter, FakeReranker, NOW, fact
from adapters.assoc import neighbors
from adapters.chat_path import ChatPath
from adapters.rag_adapter import RAGAdapter
from adapters.sync import sync_once


class ReleaseChatTests(AlphaBase):
    def test_permissions_are_rechecked_after_reranking(self):
        self.index(mid='m', facts=[fact()])
        parent = self
        class ChangesOwner(FakeReranker):
            def rerank(self, query, documents):
                parent.engine.db.execute(
                    "UPDATE memories SET visibility='private',owner='someone-else' WHERE id='m'")
                parent.engine.db.commit()
                return super().rerank(query, documents)
        self.adapter.reranker = ChangesOwner()
        report = self.ask(text='甜奶茶')
        self.assertEqual(report['injected'], [])

    def test_revision_changes_during_rerank_are_not_injected(self):
        self.index(mid='m', facts=[fact()])
        parent = self
        class ChangesRevision(FakeReranker):
            def rerank(self, query, documents):
                parent.engine.db.execute("UPDATE memories SET revision=revision+1 WHERE id='m'")
                parent.engine.db.commit()
                return super().rerank(query, documents)
        self.adapter.reranker = ChangesRevision()
        self.assertEqual(self.ask(text='甜奶茶')['injected'], [])

    def test_deleted_memory_cannot_enter_through_a_stale_adapter(self):
        self.add(mid='m')
        row = self.engine.row('m')
        self.engine.db.execute("UPDATE memories SET deleted_at=? WHERE id='m'", (NOW,))
        self.engine.db.commit()
        adapter = FakeAdapter([{'memory_id': 'm', 'relevance': .9, 'text': row['text']}])
        report = self.ask(path=self.path(adapter=adapter))
        self.assertEqual(report['injected'], [])

    def test_top_k_caps_primary_injection(self):
        for i in range(4):
            eid = 'e%d' % i
            self.index(mid='m%d' % i, eid=eid, facts=[fact(eid=eid)])
        report = self.ask(path=self.path(top_k=1))
        self.assertEqual(len(report['injected']), 1)

    def test_real_rerank_scores_affect_final_order(self):
        self.add(mid='a', eid='a', facts=[fact(eid='a')])
        self.add(mid='b', eid='b', facts=[fact(eid='b')])
        class SemanticAdapter(FakeAdapter):
            def rerank(self, query, candidates):
                return {'status': 'ok', 'candidates': [
                    dict(c, rerank_score=.1 if c['memory_id'] == 'a' else .9)
                    for c in candidates]}
        cands = [{'memory_id': 'a', 'relevance': .9, 'weight': 50},
                 {'memory_id': 'b', 'relevance': .5, 'weight': 50}]
        report = self.ask(path=self.path(adapter=SemanticAdapter(cands)))
        self.assertEqual([c['memory_id'] for c in report['injected']], ['b', 'a'])

    def test_zero_discount_rejects_neighbours_even_with_zero_floor(self):
        self.cat_world()
        report = self.ask(text='猫粮', path=self.path(
            assoc=True, assoc_discount=0, assoc_min_relevance=0))
        self.assertEqual(report['stats']['assoc_injected'], 0)

    def test_supplement_diagnostics_obey_shared_budget(self):
        class Offline:
            def search(self, *args, **kwargs):
                raise RuntimeError('offline')
        path = self.path(adapter=Offline(), style='recall', require_rerank=False)
        for limit in (0, 1, 20, 80):
            budget = path.new_turn_budget(limit)
            report = path.recall('生日', turn_budget=budget)
            self.assertLessEqual(report['tokens'], limit)
            self.assertEqual(budget.remaining + report['tokens'], limit)

    def test_already_loaded_memory_is_not_reported_as_missing(self):
        self.index(mid='m', facts=[fact()])
        path = self.path(style='recall')
        budget = path.new_turn_budget()
        path.inject(['甜奶茶'], requester='qq:10001', turn_budget=budget)
        again = path.recall('甜奶茶', requester='qq:10001', turn_budget=budget)
        self.assertNotIn('尚未展开', again['block'])
        self.assertIn('本轮已加载', again['block'])

    def test_private_seed_cannot_be_used_to_infer_neighbours(self):
        self.cat_world()
        self.engine.db.execute(
            "UPDATE memories SET visibility='private', owner='other' WHERE id='m_cat'")
        self.engine.db.commit()
        self.assertEqual(neighbors(self.engine, ['m_cat'], requester='qq:10001'), [])

    def test_strong_topic_edge_is_not_hidden_by_a_weak_conversation_edge(self):
        self.cat_world()
        self.engine.db.execute("UPDATE events SET at=? WHERE id='e_cat'", (NOW - 44 * 86400,))
        self.engine.db.commit()
        hit = neighbors(self.engine, ['m_food'], requester='qq:10001')[0]
        self.assertEqual(hit['via'], 'topic')
        self.assertEqual(hit['strength'], .6)

    def test_repeated_recall_keeps_the_report_schema(self):
        path = self.path(style='recall')
        budget = path.new_turn_budget()
        first = path.recall('birthday', turn_budget=budget)
        again = path.recall('birthday', turn_budget=budget)
        self.assertEqual(set(first), set(again))
        self.assertEqual(set(first['stats']), set(again['stats']))

    def test_mixed_primary_and_supplement_calls_never_overdraw(self):
        self.index(mid='m', facts=[fact()])
        path = self.path(style='recall')
        for limit in range(0, 161, 5):
            with self.subTest(limit=limit):
                budget = path.new_turn_budget(limit)
                first = path.inject(['甜奶茶'], requester='qq:10001', turn_budget=budget)
                second = path.recall('甜奶茶', requester='qq:10001', turn_budget=budget)
                self.assertEqual(limit - budget.remaining, first['tokens'] + second['tokens'])
                self.assertGreaterEqual(budget.remaining, 0)
                ids = [c['memory_id'] for r in (first, second) for c in r['injected']]
                self.assertEqual(len(ids), len(set(ids)))


class ReleaseAdapterTests(AdapterBase):
    def test_private_metadata_is_filtered_without_an_authority_engine(self):
        adapter = RAGAdapter(self.emb, self.store)
        adapter.upsert('m', 1, 'private text', {'visibility': 'private', 'owner': 'owner'})
        self.assertEqual(adapter.search('private text'), [])
        self.assertEqual(adapter.search('private text', {'requester': 'other'}), [])
        self.assertEqual(len(adapter.search('private text', {'requester': 'owner'})), 1)

    def test_nonfinite_query_vectors_are_rejected(self):
        self.store.upsert('m', 1, ['text'], [[1., 0.]])
        with self.assertRaises(ValueError):
            self.store.search([float('nan'), 0.])

    def test_failed_upsert_confirmation_keeps_the_outbox_pending(self):
        self.add()
        class Rejects:
            def upsert(self, *args, **kwargs):
                return {'ok': False, 'chunks': 0}
        report = sync_once(self.engine, Rejects())
        self.assertEqual(report['acked'], 0)
        self.assertEqual(report['failed'], 1)
        self.assertEqual(len(self.engine.outbox()), 1)

    def test_failed_delete_confirmation_keeps_the_outbox_pending(self):
        self.add()
        self.engine.db.execute("UPDATE memories SET deleted_at=? WHERE id='m1'", (NOW,))
        self.engine.db.commit()
        class Rejects:
            def delete(self, *args, **kwargs):
                return {'ok': False}
        report = sync_once(self.engine, Rejects())
        self.assertEqual(report['acked'], 0)
        self.assertEqual(report['failed'], 1)

    def test_empty_projection_removes_existing_chunks(self):
        self.adapter.upsert('m', 1, 'some text')
        self.adapter.upsert('m', 2, '')
        self.assertEqual(self.adapter.count('m'), 0)

    def test_incomplete_embedding_batch_cannot_replace_a_good_index(self):
        self.store.upsert('m', 1, ['old'], [[1., 0.]])
        with self.assertRaises(ValueError):
            self.store.upsert('m', 2, ['new', 'missing'], [[1., 0.]])
        self.assertEqual(self.store.chunks_of('m')[0]['text'], 'old')

    def test_vector_dimension_mismatch_is_explicit(self):
        self.store.upsert('m', 1, ['old'], [[1., 0.]])
        with self.assertRaises(ValueError):
            self.store.search([1., 0., 0.])

    def test_invalid_reranker_payload_reports_degraded(self):
        class Broken:
            backend = 'broken'
            def rerank(self, query, documents):
                return [{'index': 'not-an-index', 'score': float('nan')}]
        self.adapter.reranker = Broken()
        result = self.adapter.rerank('q', [{'text': 'text'}])
        self.assertEqual(result['status'], 'degraded')

    def test_permanent_profile_requires_trusted_management(self):
        with self.assertRaises(ValueError):
            self.add(profile='permanent', weight=50)

    def test_automatic_reinforcement_cannot_grant_permanent_protection(self):
        for kind in ('used', 'remind'):
            with self.subTest(kind=kind):
                mid = 'm-' + kind
                self.add(eid='add-' + kind, mid=mid, weight=89, content=kind)
                row = self.engine.row(mid)
                action = {'kind': kind, 'id': mid, 'revision': row['revision']}
                event = {'id': kind, 'kind': 'delivery' if kind == 'used' else 'human_message',
                         'peer_id': kind, 'sender_id': 'qq:10001', 'requester_id': 'qq:10001',
                         'content': kind + ' again', 'delivered': True, 'actions': [action]}
                self.engine.process_event(event, NOW)
                self.assertLess(self.engine.row(mid)['weight'], self.cfg['protected_weight'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
