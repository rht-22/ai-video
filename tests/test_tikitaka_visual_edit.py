import copy
from pathlib import Path
import pytest
from app.tikitaka.common import Job
from app.tikitaka import visual_edit as ve


def clip(a,z,**kwargs):
    return dict(clip_start_sec=a,clip_end_sec=z,beat=0,role='build',span_ids=['sp0'],
                use_original_audio=not kwargs.get('cover',False),**kwargs)


def setup(tmp_path):
    f=tmp_path/'source.mp4';f.write_bytes(b'source')
    return Job(f,tmp_path,'test'),{'source':{'duration_sec':30},'span_candidates':[{'id':'sp0','t_in':0,'t_out':30}]}


def test_short_interior_shots_are_removed_without_changing_narration_clock(tmp_path):
    job,grid=setup(tmp_path);p={'timeline':[clip(0,3.6,cover=True,playback_speed=1.2)]}
    resource={'tts_cue_files':[{'path':'same.mp3','cue':{'start_sec':0,'end_sec':3,'duration_sec':3,'beat':0,'text':'keep every word'}}]}
    before=copy.deepcopy(p)
    q,segs,res,a=ve.run(job,p,[],resource,grid,boundaries=[.2,2.4,2.6])
    assert p==before and sum(ve.frames(c) for c in q['timeline'])==90
    assert not a['remaining'] and len(a['before'])==2
    assert res['tts_cue_files'][0]['path']=='same.mp3'
    assert res['tts_cue_files'][0]['cue']['text']=='keep every word'
    assert res['tts_cue_files'][0]['cue']['duration_sec']==3
    assert all(1<=c['playback_speed']<=1.2 for c in q['timeline'])
    assert all(not c.get('hold_sec') for c in q['timeline'])


def test_spoken_short_shot_is_not_deleted_even_when_speaker_is_unchanged(tmp_path):
    job,grid=setup(tmp_path);p={'timeline':[clip(0,3)]};segments=[{'start_sec':0,'end_sec':2.9,'text':'every word','speaker':'A'}]
    q,s,r,a=ve.run(job,p,segments,{},grid,boundaries=[.2])
    assert q['timeline']==p['timeline'] and s==segments and a['blocked']
    assert a['remaining'][0]['start']==0 and a['remaining'][0]['end']==6


def test_silent_short_tail_removed_and_all_later_clocks_remapped(tmp_path):
    job,grid=setup(tmp_path);p={'timeline':[clip(0,1.5),clip(3,4,cover=True)]}
    segments=[{'start_sec':.1,'end_sec':1.,'text':'hello','speaker':'A'}]
    resources={'tts_cue_files':[{'path':'same.mp3','cue':{'start_sec':1.5,'end_sec':2.5,'duration_sec':1.,'beat':0,'text':'narration'}}],
               'tts_caption_segments':[{'start_sec':1.6,'end_sec':2.4,'text':'caption'}]}
    q,s,r,a=ve.run(job,p,segments,resources,grid,boundaries=[1.3],silent_intervals=[(1.3,1.5)])
    assert a['removed_output']==[{'start':1.3,'end':1.5}]
    assert sum(ve.frames(c) for c in q['timeline'])==69
    assert s==segments
    cue=r['tts_cue_files'][0]['cue'];assert cue['start_sec']==pytest.approx(1.3)
    assert cue['duration_sec']==1. and cue['text']=='narration'
    assert r['tts_caption_segments'][0]['start_sec']==pytest.approx(1.4)


def test_missing_transcript_manual_cut_and_tts_are_not_silence(tmp_path):
    job,grid=setup(tmp_path)
    for c,segments,res in [
        (clip(0,1),[],{}),
        (clip(0,1,reframe={'mode':'fixed','x':200}),[{'start_sec':.05,'end_sec':.5,'text':'hi'}],{}),
        (clip(0,1),[{'start_sec':.05,'end_sec':.5,'text':'hi'}],{'tts_cue_files':[{'cue':{'start_sec':.8,'end_sec':1.,'text':'keep'}}]})]:
        q,s,r,a=ve.run(job,{'timeline':[c]},segments,res,grid,boundaries=[.8])
        assert not a['removed_output'] and a['blocked']


def test_technical_chunk_boundary_is_not_a_flash():
    p={'timeline':[clip(0,.1,cover=True),clip(.1,1,cover=True)]}
    shots=ve.shots(p,[])
    assert len(shots)==1 and shots[0]['end']==30


def test_retime_and_hold_use_real_output_exposure():
    p={'timeline':[clip(0,.25,cover=True,hold_sec=.75)]}
    assert ve.shots(p,[])[0]['end']==30
    p={'timeline':[clip(0,.45,cover=True,playback_speed=1.2)]}
    assert ve.shots(p,[])[0]['end']==11


