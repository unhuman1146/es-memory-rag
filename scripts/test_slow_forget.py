"""Tests for the "forget less" configuration presets.

Two presets ship with the package:

``slow-forget``
    Decay is 20x slower and the profile floors sit at or above the deletion
    threshold, so the nightly lottery cannot fire. Facts survive for decades;
    the full-text projection still ages out eventually.

``evergreen``
    Decay is 100x slower, every profile floor holds at the "important" line,
    the compression thresholds drop to 1 so the projection keeps the original
    text forever, and random deletion is switched off entirely.

The point of these tests is not that the numbers are what the preset files
say -- it is that the *behaviour* changes in the direction the preset claims,
and that the guard rails (the deletion lottery, the injection gate) stay
closed instead of being opened by accident.

Every test runs against a fresh temporary database. No network, no key.

    python scripts/test_slow_forget.py
"""
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from memory_engine import MemoryEngine, decayed, load_config
from presets import apply_preset, list_presets, merge, resolve_preset

NOW = 1_700_000_000.0
DAY = 86400.0
ENGINE = Path(__file__).resolve().parent / 'memory_engine.py'


def event(eid='e1', mid='m1', weight=80, visibility='shared', body='原始详细叙述。' * 120,
          emotion=None):
    action = {
        'kind': 'add', 'id': mid, 'weight': weight, 'body': body,
        'facts': [
            {'subject': 'qq:10001', 'predicate': 'likes', 'object': '甜奶茶', 'negated': True,
             'time': None, 'source_ids': [eid], 'core': True},
            {'subject': 'qq:10001', 'predicate': 'tried', 'object': '乌龙茶', 'negated': False,
             'time': '时间未知', 'source_ids': [eid], 'core': False}],
    }
    if emotion is not None:
        action['emotion'] = emotion
    return {'id': eid, 'kind': 'human_message', 'peer_id': 'agent:private:qq10001:sender10001',
            'sender_id': 'qq:10001', 'visibility': visibility, 'content': 'new fact ' + eid,
            'actions': [action]}


def key_paths(obj, prefix=''):
    """Every leaf path in a nested dict, e.g. profiles.standard.floor."""
    paths = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            paths |= key_paths(value, prefix + key + '.')
    else:
        paths.add(prefix.rstrip('.'))
    return paths


class PresetRegistryTests(unittest.TestCase):
    """The preset files must stay loadable, valid, and in sync with the default."""

    def setUp(self):
        self.default = load_config()

    def test_both_presets_ship_and_load(self):
        self.assertEqual(list_presets(), ['evergreen', 'slow-forget'])
        for name in list_presets():
            cfg = load_config(preset=name)
            self.assertIsInstance(cfg, dict)
            self.assertEqual(cfg['rag'], self.default['rag'])  # Retrieval layer untouched.

    def test_every_overlay_key_exists_in_the_default_config(self):
        """A typo in a preset would silently do nothing. Catch it here."""
        known = key_paths(self.default)
        for name in list_presets():
            overlay = resolve_preset(name)['config']
            for path in key_paths(overlay):
                self.assertIn(path, known, 'preset %s sets unknown config key %s' % (name, path))

    def test_preset_is_an_overlay_not_a_config_copy(self):
        """A copy would rot the day the default gains a key. The overlay must
        stay a small fraction of the config."""
        default_paths = key_paths(self.default)
        for name in list_presets():
            overlay_paths = key_paths(resolve_preset(name)['config'])
            self.assertLess(len(overlay_paths), len(default_paths),
                            'preset %s is not smaller than the default config' % name)
            self.assertLess(len(overlay_paths) / len(default_paths), 0.5,
                            'preset %s is too close to a full config copy' % name)

    def test_preset_only_touches_decay_knobs(self):
        """A preset that changed the retrieval budget, the injection policy or
        the emotion policy would be a different feature wearing a preset's
        clothes. The decay system is the only thing a preset may move."""
        allowed = {'rates_per_hour', 'profiles', 'full_threshold', 'facts_threshold',
                   'delete_below', 'delete_probability_per_day', 'recycle_days'}
        for name in list_presets():
            touched = set(resolve_preset(name)['config'].keys())
            self.assertTrue(touched <= allowed,
                            'preset %s touches keys outside the decay system: %s'
                            % (name, sorted(touched - allowed)))

    def test_new_default_keys_survive_the_overlay(self):
        """The anti-rot property: a future default key passes through the
        preset untouched instead of being pinned to today's value."""
        base = load_config()
        base['future_feature'] = {'enabled': True, 'level': 3}
        for name in list_presets():
            merged = apply_preset(base, resolve_preset(name))
            self.assertEqual(merged['future_feature'], {'enabled': True, 'level': 3})
            self.assertEqual(merged['rates_per_hour']['fact'],
                             base['rates_per_hour']['fact'] * 0.05 if name == 'slow-forget'
                             else base['rates_per_hour']['fact'] * 0.01)

    def test_merge_is_deep_and_non_destructive(self):
        base = {'profiles': {'standard': {'mult': 1.0, 'floor': 0}}, 'keep': 1}
        merged = merge(base, {'profiles': {'standard': {'floor': 30}}})
        self.assertEqual(merged['profiles']['standard'], {'mult': 1.0, 'floor': 30})
        self.assertEqual(merged['keep'], 1)
        self.assertEqual(base['profiles']['standard']['floor'], 0)  # base untouched

    def test_threshold_chain_holds_for_every_preset(self):
        for name in list_presets():
            cfg = load_config(preset=name)
            self.assertTrue(0 < cfg['delete_below'] <= cfg['facts_threshold']
                            <= cfg['full_threshold'] <= cfg['protected_weight'] <= cfg['max_weight'])

    def test_unknown_preset_fails_loudly(self):
        with self.assertRaises(ValueError):
            load_config(preset='no-such-preset')

    def test_preset_by_path_resolves(self):
        path = Path(__file__).resolve().parents[1] / 'assets' / 'presets' / 'evergreen.json'
        self.assertEqual(resolve_preset(str(path))['id'], 'evergreen')

    def test_preset_resolves_by_its_chinese_mode_name(self):
        """The mode is called 请你慢一点遗忘我. Passing that name must work --
        a Chinese-facing feature whose name cannot be typed is a joke."""
        preset = resolve_preset('请你慢一点遗忘我')
        self.assertEqual(preset['id'], 'slow-forget')
        self.assertEqual(preset['name'], '请你慢一点遗忘我')
        cfg = load_config(preset='请你慢一点遗忘我')
        self.assertEqual(cfg['delete_below'], 30)

    def test_the_two_levels_share_the_mode_name(self):
        ids = {p['id']: p for p in (resolve_preset(n) for n in list_presets())}
        self.assertTrue(ids['slow-forget']['name'].startswith('请你慢一点遗忘我'))
        self.assertTrue(ids['evergreen']['name'].startswith('请你慢一点遗忘我'))
        self.assertNotEqual(ids['slow-forget']['name'], ids['evergreen']['name'])


