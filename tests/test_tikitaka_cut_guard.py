"""Cut guards preserve speech clocks, approved footage and frame budgets."""
import copy
import shutil
import subprocess

import pytest

from app.tikitaka import cut_guard as cg
from app.tikitaka.common import Job


def cut(a,z,frames=None,sid='a'):
    frames = round((z-a)*30) if frames is None else frames
    return {'in':a,'out':z,'dur':frames/30,'playback_speed':(z-a)/(frames/30),
            'src':sid,'span_ids':[sid],'authority':'grid+tts'}


def test_technical_split_is_not_a_flash_and_clean_input_is_unchanged():
    clips=[cut(0,2),cut(2,2.1,3),cut(2.1,4)]
    result,audit=cg.repair_cover(clips,[],{})
    assert audit['status']=='clean'
    assert result==clips
    assert cg.shot_fragments(clips,[])==[]


def test_short_tail_removed_using_speed_slack_without_touching_clock():
    clips=[cut(0,2.2,60)]
    before=copy.deepcopy(clips)
    result,audit=cg.repair_cover(clips,[2.05],{'a':{'t_in':0,'t_out':2.2}})
    assert audit['status']=='repaired'
    assert clips==before
    assert result[-1]['out']==pytest.approx(2.05)
    assert sum(round(c['dur']*30) for c in result)==60
    assert all(1<=c['playback_speed']<=1.2 for c in result)
    assert not cg.shot_fragments(result,[2.05])


def test_v4_head_fragment_uses_other_cover_slack_and_approved_extension():
    clips=[cut(1171.866666667,1172.633333333,23,'a'),
           cut(1177.133333333,1179.,55,'b'),cut(1180.,1181.366666667,40,'c')]
    spans={'a':{'t_in':1171.836,'t_out':1172.656},
           'b':{'t_in':1177.116,'t_out':1179.016},
           'c':{'t_in':1179.976,'t_out':1181.376}}
    result,audit=cg.repair_cover(clips,[1171.937433],spans)
    assert audit['status']=='repaired'
    assert sum(round(c['dur']*30) for c in result)==118
    assert result[0]['in']==pytest.approx(1171.937433)
    assert all(spans[c['src']]['t_in']-1e-6<=c['in']<c['out']<=spans[c['src']]['t_out']+1e-6 for c in result)
    assert all(1<=c['playback_speed']<=1.2 for c in result)


def test_insufficient_footage_is_atomic_no_freeze_no_slowdown():
    clips=[cut(1,2,30)]
    result,audit=cg.repair_cover(clips,[1.1],{'a':{'t_in':1,'t_out':2}})
    assert audit['status']=='needs_reselection'
    assert result==clips


def test_blocked_extension_cannot_fill_missing_frames():
    clips=[cut(1,2,30)]
    result,audit=cg.repair_cover(clips,[1.1],{'a':{'t_in':1,'t_out':2.3}},[(2,2.3)])
    assert audit['status']=='needs_reselection'
    assert result==clips


def test_original_short_interior_shot_requires_review_not_automatic_deletion():
    clips=[cut(0,3,80)]
    result,audit=cg.repair_cover(clips,[1,1.1],{'a':{'t_in':0,'t_out':3}})
    assert audit['status']=='needs_review'
    assert result==clips


def test_whole_table_keeps_speech_tts_paths_text_and_row_starts(tmp_path,monkeypatch):
    table={'fingerprint':'old','rows':[
        {'i':1,'mode':'S','cuts':[cut(5,6)],'text':'대사','t0':0},
        {'i':2,'mode':'N','cuts':[cut(0,2.2,60)],'text':'내레이션','tts':'same.mp3','dur':2,'t0':1},
        {'i':3,'mode':'A','cuts':[cut(7,8)],'text':'반응','t0':3}]}
    before=copy.deepcopy(table)
    monkeypatch.setattr(cg,'refine_boundaries',lambda *a:[2.05,5.9])
    job=Job(tmp_path/'source.mp4',tmp_path,'test')
    result,audit=cg.guard_table(job,table,{'source':{'duration_sec':10},
                              'span_candidates':[{'id':'a','t_in':0,'t_out':2.2}]})
    assert table==before
    assert not audit['blocked']
    assert result['rows'][0]==table['rows'][0]
    assert result['rows'][2]==table['rows'][2]
    assert {k:v for k,v in result['rows'][1].items() if k!='cuts'}=={k:v for k,v in table['rows'][1].items() if k!='cuts'}
    assert audit['remaining'] # S tail flagged, not cut


