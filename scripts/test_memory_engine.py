"""Behavioral tests run only against fresh temporary databases; no network."""
import copy
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from memory_engine import MemoryEngine, load_config, decayed, local_day, charge_at

NOW=1_700_000_000.0


def event(eid='e1',mid='m1',weight=50,visibility='shared'):
    return {'id':eid,'kind':'human_message','peer_id':'agent:private:qq10001:sender10001',
        'sender_id':'qq:10001','visibility':visibility,'content':'new fact '+eid,'actions':[{
        'kind':'add','id':mid,'weight':weight,'body':'原始详细叙述。'*120,
        'facts':[
            {'subject':'qq:10001','predicate':'likes','object':'甜奶茶','negated':True,
             'time':None,'source_ids':[eid],'core':True},
            {'subject':'qq:10001','predicate':'tried','object':'乌龙茶','negated':False,
             'time':'时间未知','source_ids':[eid],'core':False}]}]}


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.cfg=load_config()
        self.engine=MemoryEngine(Path(self.tmp.name)/'test.db',self.cfg)

    def tearDown(self):
        self.engine.close();self.tmp.cleanup()

    def add(self,**kwargs):
        e=event(**kwargs);self.engine.process_event(e,NOW);return e

    def remind(self,eid='reminder-1',mid='m1',now=NOW+3600,sender='qq:10001'):
        e={'id':eid,'kind':'human_message','peer_id':'a:p:'+sender,'sender_id':sender,
           'content':'我再确认这条事实 '+eid,'actions':[{'kind':'remind','id':mid}]}
        return self.engine.process_event(e,now)

    def test_curve_units_and_type_difference(self):
        self.add();row=self.engine.row('m1')
        self.assertAlmostEqual(decayed(row,NOW+720*3600,self.cfg)/50,math.exp(-.72))
        note={**row,'curve':'note'}
        self.assertLess(decayed(note,NOW+86400,self.cfg),decayed(row,NOW+86400,self.cfg))

    def test_corrected_profile_is_independent_of_tick_frequency(self):
        self.add();row={**self.engine.row('m1'),'profile':'important','weight':80}
        whole=decayed(row,NOW+24*3600,self.cfg)
        half=decayed(row,NOW+12*3600,self.cfg)
        split=decayed({**row,'weight':half,'last_decay':NOW+12*3600},NOW+24*3600,self.cfg)
        self.assertAlmostEqual(whole,split)
        legacy=copy.deepcopy(self.cfg);legacy['formula_mode']='legacy_tick'
        self.assertNotAlmostEqual(decayed(row,NOW+24*3600,legacy),whole)

    def test_protected_pinned_and_permanent_do_not_fade(self):
        self.add();row=self.engine.row('m1')
        for protected in ({**row,'weight':90},{**row,'weight':5,'pinned':1}):
            self.assertEqual(decayed(protected,NOW+1000*86400,self.cfg),protected['weight'])
        self.assertEqual(decayed({**row,'profile':'permanent'},NOW+86400,self.cfg),101)

    def test_low_weight_deletes_only_below_ten_and_removes_active_projection(self):
        self.cfg['delete_probability_per_day']=1
        self.add(weight=9)
        self.add(eid='e2',mid='edge',weight=10)
        stats=self.engine.apply_plan(self.engine.plan(NOW))
        self.assertEqual(stats['deleted'],1)
        self.assertIsNotNone(self.engine.row('m1')['deleted_at'])
        self.assertEqual([r['id'] for r in self.engine.active()],['edge'])
        ops=[(j['memory_id'],j['operation']) for j in self.engine.outbox()]
        self.assertEqual([op for mid,op in ops if mid=='m1'],['upsert','delete'])
        # The W=10 edge case is unchanged by maintenance: no revision churn, no resync.
        self.assertEqual([op for mid,op in ops if mid=='edge'],['upsert'])

    def test_preview_retry_and_restart_never_reroll_same_day(self):
        self.add(weight=5)
        before=self.engine.plan(NOW);again=self.engine.plan(NOW)
        self.assertEqual(before,again)
        dbpath=Path(self.tmp.name)/'test.db';self.engine.close()
        self.engine=MemoryEngine(dbpath,self.cfg)
        self.assertEqual(self.engine.plan(NOW),before)
        self.engine.apply_plan(before)
        self.assertEqual(self.engine.plan(NOW)['actions'],[])

    def test_compression_preserves_negation_identity_time_and_provenance(self):
        self.add(weight=80)
        full=self.engine.row('m1')['text']
        self.engine.apply_plan(self.engine.plan(NOW+1000*3600))
        row=self.engine.row('m1');text=json.loads(row['text'])
        self.assertLess(len(row['text']),len(full))
        self.assertEqual(row['tier'],'core')
        fact=text['facts'][0]
        self.assertEqual(fact['subject'],'qq:10001');self.assertTrue(fact['negated'])
        self.assertIsNone(fact['time']);self.assertEqual(fact['source_ids'],['e1'])
        self.assertEqual(len(json.loads(row['facts'])),2)  # Details still recoverable, not in current RAG text.

    def test_fresh_reminder_boosts_and_duplicate_transport_does_not(self):
        self.add(weight=5)
        self.remind()
        expected=5*math.exp(-.001)+10
        self.assertAlmostEqual(self.engine.row('m1')['weight'],expected)
        self.assertEqual(self.remind()['status'],'duplicate')
        self.assertAlmostEqual(self.engine.row('m1')['weight'],expected)
        # A bridge replay can even change the event ID; 120s hash guard still rejects it.
        replay={'id':'bridge-new-id','kind':'human_message','peer_id':'a:p:qq:10001','sender_id':'qq:10001',
                'content':'我再确认这条事实 reminder-1','actions':[{'kind':'remind','id':'m1'}]}
        self.assertEqual(self.engine.process_event(replay,NOW+3601)['status'],'duplicate')

    def test_new_reminder_wins_over_old_nightly_delete_plan(self):
        self.cfg['delete_probability_per_day']=1;self.add(weight=5)
        old=self.engine.plan(NOW+3600)
        self.remind(now=NOW+3601)
        stats=self.engine.apply_plan(old)
        self.assertEqual(stats['skipped_changed'],1)
        self.assertIsNone(self.engine.row('m1')['deleted_at'])
        self.assertGreater(self.engine.row('m1')['weight'],10)

    def test_reminder_restores_projection_when_weight_crosses_tier(self):
        self.add(weight=59)
        self.assertNotIn('detail',json.loads(self.engine.row('m1')['text']))
        self.remind()
        self.assertIn('detail',json.loads(self.engine.row('m1')['text']))

    def test_indexing_and_unconfirmed_delivery_never_reinforce(self):
        self.add();before=self.engine.row('m1')['weight']
        self.engine.active();self.engine.outbox()
        self.assertEqual(self.engine.row('m1')['weight'],before)
        use={'id':'delivery1','kind':'delivery','peer_id':'reply:qq10001','requester_id':'qq:10001',
             'content':'reply','delivered':False,'actions':[{'kind':'used','id':'m1','revision':1}]}
        with self.assertRaises(ValueError):self.engine.process_event(use,NOW+1)
        use['delivered']=True
        self.engine.process_event(use,NOW+1)
        weight=self.engine.row('m1')['weight']
        self.assertAlmostEqual(weight,50*math.exp(-.001/3600)+1)
        self.assertEqual(self.engine.process_event(use,NOW+2)['status'],'duplicate')
        self.assertEqual(self.engine.row('m1')['weight'],weight)

    def test_private_stays_private_during_reminder_and_recycle_recovery(self):
        self.cfg['delete_probability_per_day']=1;self.add(weight=5,visibility='private')
        self.assertEqual(self.engine.active('qq:10002'),[])
        self.engine.apply_plan(self.engine.plan(NOW))
        with self.assertRaises(ValueError):self.remind(sender='qq:10002')
        self.remind()
        self.assertEqual(self.engine.row('m1')['visibility'],'private')
        self.assertEqual(self.engine.active('qq:10002'),[])
        self.assertEqual(len(self.engine.active('qq:10001')),1)

    def test_purge_waits_for_real_index_ack_and_prevents_invented_recovery(self):
        self.cfg['delete_probability_per_day']=1;self.add(weight=5)
        self.engine.apply_plan(self.engine.plan(NOW))
        self.assertEqual(self.engine.purge(NOW+8*86400),0)
        # Simulated adapter acknowledgments in an isolated test only.
        for job in self.engine.outbox():self.engine.acknowledge(job['id'])
        self.assertEqual(self.engine.purge(NOW+8*86400),1)
        self.assertEqual(self.engine.row('m1')['body'],'')
        self.assertEqual(self.engine.outbox(),[])
        with self.assertRaises(ValueError):self.remind(now=NOW+9*86400)

    def test_event_failure_rolls_back_all_actions_and_receipt(self):
        e=event();e['actions'].append({'kind':'remind','id':'missing'})
        with self.assertRaises(ValueError):self.engine.process_event(e,NOW)
        self.assertIsNone(self.engine.row('m1'))
        self.assertEqual(self.engine.outbox(),[])
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],0)

    def test_modified_model_plan_cannot_force_deletion_or_change_fact_text(self):
        self.add(weight=80)
        plan=self.engine.plan(NOW)
        plan['actions'][0]['delete']=True
        plan['actions'][0]['text']='篡改后的事实'
        with self.assertRaises(ValueError):self.engine.apply_plan(plan)
        self.assertIsNone(self.engine.row('m1')['deleted_at'])
        self.assertNotIn('篡改',self.engine.row('m1')['text'])

    def test_local_date_uses_shanghai_boundary(self):
        self.assertEqual(local_day(16*3600,self.cfg),'1970-01-02')

    def test_backwards_clock_does_not_schedule_extra_deletion(self):
        self.add(weight=5)
        self.assertEqual(self.engine.plan(NOW-86400)['actions'],[])