class DecaySpeedTests(unittest.TestCase):
    """The actual claim of the feature: things are forgotten more slowly."""

    def setUp(self):
        self.cfg = {'default': load_config(),
                    'slow-forget': load_config(preset='slow-forget'),
                    'evergreen': load_config(preset='evergreen')}

    def weight_at(self, name, hours, weight=80, curve='fact', profile='standard'):
        row = {'weight': weight, 'last_decay': NOW, 'pinned': 0, 'profile': profile,
               'curve': curve, 'emotion': ''}
        return decayed(row, NOW + hours * 3600, self.cfg[name])

    def test_decay_order_default_then_slow_then_evergreen(self):
        for hours in (24 * 30, 24 * 120, 24 * 365):
            default = self.weight_at('default', hours)
            slow = self.weight_at('slow-forget', hours)
            evergreen = self.weight_at('evergreen', hours)
            self.assertLess(default, slow, 'at %s hours' % hours)
            self.assertLess(slow, evergreen, 'at %s hours' % hours)

    def test_slow_forget_decays_exactly_twenty_times_slower(self):
        """lambda x0.05 means the exponent shrinks by 20x. The weight ratio
        against the default is exp(-(r_default - r_slow) * t) -- the slower
        config loses *less*, so the ratio grows above 1."""
        hours = 24 * 30
        default = self.weight_at('default', hours)
        slow = self.weight_at('slow-forget', hours)
        expected = default * math.exp(0.001 * hours * (1 - 0.05))
        self.assertAlmostEqual(slow / expected, 1.0, places=6)

    def test_evergreen_decays_one_hundred_times_slower(self):
        hours = 24 * 30
        default = self.weight_at('default', hours)
        evergreen = self.weight_at('evergreen', hours)
        expected = default * math.exp(0.001 * hours * (1 - 0.01))
        self.assertAlmostEqual(evergreen / expected, 1.0, places=6)

    def test_floors_hold_the_weight_from_below(self):
        self.assertAlmostEqual(self.weight_at('slow-forget', 24 * 3650), 30.0, places=6)
        self.assertAlmostEqual(self.weight_at('evergreen', 24 * 3650), 60.0, places=6)
        self.assertAlmostEqual(self.weight_at('default', 24 * 3650), 0.0, places=6)

    def test_floor_is_a_floor_not_a_ceiling(self):
        """A memory that is *above* its floor still decays normally."""
        self.assertGreater(self.weight_at('slow-forget', 24 * 365, weight=100), 30.0)