@pytest.mark.skipif(not shutil.which('ffmpeg'),reason='ffmpeg required')
def test_native_frame_probe_finds_two_frames_and_caches(tmp_path,monkeypatch):
    source=tmp_path/'source.mp4'
    subprocess.run([shutil.which('ffmpeg'),'-v','error','-f','lavfi','-i','color=red:s=64x64:r=30:d=1',
        '-f','lavfi','-i','color=blue:s=64x64:r=30:d=2',
        '-filter_complex','[0:v][1:v]concat=n=2:v=1:a=0[v]','-map','[v]',str(source)],check=True)
    job=Job(source,tmp_path,'test')
    table={'rows':[{'cuts':[cut(28/30,2)]}]}
    edges=cg.refine_boundaries(job,table,[.9],3)
    assert edges==pytest.approx([1.],abs=.001)
    assert cg.shot_fragments(table['rows'][0]['cuts'],edges)[0]['end']==pytest.approx(2/30,abs=.001)
    monkeypatch.setattr(cg.subprocess,'run',lambda *a,**kw:pytest.fail('cache not used'))
    assert cg.refine_boundaries(job,table,[.9],3)==edges


def test_intact_short_original_shot_at_edge_is_review_only():
    clips=[cut(1,3,55)]
    result,audit=cg.repair_cover(clips,[1,1.1],{'a':{'t_in':1,'t_out':3}})
    assert audit['status']=='needs_review'
    assert result==clips


def test_renderer_adapter_preserves_narration_clock_and_speech_subtitles():
    from app.tikitaka.finish import bundle
    original={'version':{'title':'제목\n아랫줄'},'rows':[
        {'i':1,'mode':'N','cuts':[cut(0,2.2,60)],'text':'말은 유지','dur':1.9,'tts':'same.mp3'},
        {'i':2,'mode':'S','cuts':[cut(5,6)],'text':'대사','dur':1,
         'sub_lines':[{'start':5.1,'end':5.8,'text':'대사'}]}]}
    guarded=copy.deepcopy(original)
    guarded['rows'][0]['cuts'],audit=cg.repair_cover(original['rows'][0]['cuts'],[2.05],
                                                   {'a':{'t_in':0,'t_out':2.2}})
    assert audit['status']=='repaired'
    before=bundle(original,{},title='작품');after=bundle(guarded,{},title='작품')
    assert before[2]==after[2]
    b,a=before[3]['tts_cue_files'][0],after[3]['tts_cue_files'][0]
    assert b['path']==a['path']
    for key in ['text','start_sec','end_sec','duration_sec','fit_actual_sec','beat']:
        assert a['cue'][key]==b['cue'][key]


def test_unrepairable_cover_blocks_but_intact_short_shot_only_warns(tmp_path,monkeypatch):
    job=Job(tmp_path/'source.mp4',tmp_path,'test')
    table={'rows':[{'i':1,'mode':'N','cuts':[cut(1,2)],'text':'말','dur':1}]}
    grid={'source':{'duration_sec':3},'span_candidates':[{'id':'a','t_in':1,'t_out':2}]}
    monkeypatch.setattr(cg,'refine_boundaries',lambda *a:[1.1])
    result,audit=cg.guard_table(job,table,grid)
    assert audit['blocked']
    assert result['rows']==table['rows']
    monkeypatch.setattr(cg,'refine_boundaries',lambda *a:[1,1.1])
    result,audit=cg.guard_table(job,table,grid)
    assert not audit['blocked']
    assert audit['rows'][0]['status']=='needs_review'
    assert result['rows']==table['rows']
