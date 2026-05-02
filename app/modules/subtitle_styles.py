"""라운드 10 — 장르 기반 자막 스타일 프리셋 라이브러리.

6개 프리셋 (2026 한국어 쇼츠 트렌드 리서치 기반):
    hormozi       — TikTok-Bounce 워드팝 / 시사·다큐·정보형
    kvar_yellow   — K-예능 굵은 노랑 / 런닝맨류 예능·먹방
    drama_cine    — 시네마틱 멜로 / 로맨스 멜로·정통 드라마
    thriller_mono — 범죄·스릴러 모노 / 범죄·미스터리
    y2k_pink      — Y2K 핑크 버블 / 로맨틱 코미디·뷰티
    kakao_bubble  — 카톡 말풍선 박스 / 시트콤·일상 브이로그

폰트 자산 한계로 v1은 보유 폰트 4종(Jalnan, JalnanGothic, Griun, mulmaru)으로 매핑.
폰트 시각 차이는 약하지만 색상·외곽선·박스 BorderStyle로 시각 차별화.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.modules.subtitle import SubtitleStyle


# 6 프리셋 — 보유 폰트로 매핑된 v1
# margin_v: 라운드 5 동적 계산이 우선 적용되므로 여기 값은 폴백용 base.
SUBTITLE_PRESETS: dict[str, SubtitleStyle] = {
    "hormozi": SubtitleStyle(
        font_name="JalnanGothic",
        font_size=70,
        primary_color="&H00FFFFFF",      # 흰
        outline_color="&H00000000",       # 검정 외곽
        outline=4,
        shadow=0,
        margin_v=320,
        bold=True,
        alignment=2,
        border_style=1,
        back_color="&H00000000",
    ),
    "kvar_yellow": SubtitleStyle(
        font_name="JalnanGothic",
        font_size=72,
        primary_color="&H0000FFFF",       # 노랑
        outline_color="&H00000000",
        outline=6,
        shadow=2,
        margin_v=380,
        bold=True,
        alignment=2,
        border_style=1,
        back_color="&H00000000",
    ),
    "drama_cine": SubtitleStyle(
        font_name="mulmaru",
        font_size=58,
        primary_color="&H00FFFFFF",
        outline_color="&H00202020",       # 어두운 회색
        outline=1,
        shadow=1,
        margin_v=180,
        bold=False,
        alignment=2,
        border_style=1,
        back_color="&H00000000",
    ),
    "thriller_mono": SubtitleStyle(
        font_name="JalnanGothic",
        font_size=64,
        primary_color="&H00F5F5F5",       # 오프화이트
        outline_color="&H00000000",
        outline=3,
        shadow=3,
        margin_v=320,
        bold=True,
        alignment=2,
        border_style=1,
        back_color="&H00000000",
    ),
    "y2k_pink": SubtitleStyle(
        font_name="Griun",
        font_size=70,
        primary_color="&H00FFFFFF",
        outline_color="&H00B469FF",       # 핫핑크 외곽 (BGR: B=B4, G=69, R=FF)
        outline=5,
        shadow=0,
        margin_v=380,
        bold=False,
        alignment=2,
        border_style=1,
        back_color="&H00000000",
    ),
    "kakao_bubble": SubtitleStyle(
        font_name="mulmaru",
        font_size=56,
        primary_color="&H00000000",       # 검정 텍스트
        outline_color="&H00FFFFFF",       # 흰 외곽 (border_style=3에선 잘 안 보임)
        outline=0,
        shadow=0,
        margin_v=380,
        bold=False,
        alignment=2,
        border_style=3,                   # opaque box
        back_color="&H00FFFFFF",          # 흰 박스 배경
    ),
}


# 장르 enum → 프리셋 ID
GENRE_TO_PRESET: dict[str, str] = {
    "romance":  "drama_cine",
    "romcom":   "y2k_pink",
    "thriller": "thriller_mono",
    "variety":  "kvar_yellow",
    "talk":     "kakao_bubble",
    "docu":     "hormozi",
    "vlog":     "kvar_yellow",
    "other":    "kvar_yellow",            # 안전한 default
}

DEFAULT_PRESET = "kvar_yellow"

# 사용자에게 노출되는 CLI 옵션 ID 목록
PRESET_IDS = list(SUBTITLE_PRESETS.keys())


def select_subtitle_style(
    genre_tag: str | None,
    cli_override: str | None = None,
    base_overrides: dict[str, Any] | None = None,
) -> tuple[SubtitleStyle, str]:
    """장르 태그 + CLI override → (최종 SubtitleStyle, 적용된 preset id).

    우선순위: cli_override > genre_tag 매핑 > DEFAULT_PRESET
    base_overrides: DesignConfig의 사용자 명시 (subtitle_size 등)는 프리셋 위에 덮어쓴다.
    """
    if cli_override and cli_override != "auto" and cli_override in SUBTITLE_PRESETS:
        preset_id = cli_override
    elif genre_tag and genre_tag in GENRE_TO_PRESET:
        preset_id = GENRE_TO_PRESET[genre_tag]
    else:
        preset_id = DEFAULT_PRESET
    style = SUBTITLE_PRESETS.get(preset_id, SUBTITLE_PRESETS[DEFAULT_PRESET])
    if base_overrides:
        # 사용자 CLI(--design-subtitle-size 등)는 프리셋 위에 덮어쓰기
        clean_overrides = {k: v for k, v in base_overrides.items() if v is not None}
        if clean_overrides:
            style = replace(style, **clean_overrides)
    return style, preset_id
