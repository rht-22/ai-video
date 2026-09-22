import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.tikitaka import cover_framing as cf
from app.tikitaka.common import Job
from app.v3.narration_framing import resolve, apply as render_framing
from app.modules.story_builder import StoryClip


def decision(box=(.65, .2, .8, .7)):
    return {'mode': 'focus', 'confidence': 'high', 'target': '설명하는 인물',
            'kind': 'person', 'target_box': list(box), 'reason': '내레이션에서 설명하는 행동의 주체'}


@pytest.mark.parametrize('raw', [None, {}, {'mode': 'focus'},
    decision((0, 0, float('nan'), 1)), decision((.8, .2, .4, .8)),
    {**decision(), 'confidence': 'low'}, decision((200, 0, 400, 500))])
def test_uncertain_or_invalid_model_output_requests_reselection(raw):
    assert cf.normalize(raw)['mode'] == 'needs_review'


def test_target_is_inside_safe_region_even_near_source_boundary():
    kw = dict(src_size=(1920,1080), picture={'x':0,'y':56,'w':1920,'h':968}, aspect_ratio='1:1')
    r = resolve(cf.normalize(decision()), **kw)
    assert r['mode'] == 'focus'
    left = r['x_center'] - r['crop_w']/2
    assert 100 <= (.65*1920-left)/r['crop_w']*1080
    assert (.8*1920-left)/r['crop_w']*1080 <= 980
    assert resolve(cf.normalize(decision((.95,.2,1.,.6))), **kw)['mode'] == 'needs_review'
    assert resolve(cf.normalize(decision((.1,.2,.9,.8))), **kw)['mode'] == 'needs_review'


def test_narrow_band_uses_canvas_safe_area_not_fixed_fraction():
    r = resolve(cf.normalize(decision((.92,.2,1.,.6))), src_size=(1920,1080),
                picture=None, aspect_ratio='1:1', band_width=800)
    assert r['mode'] == 'focus'  # Full 800px band is already within x100..980.


def setup_job(tmp_path, monkeypatch):
    source = tmp_path/'source.mp4';source.write_bytes(b'source')
    job = Job(source, tmp_path, 'test')
    monkeypatch.setattr(cf, 'find_bin', lambda x: x)
    def extract(cmd, **kwargs): Path(cmd[-1]).write_bytes(b'jpeg')
    monkeypatch.setattr(cf.subprocess, 'run', extract)
    c = {'clip_start_sec':1.,'clip_end_sec':3.,'beat':0,'cover':True,'playback_speed':1.2,'hold_sec':.1}
    plan = {'timeline':[c, {**c,'cover':False}, {**c,'reframe':{'mode':'fixed','x':400}}]}
    story = {'beats':[{'number':0,'action':'손에 쥔 로또를 확인한다'}]}
    return job, plan, story


def test_only_covers_change_clock_manual_and_dialogue_are_preserved(tmp_path, monkeypatch):
    job, plan, story = setup_job(tmp_path,monkeypatch)
    before=copy.deepcopy(plan);calls=[]
    class Gemini:
        def images_json(self,prompt,frames,**kwargs):
            calls.append(prompt)
            assert len(frames)==5
            return decision()
    new, audit=cf.apply(job,plan,story,get_gemini=Gemini)
    assert plan==before and len(calls)==1
    assert '손에 쥔 로또' in calls[0]
    assert new['timeline'][1:]==before['timeline'][1:]
    stripped=copy.deepcopy(new);stripped['timeline'][0].pop('narration_framing')
    assert stripped==before
    again,_=cf.apply(job,plan,story,get_gemini=lambda:pytest.fail('cached'))
    assert again==new
    story['beats'][0]['action']='옆 사람의 손에 든 휴대폰'
    cf.apply(job,plan,story,get_gemini=Gemini)
    assert len(calls)==2


def test_off_and_failure_do_not_guess_or_cache_failure(tmp_path,monkeypatch):
    job,plan,story=setup_job(tmp_path,monkeypatch)
    assert cf.apply(job,plan,story,get_gemini=lambda:pytest.fail('disabled'),enabled=False)[0]==plan
    def fail():raise RuntimeError('model unavailable')
    new,audit=cf.apply(job,plan,story,get_gemini=fail)
    assert new['timeline'][0]['narration_framing']['mode']=='needs_review'
    assert audit['clips'][0]['error']
    assert not list((tmp_path/'cover_framing').glob('*.json'))


def test_saved_narration_target_overrides_auto_zoom_not_manual_or_info_fit(tmp_path):
    d=cf.normalize(decision())
    c={'clip_start_sec':1.,'clip_end_sec':3.,'role':'build','cover':True,'narration_framing':d}
    timeline=[c,{**c,'reframe':{'mode':'fixed','x':400}},c,{**c,'narration_framing':{'mode':'fit'}}]
    clips=[StoryClip(role='build',start_sec=1,end_sec=3,subtitle='',use_original_audio=False) for _ in timeline]
    from dataclasses import replace
    clips[2]=replace(clips[2],fit_picture=(0,0,1920,1080))
    crops={f'build_{i}':Path('zoom.json') for i in range(4)}
    audit=render_framing(timeline,clips,crops,output_dir=tmp_path,src_size=(1920,1080),picture=None,
       design=SimpleNamespace(aspect_ratio='1:1',video_width=None),log=lambda x:None)
    assert len(audit)==3 and crops['build_1']==Path('zoom.json')
    assert crops['build_0'].name=='v3_crop_narration_0.json'
    assert crops['build_2'].name=='v3_crop_narration_2.json'
    assert crops['build_3']==Path('zoom.json')
    assert clips[2].fit_picture is None and clips[3].fit_picture is None


def test_mixed_coordinates_are_reobserved_once_and_retry_is_cached(tmp_path,monkeypatch):
    job,plan,story=setup_job(tmp_path,monkeypatch);calls=[]
    class Gemini:
        def images_json(self,prompt,frames,**kwargs):
            calls.append(prompt)
            return decision((.2,100,.4,500)) if len(calls)==1 else decision()
    result,_=cf.apply(job,plan,story,get_gemini=Gemini)
    assert result['timeline'][0]['narration_framing']['mode']=='focus' and len(calls)==2
    again,_=cf.apply(job,plan,story,get_gemini=lambda:pytest.fail('retry cached'))
    assert result==again
