import copy
from types import SimpleNamespace

import pytest
from app.tikitaka import v3_analysis as va
from app.tikitaka.common import Job
from app.tikitaka.rebuild import source_script


def material():
    spans = [{"id": f"sp{i}", "t_in": float(i), "t_out": float(i+1), "is_audio": i in (0, 1),
              "text": "대사" if i < 2 else "", "time_authority": "stt" if i < 2 else "scene"} for i in range(4)]
    facts = [{"span_id": s['id'], "time": {"start": s['t_in'], "end": s['t_out']},
              "scene_script": "고개를 든다", "characters": ["청자"], "importance": 4,
              "audio_script": [{"speaker": "경희", "line": "원문과 다른 청취"}] if s['is_audio'] else [],
              "heard_text": "들은 대사", "conf": .8, "text_source": "heard", "is_audio": s['is_audio'],
              "time_authority": s['time_authority']} for s in spans[:3]]
    facts[0].update(screen_text="읽은 글자", diegesis="recalled", is_claim=True)
    facts[2].update(subject_pos="right")
    doc = {"sequences": [{"number": 0, "content": "가족의 대화", "chunks": [{"number": 0,
        "time": {"start": 0, "end": 3}, "meanings": [{"content": "대화한다", "characters": ["경희"],
        "mood": "긴장", "importance": 4, "spans": facts}]}]}],
        "exception_sector": {"credit": {"start": 3, "end": 4}}, "coverage": {"analyzed": 1, "failed": 0}}
    tr = {"words": [], "lines": [{"id": "L-001", "start": 0., "end": .8, "text": "보존할 전사"},
          {"id": "L-002", "start": 3.2, "end": 3.8, "text": "크레딧"}]}
    return doc, {"span_candidates": spans}, tr


def test_adapter_preserves_evidence_and_excludes_credits():
    doc, grid, tr = material(); before = copy.deepcopy(tr)
    index = va.adapt(doc, grid, tr, title="작품", cast=[], fp="fp")
    assert tr == before
    assert index['speakers'] == {'L-001': '경희'}  # not the person visible on screen
    assert index['v3_stage2'] == doc
    assert index['grid_facts']['sp0']['audio_script'][0]['line'] == '원문과 다른 청취'
    assert index['moments'][0]['span_ids'] == ['sp2']
    assert index['moments'][0]['subject_pos'] == 'right'
    text = source_script(index, tr, index['analysis_excluded_ranges'])
    assert '보존할 전사' in text and '크레딧' not in text
    assert '읽은 글자' in text and 'recalled' in text and '주장=True' in text
    assert '긴장' in text


def test_multiple_heard_speakers_are_not_guessed_and_failures_are_excluded():
    doc, grid, tr = material()
    doc['sequences'][0]['chunks'][0]['meanings'][0]['spans'][0]['audio_script'].append({'speaker': '수정', 'line': '다른 대사'})
    index = va.adapt(doc, grid, tr, title='작품', cast=[], fp='x')
    assert index['ambiguous_speaker_lines'] == ['L-001']
    assert not index['speakers']
    doc['sequences'][0]['chunks'][0]['meanings'] = []
    index = va.adapt(doc, grid, tr, title='작품', cast=[], fp='x')
    assert (0., 3.) in index['analysis_excluded_ranges']
    assert index['moments'] == []


