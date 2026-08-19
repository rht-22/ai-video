"""편집실 오버라이드 — 계약 검증 + 적용 규약(전량 교체·pinned·첫 variant 한정).

배경: edit_plan.json 은 기록물이라 사람이 고쳐도 렌더가 보지 않는다(2026-08-16 실측).
관제 편집실의 수정을 파이프라인에 먹이는 유일한 통로가 --edit-overrides 이고,
이 파일이 그 계약을 못 박는다. 계약이 조용히 바뀌면 사람이 고친 값이 반영 안 된 채
영상이 나가므로, 위반은 예외로 즉시 실패해야 한다.
"""
from __future__ import annotations

import json

import pytest

from app.modules.edit_overrides import (
    IMAGE_MAX_BYTES,
    EditOverrideError,
    apply_overrides,
    load_edit_overrides,
    overrides_clips,
    overrides_subtitles,
    overrides_tts,
    place_anchored_images,
    place_anchored_subtitles,
    resolve_image_files,
    total_duration,
    validate_overrides,
)
from app.modules.story_builder import StoryClip


def _clip(role, start, end):
    return StoryClip(role=role, start_sec=float(start), end_sec=float(end),
                     subtitle="원문", use_original_audio=True, chunk_index=3,
                     candidate_index=1, character_focus=("A",))


def _variants():
    return [([_clip("hook", 10, 25), _clip("payoff", 100, 120)], "옛 제목", 1.0),
            ([_clip("hook", 50, 70)], "variant2 제목", 0.8)]


OV_FULL = {
    "schema": "edit_overrides/v1",
    "title": {"top_title": "새 제목 1줄\n2줄"},
    "clips": [{"start_sec": 742.5, "end_sec": 771.0, "role": "hook"},
              {"start_sec": 1102.0, "end_sec": 1118.5, "role": "payoff",
               "use_original_audio": False}],
}


# ── 검증 ──────────────────────────────────────────────────────────────
def test_schema_must_match():
    # v1~v3 유효 — v2 = v1 + tts, v3 = v2 + 자막 앵커/줄 스타일 + images.
    # 미지 스키마 즉시 거절은 구/신 엔진 공통 안전장치다: v2 엔진에 v3 를 넣으면
    # "알 수 없는 스키마"로 즉시 실패한다(2026-08-19 실측 — v3 배포 전 스탬프 전환 금지 근거).
    validate_overrides({"schema": "edit_overrides/v1", "title": {"top_title": "x"}})
    validate_overrides({"schema": "edit_overrides/v2", "title": {"top_title": "x"}})
    validate_overrides({"schema": "edit_overrides/v3", "title": {"top_title": "x"}})
    with pytest.raises(EditOverrideError):
        validate_overrides({"schema": "edit_overrides/v4", "title": {"top_title": "x"}})
    with pytest.raises(EditOverrideError):
        validate_overrides({"title": {"top_title": "x"}})          # 스키마 누락
    with pytest.raises(EditOverrideError):
        validate_overrides([1, 2, 3])                              # 객체 아님


def test_reversed_or_negative_span_rejected():
    for bad in ([{"start_sec": 30, "end_sec": 10}],                # 뒤집힘
                [{"start_sec": -5, "end_sec": 10}],                # 음수
                [{"start_sec": 10, "end_sec": 10}]):               # 길이 0
        with pytest.raises(EditOverrideError):
            validate_overrides({"schema": "edit_overrides/v1", "clips": bad})


def test_missing_fields_and_bad_role_rejected():
    with pytest.raises(EditOverrideError):
        validate_overrides({"schema": "edit_overrides/v1", "clips": [{"start_sec": 1}]})
    with pytest.raises(EditOverrideError):
        validate_overrides({"schema": "edit_overrides/v1",
                            "clips": [{"start_sec": 1, "end_sec": 2, "role": "몰라"}]})


def test_empty_clips_rejected_but_absent_key_ok():
    # 전량 교체 규약: 빈 배열은 '전부 지움'이 아니라 실수로 본다
    with pytest.raises(EditOverrideError):
        validate_overrides({"schema": "edit_overrides/v1", "clips": []})
    # 구간을 안 고치는 경우 = 키 자체가 없음
    assert validate_overrides({"schema": "edit_overrides/v1",
                               "title": {"top_title": "제목만"}})


def test_blank_title_rejected():
    with pytest.raises(EditOverrideError):
        validate_overrides({"schema": "edit_overrides/v1", "title": {"top_title": "   "}})


# ── 적용 ──────────────────────────────────────────────────────────────
def test_clips_replaced_wholesale_and_pinned():
    out, pinned = apply_overrides(_variants(), OV_FULL)
    clips, title, score = out[0]
    assert pinned is True                                   # 자동 보정 건너뛰기 신호
    assert title == "새 제목 1줄\n2줄"
    assert [(c.start_sec, c.end_sec) for c in clips] == [(742.5, 771.0), (1102.0, 1118.5)]
    assert [c.role for c in clips] == ["hook", "payoff"]
    assert clips[1].use_original_audio is False
    assert score == 1.0                                     # 점수는 보존
    # 사람이 고른 구간은 Gemini 후보에 매달려 있지 않다 → 확장·클램프 lookup 무력화
    assert clips[0].chunk_index == -1 and clips[0].candidate_index == -1