class ProjectionTierTests(unittest.TestCase):
    """Forgetting less must also mean the original text survives longer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engines = {}
        for name in ('default', 'slow-forget', 'evergreen'):
            cfg = load_config() if name == 'default' else load_config(preset=name)
            engine = MemoryEngine(Path(self.tmp.name) / (name + '.db'), cfg)
            self.addCleanup(engine.close)
            self.engines[name] = engine

    def test_default_compresses_away_the_original_text(self):
        engine = self.engines['default']
        engine.process_event(event(), NOW)
        # 60 days: the weight has fallen under facts_threshold but is still
        # above delete_below, so the memory is compressed, not deleted.
        engine.apply_plan(engine.plan(NOW + 60 * DAY))
        row = engine.row('m1')
        self.assertEqual(row['tier'], 'core')
        self.assertNotIn('detail', json.loads(row['text']))

    def test_slow_forget_stays_full_for_months_then_degrades(self):
        engine = self.engines['slow-forget']
        engine.process_event(event(), NOW)
        engine.apply_plan(engine.plan(NOW + 60 * DAY))
        row = engine.row('m1')
        self.assertEqual(row['tier'], 'full')
        self.assertIn('detail', json.loads(row['text']))
        # ... and it does eventually degrade: compression is part of the lifecycle.
        engine.apply_plan(engine.plan(NOW + 600 * DAY))
        row = engine.row('m1')
        self.assertLess(row['tier'], 'full')

    def test_evergreen_keeps_the_original_text_forever(self):
        engine = self.engines['evergreen']
        body = '原始详细叙述。' * 120
        engine.process_event(event(body=body), NOW)
        engine.apply_plan(engine.plan(NOW + 3650 * DAY))
        row = engine.row('m1')
        self.assertEqual(row['tier'], 'full')
        self.assertEqual(json.loads(row['text'])['detail'], body)

    def test_evergreen_projection_is_longer_than_default(self):
        for name in ('default', 'evergreen'):
            self.engines[name].process_event(event(), NOW)
            self.engines[name].apply_plan(self.engines[name].plan(NOW + 60 * DAY))
        default_len = len(self.engines['default'].row('m1')['text'])
        evergreen_len = len(self.engines['evergreen'].row('m1')['text'])
        self.assertGreater(default_len, 0)
        self.assertGreater(evergreen_len, default_len * 2)


class DeletionGuardTests(unittest.TestCase):
    """The presets must not open the deletion lottery. They must also not be
    the reason the lottery cannot fire -- the guard has to be real, so every
    test here first proves the default config *does* delete."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def make(self, name, cfg=None):
        if cfg is None:
            cfg = load_config() if name == 'default' else load_config(preset=name)
        engine = MemoryEngine(Path(self.tmp.name) / (name + '.db'), cfg)
        self.addCleanup(engine.close)  # Windows keeps the file locked otherwise.
        return engine

    def test_default_lottery_really_fires(self):
        """The control group. If this stops deleting, the guard tests below
        would pass for the wrong reason."""
        cfg = load_config()
        cfg['delete_probability_per_day'] = 1.0
        engine = self.make('default', cfg)
        engine.process_event(event(weight=5), NOW)
        stats = engine.apply_plan(engine.plan(NOW))
        self.assertEqual(stats['deleted'], 1)
        engine.close()

    def test_slow_forget_never_draws_the_lottery(self):
        cfg = load_config(preset='slow-forget')
        cfg['delete_probability_per_day'] = 1.0  # Even at 100% chance nothing goes.
        engine = self.make('slow-forget', cfg)
        engine.process_event(event(weight=5), NOW)
        for day in range(1, 20):
            plan = engine.plan(NOW + day * DAY)
            self.assertEqual([a for a in plan['actions'] if a['draw'] is not None], [])
            self.assertEqual([a for a in plan['actions'] if a['delete']], [])
        self.assertIsNone(engine.row('m1')['deleted_at'])
        engine.close()

    def test_evergreen_never_draws_the_lottery(self):
        cfg = load_config(preset='evergreen')
        cfg['delete_probability_per_day'] = 1.0  # The preset itself says 0; force it up.
        engine = self.make('evergreen', cfg)
        engine.process_event(event(weight=5), NOW)
        for day in range(1, 20):
            plan = engine.plan(NOW + day * DAY)
            self.assertEqual([a for a in plan['actions'] if a['delete']], [])
        self.assertIsNone(engine.row('m1')['deleted_at'])
        engine.close()

    def test_evergreen_weight_is_floored_above_the_delete_threshold(self):
        engine = self.make('evergreen')
        engine.process_event(event(weight=1), NOW)
        engine.apply_plan(engine.plan(NOW + DAY))
        row = engine.row('m1')
        self.assertGreaterEqual(row['weight'], engine.cfg['delete_below'])
        self.assertIsNone(row['deleted_at'])
        engine.close()

    def test_explicit_deletion_still_works_under_a_preset(self):
        """Slow forgetting must not make memories undeletable on purpose."""
        engine = self.make('evergreen')
        engine.process_event(event(), NOW)
        with engine.db:
            engine.db.execute('UPDATE memories SET deleted_at=? WHERE id=?',
                              (NOW - 40 * DAY, 'm1'))
        # Purge only touches tombstones the index already confirmed; an
        # unacknowledged delete is deliberately held back.
        for job in engine.outbox():
            engine.acknowledge(job['id'])
        self.assertEqual(engine.purge(NOW), 1)
        row = engine.row('m1')
        self.assertEqual(row['purged'], 1)
        self.assertEqual(row['body'], '')


