import copy
from types import SimpleNamespace

from app.tikitaka.common import Job
from app.tikitaka import transcript_polish as tp


def test_research_invalidates_polished_cache_and_preserves_word_clock(tmp_path, monkeypatch):
    source = tmp_path/'source.mp4'; source.write_bytes(b'source')
    job = Job(source, tmp_path/'job', '작품'); job.out_dir.mkdir()
    tr = {'polished': True, 'lines': [{'id': 'L-1', 'start': 1., 'end': 2.,
          'text': '임재웅이 왔다', 'text_orig': '임지영이 왔다', 'word_i': [0, 1]}],
          'words': [{'start': 1., 'end': 1.5, 'text': '임지영이'}, {'start': 1.5, 'end': 2., 'text': '왔다'}]}
    old = copy.deepcopy(tr)
    prompts = []
    class Model:
        usage = SimpleNamespace(calls=[])
        def media_json(self, prompt, *a, **kw):
            prompts.append(prompt)
            return {'corrections': {'L-1': '임재홍이 왔다'}}
    monkeypatch.setattr(tp, '_window_audio', lambda *a: source)
    args = (job, Model(), tr, source, 3.)
    tp.polish_transcript(*args, title='작품', cast=['임재홍'], research_context='극중 임재홍=배우 김지훈')
    assert tr['lines'][0]['text'] == '임재홍이 왔다'
    assert tr['lines'][0]['text_orig'] == '임지영이 왔다'
    assert tr['words'] == old['words'] and tr['lines'][0]['word_i'] == [0, 1]
    assert '극중 임재홍=배우 김지훈' in prompts[0]
    tp.polish_transcript(*args, title='작품', cast=['임재홍'], research_context='극중 임재홍=배우 김지훈')
    assert len(prompts) == 1
    tp.polish_transcript(*args, title='작품', cast=['임재홍'], research_context='인물 관계 변경')
    assert len(prompts) == 2
    assert job.has('transcript_dependents_dirty.json')


def test_failed_window_remains_retryable(tmp_path, monkeypatch):
    source = tmp_path/'source.mp4'; source.write_bytes(b'source')
    job = Job(source, tmp_path/'job', '작품'); job.out_dir.mkdir()
    tr = {'lines': [{'id': 'L-1', 'start': 1., 'end': 2., 'text': '원문'}]}
    class Model:
        usage = SimpleNamespace(calls=[])
        def media_json(self, *a, **kw):
            raise RuntimeError('temporary')
    monkeypatch.setattr(tp, '_window_audio', lambda *a: source)
    tp.polish_transcript(job, Model(), tr, source, 3., title='작품', cast=[])
    assert not tr['polished'] and tr['lines'][0]['text'] == '원문'
    assert job.load('transcript_polish.json')['windows'][0]['end'] == 3.