def test_title_only_does_not_pin():
    """제목만 고쳤으면 구간은 종전 자동 보정을 그대로 받아야 한다 —
    제목 수정 때문에 구간 품질이 달라지면 사람이 놀란다."""
    out, pinned = apply_overrides(_variants(), {"schema": "edit_overrides/v1",
                                                "title": {"top_title": "제목만 교체"}})
    clips, title, _ = out[0]
    assert pinned is False
    assert title == "제목만 교체"
    assert [(c.start_sec, c.end_sec) for c in clips] == [(10.0, 25.0), (100.0, 120.0)]


def test_other_variants_untouched():
    """편집은 shorts #1 한 편을 고치는 일 — variant #2 는 자동 후보로 남겨둔다."""
    out, _ = apply_overrides(_variants(), OV_FULL)
    assert len(out) == 2
    assert out[1][1] == "variant2 제목"
    assert [(c.start_sec, c.end_sec) for c in out[1][0]] == [(50.0, 70.0)]


def test_no_override_is_identity():
    v = _variants()
    out, pinned = apply_overrides(v, None)
    assert out is v and pinned is False
    out2, pinned2 = apply_overrides([], OV_FULL)
    assert out2 == [] and pinned2 is False


def test_clips_absent_keeps_original_clips():
    out, pinned = apply_overrides(_variants(), {"schema": "edit_overrides/v1"})
    assert pinned is False
    assert out[0][1] == "옛 제목"                            # 제목도 그대로


def test_overrides_clips_none_when_no_key():
    assert overrides_clips({"schema": "edit_overrides/v1"}) is None
    assert overrides_clips(None) is None


def test_total_duration():
    assert total_duration([_clip("hook", 10, 25), _clip("payoff", 100, 120)]) == 35.0
    assert total_duration([]) == 0.0


# ── 로드 ──────────────────────────────────────────────────────────────
def test_load_roundtrip(tmp_path):
    p = tmp_path / "ov.json"
    p.write_text(json.dumps(OV_FULL, ensure_ascii=False), encoding="utf-8")
    assert load_edit_overrides(p)["title"]["top_title"] == "새 제목 1줄\n2줄"
    assert load_edit_overrides(None) is None


def test_load_missing_or_broken_fails_loudly(tmp_path):
    with pytest.raises(EditOverrideError):
        load_edit_overrides(tmp_path / "없는파일.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{ 깨진 json", encoding="utf-8")
    with pytest.raises(EditOverrideError):
        load_edit_overrides(bad)


# ── 메타데이터 상속 (품질 직결) ────────────────────────────────────────
def test_metadata_inherited_from_best_overlap():
    """경계를 몇 초 옮긴 것은 같은 소재를 그대로 쓰는 것 — character_focus 를 잃으면
    얼굴 추적 타겟이 사라져 리프레이밍 품질이 눈에 띄게 나빠진다."""
    olds = [
        StoryClip(role="hook", start_sec=10, end_sec=25, subtitle="옛 자막A",
                  use_original_audio=True, chunk_index=2, candidate_index=5,
                  character_focus=("혜리", "리정"), visual_essential=True, tts_draft="초안"),
        StoryClip(role="payoff", start_sec=100, end_sec=120, subtitle="옛 자막B",
                  use_original_audio=True, chunk_index=7, candidate_index=1,
                  character_focus=("미연",)),
    ]
    ov = {"schema": "edit_overrides/v1",
          "clips": [{"start_sec": 12.0, "end_sec": 24.0, "role": "hook"},      # A 와 대부분 겹침
                    {"start_sec": 105.0, "end_sec": 118.0, "role": "payoff"}]}  # B 와 겹침
    got = overrides_clips(ov, olds)
    assert got[0].character_focus == ("혜리", "리정")
    assert (got[0].chunk_index, got[0].candidate_index) == (2, 5)
    assert got[0].subtitle == "옛 자막A" and got[0].visual_essential is True
    assert got[0].tts_draft == "초안"
    assert got[1].character_focus == ("미연",) and got[1].chunk_index == 7
    # 구간 자체는 사람이 지정한 값 그대로
    assert (got[0].start_sec, got[0].end_sec) == (12.0, 24.0)


def test_new_span_with_no_overlap_gets_empty_metadata():
    """원본 어디와도 겹치지 않는 새 구간 = 그 자리에 계획된 나레이션·인물 타겟이 없다."""
    olds = [StoryClip(role="hook", start_sec=10, end_sec=25, subtitle="A",
                      use_original_audio=True, chunk_index=2, candidate_index=5,
                      character_focus=("혜리",))]
    got = overrides_clips({"schema": "edit_overrides/v1",
                           "clips": [{"start_sec": 900.0, "end_sec": 915.0}]}, olds)
    assert got[0].character_focus == () and got[0].chunk_index == -1
    assert got[0].subtitle == "" and got[0].use_original_audio is True


def test_explicit_fields_win_over_inheritance():
    olds = [StoryClip(role="hook", start_sec=10, end_sec=25, subtitle="옛 자막",
                      use_original_audio=True, chunk_index=2, candidate_index=5)]
    got = overrides_clips({"schema": "edit_overrides/v1",
                           "clips": [{"start_sec": 11.0, "end_sec": 20.0,
                                      "subtitle": "사람이 쓴 자막",
                                      "use_original_audio": False}]}, olds)
    assert got[0].subtitle == "사람이 쓴 자막" and got[0].use_original_audio is False
    assert got[0].chunk_index == 2          # 명시 안 한 값은 여전히 상속


