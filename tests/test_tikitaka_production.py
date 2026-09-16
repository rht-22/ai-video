"""Production intent and feasibility travel from writing to measured assembly."""
import copy

from app.tikitaka import production as p, rebuild, grid_table as gt


def material():
    facts = {f'sp{i}': {'scene_script': '진행자가 규칙을 설명한다', 'importance': 4}
             for i in range(5)}
    moments = [{'id': f'S-{i}', 'span_ids': [f'sp{i}'], 'start': i*2., 'end': i*2.+2,
                'desc': '진행자가 규칙을 설명한다', 'kind': 'ambience'} for i in range(5)]
    transcript = {'lines': [{'id': 'L-1', 'start': 5., 'end': 6., 'text': '다른 팀 표는 두 표입니다.',
                             'speaker': '진행자', 'word_i': [0]}],
                  'words': [{'start': 5., 'end': 6., 'text': '두 표입니다.'}]}
    index = {'grid_facts': facts, 'moments': moments, 'scenes': []}
    return index, transcript


def plan(mid='S-0', kind='rule_summary'):
    return {'kind': kind, 'evidence_ids': ['L-1'], 'cover_moment_ids': [mid]}


def joint_plan(sid='sp0', role='evidence', kind='action'):
    return {'kind': kind, 'evidence_ids': ['L-1'],
            'cover': [{'span_id': sid, 'role': role}]}


def test_plan_validation_retains_only_existing_allowed_evidence():
    index, transcript = material()
    raw = {'kind': 'anything', 'evidence_ids': ['L-1', 'invented', 'L-1'],
           'cover_moment_ids': ['L-1', 'S-0', 'S-1']}
    parsed, issues = p.normalize_plan(raw, index, transcript, [(2,4)])
    assert parsed == {'kind': 'unspecified', 'evidence_ids': ['L-1'], 'cover_moment_ids': ['S-0']}
    assert len(issues) == 4
    assert p.normalize_plan(None, index, transcript) == (None, [])


def test_joint_plan_keeps_exact_span_and_visual_role():
    index, transcript = material()
    parsed, issues = p.normalize_plan(joint_plan(), index, transcript)
    assert parsed == joint_plan()
    assert issues == []
    assert p.planned_cover_span_ids(parsed, index) == ['sp0']


def test_joint_action_plan_requires_direct_evidence_cover():
    index, transcript = material()
    raw = {'kind': 'action', 'evidence_ids': ['L-1'],
           'cover': [
               {'span_id': 'sp0', 'role': 'support'},
               {'span_id': 'missing', 'role': 'evidence'},
               {'span_id': 'sp1', 'role': 'wrong'},
           ]}
    parsed, issues = p.normalize_plan(raw, index, transcript)
    assert parsed['cover'] == [{'span_id': 'sp0', 'role': 'support'}]
    assert any('없는/잘못된 span_id' in x for x in issues)
    assert any('잘못된 역할' in x for x in issues)
    assert any('evidence 덮개 없음' in x for x in issues)


def test_legacy_moment_plan_still_expands_to_exact_spans():
    index, transcript = material()
    parsed, issues = p.normalize_plan(plan('S-2'), index, transcript)
    assert issues == []
    assert parsed == plan('S-2')
    assert p.planned_cover_span_ids(parsed, index) == ['sp2']


def test_production_metadata_survives_version_validation_without_altering_dialogue():
    index, transcript = material()
    raw = {'versions': [{'n': 1, 'title': '투표 규칙', 'items': [
        {'type': 'N', 'text': '두 배죠.', 'production_plan': plan()},
        {'type': 'S', 'line_ids': ['L-1']}]}]}
    before = copy.deepcopy(transcript)
    version = rebuild.validate_versions(raw, index, transcript)['versions'][0]
    assert version['items'][0]['production_plan'] == plan()
    assert transcript == before
    assert version['items'][1]['line_ids'] == ['L-1']
    assert 'production_plan' not in version['items'][1]
    del raw['versions'][0]['items'][0]['production_plan']
    legacy = rebuild.validate_versions(raw, index, transcript)['versions'][0]
    assert 'production_plan' not in legacy['items'][0]


