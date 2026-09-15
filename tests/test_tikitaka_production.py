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


def test_plan_validation_retains_only_existing_allowed_evidence():
    index, transcript = material()
    raw = {'kind': 'anything', 'evidence_ids': ['L-1', 'invented', 'L-1'],
           'cover_moment_ids': ['L-1', 'S-0', 'S-1']}
    parsed, issues = p.normalize_plan(raw, index, transcript, [(2,4)])
    assert parsed == {'kind': 'unspecified', 'evidence_ids': ['L-1'], 'cover_moment_ids': ['S-0']}
    assert len(issues) == 4
    assert p.normalize_plan(None, index, transcript) == (None, [])


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
