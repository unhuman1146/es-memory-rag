"""Chat path tests: offline, no network, no API key, no shared state.

The embedding backend is a deterministic hashing stub, the vector store is a
temporary SQLite file and the memory store is a fresh temporary database per
test. The chat path is exercised against the real ``MemoryEngine`` and the
real ``RAGAdapter`` so the permission join, the revision check and the
chunk dedupe are genuinely covered -- not mocked away.

``scripts/demo_live_injection.py`` covers what cannot be covered offline: a
real embedding model, a real reranker and a paraphrase query.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

from context_budget import ContextBudget
from memory_engine import MemoryEngine, load_config

from adapters.chat_path import BLOCK_HEADER, ChatPath, estimate_tokens
from adapters.embedding import HashingEmbedding
from adapters.rag_adapter import RAGAdapter
from adapters.rerank import LexicalReranker
from adapters.sync import sync_once
from adapters.vector_store import SqliteVectorStore

NOW = 1_700_000_000.0


def fact(subject='qq:10001', predicate='喜欢', obj='甜奶茶', negated=False,
         time='2026-10-11', eid='evt-20261011-01'):
    return {'subject': subject, 'predicate': predicate, 'object': obj,
            'negated': negated, 'time': time, 'source_ids': [eid], 'core': True}


def event(eid='evt-20261011-01', mid='mem_milk_tea', weight=50, visibility='shared',
          sender='qq:10001', facts=None, content='new fact', profile='standard',
          emotion=None):
    action = {'kind': 'add', 'id': mid, 'weight': weight, 'profile': profile,
              'body': '原始详细叙述。' * 40,
              'facts': facts if facts is not None else [fact(eid=eid)]}
    if emotion is not None:
        action['emotion'] = emotion
    return {'id': eid, 'kind': 'human_message',
            'peer_id': 'agent:private:%s:sender' % sender.replace(':', ''),
            'sender_id': sender, 'visibility': visibility, 'content': content,
            'actions': [action]}


class FakeReranker:
    """A reranker that claims to be a learned one (backend is not 'lexical')."""

    backend = 'fake'

    def __init__(self, order=None):
        self.order = order
        self.calls = 0

    def rerank(self, query, documents):
        self.calls += 1
        idx = list(range(len(documents)))
        if self.order:
            idx = self.order
        return [{'index': i, 'score': 1.0 - 0.1 * n} for n, i in enumerate(idx)]


class FakeAdapter:
    """A hand-driven adapter: exact relevance and weight, no index at all."""

    def __init__(self, candidates=None, status='ok', reason='', order=None):
        self.candidates = list(candidates or [])
        self.status = status
        self.reason = reason
        self.order = order
        self.searched = None

    def search(self, query, acl_filter=None, limit=10):
        self.searched = (query, acl_filter, limit)
        return list(self.candidates)

    def rerank(self, query, candidates):
        candidates = list(candidates or [])
        if self.status != 'ok':
            return {'status': self.status, 'candidates': candidates, 'reason': self.reason}
        idx = list(range(len(candidates)))
        if self.order:
            idx = self.order
        ordered = [candidates[i] for i in idx if 0 <= i < len(candidates)]
        return {'status': 'ok', 'candidates': ordered}


def projection(facts):
    return json.dumps({'facts': facts}, ensure_ascii=False)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.environ.get('USERPROFILE')
        os.environ['USERPROFILE'] = self.tmp.name
        os.environ['HOME'] = self.tmp.name
        self.cfg = load_config()
        self.engine = MemoryEngine(Path(self.tmp.name) / 'mem.db', self.cfg)
        self.store = SqliteVectorStore(Path(self.tmp.name) / 'index.db')
        self.emb = HashingEmbedding(dim=64)
        self.reranker = FakeReranker()
        self.adapter = RAGAdapter(self.emb, self.store, engine=self.engine,
                                  reranker=self.reranker)

    def tearDown(self):
        self.store.close()
        self.engine.close()
        self.tmp.cleanup()
        if self.home is not None:
            os.environ['USERPROFILE'] = self.home
            os.environ['HOME'] = self.home
        else:
            os.environ.pop('USERPROFILE', None)
            os.environ.pop('HOME', None)

    def add(self, **kwargs):
        # The engine drops a message that repeats the same peer and content
        # inside the dedup window, so every memory needs its own text.
        kwargs.setdefault('content', 'fact %s' % kwargs.get('mid', 'm'))
        e = event(**kwargs)
        self.engine.process_event(e, NOW)
        return e

    def index(self, **kwargs):
        """Add a memory and push its projection into the RAG index."""
        e = self.add(**kwargs)
        sync_once(self.engine, self.adapter)
        return e

    def path(self, engine=None, adapter=None, **kwargs):
        return ChatPath(self.engine if engine is None else engine,
                        self.adapter if adapter is None else adapter, **kwargs)

    def ask(self, text='我喜欢什么奶茶', path=None, **kwargs):
        path = path or self.path()
        return path.inject([{'role': 'user', 'content': text}],
                           requester=kwargs.pop('requester', 'qq:10001'), **kwargs)


# ------------------------------------------------------------------- query


class QueryTests(Base):
    def test_query_carries_topic_and_messages(self):
        path = self.path()
        query = path.build_query([{'role': 'user', 'content': '我想喝奶茶'}],
                                  topic='饮料', speaker='qq:10001')
        self.assertIn('饮料', query)
        self.assertIn('我想喝奶茶', query)

    def test_query_keeps_the_newest_message_when_truncated(self):
        path = self.path()
        messages = [{'role': 'user', 'content': '旧消息' * 60},
                    {'role': 'user', 'content': '新消息' * 60}]
        query = path.build_query(messages, max_chars=80)
        self.assertLessEqual(len(query), 80)
        self.assertIn('新消息', query)

    def test_empty_query_yields_an_empty_block_and_a_note(self):
        report = self.path().inject([], requester='qq:10001')
        self.assertEqual(report['block'], '')
        self.assertEqual(report['stats']['candidates'], 0)
        self.assertTrue(report['note'])

    def test_speaker_becomes_the_requester_when_none_is_given(self):
        adapter = FakeAdapter()
        report = ChatPath(self.engine, adapter, require_rerank=False).inject(
            [{'role': 'user', 'content': 'x'}], speaker='qq:10001')
        self.assertEqual(report['requester'], 'qq:10001')

    def test_the_stable_id_is_not_in_the_query_by_default(self):
        path = self.path()
        query = path.build_query([{'role': 'user', 'content': '我平时在哪个城市生活'}],
                                  speaker='qq:10001')
        self.assertNotIn('qq:10001', query)
        self.assertIn('我平时在哪个城市生活', query)

    def test_include_speaker_puts_the_stable_id_back(self):
        path = self.path(include_speaker=True)
        query = path.build_query([{'role': 'user', 'content': '我平时在哪个城市生活'}],
                                  speaker='qq:10001')
        self.assertIn('qq:10001', query)

    def test_the_topic_stays_in_the_query(self):
        path = self.path()
        query = path.build_query([{'role': 'user', 'content': '我平时在哪个城市生活'}],
                                  topic='常住城市', speaker='qq:10001')
        self.assertIn('常住城市', query)


# ------------------------------------------------------------------ block


class BlockTests(Base):
    def test_block_carries_the_fact_atoms(self):
        self.index(facts=[fact()])
        report = self.ask()
        self.assertIn('甜奶茶', report['block'])
        self.assertIn('喜欢', report['block'])

    def test_block_is_marked_as_reference_material_not_instructions(self):
        self.index(facts=[fact()])
        report = self.ask()
        self.assertTrue(report['block'].startswith(BLOCK_HEADER))

    def test_block_shows_the_source_and_the_date(self):
        self.index(facts=[fact()])
        report = self.ask()
        self.assertIn('evt-20261011-01', report['block'])
        self.assertIn('2026-10-11', report['block'])

    def test_unknown_time_is_marked_as_unknown(self):
        self.index(facts=[fact(time=None)])
        report = self.ask()
        self.assertIn('时间未知', report['block'])

    def test_negation_is_kept_visible_for_a_chinese_predicate(self):
        self.index(facts=[fact(predicate='喜欢', negated=True)])
        report = self.ask()
        self.assertIn('不喜欢', report['block'])

    def test_negation_is_kept_visible_for_a_latin_predicate(self):
        self.index(facts=[fact(predicate='likes', obj='sweet milk tea', negated=True)])
        report = self.ask()
        self.assertIn('NOT likes', report['block'])

    def test_a_predicate_that_already_negates_is_not_negated_twice(self):
        self.index(facts=[fact(predicate='不喝', obj='甜奶茶', negated=True)])
        report = self.ask()
        self.assertIn('不喝', report['block'])
        self.assertNotIn('不不喝', report['block'])

    def test_a_complete_result_does_not_count_zero_missing_memories(self):
        self.index(facts=[fact()])
        report = self.ask()
        self.assertNotIn('另有 0 条', report['missing_notice'])
        self.assertIn('无遗漏', report['missing_notice'])

    def test_internal_record_id_never_reaches_the_chat_output(self):
        self.index(mid='mem_internal_id_marker')
        report = self.ask()
        self.assertNotIn('mem_internal_id_marker', report['block'])

    def test_block_declares_what_was_left_out(self):
        self.index(mid='mem_a', facts=[fact(obj='甜奶茶')])
        self.index(mid='mem_b', facts=[fact(obj='乌龙茶', eid='evt-20261011-02')])
        path = self.path(token_budget=40)
        report = path.inject([{'role': 'user', 'content': '奶茶 乌龙茶'}],
                              requester='qq:10001')
        self.assertIn('未加载', report['missing_notice'])
        self.assertGreater(report['stats']['dropped_by_budget'], 0)

    def test_a_memory_indexed_as_several_chunks_is_injected_once(self):
        facts = [fact(obj='事实%02d' % i) for i in range(12)]
        self.index(facts=facts)
        self.assertGreater(self.adapter.count('mem_milk_tea'), 1)
        report = self.ask(text='事实')
        self.assertEqual(report['stats']['unique'], 1)
        self.assertEqual(report['stats']['injected'], 1)
        self.assertEqual(report['block'].count('事实00'), 1)

    def test_the_whole_diary_is_never_pasted(self):
        self.index(facts=[fact()])
        report = self.ask()
        self.assertNotIn('原始详细叙述', report['block'])


# -------------------------------------------------------------------- ACL


class AclTests(Base):
    def test_a_private_memory_of_another_user_is_not_injected(self):
        self.index(visibility='private', sender='qq:20002')
        report = self.ask(requester='qq:10001')
        self.assertEqual(report['stats']['injected'], 0)
        self.assertEqual(report['block'], '')

    def test_the_owner_may_receive_their_own_private_memory(self):
        self.index(visibility='private', sender='qq:20002')
        report = self.ask(requester='qq:20002')
        self.assertEqual(report['stats']['injected'], 1)


# ------------------------------------------------------------------- gate


class GateTests(Base):
    SENSITIVE = {'valence': -0.8, 'arousal': 0.7, 'confidence': 0.9, 'sensitive': True}

    def test_a_sensitive_memory_is_not_injected_proactively(self):
        self.index(emotion=self.SENSITIVE)
        report = self.ask()
        self.assertEqual(report['stats']['injected'], 0)
        self.assertEqual(report['stats']['gated'], 1)
        self.assertEqual(report['omitted'][0]['reason'], 'gated_sensitive_or_high_load')

    def test_a_user_initiated_question_releases_a_sensitive_memory(self):
        self.index(emotion=self.SENSITIVE)
        report = self.ask(user_initiated=True)
        self.assertEqual(report['stats']['gated'], 0)
        self.assertEqual(report['stats']['injected'], 1)


# ------------------------------------------------------------------ rank


class RankTests(Base):
    def setUp(self):
        Base.setUp(self)
        # The memories must exist in the authoritative store: injection_allowed
        # refuses an id it cannot find, which is the point of the gate.
        self.add(mid='mem_a', eid='evt-a')
        self.add(mid='mem_b', eid='evt-b')
        # The authoritative projection wins over the candidate text, so the
        # subject has to be recorded in the store, not only in the fake hit.
        self.add(mid='mem_other', eid='evt-other',
                facts=[fact(subject='qq:20002', obj='乌龙茶', eid='evt-other')])
        self.add(mid='mem_self', eid='evt-self',
                facts=[fact(subject='qq:10001', obj='甜奶茶', eid='evt-self')])

    def candidates(self):
        return [{'memory_id': 'mem_a', 'relevance': 0.9, 'weight': 10.0, 'score': 0.10,
                 'text': projection([fact(obj='甜奶茶')])},
                {'memory_id': 'mem_b', 'relevance': 0.5, 'weight': 90.0, 'score': 0.90,
                 'text': projection([fact(obj='乌龙茶')])}]

    def test_the_engine_rank_score_decides_the_order_not_the_adapter_score(self):
        adapter = FakeAdapter(self.candidates())
        report = ChatPath(self.engine, adapter).inject([{'role': 'user', 'content': 'x'}],
                                                       requester='qq:10001')
        self.assertEqual([c['memory_id'] for c in report['injected']], ['mem_a', 'mem_b'])
        expected = self.engine.rank_score(0.9, 10.0)
        self.assertAlmostEqual(report['injected'][0]['score'], expected, places=6)

    def test_a_high_weight_but_unrelated_memory_is_not_injected(self):
        adapter = FakeAdapter(self.candidates())
        path = ChatPath(self.engine, adapter, min_relevance=0.6)
        report = path.inject([{'role': 'user', 'content': 'x'}], requester='qq:10001')
        self.assertEqual([c['memory_id'] for c in report['injected']], ['mem_a'])
        self.assertIn('below_relevance', [o['reason'] for o in report['omitted']])

    def test_a_memory_about_the_speaker_wins_a_tie(self):
        cands = [{'memory_id': 'mem_other', 'relevance': 0.5, 'weight': 50.0,
                  'text': projection([fact(subject='qq:20002', obj='乌龙茶')])},
                 {'memory_id': 'mem_self', 'relevance': 0.5, 'weight': 50.0,
                  'text': projection([fact(subject='qq:10001', obj='甜奶茶')])}]
        adapter = FakeAdapter(cands)
        report = ChatPath(self.engine, adapter).inject([{'role': 'user', 'content': 'x'}],
                                                       requester='qq:10001',
                                                       speaker='qq:10001')
        self.assertEqual(report['injected'][0]['memory_id'], 'mem_self')
        self.assertEqual(report['stats']['person_matched'], 1)


# ----------------------------------------------------------------- budget


class BudgetTests(Base):
    def setUp(self):
        Base.setUp(self)
        # The line the block renders comes from the authoritative projection,
        # so the long memory has to be long in the store.
        self.add(mid='mem_huge', eid='evt-huge', facts=[fact(obj='长' * 400, eid='evt-huge')])
        self.add(mid='mem_small', eid='evt-small', facts=[fact(obj='短', eid='evt-small')])

    def long_candidates(self):
        return [{'memory_id': 'mem_huge', 'relevance': 0.9, 'weight': 50.0,
                 'text': projection([fact(obj='长' * 400)])},
                {'memory_id': 'mem_small', 'relevance': 0.8, 'weight': 50.0,
                 'text': projection([fact(obj='短')])}]

    def test_the_block_stays_inside_the_token_budget(self):
        adapter = FakeAdapter(self.long_candidates() * 3)
        report = ChatPath(self.engine, adapter, token_budget=150).inject(
            [{'role': 'user', 'content': 'x'}], requester='qq:10001')
        self.assertLessEqual(report['tokens'], 150)
        self.assertGreater(report['stats']['dropped_by_budget'], 0)

    def test_an_oversized_atom_is_skipped_and_a_smaller_one_still_fits(self):
        adapter = FakeAdapter(self.long_candidates())
        report = ChatPath(self.engine, adapter, token_budget=120).inject(
            [{'role': 'user', 'content': 'x'}], requester='qq:10001')
        injected = [c['memory_id'] for c in report['injected']]
        self.assertIn('mem_small', injected)
        self.assertNotIn('mem_huge', injected)

    def test_a_context_budget_object_is_accepted(self):
        adapter = FakeAdapter(self.long_candidates())
        report = ChatPath(self.engine, adapter, budget=ContextBudget(),
                          token_budget=150).inject([{'role': 'user', 'content': 'x'}],
                                                  requester='qq:10001')
        self.assertLessEqual(report['tokens'], 150)

    def test_the_default_budget_comes_from_the_engine_config(self):
        self.cfg['rag']['context_token_budget'] = 321
        self.cfg['rag']['top_k'] = 4
        path = self.path()
        self.assertEqual(path.token_budget, 321)
        self.assertEqual(path.top_k, 4)


# ----------------------------------------------------------------- rerank


class RerankPolicyTests(Base):
    def test_a_real_reranker_produces_no_degraded_marker(self):
        self.index(facts=[fact()])
        report = self.ask()
        self.assertEqual(report['rerank']['status'], 'ok')
        self.assertNotIn('召回降级', report['block'])
        self.assertEqual(self.reranker.calls, 1)

    def test_a_required_but_missing_reranker_injects_nothing(self):
        self.index(facts=[fact()])
        adapter = RAGAdapter(self.emb, self.store, engine=self.engine,
                             reranker=LexicalReranker())
        report = self.ask(path=self.path(adapter=adapter))
        self.assertEqual(report['stats']['injected'], 0)
        self.assertEqual(report['rerank']['status'], 'degraded')
        self.assertIn('rerank', report['note'])

    def test_an_optional_missing_reranker_marks_the_block(self):
        self.index(facts=[fact()])
        adapter = RAGAdapter(self.emb, self.store, engine=self.engine,
                             reranker=LexicalReranker())
        report = self.ask(path=self.path(adapter=adapter, require_rerank=False))
        self.assertIn('召回降级', report['block'])
        self.assertGreater(report['stats']['injected'], 0)


# --------------------------------------------------------------- failures


class FailureTests(Base):
    def test_a_search_failure_is_reported_and_does_not_break_the_turn(self):
        class Boom:
            def search(self, *a, **k):
                raise RuntimeError('index down')

            def rerank(self, *a, **k):
                return {'status': 'ok', 'candidates': []}

        report = ChatPath(self.engine, Boom()).inject([{'role': 'user', 'content': 'x'}],
                                                      requester='qq:10001')
        self.assertIn('search failed', report['error'])
        self.assertEqual(report['block'], '')

    def test_a_rerank_failure_degrades_instead_of_raising(self):
        class Boom:
            def search(self, query, acl_filter=None, limit=10):
                return [{'memory_id': 'mem_a', 'relevance': 0.9, 'weight': 50.0,
                         'text': projection([fact()])}]

            def rerank(self, query, candidates):
                raise RuntimeError('rerank down')

        report = ChatPath(self.engine, Boom(), require_rerank=False).inject(
            [{'role': 'user', 'content': 'x'}], requester='qq:10001')
        self.assertEqual(report['rerank']['status'], 'degraded')
        self.assertIn('rerank failed', report['rerank']['reason'])


# ------------------------------------------------------------ without engine


class NoEngineTests(Base):
    def test_the_block_is_still_built_without_an_engine(self):
        adapter = FakeAdapter([{'memory_id': 'mem_a', 'relevance': 0.9, 'weight': 50.0,
                                 'text': projection([fact()])}])
        report = ChatPath(None, adapter, require_rerank=False).inject(
            [{'role': 'user', 'content': 'x'}], requester='qq:10001')
        self.assertIn('甜奶茶', report['block'])


# ------------------------------------------------------------------ end to end


class EndToEndTests(Base):
    def test_a_turn_without_any_retrieval_tool_call_still_gets_the_projection(self):
        self.index(mid='mem_hangzhou', facts=[fact(obj='杭州后端开发')])
        report = self.ask(text='杭州 后端')
        self.assertIn('杭州后端开发', report['block'])
        self.assertEqual(report['stats']['injected'], 1)

    def test_a_deleted_memory_is_not_injected_any_more(self):
        self.index(mid='mem_gone', facts=[fact(obj='会消失的记忆')])
        self.engine.db.execute("UPDATE memories SET deleted_at=? WHERE id='mem_gone'", (NOW,))
        self.engine.db.commit()
        report = self.ask(text='会消失的记忆')
        self.assertNotIn('会消失的记忆', report['block'])
        self.assertEqual(report['stats']['injected'], 0)

    def test_the_report_explains_every_omitted_candidate(self):
        self.index(mid='mem_ok', facts=[fact(obj='甜奶茶')])
        # A shared but sensitive memory is retrieved and then dropped by the
        # engine's gate -- the case the report has to explain.
        self.index(mid='mem_gated', eid='evt-gated',
                   facts=[fact(obj='乌龙茶', eid='evt-gated')],
                   emotion={'valence': -0.8, 'arousal': 0.7, 'confidence': 0.9,
                            'sensitive': True})
        report = self.ask(text='甜奶茶 乌龙茶')
        self.assertEqual(report['stats']['injected'], 1)
        self.assertEqual(len(report['omitted']), 1)
        self.assertEqual(report['omitted'][0]['reason'], 'gated_sensitive_or_high_load')
        self.assertNotIn('乌龙茶', report['block'])


class PresetCalibrationTests(Base):
    """A preset's measured relevance floor must actually reach the chat path.

    The floor is per projection shape (fact atoms ~0.51, full text ~0.53), so
    the preset ships it next to the numbers it was measured with. Before this
    was wired up, ``calibration.min_relevance`` was read by nothing.
    """

    def preset_path(self, preset, engine_cfg=None, **kwargs):
        """A ChatPath over an engine built with a preset config.

        The engine is closed here on purpose: ``tearDown`` deletes the temp
        directory first and a still-open SQLite file cannot be removed on
        Windows.
        """
        path_on_disk = Path(self.tmp.name) / ('mem-%s.db' % preset)
        engine = MemoryEngine(path_on_disk, engine_cfg or load_config(preset=preset))
        try:
            return ChatPath(engine, self.adapter, **kwargs)
        finally:
            engine.close()

    def test_floor_comes_from_the_preset_config(self):
        self.assertAlmostEqual(self.preset_path('evergreen').min_relevance, 0.53)
        self.assertAlmostEqual(self.preset_path('slow-forget').min_relevance, 0.51)

    def test_explicit_argument_still_wins(self):
        path = self.preset_path('evergreen', min_relevance=0.8)
        self.assertEqual(path.min_relevance, 0.8)

    def test_default_engine_keeps_no_floor(self):
        self.assertIsNone(self.path().min_relevance)

    def test_explicit_rag_floor_beats_the_preset_metadata(self):
        cfg = load_config(preset='evergreen')
        cfg['rag'] = dict(cfg['rag'], min_relevance=0.7)
        path = self.preset_path('evergreen', engine_cfg=cfg)
        self.assertEqual(path.min_relevance, 0.7)


class RecallStyleTests(Base):
    """1.4.0-alpha: the same facts, rendered as memory instead of as a record.

    ``archive`` stays the default; these tests pin what ``recall`` changes and
    what it must not change (negation, uncertainty, the missing notice).
    """

    def recall_path(self, **kwargs):
        kwargs.setdefault('style', 'recall')
        return self.path(**kwargs)

    def test_the_viewer_is_spoken_to_in_the_second_person(self):
        self.index(facts=[fact(obj='甜奶茶')])
        report = self.ask(path=self.recall_path(viewer='qq:10001'))
        self.assertIn('你喜欢甜奶茶', report['block'])

    def test_a_known_third_party_renders_as_the_alias(self):
        self.index(mid='m_hotpot', facts=[fact(subject='qq:20002', obj='火锅')])
        report = self.ask(text='火锅',
                          path=self.recall_path(person_names={'qq:20002': '张三'}))
        self.assertIn('张三', report['block'])
        self.assertNotIn('qq:20002', report['block'])

    def test_an_unknown_account_id_degrades_to_a_neutral_word(self):
        self.index(mid='m_hotpot', facts=[fact(subject='qq:20002', obj='火锅')])
        report = self.ask(text='火锅', path=self.recall_path())
        self.assertNotIn('qq:20002', report['block'])
        self.assertIn('某人', report['block'])

    def test_negation_stays_visible(self):
        self.index(facts=[fact(predicate='喜欢', negated=True)])
        report = self.ask(path=self.recall_path(viewer='qq:10001'))
        self.assertIn('不喜欢', report['block'])

    def test_an_unknown_recording_time_is_admitted(self):
        self.index(facts=[fact(time=None)])
        report = self.ask(path=self.recall_path())
        self.assertIn('时间不明', report['block'])

    def test_the_archive_stamps_are_gone(self):
        self.index(facts=[fact()])
        report = self.ask(path=self.recall_path())
        self.assertNotIn('记录于', report['block'])
        self.assertNotIn('来源：', report['block'])

    def test_the_missing_notice_reads_as_hesitation(self):
        self.index(mid='mem_a', facts=[fact(obj='甜奶茶')])
        self.index(mid='mem_b', facts=[fact(obj='乌龙茶', eid='evt-20261011-02')])
        report = self.inject_with_budget()
        self.assertIn('没想起来', report['missing_notice'])

    def inject_with_budget(self):
        path = self.recall_path(token_budget=40)
        return path.inject([{'role': 'user', 'content': '奶茶 乌龙茶'}],
                           requester='qq:10001')

    def test_archive_remains_the_default(self):
        self.index(facts=[fact()])
        report = self.ask()
        self.assertTrue(report['block'].startswith(BLOCK_HEADER))
        self.assertIn('来源：', report['block'])
        self.assertNotIn('时间不明', report['block'])

    def test_an_unknown_style_is_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            ChatPath(self.engine, self.adapter, style='poetic')


class AlphaBase(Base):
    """Retrieval that can actually miss.

    The stub embedding scores *every* projection against *every* query, so
    without a relevance floor each candidate becomes a direct hit and a
    one-hop neighbour can never add anything -- it was already injected.
    0.25 is calibrated for this stub only; a real deployment calibrates
    against its own model and corpus.
    """

    def setUp(self):
        super().setUp()
        self.adapter = RAGAdapter(HashingEmbedding(dim=1024), self.store,
                                  engine=self.engine, reranker=self.reranker,
                                  min_relevance=0.25)


class AssocTests(AlphaBase):
    """1.4.0-alpha: one-hop neighbours are guesses, so they are capped, budgeted
    separately, and marked. They are never silent."""

    def cat_world(self):
        """猫 (200 days ago) and 猫粮 (now): same thread, but too far apart for
        the conversation edge, so only the shared object can link them."""
        self.engine.process_event(
            event(eid='e_cat', mid='m_cat', content='养猫',
                  facts=[fact(obj='猫', eid='e_cat')]), NOW - 200 * 86400)
        self.engine.process_event(
            event(eid='e_food', mid='m_food', content='猫粮牌子',
                  facts=[fact(obj='猫粮', eid='e_food')]), NOW)
        sync_once(self.engine, self.adapter)

    def test_association_is_off_by_default(self):
        self.cat_world()
        report = self.ask(text='猫粮')
        self.assertEqual(report['stats']['assoc_candidates'], 0)
        self.assertEqual(report['stats']['assoc_injected'], 0)

    def test_a_shared_object_links_two_memories(self):
        self.cat_world()
        report = self.ask(text='猫粮', path=self.path(style='recall', assoc=True))
        self.assertGreaterEqual(report['stats']['assoc_candidates'], 1)
        self.assertEqual(report['stats']['assoc_injected'], 1)
        self.assertIn('顺带想到', report['block'])

    def test_a_neighbour_is_marked_and_sits_after_the_direct_hit(self):
        self.cat_world()
        report = self.ask(text='猫粮', path=self.path(style='recall', assoc=True))
        block = report['block']
        self.assertLess(block.index('猫粮'), block.index('顺带想到'))

    def test_a_neighbour_cannot_eat_the_main_budget(self):
        self.cat_world()
        report = self.ask(text='猫粮',
                          path=self.path(style='recall', assoc=True, assoc_budget=1))
        self.assertEqual(report['stats']['assoc_injected'], 0)
        reasons = [item['reason'] for item in report['omitted']]
        self.assertIn('assoc_budget', reasons)

    def test_a_gated_neighbour_is_not_admitted(self):
        self.cat_world()
        self.engine.db.execute(
            "UPDATE memories SET emotion=? WHERE id='m_cat'",
            ('{"sensitive":true,"snapshot":null,"affect":{"charge":0,"updated_at":0},'
             '"appraisal":{"revision":0,"current":null,"at":null,"evidence":null,"history":[]}}',))
        self.engine.db.commit()
        report = self.ask(text='猫粮', path=self.path(style='recall', assoc=True))
        self.assertEqual(report['stats']['assoc_injected'], 0)


class SupplementTests(AlphaBase):
    """1.4.0-alpha: the second injection of a turn, triggered by reasoning."""

    def test_it_is_marked_as_a_supplement_not_a_direct_hit(self):
        self.index(mid='m_birthday', facts=[fact(predicate='生日', obj='10-15')])
        path = self.path(style='recall')
        first = path.inject([{'role': 'user', 'content': '我想吃蛋糕'}],
                            requester='qq:10001')
        taken = [c['memory_id'] for c in first['injected']]
        supplement = path.recall('生日', why='生日场景下蛋糕有意义',
                                 requester='qq:10001', exclude_ids=taken)
        self.assertTrue(supplement['block'].startswith('[补充'))
        self.assertIn('生日', supplement['block'])
        self.assertIn('不是当前问题直接命中', supplement['block'])

    def test_it_never_repeats_what_was_already_injected(self):
        self.index(mid='m_birthday', facts=[fact(predicate='生日', obj='10-15')])
        path = self.path(style='recall')
        first = path.inject([{'role': 'user', 'content': '生日'}], requester='qq:10001')
        taken = [c['memory_id'] for c in first['injected']]
        self.assertTrue(taken)
        again = path.recall('生日', requester='qq:10001', exclude_ids=taken)
        self.assertEqual(again['stats']['injected'], 0)
        self.assertIn('没找到更多', again['block'])

    def test_a_miss_says_there_is_nothing_rather_than_guessing(self):
        self.index(mid='m_food', facts=[fact(obj='甜奶茶')])
        path = self.path(style='recall')
        supplement = path.recall('南极洲的企鹅', requester='qq:10001')
        self.assertIn('没找到更多', supplement['block'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