def test_shared_stages_and_cache_resume(tmp_path, monkeypatch):
    from app.v3 import seq_analyze, refine, pipeline, audio, arousal
    doc, grid, tr = material()
    source = tmp_path/'source.mp4'; source.write_bytes(b'source')
    job = Job(source, tmp_path, '작품')
    client = SimpleNamespace(config=SimpleNamespace(model_name='gemini-3.7-flash',flash_model_name='gemini-3.7-flash',analysis_thinking_level='medium'))
    monkeypatch.setattr(va, 'build_grid', lambda *a, **kw: {**grid, 'words': []})
    monkeypatch.setattr(audio,'detect_silence_intervals',lambda *a: [])
    monkeypatch.setattr(audio,'load_pcm',lambda *a: [])
    monkeypatch.setattr(arousal,'compute_arousal',lambda *a: [])
    calls=[]
    monkeypatch.setattr(seq_analyze,'build_scan_proxy',lambda *a, **kw: calls.append('proxy'))
    monkeypatch.setattr(seq_analyze,'run_seq_analyze',lambda *a, **kw: (calls.append('s1') or copy.deepcopy(doc), {}))
    monkeypatch.setattr(refine,'refine_exception',lambda c,d,*a,**kw:(d,{}))
    monkeypatch.setattr(refine,'describe_zone_heads',lambda c,d,*a,**kw:(d,{}))
    def m2(**kw):
        calls.append('m2')
        out=Job(source,kw['output_dir'],'작품')
        out.save('stage2.json', doc)
        out.save('checkpoint_chunk_split.json',{'chunks': []})
        out.save('checkpoint_chunk_analyze.json',{'chunks': []})
    monkeypatch.setattr(pipeline,'_run_m2',m2)
    args=(job,None,tr,source,{'duration_sec':4},[])
    kw={'title':'작품','cast':['경희'],'get_v3':lambda:client,'research_context':'가족'}
    va.build_index(*args,**kw); va.build_index(*args,**kw)
    assert calls == ['proxy','s1','m2']
    va.build_index(*args, **{**kw, 'reuse_analysis': True, 'get_v3': lambda: pytest.fail('API must not be loaded')})
    assert calls == ['proxy','s1','m2']
    with pytest.raises(ValueError, match='인물'):
        va.build_index(*args, **{**kw, 'reuse_analysis': True, 'cast': ['다른 인물']})
    with pytest.raises(ValueError, match='함께'):
        va.build_index(*args, **{**kw, 'reuse_analysis': True, 'retry_failed': True})
    job.save('grid.json', {'changed': True})
    with pytest.raises(ValueError, match='시간 격자'):
        va.build_index(*args, **{**kw, 'reuse_analysis': True})
    job.save('grid.json', {**grid, 'words': []})
    va.build_index(*args,**{**kw,'retry_failed':True})
    assert calls == ['proxy','s1','m2','m2']
    va.build_index(*args,**{**kw,'research_context':'인물 정정'})
    assert calls[-3:] == ['proxy','s1','m2']
    assert job.load('stage2.json') == doc


def test_essential_text_keeps_story_info_without_changing_speech_or_clocks():
    from app.v3.text_policy import filter_index, filter_document
    doc, grid, tr = material()
    spans = doc['sequences'][0]['chunks'][0]['meanings'][0]['spans']
    spans[0].update(screen_text='ㅋㅋㅋ / 프로그램 로고', screen_text_role='decorative')
    spans[1].update(screen_text='남은 시간 15분', screen_text_role='story_info', has_text=True)
    spans[2].update(screen_text='편지의 내용', screen_text_kind='문서', has_text=True)
    index = va.adapt(doc, grid, tr, title='작품', cast=[], fp='fp')
    before = copy.deepcopy(index)
    filtered, audit = filter_index(index)
    assert index == before
    assert 'ㅋㅋㅋ' not in source_script(filtered, tr, [])
    assert '남은 시간 15분' in source_script(filtered, tr, [])
    assert '편지의 내용' in source_script(filtered, tr, [])
    assert filtered['grid_facts']['sp0']['audio_script'] == index['grid_facts']['sp0']['audio_script']
    assert filtered['grid_facts']['sp0']['time'] == index['grid_facts']['sp0']['time']
    assert filtered['moments'][0]['subject_pos'] == 'right'
    assert filtered['analysis_excluded_ranges'] == index['analysis_excluded_ranges']
    assert audit['roles']['decorative'] == 1
    spans[0].pop('screen_text_role')
    legacy, _ = filter_document(doc)
    s = legacy['sequences'][0]['chunks'][0]['meanings'][0]['spans'][0]
    assert s['screen_text_role'] == 'uncertain' and 'screen_text' not in s
