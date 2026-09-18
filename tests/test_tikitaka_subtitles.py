import copy

from app.tikitaka.subtitles import corrected_words, phrase_lines, refresh_table


def fixture():
    tokens = '아, 근데 나 끝나고 피디님이랑 잠깐 보기로 했는데.'.split()
    words = [{'text': t, 'start': 10+i*.5, 'end': 10+(i+1)*.5} for i,t in enumerate(tokens)]
    line = {'id': 'L-1', 'text': ' '.join(tokens), 'start': 10., 'end': 14., 'word_i': list(range(8))}
    return words, line


def test_v3_phrases_use_word_times_and_preserve_text():
    words, line = fixture(); before = copy.deepcopy(words)
    parts = phrase_lines(line, words)
    assert [p['text'] for p in parts] == ['아, 근데 나 끝나고', '피디님이랑 잠깐', '보기로 했는데']
    assert parts[0]['start'] == 10 and parts[0]['end'] == 12
    assert parts[-1]['end'] == 14
    assert all(a['end'] <= b['start'] for a,b in zip(parts,parts[1:]))
    assert words == before


def test_corrected_name_never_reverts_to_raw_stt():
    words = [{'text': '임지영이', 'start': 1., 'end': 1.7}, {'text': '왔다.', 'start': 1.7, 'end': 2.}]
    line = {'text': '임재홍이 왔다.', 'start': 1., 'end': 2., 'word_i': [0,1]}
    aligned = corrected_words(line, words)
    assert aligned[0] == {'text': '임재홍이', 't0': 1., 't1': 1.7}
    assert phrase_lines(line, words)[0]['text'] == '임재홍이 왔다'
    # Changed token count stays within measured group, with no invented word clock.
    line['text'] = '임재홍이가 왔어요.'
    assert ' '.join(w['text'] for w in corrected_words(line,words)) == line['text']


def test_refresh_preserves_edit_and_tts():
    words,line = fixture()
    table = {'rows': [{'mode': 'S', 'src': ['L-1'], 'text': 'old', 'cuts': [{'in': 10., 'out': 14.}]},
                      {'mode': 'N', 'text': '내레이션', 'tts': 'audio.mp3', 'dur': 2., 'cuts': []}]}
    before = copy.deepcopy(table)
    result = refresh_table(table, {'lines': [line], 'words': words})
    assert len(result['rows'][0]['sub_lines']) == 3
    assert result['rows'][0]['cuts'] == before['rows'][0]['cuts']
    assert result['rows'][1] == before['rows'][1]
    assert table == before


def test_narration_phrase_clock_tracks_audio_and_reuses_alignment(tmp_path):
    from app.tikitaka.common import Job
    from app.tikitaka.subtitles import narration_captions
    p=tmp_path/'voice.mp3';p.write_bytes(b'voice')
    job=Job(tmp_path/'source.mp4',tmp_path,'작품')
    words,line=fixture()
    payload={'words':[{'type':'word','text':w['text'],'start':w['start']-10,'end':w['end']-10} for w in words]}
    resources={'tts_cue_files':[{'path':str(p),'cue':{'text':line['text'],'start_sec':20.,'end_sec':24.,'duration_sec':4.}}]}
    original=copy.deepcopy(resources);calls=[]
    def transcribe(path):
        calls.append(path);return payload
    caps=narration_captions(job,resources,transcribe=transcribe)
    assert len(caps)==3 and caps[0]['start_sec']==20. and caps[-1]['end_sec']==24.
    assert resources==original and calls==[p]
    resources['tts_cue_files'][0]['cue'].update(start_sec=30.,end_sec=34.)
    shifted=narration_captions(job,resources,transcribe=transcribe)
    assert shifted[0]['start_sec']==30. and calls==[p]


def test_surname_and_title_never_split_across_lines():
    """2026-09-18 로또 clip01 실측: '입사 선배인 양 / 대리가 탕비실에서' · '말을 튕겨내자, 공 / 팀장은' — 성이 앞 줄 꼬리에 매달렸다."""
    from app.tikitaka.subtitles import glues_to_next, glue_name_titles
    assert glues_to_next('양', '대리가') and glues_to_next('공', '팀장은') and glues_to_next('민준', '씨가') and glues_to_next('팀장', '님')
    assert not glues_to_next('공,', '팀장은')            # 문장부호 뒤는 원래 경계
    assert not glues_to_next('선배인', '대리가')         # 두 글자 이상 어절 + 직함은 묶지 않는다(이름 아님)
    assert not glues_to_next('양', '탕비실에서')
    for text in ['회의 시작 전부터 입사 선배인 양 대리가 탕비실에서 밍기적댔기 때문이죠.',
                 '입사 선배랍시고 대놓고 말을 튕겨내자, 공 팀장은 회의 말미에 정곡을 찌르는 일침을 날리는데요.']:
        tokens = text.split()
        words = [{'text': t, 'start': i * .4, 'end': (i + 1) * .4} for i, t in enumerate(tokens)]
        line = {'text': text, 'start': 0., 'end': len(tokens) * .4, 'word_i': list(range(len(tokens)))}
        parts = [p['text'] for p in phrase_lines(line, words)]
        assert not any(p.endswith((' 양', ' 공', ', 공')) for p in parts), parts
        assert any('양 대리가' in p or '공 팀장은' in p for p in parts), parts
    aligned = [{'text': '양', 't0': 1., 't1': 1.2}, {'text': '대리가', 't0': 1.2, 't1': 1.6}]
    assert glue_name_titles(aligned) == [{'text': '양 대리가', 't0': 1., 't1': 1.6}] and len(aligned) == 2