# ── CLI 배선 ──────────────────────────────────────────────────────────
def test_cli_accepts_edit_overrides_flag():
    from app.cli import build_parser
    args = build_parser().parse_args([
        "create_shorts", "--video", "v.mp4", "--title", "작품",
        "--from-step", "render", "--job-id", "작품_abc12345",
        "--edit-overrides", "/tmp/ov.json",
    ])
    assert args.edit_overrides == "/tmp/ov.json"
    assert args.from_step == "render" and args.job_id == "작품_abc12345"
    # 미지정 시 None — 종전 동작과 완전히 동일해야 한다
    plain = build_parser().parse_args(["create_shorts", "--video", "v.mp4", "--title", "작품"])
    assert plain.edit_overrides is None


# ── 자막 오버라이드(2단계) ────────────────────────────────────────────
# 좌표계가 clips 와 다르다: clips 는 원본 절대초, subtitles 는 편집본 시간축
# (쇼츠 0초 시작). subtitle_segments.json 과 같은 축이며 그 파일이 자막의 정본이다.
OV_SUBS = {
    "schema": "edit_overrides/v1",
    "subtitles": [{"start_sec": 0.2, "end_sec": 2.3, "text": "고친 첫 줄"},
                  {"start_sec": 2.4, "end_sec": 5.0, "text": "고친 둘째 줄"}],
}


def test_subtitles_normalized_to_three_fields():
    """subtitle_segments.json 과 같은 3필드로 정규화 — 호출부가 그대로 되쓴다."""
    got = overrides_subtitles(OV_SUBS)
    assert got == [{"start_sec": 0.2, "end_sec": 2.3, "text": "고친 첫 줄"},
                   {"start_sec": 2.4, "end_sec": 5.0, "text": "고친 둘째 줄"}]
    # 문자열 숫자·여백도 받아서 정규화한다(화면 input 은 문자열을 보낸다)
    loose = overrides_subtitles({"schema": "edit_overrides/v1",
                                 "subtitles": [{"start_sec": "1", "end_sec": "2",
                                                "text": "  공백 낀 줄  "}]})
    assert loose == [{"start_sec": 1.0, "end_sec": 2.0, "text": "공백 낀 줄"}]


def test_subtitles_absent_returns_none():
    """키가 없으면 None — 종전 동작(캐시·재매핑 결과)을 그대로 쓴다."""
    assert overrides_subtitles(None) is None
    assert overrides_subtitles({"schema": "edit_overrides/v1"}) is None
    assert overrides_subtitles(OV_FULL) is None          # 구간만 고친 요청


def test_subtitles_and_clips_coexist():
    """구간+자막 동시 수정: clips 는 pinned 를 켜고, 자막은 따로 살아남는다."""
    ov = {**OV_FULL, "subtitles": OV_SUBS["subtitles"]}
    validate_overrides(ov)
    variants, pinned = apply_overrides(_variants(), ov)
    assert pinned is True and len(variants[0][0]) == 2
    assert overrides_subtitles(ov) is not None           # 자막이 clips 에 먹히지 않는다


@pytest.mark.parametrize("bad,msg", [
    ([], "비어 있지 않은 배열"),
    ("문자열", "비어 있지 않은 배열"),
    ([{"start_sec": 5.0, "end_sec": 2.0, "text": "뒤집힘"}], "뒤집혔"),
    ([{"start_sec": -1.0, "end_sec": 2.0, "text": "음수"}], "뒤집혔"),
    ([{"end_sec": 2.0, "text": "시작없음"}], "start_sec"),
    ([{"start_sec": 0.0, "end_sec": 2.0, "text": "   "}], "비어 있습니다"),
    ([{"start_sec": 0.0, "end_sec": 2.0}], "비어 있습니다"),
])
def test_subtitles_contract_violations_fail_loudly(bad, msg):
    with pytest.raises(EditOverrideError) as e:
        validate_overrides({"schema": "edit_overrides/v1", "subtitles": bad})
    assert msg in str(e.value)


def test_subtitles_overlap_is_allowed():
    """겹치는 자막을 거부하면 안 된다 — 2026-08-17 실측에서 걷어낸 검사다.

    실제 subtitle_segments.json 이 겹치는 세그먼트를 정상적으로 담는다(피의_게임_X_9d2d1b85
    는 20건 중 여러 쌍이 겹쳤다). 전량 교체 규약상 편집실은 원본 목록을 그대로 되돌려
    보내므로, 겹침을 막으면 한 줄만 고쳐도 전체가 거부된다."""
    ov = {"schema": "edit_overrides/v1",
          "subtitles": [{"start_sec": 0.2, "end_sec": 3.67, "text": "앞 문장"},
                        {"start_sec": 1.7, "end_sec": 5.3, "text": "겹치는 뒷 문장"},
                        {"start_sec": 2.0, "end_sec": 4.0, "text": "완전히 안긴 문장"}]}
    assert validate_overrides(ov) is ov
    assert len(overrides_subtitles(ov)) == 3