def test_source_tile_cache_is_shared_by_different_candidate_plans(tmp_path,monkeypatch):
    job,grid=setup(tmp_path);calls=[]
    class Result:returncode=0;stderr='pts_time:2.0 '
    def ff(cmd,**kwargs):calls.append(cmd);return Result()
    monkeypatch.setattr(ve.subprocess,'run',ff);monkeypatch.setattr(ve,'find_bin',lambda x:x)
    assert ve.scan(job,{'timeline':[clip(1,3)]},30)==[2.]
    assert ve.scan(job,{'timeline':[clip(4,8)]},30)==[2.]
    assert len(calls)==1


def test_insufficient_cover_capacity_is_reported_not_slowed_or_frozen(tmp_path):
    job,grid=setup(tmp_path);grid['span_candidates'][0]['t_out']=1.
    p={'timeline':[clip(0,1,cover=True)]}
    q,s,r,a=ve.run(job,p,[],{},grid,boundaries=[.2])
    assert q['timeline']==p['timeline'] and a['blocked']


def test_frame_surgery_preserves_untouched_retimed_clip_clock():
    p={'timeline':[clip(0,1),clip(3,4.2,cover=True,playback_speed=1.2),clip(5,6)]}
    q=ve.remove_frames(p,[(24,30)])
    assert q['timeline'][1]==p['timeline'][1]
    assert sum(ve.frames(c) for c in q['timeline'])==sum(ve.frames(c) for c in p['timeline'])-6


def test_review_does_not_approve_itself_and_keeps_require_exact_identity(tmp_path):
    job,grid=setup(tmp_path);p={'timeline':[clip(0,1)]}
    q,_,_,audit=ve.run(job,p,[{'start_sec':0,'end_sec':1,'text':'all words'}],{},grid,boundaries=[.2])
    original=copy.deepcopy(q)
    report=ve.review(job,q,audit,mode='preview')
    assert report['blocked'] and report['review_items'][0]['status']=='pending'
    assert job.has('visual_review.md')
    with pytest.raises(ValueError,match='시각 편집 검토 필요'):
        ve.review(job,q,audit,mode='strict')
    issue=report['review_items'][0]
    job.save('visual_review_decisions.json',{'source_identity':report['source_identity'],
        'decisions':[{'id':issue['id'],'action':'keep','reason':'사람이 영상에서 확인함'}]})
    accepted=ve.review(job,q,audit,mode='strict')
    assert not accepted['blocked'] and accepted['review_items'][0]['status']=='accepted'
    assert q==original  # accepting keeps the original video/audio plan
    altered=copy.deepcopy(audit);altered['remaining'][0]['parts'][0]['out']+=.01
    with pytest.raises(ValueError):ve.review(job,q,altered,mode='strict')
    job.source.write_bytes(b'another source')
    with pytest.raises(ValueError):ve.review(job,q,audit,mode='strict')


def test_unfit_crop_is_reviewed_even_without_short_shots(tmp_path):
    job,grid=setup(tmp_path);p={'timeline':[clip(0,1,cover=True)]}
    audit={'remaining':[],'framing_issues':[{'clip':0,'reason':'핵심 영역 재선택 필요'}]}
    with pytest.raises(ValueError):ve.review(job,p,audit,mode='strict')
    assert job.load('visual_edit.json')['review_items'][0]['time']==[0,1]


def test_clean_narration_is_not_retimed(tmp_path):
    job,grid=setup(tmp_path);p={'timeline':[clip(0,1.2,cover=True,playback_speed=1.2)]}
    q,_,_,a=ve.run(job,p,[],{},grid,boundaries=[])
    assert q['timeline']==p['timeline'] and not a['changes']


def test_borrowing_cover_tail_cannot_repeat_other_surviving_piece():
    cs=[clip(0,1.2,cover=True),clip(1.3,2,cover=True)]
    assert ve._allocate_cover(cs,[.2],{'sp0':{'t_in':0,'t_out':2}},[(0,.2)],[]) is None


def test_subtitle_gap_does_not_authorize_audio_deletion(tmp_path):
    job,grid=setup(tmp_path);p={'timeline':[clip(0,1.5)]}
    q,_,_,a=ve.run(job,p,[{'start_sec':.1,'end_sec':1.,'text':'transcribed'}],{},grid,boundaries=[1.3])
    assert q['timeline']==p['timeline'] and a['blocked'] and not a['removed_output']


def test_short_unique_evidence_is_retained_even_if_same_span_has_other_footage(tmp_path):
    job,grid=setup(tmp_path);p={'timeline':[clip(0,1.2,cover=True,playback_speed=1.2)]}
    q,_,_,a=ve.run(job,p,[],{},grid,boundaries=[.2],protected_spans=['sp0'])
    assert q['timeline']==p['timeline'] and a['blocked']


def test_face_scan_failure_is_not_a_successful_empty_scan(tmp_path,monkeypatch):
    from app.v3 import safe_zone
    def fail(*a,**kw):raise RuntimeError('decode failed')
    monkeypatch.setattr(safe_zone.subprocess,'run',fail)
    assert safe_zone.edge_faces_in_video(tmp_path/'missing.mp4',300,1400)==[]
    with pytest.raises(RuntimeError,match='decode failed'):
        safe_zone.edge_faces_in_video(tmp_path/'missing.mp4',300,1400,require_success=True)