def test_joint_cover_metadata_survives_version_validation():
    index, transcript = material()
    raw = {'versions': [{'n': 1, 'title': '함께 쓰는 화면', 'items': [
        {'type': 'N', 'text': '직접 보여줍니다.', 'production_plan': joint_plan()},
        {'type': 'S', 'line_ids': ['L-1']}]}]}
    version = rebuild.validate_versions(raw, index, transcript)['versions'][0]
    assert version['items'][0]['production_plan'] == joint_plan()


def test_preflight_reserves_spoken_action_and_prior_narration_footage():
    index, transcript = material()
    version = {'items': [
        {'type': 'S', 'line_ids': ['L-1']},
        {'type': 'A', 'moment_id': 'S-0'},
        {'type': 'N', 'text': '왜죠?', 'production_plan': plan('S-0')},
        {'type': 'N', 'text': '왜죠?', 'production_plan': plan('S-2')},
        {'type': 'N', 'text': '왜죠?', 'production_plan': plan('S-4')},
        {'type': 'N', 'text': '왜죠?', 'production_plan': plan('S-4')} ]}
    result = p.preflight(version, index, transcript)
    assert result['estimated_only']
    assert result['items'][0]['issues']  # A used this moment
    assert result['items'][1]['issues']  # S uses this interval
    assert result['items'][2]['issues'] == []
    assert result['items'][3]['issues']  # previous N reserves it


def test_preflight_reads_joint_cover_spans_without_legacy_moment_ids():
    index, transcript = material()
    version = {'items': [
        {'type': 'N', 'text': '봐요.', 'production_plan': joint_plan('sp0')},
    ]}
    result = p.preflight(version, index, transcript)
    assert result['items'][0]['planned_span_ids'] == ['sp0']
    assert result['items'][0]['issues'] == []


def test_preflight_allows_matching_later_dialogue_visual_once():
    index, transcript = material()
    version = {'items': [
        {'type': 'N', 'text': '두 표죠.',
         'production_plan': joint_plan('sp2')},
        {'type': 'S', 'line_ids': ['L-1']},
    ]}
    report = p.preflight(version, index, transcript)['items'][0]
    assert report['issues'] == []
    assert report['reused_later_dialogue_span_ids'] == ['sp2']


def test_preflight_flags_short_visual_and_missing_rule_evidence():
    index, transcript = material()
    pp = plan(); pp['evidence_ids'] = []
    result = p.preflight({'items': [{'type': 'N', 'text': '이 규칙을 따르면 다른 팀의 표가 두 배가 됩니다.',
                                    'production_plan': pp}]}, index, transcript)
    issues = result['items'][0]['issues']
    assert any('대사 근거 없음' in x for x in issues)
    assert any('부족' in x for x in issues)


def test_context_carries_explicit_evidence_and_only_local_continuity():
    index, transcript = material()
    rows = [{'i': i, 'mode': 'N', 'text': f'문장{i}'} for i in range(8)]
    rows[4]['production_plan'] = plan()
    context = p.narration_context(rows, rows[4], index, transcript)
    assert [x['text'] for x in context['adjacent']] == ['문장2','문장3','문장4','문장5']
    assert context['evidence'][0]['text'] == transcript['lines'][0]['text']
    assert context['suggested_kind'] == 'rule_summary'


def test_planning_uses_assembler_limits_and_semantics(monkeypatch):
    monkeypatch.setattr(gt, 'MAX_STACK', 3)
    rules = p.planning_rules()
    assert '최대 3컷' in rules
    assert p.SEMANTIC_RULES in rules
    assert '대사 직전' in rules and '정지·슬로우 금지' in rules
    assert '최대 1.2배속' in rules
    assert '"cover"' in rules and '"role":"evidence|support"' in rules
    assert '가까운 화면을 대신 쓰지 말고' in rules


