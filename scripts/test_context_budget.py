"""Behavioural tests for the context budget assembler. No network, no deps."""
import math
import unittest

from context_budget import BudgetError, ContextBudget, estimate_tokens

HARD, SOFT = 512_000, 256_000


def turns(n, size=100):
    return [{'role': 'user' if i % 2 else 'assistant', 'content': 'x' * size} for i in range(n)]


class EstimatorTests(unittest.TestCase):
    def test_cjk_counts_one_per_char_ascii_quarter(self):
        self.assertEqual(estimate_tokens('你好'), 2)
        self.assertEqual(estimate_tokens('abcd'), 1)
        self.assertEqual(estimate_tokens(''), 0)

    def test_mixed_text(self):
        self.assertEqual(estimate_tokens('你好abcd'), 2 + 1)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.budget = ContextBudget(hard=HARD, soft=SOFT)

    def test_small_session_is_under_soft_line(self):
        ctx = self.budget.assemble(system='系统', tools='工具', ledger='台账',
                                   history=turns(10), memory=[{'text': '一条记忆', 'at': '2026-01'}],
                                   media=[{'kind': 'image', 'ref': 'a.png', 'tokens': 1600}])
        self.assertFalse(ctx.should_reset)
        self.assertFalse(ctx.cache_reset)
        self.assertEqual(ctx.stats['history_turns'], 10)
        self.assertEqual(ctx.stats['history_evicted'], 0)
        self.assertLess(ctx.committed, SOFT)

    def test_steady_state_total_stays_well_under_the_soft_line(self):
        # Soft line is 256K; the default allocation sums to ~124K, i.e. under half
        # the line. The 256K line therefore only bites during peak expansion.
        steady = sum(self.budget.alloc.values())
        self.assertLess(steady, SOFT * 0.6)
        self.assertGreater(steady, SOFT * 0.4)

    def test_history_evicts_from_the_oldest_end_only(self):
        ctx = self.budget.assemble(history=turns(2000, size=200))   # ~100K, cap 60K
        self.assertEqual(ctx.stats['history_evicted'], 2000 - ctx.stats['history_turns'])
        self.assertGreater(ctx.stats['history_turns'], 0)
        self.assertTrue(ctx.cache_reset)

    def test_eviction_leaves_batch_headroom_not_one_turn(self):
        # Dropping a single turn would rebuild the cache prefix every turn.
        ctx = self.budget.assemble(history=turns(2000, size=200))
        cap = self.budget.alloc['history']
        self.assertLessEqual(ctx.stats['history'], cap)
        self.assertLessEqual(ctx.stats['history'], cap * 0.8)

    def test_newest_turn_survives_eviction(self):
        history = turns(2000, size=200)
        history[-1] = {'role': 'user', 'content': '最新一句' * 10}
        ctx = self.budget.assemble(history=history)
        self.assertTrue(ctx.render().endswith('最新一句' * 10))

    def test_block_order_keeps_variables_after_cache_prefix(self):
        ctx = self.budget.assemble(system='S', tools='T', ledger='L', summary='M',
                                   history=turns(3), memory=[{'text': 'R', 'at': 'a'}],
                                   media=[{'kind': 'image', 'ref': 'x.png', 'tokens': 1500}])
        names = [name for name, _, _ in ctx.segments]
        self.assertEqual(names, ['system_tools', 'ledger', 'summary', 'history', 'memory', 'media'])
        self.assertEqual(ctx.cache_prefix_tokens, ctx.stats['system_tools'] + ctx.stats['ledger']
                         + ctx.stats['summary'] + ctx.stats['history'])

    def test_media_is_capped_by_count_even_when_budget_allows(self):
        items = [{'kind': 'image', 'ref': f'{i}.png', 'tokens': 100} for i in range(20)]
        ctx = self.budget.assemble(media=items)
        self.assertEqual(len([n for n, _, _ in ctx.segments if n == 'media']), 1)
        self.assertEqual(20 - ctx.stats['media_dropped'], 4)

    def test_oversized_media_is_skipped_not_truncated(self):
        items = [{'kind': 'video', 'ref': 'big.mp4', 'tokens': 40_000},
                 {'kind': 'image', 'ref': 'ok.png', 'tokens': 1_500}]
        ctx = self.budget.assemble(media=items)
        self.assertEqual(ctx.stats['media_dropped'], 1)
        self.assertIn('ok.png', ctx.render())

    def test_eviction_schedules_handoff_without_forcing_an_immediate_reset(self):
        # With a 60K history cap the total never reaches 256K, so eviction is the
        # only signal that fires; it means "schedule the handoff", not "stop now".
        ctx = self.budget.assemble(history=turns(2000, size=200))
        self.assertTrue(ctx.needs_handoff)
        self.assertFalse(ctx.should_reset)
        self.assertTrue(ctx.cache_reset)

    def test_soft_line_still_stops_a_peak_burst(self):
        tight = ContextBudget(hard=HARD, soft=50_000)
        ctx = tight.assemble(history=turns(2000, size=200))
        self.assertTrue(ctx.should_reset)
        self.assertTrue(ctx.needs_handoff)

    def test_hard_cap_raises_with_a_usable_suggestion(self):
        small = ContextBudget(hard=50_000, soft=40_000)
        with self.assertRaises(BudgetError) as caught:
            small.assemble(history=turns(2000, size=200))
        self.assertIn(caught.exception.suggestion, ('history', 'media', 'memory', 'summary'))
        self.assertGreater(caught.exception.over_by, 0)

    def test_oversized_fixed_block_names_the_real_offender(self):
        with self.assertRaises(BudgetError) as caught:
            self.budget.assemble(system='x' * 90_000)
        self.assertEqual(caught.exception.suggestion, 'system_tools')

    def test_reserve_is_committed_but_not_renderable(self):
        ctx = self.budget.assemble(system='S', history=turns(2))
        self.assertEqual(ctx.stats['reserve'], 16_000)
        self.assertEqual(ctx.committed, ctx.total + 16_000)
        self.assertNotIn('reserve', [name for name, _, _ in ctx.segments])

    def test_empty_input_is_safe(self):
        ctx = self.budget.assemble()
        self.assertEqual(ctx.total, 0)
        self.assertEqual(ctx.render(), '')
        self.assertFalse(ctx.should_reset)


class ResetScenarioTests(unittest.TestCase):
    """The documented remedy: reset at the soft line and hand over state only."""

    def test_reset_drops_history_orders_of_magnitude_and_keeps_coordinates(self):
        budget = ContextBudget(hard=HARD, soft=SOFT)
        ledger = '关系：熟悉偏调侃；禁区：不提父亲病情；线程：复查结果待问'
        before = budget.assemble(ledger=ledger, history=turns(2000, size=200))
        self.assertTrue(before.needs_handoff)
        after = budget.assemble(ledger=ledger, history=turns(5, size=200))
        self.assertFalse(after.should_reset)
        self.assertFalse(after.needs_handoff)
        self.assertLess(after.total, before.total / 50)
        self.assertIn('不提父亲病情', after.render())


if __name__ == '__main__':
    unittest.main(verbosity=2)