class EmotionTests(unittest.TestCase):
    """L1 frozen snapshot / L2 fading charge / L3 versioned appraisal."""

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.cfg=load_config()
        self.engine=MemoryEngine(Path(self.tmp.name)/'test.db',self.cfg)

    def tearDown(self):
        self.engine.close();self.tmp.cleanup()

    def add_emotion(self,eid='em1',mid='emo1',weight=50,arousal=.9,valence=-.8,confidence=1.0,
                    sensitive=False,visibility='shared'):
        e=event(eid=eid,mid=mid,weight=weight,visibility=visibility)
        e['actions'][0]['emotion']={'valence':valence,'arousal':arousal,'confidence':confidence,
                                    'sensitive':sensitive}
        self.engine.process_event(e,NOW);return e

    def appraise(self,eid,mid,text,now,sender='qq:10001',kind='human_message'):
        return self.engine.process_event({'id':eid,'kind':kind,'peer_id':'a:p:'+sender,'sender_id':sender,
            'content':'我对那段经历的新理解 '+eid,'actions':[{'kind':'appraise','id':mid,'text':text}]},now)

    def test_absent_emotion_leaves_projection_and_decay_untouched(self):
        self.add_emotion(confidence=0.0)  # Unsupported LLM guess collapses to neutral.
        row=self.engine.row('emo1')
        self.assertEqual(row['weight'],50)
        self.assertNotIn('emotion',json.loads(row['text']))
        self.assertAlmostEqual(decayed(row,NOW+720*3600,self.cfg)/50,math.exp(-.72))

    def test_encoding_boost_never_grants_permanent_protection(self):
        self.add_emotion(arousal=1.0,valence=.5)
        row=self.engine.row('emo1')
        self.assertEqual(row['weight'],self.cfg['protected_weight']-1)  # 50*1.8=90 -> capped at 89
        self.assertLess(decayed(row,NOW+24*3600,self.cfg),row['weight'])

    def test_emotion_slows_decay_and_split_ticks_match_exactly(self):
        self.add_emotion()
        row=self.engine.row('emo1')
        whole=decayed(row,NOW+240*3600,self.cfg)
        half=decayed(row,NOW+120*3600,self.cfg)
        split=decayed({**row,'weight':half,'last_decay':NOW+120*3600},NOW+240*3600,self.cfg)
        self.assertAlmostEqual(whole,split,places=9)
        plain=decayed({**row,'emotion':''},NOW+240*3600,self.cfg)
        self.assertGreater(whole,plain)

    def test_emotion_split_over_24_ticks_matches_single_tick(self):
        self.add_emotion()
        row=self.engine.row('emo1')
        whole=decayed(row,NOW+24*3600,self.cfg)
        cur=dict(row)
        for i in range(1,25):
            cur={**cur,'weight':decayed(cur,NOW+i*3600,self.cfg),'last_decay':NOW+i*3600}
        self.assertAlmostEqual(whole,cur['weight'],places=12)

    def test_emotion_shield_expires_and_rate_returns_to_baseline(self):
        self.add_emotion()
        row=self.engine.row('emo1');far=NOW+3650*86400
        emo={**row,'last_decay':far,'weight':50.0}
        plain={**emo,'emotion':''}
        self.assertAlmostEqual(decayed(emo,far+3600,self.cfg),decayed(plain,far+3600,self.cfg),delta=1e-9)

    def test_snapshot_is_frozen_across_maintenance_and_appraisal(self):
        self.add_emotion()
        before=self.engine.emotion_view('emo1')['snapshot']
        self.engine.apply_plan(self.engine.plan(NOW+500*3600))
        self.appraise('ap1','emo1','那时我把一次批评理解成了全盘否定',NOW+501*3600)
        self.assertEqual(self.engine.emotion_view('emo1')['snapshot'],before)

    def test_appraisal_is_versioned_and_never_boosts_weight(self):
        self.add_emotion();before=self.engine.row('emo1')
        self.appraise('ap1','emo1','第一版理解',NOW+3600)
        row=self.engine.row('emo1')
        self.assertAlmostEqual(row['weight'],decayed(before,NOW+3600,self.cfg))
        view=self.engine.emotion_view('emo1')
        self.assertEqual(view['appraisal']['revision'],1)
        self.assertEqual(view['appraisal']['current'],'第一版理解')
        self.appraise('ap2','emo1','第二版理解',NOW+7200)
        view=self.engine.emotion_view('emo1')
        self.assertEqual(view['appraisal']['revision'],2)
        self.assertEqual(view['appraisal']['current'],'第二版理解')
        self.assertEqual([h['text'] for h in view['appraisal']['history']],['第一版理解'])
        self.assertEqual(view['appraisal']['history'][0]['evidence'],'ap1')
        # L3 current interpretation is projected, history stays in the authority DB.
        text=json.loads(self.engine.row('emo1')['text'])
        self.assertEqual(text['emotion']['appraisal'],'第二版理解')
        self.assertNotIn('第一版理解',self.engine.row('emo1')['text'])

    def test_appraisal_requires_fresh_human_evidence_and_acl(self):
        self.add_emotion(visibility='private')
        with self.assertRaises(ValueError):self.appraise('apX','emo1','越权改写',NOW+3600,sender='qq:10002')
        with self.assertRaises(ValueError):self.appraise('apY','emo1','回执伪造',NOW+3600,kind='delivery')
        with self.assertRaises(ValueError):self.appraise('apZ','emo1','   ',NOW+3600)
        self.assertEqual(self.engine.emotion_view('emo1')['appraisal']['revision'],0)

    def test_human_reminder_recharges_but_own_retrieval_does_not(self):
        self.add_emotion();t=NOW+100*86400
        before=self.engine.row('emo1');charge_before=charge_at(before,t,self.cfg)
        self.assertLess(charge_before,.9)
        self.engine.process_event({'id':'d1','kind':'delivery','peer_id':'reply:q','requester_id':'qq:10001',
            'content':'reply','delivered':True,'actions':[{'kind':'used','id':'emo1','revision':1}]},t)
        self.assertAlmostEqual(self.engine.emotion_view('emo1',t)['charge'],charge_before)
        row=self.engine.row('emo1');charge_t2=charge_at(row,t+3600,self.cfg)
        self.engine.process_event({'id':'r1','kind':'human_message','peer_id':'a:p:qq:10001',
            'sender_id':'qq:10001','content':'我又想起那次难过','actions':[{'kind':'remind','id':'emo1'}]},t+3600)
        recharged=self.engine.emotion_view('emo1',t+3600)['charge']
        self.assertAlmostEqual(recharged,charge_t2+.7*(.9-charge_t2))
        self.assertLessEqual(recharged,.9)

    def test_zero_charge_never_erases_history_or_blocks_recall(self):
        self.cfg['delete_probability_per_day']=0
        self.add_emotion();far=NOW+4000*86400
        self.assertAlmostEqual(charge_at(self.engine.row('emo1'),far,self.cfg),0,delta=1e-9)
        self.engine.apply_plan(self.engine.plan(far))
        row=self.engine.row('emo1');text=json.loads(row['text'])
        self.assertEqual(text['emotion']['valence_at_event'],-.8)
        self.assertNotIn('charge',text['emotion'])  # Charge is settled live, not projected.
        self.assertAlmostEqual(self.engine.emotion_view('emo1',far)['charge'],0,delta=1e-9)
        self.assertEqual(len(text['facts']),1)  # Core fact atom survives.
        self.assertIn('emo1',[r['id'] for r in self.engine.active('qq:10001')])
        self.assertTrue(self.engine.injection_allowed('emo1',far,user_initiated=True))

    def test_sensitive_and_high_charge_gate_only_proactive_injection(self):
        self.add_emotion(mid='s1',eid='es1',sensitive=True)
        self.assertFalse(self.engine.injection_allowed('s1',NOW))
        self.assertTrue(self.engine.injection_allowed('s1',NOW,user_initiated=True))
        self.add_emotion(mid='s2',eid='es2')  # Not flagged, but high negative charge.
        self.assertFalse(self.engine.injection_allowed('s2',NOW))
        later=NOW+400*86400
        self.assertTrue(self.engine.injection_allowed('s2',later))
        self.add_emotion(mid='s3',eid='es3',valence=.9)  # Positive high charge is not gated.
        self.assertTrue(self.engine.injection_allowed('s3',NOW))

    def test_mood_score_is_optional_and_backward_compatible(self):
        base=self.cfg['rag']['relevance_weight']*.9+self.cfg['rag']['memory_weight']*(50/101)
        self.assertAlmostEqual(self.engine.rank_score(.9,50),base)
        self.assertAlmostEqual(self.engine.rank_score(.9,50,1.0),.9*base+.1)
        self.assertAlmostEqual(self.engine.rank_score(.9,50,5.0),.9*base+.1)  # Clamped.

    def test_emotion_fields_are_validated(self):
        for bad in ({'arousal':2},{'confidence':-.1},{'valence':'x'},{'arousal':True}):
            payload={'valence':.5,'arousal':.5,'confidence':1.0};payload.update(bad)
            e=event(eid='bad'+str(bad),mid='bad');e['actions'][0]['emotion']=payload
            with self.assertRaises(ValueError):self.engine.process_event(e,NOW)