def test_load_subtitles_roundtrip(tmp_path):
    p = tmp_path / "ov.json"
    p.write_text(json.dumps(OV_SUBS, ensure_ascii=False), encoding="utf-8")
    got = load_edit_overrides(p)
    assert overrides_subtitles(got)[1]["text"] == "고친 둘째 줄"


# ── YouTube 403 대응(2026-08-18) ──────────────────────────────────────
def test_youtube_access_opts_defaults_and_cookies():
    """403 회피 손잡이는 **env 로** 돌아가야 한다 — 차단이 왔을 때 코드 재배포 없이
    노드에서 즉시 켤 수 있어야 하기 때문이다(2026-08-18 실측: 최신 yt-dlp 에서도
    YouTube 소스 5건이 전부 403, 같은 시각 드라이브 소스 12건은 전부 성공).

    🛑 기본값은 `default` 하나다. mm-06 실측에서 web_safari·web_embedded 는 쿠키가
    있어도 'Requested format is not available'(PO 토큰 없이는 포맷이 안 나온다), tv 는
    'The page needs to be reloaded' 였다. 그런 클라이언트를 목록에 섞은 것이 처음의
    `tv,web_safari,default` 였고, 그게 막힌 문을 세 개 두드리는 설정이었다.
    통과한 것은 쿠키 있는 android_vr 과 default 둘뿐이다 — 다중화가 아니라 쿠키가 답이었다."""
    from app.modules.youtube_downloader import youtube_access_opts
    d = youtube_access_opts({})
    assert d["extractor_args"]["youtube"]["player_client"] == ["default"]
    assert d["retries"] >= 10 and d["fragment_retries"] >= 10
    assert "cookiefile" not in d and "cookiesfrombrowser" not in d   # 기본은 쿠키 없음
    got = youtube_access_opts({"YTDLP_PLAYER_CLIENT": "ios, tv"})
    assert got["extractor_args"]["youtube"]["player_client"] == ["ios", "tv"]
    assert youtube_access_opts({"YTDLP_COOKIES": "/opt/ves/secrets/yt.txt"})["cookiefile"] \
        == "/opt/ves/secrets/yt.txt"
    assert youtube_access_opts({"YTDLP_COOKIES_FROM_BROWSER": "chrome:Default"})["cookiesfrombrowser"] \
        == ("chrome", "Default")
    # 빈 값은 '안 켬'(공백만 넣은 실수가 쿠키 경로로 둔갑하지 않게)
    assert "cookiefile" not in youtube_access_opts({"YTDLP_COOKIES": "   "})


def test_downloader_uses_access_opts():
    """다운로더가 그 옵션을 실제로 ydl_opts 에 합치는지 — 함수만 있고 안 쓰면 소용없다."""
    import inspect
    from app.modules import youtube_downloader as yd
    src = inspect.getsource(yd.download_youtube_assets)
    assert "**youtube_access_opts()" in src


def test_downloader_never_resumes():
    """🛑 남은 .part 가 실패를 영구화한다 — 2026-08-18 실측의 진짜 범인.

    08-18 새벽 첫 실패가 source.f137.mp4.part 를 9,991,353 바이트 남겼다. 이후 모든
    재시도가 `Resuming download at byte 9991353` → `HTTP Error 403` 이었다. 유튜브
    스트림 URL 은 수명이 짧아 몇 분~몇 시간 뒤의 Range 재개는 사실상 항상 거절된다.
    그래서 원인(로그인 요구)이 쿠키로 사라진 뒤에도 5개 채널이 계속 죽었고, 깨끗한
    디렉토리로 손수 재현하면 언제나 성공해서 원인을 한참 못 찾았다.

    이어받기를 켜 두면 이 사고가 그대로 돌아온다."""
    import inspect
    from app.modules import youtube_downloader as yd
    src = inspect.getsource(yd.download_youtube_assets)
    assert '"continuedl": False' in src, "이어받기를 끄지 않으면 .part 가 실패를 붙잡는다"


def test_requirements_pins_verified_ytdlp_build():
    """🛑 yt-dlp 는 **실측으로 통과를 확인한 빌드에 핀**한다 — 하한(>=)이 아니다.

    2026-08-18: 유튜브가 android_vr 을 막아(상류 주석 "Since 2026.08.17, ALL formats …
    are 403'd") 2026.7.4 의 기본 클라이언트가 통째로 죽었다. 1080p 를 받다가 항상 5.3%
    부근에서 403. 08-18 나이틀리가 기본에서 android_vr 을 빼고 visionos 를 올리자 같은
    영상이 완주했다(mm-05 실측).

    하한으로 두면 pip 가 업그레이드를 안 해서(updater._pip_sync) 6대가 서로 다른 빌드를
    쓰게 되고, 그러면 '어떤 맥에서만 죽는다'가 된다. 반대로 미검증 나이틀리가 자동으로
    들어오는 것도 위험하다. 그래서 == 로 못 박는다.

    curl-cffi extra 는 import 되는 곳이 없어(yt-dlp 가 런타임에 찾는다) 지워져도 조용하다."""
    import pathlib as _p
    import re
    req = _p.Path(__file__).resolve().parent.parent / "requirements.txt"
    line = next((ln.strip() for ln in req.read_text(encoding="utf-8").splitlines()
                 if ln.strip().startswith("yt-dlp")), None)
    assert line, "yt-dlp 요구가 사라졌다"
    assert "[curl-cffi]" in line, f"curl-cffi extra 가 빠졌다: {line}"
    assert re.match(r"^yt-dlp\[curl-cffi\]==\d", line), f"하한이 아니라 == 핀이어야 한다: {line}"


