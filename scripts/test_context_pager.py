"""Working-set regressions: disposable frames, durable facts, no network."""
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from adapters.context_pager import ContextPager, HistoryTurn, PagerConfig, PagingError, ToolPage
from test_chat_path import AlphaBase, fact


def turn(n, **kwargs):
    values = dict(message_id=str(n), role='user', content=f'原文{n}',
                  sender_id=f'qq:{n % 3}', scope='group:1', persisted=True)
    values.update(kwargs)
    return HistoryTurn(**values)


class PagerTests(unittest.TestCase):
    def setUp(self):
        self.pager = ContextPager(lambda row, who: True)

    def frame(self, **kwargs):
        values = dict(system='固定系统与工具', ledger='称呼来自已核实台账', current='现在说的话',
                      history=[], requester='qq:10001', scope='group:1')
        values.update(kwargs)
        return self.pager.prepare(**values)

    def test_idle_chat_does_not_fill_capacity(self):
        f = self.frame(history=[turn(i) for i in range(100)])
        self.assertEqual(f.history_ids, tuple(str(i) for i in range(88, 100)))
        self.assertLess(f.estimated_input_tokens, 4096)
        self.assertLess(f.estimated_input_tokens + f.reserved_output_tokens, f.active_limit)

    def test_500_pool_is_not_500_resident_records(self):
        f = self.frame(history=[turn(i) for i in range(600)])
        self.assertEqual(f.pool_size, 500)
        self.assertEqual(len(f.history_ids), 12)
        self.assertNotIn('100', f.history_ids)
        self.assertIn('未驻留', f.render())

    def test_requested_older_page_is_selected(self):
        f = self.frame(history=[turn(i) for i in range(100)], relevant_ids=['4'])
        self.assertIn('4', f.history_ids)

    def test_request_cannot_expand_pool_silently(self):
        f = self.frame(history=[turn(i) for i in range(600)], relevant_ids=['4'])
        self.assertNotIn('4', f.history_ids)

    def test_large_mode_returns_to_low_water_next_turn(self):
        history = [turn(i) for i in range(200)]
        large = self.frame(history=history, large_context=True, large_reason='核对多轮原文')
        small = self.frame(history=history)
        self.assertEqual(len(large.history_ids), 200)
        self.assertEqual(len(small.history_ids), 12)
        self.assertGreater(large.estimated_input_tokens, small.estimated_input_tokens)

    def test_large_mode_requires_reason(self):
        with self.assertRaises(PagingError):
            self.frame(large_context=True)

    def test_fixed_overhead_cannot_consume_reserve(self):
        with self.assertRaises(PagingError):
            self.frame(system='大' * 40000)

    def test_state_is_never_truncated_to_fit(self):
        with self.assertRaises(PagingError):
            self.frame(ledger='关系' * 300)

    def test_unneeded_archived_tools_unload_before_pressure(self):
        f = self.frame(tools=[ToolPage('done', '旧工具输出' * 900, False, True)])
        self.assertEqual(f.unloaded_tool_ids, ('done',))
        self.assertNotIn('旧工具输出', f.render())

    def test_tool_output_cannot_be_lost_before_archive_ack(self):
        with self.assertRaises(PagingError):
            self.frame(tools=[ToolPage('done', '未写盘', False, False)])

    def test_needed_oversize_tool_result_is_not_silently_cut(self):
        with self.assertRaises(PagingError):
            self.frame(tools=[ToolPage('needed', '重要输出' * 900, True, True)])

    def test_unpersisted_history_not_silently_evicted(self):
        with self.assertRaises(PagingError):
            self.frame(history=[turn(0, persisted=False)] + [turn(i) for i in range(1, 30)])

    def test_unpersisted_history_not_lost_from_pool(self):
        with self.assertRaises(PagingError):
            self.frame(history=[turn(0, persisted=False)] + [turn(i) for i in range(1, 600)])

    def test_scope_and_acl_filter_do_not_expose_private_rows(self):
        self.pager = ContextPager(lambda row, who: row.sender_id != 'secret')
        f = self.frame(history=[turn(1, sender_id='secret'), turn(2, scope='private:other'), turn(3)])
        self.assertEqual(f.history_ids, ('3',))
        self.assertNotIn('secret', f.render())
        self.assertEqual(f.pool_size, 2)

    def test_same_nickname_cannot_merge_sender_ids(self):
        f = self.frame(history=[turn(1, metadata={'nickname': '同名'}), turn(2, metadata={'nickname': '同名'})])
        self.assertIn('qq:1', f.render())
        self.assertIn('qq:2', f.render())

    def test_current_message_not_repeated_in_history(self):
        f = self.frame(history=[turn(1), turn(2)], current_id='2')
        self.assertEqual(f.history_ids, ('1',))

    def test_augmented_prompt_rejected_without_stripping_user_quotes(self):
        with self.assertRaises(PagingError):
            self.frame(history=[turn(1, raw=False)])
        f = self.frame(history=[turn(1, content='用户引用 [ES-Memory active page]')])
        self.assertIn('用户引用', f.render())

    def test_long_run_has_bounded_frames_and_does_not_mutate_archive(self):
        archive = [turn(i) for i in range(600)]
        original = list(archive)
        sizes = []
        for n in range(30, 601, 30):
            f = self.frame(history=archive[:n])
            sizes.append(f.estimated_input_tokens)
            self.assertEqual(len(f.history_ids), 12)
        self.assertEqual(archive, original)
        self.assertLess(max(sizes) - min(sizes), 200)

    def test_invalid_configuration_and_tokenizer_rejected(self):
        for cfg in ({'history_pool': 0}, {'normal_tokens': 300000}, {'output_reserve': True}):
            with self.assertRaises(PagingError): PagerConfig(**cfg)
        self.pager = ContextPager(lambda *args: True, counter=lambda text: float('nan'))
        with self.assertRaises(PagingError): self.frame()

    def test_low_budget_does_not_cut_whole_history_records(self):
        f = self.frame(history=[turn(1, content='长' * 6000), turn(2)])
        self.assertEqual(f.history_ids, ('2',))

    def test_memory_permissions_changing_during_retrieval_abort_frame(self):
        allowed = [True]
        self.pager = ContextPager(lambda *args: allowed[0])
        class Path:
            def inject(self, *args, **kwargs):
                allowed[0] = False
                return {'block': ''}
        with self.assertRaises(PagingError): self.frame(history=[turn(1)], path=Path())


class PagerRagTests(AlphaBase):
    def test_real_chatpath_replaces_previous_injection_without_erasing_memory(self):
        self.index(mid='m', facts=[fact()])
        pager = ContextPager(lambda *args: True)
        args = dict(system='系统', ledger='', requester='qq:10001', scope='group:1', history=[])
        before = self.engine.row('m')['weight']
        f = pager.prepare(current='甜奶茶', path=self.path(clock=lambda: 1700000000), **args)
        self.assertTrue(f.memory_report['injected'])
        empty = pager.prepare(current='无关话题', **args)
        self.assertNotIn('甜奶茶', empty.render())
        self.assertEqual(self.engine.row('m')['weight'], before)
        self.assertIsNotNone(self.engine.row('m'))


if __name__ == '__main__':
    unittest.main()