class InjectionGateTests(unittest.TestCase):
    """Forgetting less is not "proactively bringing up everything"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_sensitive_memory_is_still_gated_under_every_preset(self):
        for name in ('default', 'slow-forget', 'evergreen'):
            cfg = load_config() if name == 'default' else load_config(preset=name)
            engine = MemoryEngine(Path(self.tmp.name) / (name + '.db'), cfg)
            self.addCleanup(engine.close)  # Windows keeps the file locked otherwise.
            engine.process_event(event(eid='es1', mid='s1', emotion={
                'valence': -0.8, 'arousal': 0.9, 'confidence': 1.0, 'sensitive': True}), NOW)
            self.assertFalse(engine.injection_allowed('s1', NOW),
                             'preset %s must not unblock sensitive memories' % name)
            self.assertTrue(engine.injection_allowed('s1', NOW, user_initiated=True),
                            'a user asking about their own past always gets it back')

    def test_retrieval_budget_is_untouched_by_the_presets(self):
        """Slow forgetting changes storage. The 1200-token injection budget and
        the relevance/memory weighting stay exactly where they were."""
        for name in list_presets():
            cfg = load_config(preset=name)
            self.assertEqual(cfg['rag']['context_token_budget'], 1200)
            self.assertEqual(cfg['rag']['relevance_weight'], 0.8)
            self.assertEqual(cfg['rag']['memory_weight'], 0.2)
            self.assertTrue(cfg['rag']['require_rerank'])


class CliTests(unittest.TestCase):
    """The presets have to be usable from the command line, not just the API."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'cli.db')

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ENGINE)] + list(args),
                              capture_output=True, text=True, encoding='utf-8')

    def test_presets_command_needs_no_database(self):
        out = self.run_cli('presets')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('slow-forget', out.stdout)
        self.assertIn('evergreen', out.stdout)

    def test_init_with_a_preset_succeeds(self):
        for name in ('slow-forget', 'evergreen'):
            out = self.run_cli('--db', self.db, '--preset', name, 'init')
            self.assertEqual(out.returncode, 0, out.stderr)

    def test_unknown_preset_fails_with_a_non_zero_exit(self):
        out = self.run_cli('--db', self.db, '--preset', 'nope', 'init')
        self.assertNotEqual(out.returncode, 0)

    def test_missing_db_is_rejected_for_real_commands(self):
        out = self.run_cli('active')
        self.assertNotEqual(out.returncode, 0)

    def test_cli_maintain_applies_the_preset_end_to_end(self):
        """The CLI path and the API path must agree."""
        out = self.run_cli('--db', self.db, '--preset', 'evergreen', 'init')
        self.assertEqual(out.returncode, 0, out.stderr)
        out = self.run_cli('--db', self.db, '--preset', 'evergreen', 'active')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [])


class CalibrationWiringTests(unittest.TestCase):
    """The measured threshold must reach the chat path, not just the document.

    ``calibration.min_relevance`` used to be metadata that nothing read, so a
    preset declared 0.51 / 0.53 while every demo kept hardcoding 0.45 -- a
    knob that looked load-bearing and did nothing.
    """

    def test_preset_calibration_reaches_the_config(self):
        self.assertAlmostEqual(load_config(preset='slow-forget')['preset']['min_relevance'], 0.51)
        self.assertAlmostEqual(load_config(preset='evergreen')['preset']['min_relevance'], 0.53)

    def test_the_value_rides_outside_the_retrieval_layer(self):
        """A preset may not rewrite ``rag``; the floor travels as metadata."""
        base = load_config()
        for name in ('slow-forget', 'evergreen'):
            self.assertEqual(load_config(preset=name)['rag'], base['rag'],
                             '%s must not touch the retrieval layer' % name)

    def test_default_config_has_no_preset_section(self):
        self.assertNotIn('preset', load_config())


if __name__ == '__main__':
    unittest.main(verbosity=2)
