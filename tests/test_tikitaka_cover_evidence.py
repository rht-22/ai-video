from app.tikitaka.common import Job
from app.tikitaka.cover_evidence import retrieve, tighten_opening


def test_retrieve_can_find_earlier_action_and_excludes_banned(tmp_path):
    job=Job(tmp_path/'source.mp4',tmp_path,'작품')
    index={'scenes':[{'id':'early','start':0.,'end':5.,'summary':'추적기를 지갑에 넣는다'},
                     {'id':'late','start':10.,'end':15.,'summary':'차를 운전한다'}],
           'grid_facts':{'put':{'scene_script':'지갑에 추적기를 넣는다'},
                         'banned':{'scene_script':'금지소품이 나온다'},
                         'drive':{'scene_script':'차를 운전한다'}}}
    grid={'span_candidates':[{'id':'put','t_in':1.,'t_out':3.},
                             {'id':'banned','t_in':3.,'t_out':4.},
                             {'id':'drive','t_in':11.,'t_out':13.}]}
    class Model:
        def text_json(self,prompt,**kw):
            assert '실제 심는 씬' in prompt
            return {'rows':[{'row':7,'scene_ids':['early'],'reason':'실제 삽입 행동'}]}
    result=retrieve(job,Model(),[{'i':7,'mode':'N','text':'추적기를 심었다'}],index,grid,[],{'avoid':['금지소품']})
    assert set(result[7]['candidates'])=={'put'}


def test_static_opening_trim_preserves_dialogue_and_meaningful_audio(tmp_path,monkeypatch):
    job=Job(tmp_path/'source.mp4',tmp_path,'작품')
    monkeypatch.setattr('app.tikitaka.probe.cut_proxy_clip',lambda *a: job.source)
    class Model:
        def video_json(self,*a,**kw):
            return {'static':True,'meaningful_audio':False,'reason':'정지에 가까운 사물'}
    rows=[{'mode':'A','dur':3.,'cuts':[{'in':10.,'out':13.,'dur':3.,'span_ids':['sp']}]}]
    grid={'span_candidates':[{'id':'sp','is_audio':True}]}
    assert tighten_opening(job,Model(),rows,grid) is None
    grid['span_candidates'][0]['is_audio']=False
    audit=tighten_opening(job,Model(),rows,grid)
    assert audit['applied'] and rows[0]['cuts'][0]['out']==11.
    assert rows[0]['dur']==1.


def test_small_review_video_goes_inline_without_files_api(tmp_path):
    from google.genai import types
    from app.tikitaka.llm import Gemini
    model=Gemini.__new__(Gemini)
    model.types=types;model.video_model='gemini-3.7-flash'
    model._upload=lambda *a: (_ for _ in ()).throw(AssertionError('Files API should not run'))
    captured=[]
    model._call=lambda kind,name,contents,cfg: captured.append(contents) or {'text_matches':True}
    clip=tmp_path/'clip.mp4';clip.write_bytes(b'short clip')
    assert model.video_json('check',clip,kind='grid_cover_probe')['text_matches']
    assert captured[0][0].inline_data.data==b'short clip'


def test_opening_focused_watch_trim_uses_inline_video(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from google.genai import types
    from app.v3 import watch_trim
    draft=tmp_path/'draft.mp4';draft.write_bytes(b'draft')
    captured=[]
    def generate_content(**kw):
        captured.append(kw)
        return SimpleNamespace(text=json.dumps({'cuts':[], 'pacing':{'loose':False,'note':'첫 3초 확인'}}))
    gemini=SimpleNamespace(types=types,config=SimpleNamespace(flash_model_name='gemini-3.7-flash'),
                           client=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    monkeypatch.setattr('app.v3.seq_analyze._upload_video',lambda *a,**k: (_ for _ in ()).throw(AssertionError('upload')))
    cuts,audit=watch_trim.run_watch_trim(gemini,draft,timeline=[{'clip_start_sec':0.,'clip_end_sec':2.,'span_ids':[]}],
                                       grid={'span_candidates':[]},resources={},segments=[],importance={},opening_focus=True)
    assert not cuts and 'error' not in audit
    assert captured[0]['contents'][0].inline_data.data==b'draft'
    assert '첫 3초' in captured[0]['contents'][1]