# ── 내레이션(tts, v2) ─────────────────────────────────────────────────
def _old_cues():
    """엔진이 만든 앵커 cue — 편집실이 화면에 보여주고 되돌려 보내는 원본."""
    return [
        {"text": "원래 내레이션 A", "source_time_sec": 743.0, "duration_sec": 3.5,
         "voice": "ko_male", "speed": "fast", "chunk_index": 3, "candidate_index": 1},
        {"text": "원래 내레이션 B", "source_time_sec": 1195.5, "duration_sec": 4.0,
         "voice": "ko_female", "speed": "normal", "chunk_index": 5, "candidate_index": 0},
    ]


def test_tts_text_edit_inherits_anchor_metadata():
    # 문구만 고침 — source_time 이 같으므로 앵커 메타데이터(chunk/candidate)와
    # duration·voice·speed 를 옛 cue 에서 그대로 물려받아야 한다.
    ov = {"schema": "edit_overrides/v2",
          "tts": [{"source_time_sec": 743.0, "text": "고친 내레이션 A"},
                  {"source_time_sec": 1195.5, "text": "원래 내레이션 B"}]}
    validate_overrides(ov)
    got = overrides_tts(ov, _old_cues())
    assert [c["text"] for c in got] == ["고친 내레이션 A", "원래 내레이션 B"]
    assert got[0]["chunk_index"] == 3 and got[0]["candidate_index"] == 1
    assert got[0]["voice"] == "ko_male" and got[0]["speed"] == "fast"
    assert got[0]["duration_sec"] == 3.5


def test_tts_delete_by_omission_and_full_wipe():
    # 전량 교체 — 한 건을 빼면 그 내레이션이 삭제, 빈 배열은 전부 삭제(유효).
    ov = {"schema": "edit_overrides/v2",
          "tts": [{"source_time_sec": 743.0, "text": "남는 것"}]}
    assert len(overrides_tts(ov, _old_cues())) == 1
    wipe = {"schema": "edit_overrides/v2", "tts": []}
    validate_overrides(wipe)
    assert overrides_tts(wipe, _old_cues()) == []


def test_tts_new_cue_gets_defaults_and_no_anchor():
    # 어느 옛 cue 와도 안 붙는 새 내레이션 — 앵커 -1, 기본 목소리, 명시한 창 길이.
    ov = {"schema": "edit_overrides/v2",
          "tts": [{"source_time_sec": 2411.0, "duration_sec": 3.0, "text": "새 내레이션"}]}
    got = overrides_tts(ov, _old_cues())
    assert got[0]["chunk_index"] == -1 and got[0]["candidate_index"] == -1
    assert got[0]["voice"] == "ko_female" and got[0]["duration_sec"] == 3.0


def test_tts_explicit_fields_win_over_inheritance():
    ov = {"schema": "edit_overrides/v2",
          "tts": [{"source_time_sec": 743.0, "duration_sec": 2.0,
                   "voice": "ko_female_high", "text": "명시가 이긴다"}]}
    got = overrides_tts(ov, _old_cues())
    assert got[0]["duration_sec"] == 2.0 and got[0]["voice"] == "ko_female_high"
    assert got[0]["chunk_index"] == 3          # 앵커는 여전히 상속


def test_tts_sorted_by_source_time():
    ov = {"schema": "edit_overrides/v2",
          "tts": [{"source_time_sec": 1195.5, "text": "뒤"},
                  {"source_time_sec": 743.0, "text": "앞"}]}
    assert [c["text"] for c in overrides_tts(ov, _old_cues())] == ["앞", "뒤"]


def test_tts_absent_key_returns_none():
    assert overrides_tts({"schema": "edit_overrides/v2",
                          "title": {"top_title": "x"}}, _old_cues()) is None
    assert overrides_tts(None, _old_cues()) is None


@pytest.mark.parametrize("bad,msg", [
    ({"tts": {"a": 1}}, "배열"),
    ({"tts": [{"text": "시각 없음"}]}, "source_time_sec"),
    ({"tts": [{"source_time_sec": -1, "text": "x"}]}, "음수"),
    ({"tts": [{"source_time_sec": 10, "text": "  "}]}, "text"),
    ({"tts": [{"source_time_sec": 10, "duration_sec": 0, "text": "x"}]}, "양수"),
])
def test_tts_contract_violations_fail_loudly(bad, msg):
    ov = {"schema": "edit_overrides/v2", **bad}
    with pytest.raises(EditOverrideError, match=msg):
        validate_overrides(ov)


# ── v3: 자막 앵커(F-401)·줄 스타일(F-407)·images ──────────────────────
def _sub(start, end, text, **extra):
    return {"start_sec": start, "end_sec": end, "text": text, **extra}