def test_source_script_exposes_exact_cover_span_ids():
    index, transcript = material()
    index['scenes'] = [{'id': 'SC-1', 'start': 0., 'end': 10., 'place': '무대',
                        'summary': '규칙 설명', 'chars': ['진행자']}]
    script = rebuild.source_script(index, transcript)
    assert 'S-0' in script
    assert '덮개 sp=sp0' in script


def test_joint_gate_repairs_only_bad_narration_before_acceptance(monkeypatch):
    index, transcript = material()
    version = {'n': 6, 'items': [
        {'type': 'N', 'text': '선배들이 감탄한다', 'production_plan': joint_plan(role='support')},
        {'type': 'S', 'line_ids': ['L-1'], 'text': '원본 대사'}]}
    speech = copy.deepcopy(version['items'][1])
    class Job:
        def load(self, name):
            return {'span_candidates': [{'id': f'sp{i}', 't_in': i*2, 't_out': i*2+2} for i in range(5)]}
        def log(self, message): pass
    class Gemini:
        def text_json(self, prompt, **kwargs):
            assert 'evidence 덮개 없음' in prompt
            return {'text': '규칙 설명입니다', 'production_plan': joint_plan()}
    probes = []
    def probe(*args, **kwargs):
        probes.append(args[3])
        return {'text_matches': True}
    monkeypatch.setattr(gt, 'probe_cover', probe)
    p.enforce_joint_plans(Job(), Gemini(), version, index, transcript)
    assert probes == ['선배들이 감탄한다']
    assert version['items'][1] == speech
    assert version['items'][0]['production_plan']['cover'][0]['role'] == 'evidence'
    assert version['joint_plan_repairs'][0]['item'] == 1


def test_joint_gate_rejects_valid_ids_with_wrong_actual_footage(monkeypatch):
    import pytest
    index, transcript = material()
    item = {'type': 'N', 'text': '선배들이 감탄한다', 'production_plan': joint_plan()}
    version = {'n': 6, 'items': [copy.deepcopy(item)]}
    class Job:
        def load(self, name):
            return {'span_candidates': [{'id': 'sp0', 't_in': 0, 't_out': 2}]}
        def log(self, message): pass
    class Gemini:
        calls = 0
        def text_json(self, *args, **kwargs):
            self.calls += 1
            return item
    g = Gemini()
    monkeypatch.setattr(gt, 'probe_cover', lambda *a, **kw: {'text_matches': False, 'seen': '일반 관객'})
    with pytest.raises(ValueError, match='계획 수정 소진'):
        p.enforce_joint_plans(Job(), g, version, index, transcript)
    assert g.calls == 3
    assert version['items'][0] == item


def test_cover_reselection_cannot_replace_or_drop_planned_evidence():
    import pytest
    plan = joint_plan()
    plan['cover'].append({'span_id': 'sp1', 'role': 'support'})
    p.validate_cover_selection(plan, ['sp0', 'sp1'])
    p.validate_cover_selection(plan, ['sp0'])
    with pytest.raises(ValueError, match='evidence'):
        p.validate_cover_selection(plan, ['sp1'])
    with pytest.raises(ValueError, match='계획 밖'):
        p.validate_cover_selection(plan, ['sp2'])


def test_spoken_cover_id_does_not_need_silent_moment():
    index, transcript = material()
    index['grid_facts']['spoken'] = {'time': {'start': '00:00:10.000', 'end': '00:00:12.000'}, 'is_audio': True}
    parsed, issues = p.normalize_plan(joint_plan('spoken'), index, transcript)
    assert not issues
    assert parsed['cover'][0]['span_id'] == 'spoken'
    _, issues = p.normalize_plan(joint_plan('spoken'), index, transcript, [(11., 13.)])
    assert any('제외 구간' in x for x in issues)