class OpsTests(unittest.TestCase):
    """Durability and operational hygiene: WAL, outbox lifecycle, no-op settling."""

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.cfg=load_config()
        self.engine=MemoryEngine(Path(self.tmp.name)/'test.db',self.cfg)

    def tearDown(self):
        self.engine.close();self.tmp.cleanup()

    def test_unchanged_rows_do_not_churn_revision_or_outbox(self):
        self.add(weight=90)  # Protected: decay is a no-op, so maintenance must be too.
        baseline=len(self.engine.outbox())
        stats=self.engine.apply_plan(self.engine.plan(NOW+86400))
        self.assertEqual(stats['unchanged'],1)
        self.assertEqual(stats['processed'],0)
        self.assertEqual(self.engine.row('m1')['revision'],1)
        self.assertEqual(self.engine.row('m1')['maintenance_day'],local_day(NOW+86400,self.cfg))
        self.assertEqual(len(self.engine.outbox()),baseline)
        again=self.engine.apply_plan(self.engine.plan(NOW+86400))
        self.assertEqual(again['unchanged'],0)  # Same-day idempotency still holds.

    def test_archived_memories_do_not_queue_delete_every_night(self):
        self.add()
        self.engine.db.execute("UPDATE memories SET profile='archived' WHERE id='m1'")
        self.engine.db.commit()
        baseline=len(self.engine.outbox())
        for night in range(3):
            self.engine.apply_plan(self.engine.plan(NOW+night*86400))
        self.assertEqual(len(self.engine.outbox()),baseline)
        self.assertEqual(self.engine.row('m1')['revision'],1)

    def test_changed_rows_still_bump_and_queue(self):
        self.add()
        stats=self.engine.apply_plan(self.engine.plan(NOW+3600))
        self.assertEqual(stats['processed'],1)
        self.assertEqual(self.engine.row('m1')['revision'],2)
        self.assertEqual(len(self.engine.outbox()),2)

    def add(self,**kwargs):
        e=event(**kwargs);self.engine.process_event(e,NOW);return e

    def test_outbox_fail_dead_letter_and_retry(self):
        self.add();job=self.engine.outbox()[0]
        for attempt in range(1,self.cfg['outbox_max_attempts']):
            result=self.engine.fail(job['id'],'adapter 503')
            self.assertFalse(result['dead'])
        self.assertTrue(self.engine.fail(job['id'],'adapter 503')['dead'])
        self.assertEqual(self.engine.outbox(),[])  # Dead letters leave the live queue.
        self.assertEqual(len(self.engine.dead_letters()),1)
        self.assertIn('503',self.engine.dead_letters()[0]['last_error'])
        self.engine.retry_dead(job['id'])
        self.assertEqual(len(self.engine.outbox()),1)
        self.assertEqual(self.engine.dead_letters(),[])
        with self.assertRaises(ValueError):self.engine.fail(999999,'no such event')

    def test_sweep_acked_respects_retention_and_never_touches_live(self):
        self.add();job=self.engine.outbox()[0]
        self.engine.acknowledge(job['id'])
        later=time.time()+8*86400  # created_at uses the real clock, not replay time.
        self.assertEqual(self.engine.sweep_acked(later),1)  # dry run
        self.assertEqual(len(self.engine.db.execute('SELECT * FROM outbox').fetchall()),1)
        self.assertEqual(self.engine.sweep_acked(later,dry_run=False),1)
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)

    def test_lottery_and_plan_signing_use_distinct_seeds(self):
        seeds={r[0]:r[1] for r in self.engine.db.execute('SELECT key,value FROM meta')}
        self.assertIn('plan_seed',seeds)
        self.assertNotEqual(seeds['plan_seed'],seeds['lottery_seed'])

    def test_now_flag_requires_replay_acknowledgment(self):
        script=Path(__file__).resolve().parent/'memory_engine.py'
        db=str(Path(self.tmp.name)/'cli.db')
        env=dict(os.environ);env.pop('ES_MEMORY_REPLAY',None)
        denied=subprocess.run([sys.executable,str(script),'--db',db,'--now','1700000000','maintain'],
                              capture_output=True,text=True,env=env)
        self.assertNotEqual(denied.returncode,0)
        env['ES_MEMORY_REPLAY']='1'
        allowed=subprocess.run([sys.executable,str(script),'--db',db,'--now','1700000000','maintain'],
                               capture_output=True,text=True,env=env)
        self.assertEqual(allowed.returncode,0,allowed.stderr)

    def test_dedup_normalization_modes(self):
        cfg=dict(self.cfg);cfg['dedup_normalize']='nfkc_casefold'
        strict=MemoryEngine(Path(self.tmp.name)/'nfkc.db',cfg)
        e=event();e['content']='Ｈｅｌｌｏ　Ｗｏｒｌｄ'
        strict.process_event(e,NOW)
        replay=dict(e);replay['id']='bridge-replay';replay['content']='hello world'
        self.assertEqual(strict.process_event(replay,NOW+60)['status'],'duplicate')
        strict.close()
        e2=event(eid='orig');e2['content']='Ｈｅｌｌｏ　Ｗｏｒｌｄ'
        self.engine.process_event(e2,NOW)
        replay2={'id':'bridge2','kind':'human_message','peer_id':e2['peer_id'],'sender_id':'qq:10001',
                 'content':'hello world','actions':[{'kind':'remind','id':'m1'}]}
        self.assertEqual(self.engine.process_event(replay2,NOW+60)['status'],'ok')  # Default stays conservative.

    def test_wal_mode_is_enabled(self):
        mode=self.engine.db.execute('PRAGMA journal_mode').fetchone()[0]
        self.assertEqual(mode,'wal')

    def test_plan_and_apply_scale_to_thousands(self):
        with self.engine.db:
            for i in range(3000):
                self.engine.db.execute('''INSERT INTO memories(id,body,facts,source_ids,weight,curve,profile,
                    visibility,owner,created_at,last_decay,revision,tier,text,emotion)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'')''',
                    (f'bulk{i}','body','[]','[]',50.0,'fact','standard','shared','',NOW,NOW,1,'facts',''))
        start=time.time()
        plan=self.engine.plan(NOW+3600)
        stats=self.engine.apply_plan(plan)
        elapsed=time.time()-start
        self.assertEqual(stats['processed'],3000)
        self.assertLess(elapsed,15)  # Generous ceiling; guards against accidental O(N^2).


if __name__=='__main__':unittest.main(verbosity=2)