OV_V3 = {
    "schema": "edit_overrides/v3",
    "subtitles": [
        _sub(0.2, 2.3, "앵커 자막", source_time_sec=743.2,
             style={"size": 64, "y": 0.8, "color": "#FFDD00"}),
        _sub(2.4, 5.0, "신규 줄 (앵커 없음)"),
    ],
}


def test_v3_schema_accepts_anchor_style_images():
    validate_overrides(OV_V3)
    validate_overrides({"schema": "edit_overrides/v3",
                        "images": [{"file": "assets/arrow.png", "source_time_sec": 745.0,
                                    "duration_sec": 2.0, "x": 0.1, "y": 0.2, "w": 0.3,
                                    "layer": 1}]})
    # v3 는 v2 의 초집합 — tts 도 그대로 받는다
    validate_overrides({"schema": "edit_overrides/v3",
                        "tts": [{"source_time_sec": 743.0, "text": "내레이션"}]})


def test_v3_fields_require_v3_stamp():
    """v3 필드가 v1·v2 스탬프에 실려 오면 즉시 거절 — 구 엔진은 이 필드를 조용히
    무시하므로, 스탬프 없이 받아주면 노드마다 결과가 달라진다."""
    for schema in ("edit_overrides/v1", "edit_overrides/v2"):
        with pytest.raises(EditOverrideError, match="v3 전용"):
            validate_overrides({"schema": schema,
                                "subtitles": [_sub(0.2, 2.3, "x", source_time_sec=743.2)]})
        with pytest.raises(EditOverrideError, match="v3 전용"):
            validate_overrides({"schema": schema,
                                "subtitles": [_sub(0.2, 2.3, "x", style={"size": 64})]})
        with pytest.raises(EditOverrideError, match="v3 전용"):
            validate_overrides({"schema": schema,
                                "images": [{"file": "a.png", "source_time_sec": 1,
                                            "duration_sec": 1, "x": 0, "y": 0, "w": 0.5}]})


@pytest.mark.parametrize("bad,msg", [
    (_sub(0, 2, "x", source_time_sec=-1), "음수"),
    (_sub(0, 2, "x", source_time_sec="abc"), "숫자"),
    (_sub(0, 2, "x", style={}), "비어 있지 않은 객체"),
    (_sub(0, 2, "x", style={"font": "Jalnan"}), "모르는 키"),
    (_sub(0, 2, "x", style={"size": 0}), "양수"),
    (_sub(0, 2, "x", style={"size": "크게"}), "숫자"),
    (_sub(0, 2, "x", style={"y": 1.5}), "0~1"),
    (_sub(0, 2, "x", style={"color": "노랑"}), "#RRGGBB"),
    (_sub(0, 2, "x", style={"color": "#FFF"}), "#RRGGBB"),
])
def test_v3_subtitle_field_violations_fail_loudly(bad, msg):
    with pytest.raises(EditOverrideError, match=msg):
        validate_overrides({"schema": "edit_overrides/v3", "subtitles": [bad]})


@pytest.mark.parametrize("bad,msg", [
    ({"source_time_sec": 1, "duration_sec": 1, "x": 0, "y": 0, "w": 0.5}, "file"),
    ({"file": "/abs/a.png", "source_time_sec": 1, "duration_sec": 1,
      "x": 0, "y": 0, "w": 0.5}, "상대 경로"),
    ({"file": "../탈출.png", "source_time_sec": 1, "duration_sec": 1,
      "x": 0, "y": 0, "w": 0.5}, "상대 경로"),
    ({"file": "a.png", "duration_sec": 1, "x": 0, "y": 0, "w": 0.5}, "source_time_sec"),
    ({"file": "a.png", "source_time_sec": 1, "x": 0, "y": 0, "w": 0.5}, "duration_sec"),
    ({"file": "a.png", "source_time_sec": 1, "duration_sec": 0,
      "x": 0, "y": 0, "w": 0.5}, "양수"),
    ({"file": "a.png", "source_time_sec": 1, "duration_sec": 1,
      "x": 1.2, "y": 0, "w": 0.5}, "0~1"),
    ({"file": "a.png", "source_time_sec": 1, "duration_sec": 1,
      "x": 0, "y": 0, "w": 0.5, "layer": "위"}, "정수"),
    ({"file": "a.gif", "source_time_sec": 1, "duration_sec": 1,
      "x": 0, "y": 0, "w": 0.5}, "확장자"),
])
def test_v3_image_violations_fail_loudly(bad, msg):
    with pytest.raises(EditOverrideError, match=msg):
        validate_overrides({"schema": "edit_overrides/v3", "images": [bad]})


# ── images 파일 해석(F-408) — run_dir 상대 경로 → 절대 경로, 없는 파일은 fail-loud ──
def _img(file="assets/arrow.png", **extra):
    return {"file": file, "source_time_sec": 745.0, "duration_sec": 2.0,
            "x": 0.1, "y": 0.2, "w": 0.3, **extra}


def test_resolve_image_files_returns_absolute_paths(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "arrow.png").write_bytes(b"\x89PNG fake")
    ov = {"schema": "edit_overrides/v3", "images": [_img()]}
    got = resolve_image_files(ov, tmp_path)
    assert len(got) == 1
    from pathlib import Path as _P
    assert _P(got[0]["file"]).is_absolute()
    assert _P(got[0]["file"]) == (tmp_path / "assets" / "arrow.png").resolve()
    # 원본 dict 는 건드리지 않는다 (file 만 절대 경로로 치환한 사본)
    assert ov["images"][0]["file"] == "assets/arrow.png"
    # images 없으면 빈 목록 — 종전 동작 불변
    assert resolve_image_files(None, tmp_path) == []
    assert resolve_image_files({"schema": "edit_overrides/v3"}, tmp_path) == []


def test_resolve_image_files_missing_file_fails_loudly(tmp_path):
    """사람이 올린 이미지가 run_dir 에 없으면 렌더 전에 즉시 실패 — 조용히 빼고
    렌더하면 이미지가 소리 없이 사라진 영상이 나간다(제1원칙)."""
    ov = {"schema": "edit_overrides/v3", "images": [_img("없는파일.png")]}
    with pytest.raises(EditOverrideError, match="파일이 없습니다"):
        resolve_image_files(ov, tmp_path)


def test_resolve_image_files_enforces_size_cap(tmp_path):
    (tmp_path / "big.png").write_bytes(b"\x00" * (IMAGE_MAX_BYTES + 1))
    (tmp_path / "empty.png").write_bytes(b"")
    with pytest.raises(EditOverrideError, match="상한"):
        resolve_image_files(
            {"schema": "edit_overrides/v3", "images": [_img("big.png")]}, tmp_path)
    with pytest.raises(EditOverrideError, match="빈 파일"):
        resolve_image_files(
            {"schema": "edit_overrides/v3", "images": [_img("empty.png")]}, tmp_path)


def test_overrides_subtitles_passes_anchor_and_style_through():
    got = overrides_subtitles(OV_V3)
    assert got[0]["source_time_sec"] == 743.2
    assert got[0]["style"] == {"size": 64.0, "y": 0.8, "color": "#FFDD00"}
    assert "source_time_sec" not in got[1] and "style" not in got[1]


# ── v3 앵커 배치(F-401) — tts 와 같은 규칙: 담은 클립 오프셋 + 클립 내 상대시각 ──
def _final_clips():
    """최종 타임라인: [10~25) → 편집 0~15, [100~120) → 편집 15~35."""
    return [_clip("hook", 10, 25), _clip("payoff", 100, 120)]


def test_anchored_subtitle_follows_clip_offset():
    subs = [_sub(0.0, 2.0, "둘째 클립 자막", source_time_sec=105.0,
                 style={"size": 64.0}),
            _sub(3.0, 5.0, "첫째 클립 자막", source_time_sec=12.0)]
    placed, dropped = place_anchored_subtitles(subs, _final_clips())
    assert dropped == []
    # 시간순 정렬: 12.0 → 편집 2.0 이 앞, 105.0 → 편집 20.0 이 뒤
    assert [(s["start_sec"], s["end_sec"], s["text"]) for s in placed] == [
        (2.0, 4.0, "첫째 클립 자막"), (20.0, 22.0, "둘째 클립 자막")]
    # 변환 후 source_time_sec 은 사라지고(캐시 = 편집본 시간축 정본) style 은 남는다
    assert "source_time_sec" not in placed[1]
    assert placed[1]["style"] == {"size": 64.0}


def test_anchored_subtitle_survives_clip_change():
    """F-401 의 핵심: 구간을 고쳐도 앵커 자막은 화면 내용을 따라간다.

    같은 자막(원본 105초)이, 클립 경계가 [100~120)→[95~120)로 바뀌면
    편집 시각만 달라진 채 같은 장면 위에 남아야 한다."""
    subs = [_sub(0.0, 2.0, "따라오는 자막", source_time_sec=105.0)]
    placed_a, _ = place_anchored_subtitles(subs, [_clip("hook", 10, 25), _clip("payoff", 100, 120)])
    placed_b, _ = place_anchored_subtitles(subs, [_clip("hook", 10, 25), _clip("payoff", 95, 120)])
    assert placed_a[0]["start_sec"] == 20.0          # 15 + (105-100)
    assert placed_b[0]["start_sec"] == 25.0          # 15 + (105-95) — 장면은 동일


def test_anchor_slop_clamps_into_nearest_clip():
    """포함 판정 슬롭 ±0.5s — 편집실 UI 와 동일 규약. 경계 반올림 오차로 클립 밖에
    떨어진 앵커는 가장 가까운 클립 안쪽으로 클램프되고, 슬롭 밖(0.6s)은 고아다."""
    placed, dropped = place_anchored_subtitles(
        [_sub(0.0, 2.0, "경계 직전", source_time_sec=9.6),      # 10-0.4 → 클립 시작으로
         _sub(0.0, 2.0, "슬롭 밖", source_time_sec=9.4)],       # 10-0.6 → 고아
        _final_clips())
    assert [s["text"] for s in dropped] == ["슬롭 밖"]
    assert placed[0]["start_sec"] == 0.0                         # 편집 0 (클립 시작 클램프)
    assert placed[0]["end_sec"] == 2.0


def test_anchor_at_timeline_tail_too_short_is_dropped():
    """앵커가 마지막 클립 끝(슬롭 안)에 붙으면 변환 후 표시 시간이 0 — 화면에 못
    남으므로 고아와 같이 드롭 목록으로 돌려준다(호출부가 로그로 남긴다)."""
    placed, dropped = place_anchored_subtitles(
        [_sub(0.0, 2.0, "끝자락", source_time_sec=120.3)], _final_clips())
    assert placed == [] and len(dropped) == 1


def test_orphan_anchor_dropped_and_reported():
    """앵커 소재가 최종 구간에 없으면 tts 고아 규칙과 동일 = 드롭. 조용히 사라지면
    안 되므로 드롭 목록을 함께 돌려준다 — 호출부(pipeline)가 로그로 남긴다."""
    placed, dropped = place_anchored_subtitles(
        [_sub(0.0, 2.0, "잘려나간 장면", source_time_sec=500.0),
         _sub(3.0, 5.0, "남은 장면", source_time_sec=12.0)],
        _final_clips())
    assert [s["text"] for s in placed] == ["남은 장면"]
    assert [s["text"] for s in dropped] == ["잘려나간 장면"]
    assert dropped[0]["source_time_sec"] == 500.0    # 로그에 좌표를 남길 수 있게 보존


def test_unanchored_subtitle_keeps_edit_timeline_coords():
    """앵커 없는 항목(신규 줄)은 종전대로 start_sec(편집본 시간축) 그대로 —
    v1·v2 자막 오버라이드의 동작 불변."""
    subs = [_sub(1.0, 3.0, "신규 줄", style={"color": "#FF0000"})]
    placed, dropped = place_anchored_subtitles(subs, _final_clips())
    assert dropped == []
    assert placed == [{"start_sec": 1.0, "end_sec": 3.0, "text": "신규 줄",
                       "style": {"color": "#FF0000"}}]
    # 클립이 아예 없어도(방어) 무앵커 항목은 통과한다
    placed2, _ = place_anchored_subtitles(subs, [])
    assert placed2[0]["text"] == "신규 줄"


def test_v3_full_roundtrip_with_clips(tmp_path):
    """구간+앵커 자막 동시 제출 — v3 의 존재 이유. clips 는 pinned, 자막은 앵커를
    따라 새 구간 위에 배치된다."""
    ov = {"schema": "edit_overrides/v3",
          "clips": [{"start_sec": 95.0, "end_sec": 120.0, "role": "hook"}],
          "subtitles": [_sub(0.0, 2.0, "따라오는 자막", source_time_sec=105.0,
                             style={"size": 60, "y": 0.75, "color": "#FFDD00"})]}
    p = tmp_path / "ov.json"
    p.write_text(json.dumps(ov, ensure_ascii=False), encoding="utf-8")
    loaded = load_edit_overrides(p)
    variants, pinned = apply_overrides(_variants(), loaded)
    assert pinned is True
    placed, dropped = place_anchored_subtitles(
        overrides_subtitles(loaded), variants[0][0])
    assert dropped == []
    assert placed[0]["start_sec"] == 10.0            # 0 + (105-95)
    assert placed[0]["style"]["color"] == "#FFDD00"


# ── v3 images 배치(F-408) — 자막 앵커와 같은 규칙, 창 길이는 duration_sec ──
def test_anchored_image_follows_clip_offset():
    imgs = [_img(source_time_sec=105.0, duration_sec=2.0, layer=1),
            _img(source_time_sec=12.0, duration_sec=3.0)]
    placed, dropped = place_anchored_images(imgs, _final_clips())
    assert dropped == []
    # 배열 순서 보존 — 같은 layer 의 쌓임 순서 계약이라 시각순 정렬하지 않는다
    assert [(i["start_sec"], i["end_sec"]) for i in placed] == [(20.0, 22.0), (2.0, 5.0)]
    # 변환 후 원본축 좌표는 사라지고 렌더러 입력(위치·레이어)은 그대로 통과
    assert "source_time_sec" not in placed[0] and "duration_sec" not in placed[0]
    assert (placed[0]["x"], placed[0]["y"], placed[0]["w"], placed[0]["layer"]) == (0.1, 0.2, 0.3, 1)


def test_anchored_image_orphan_dropped_and_tail_clamped():
    """클립 밖 앵커는 tts 고아 규칙 = 드롭(호출부가 로그). 타임라인 끝을 넘는 창은
    클램프되고, 클램프 후 0.1s 미만이면 역시 드롭이다."""
    placed, dropped = place_anchored_images(
        [_img(source_time_sec=500.0),                       # 최종 구간 밖 → 고아
         _img(source_time_sec=119.0, duration_sec=5.0),     # 편집 34.0 + 5.0 → 35.0 클램프
         _img(source_time_sec=120.3, duration_sec=2.0)],    # 슬롭 매칭 끝자락 → 0s → 드롭
        _final_clips())
    assert len(dropped) == 2
    assert dropped[0]["source_time_sec"] == 500.0            # 로그용 좌표 보존
    assert [(i["start_sec"], i["end_sec"]) for i in placed] == [(34.0, 35.0)]


def test_anchored_image_slop_matches_boundary():
    """포함 판정 슬롭 ±0.5s — 자막·편집실 UI 와 동일 규약."""
    placed, dropped = place_anchored_images(
        [_img(source_time_sec=9.6, duration_sec=2.0)], _final_clips())
    assert dropped == []
    assert (placed[0]["start_sec"], placed[0]["end_sec"]) == (0.0, 2.0)

