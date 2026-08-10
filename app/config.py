from __future__ import annotations

from dataclasses import dataclass,field
from pathlib import Path
import requests
import os

@dataclass(frozen=True)
class DesignConfig:
    # 영상 프레임 (9:16, 1:1 등)
    aspect_ratio: str = "1:1"
    video_width: int = 800
    video_height: int = 1100
    video_y_pos: int = 480

    # 제목 설정 (2줄 구조: line1=맥락, line2=후킹)
    title_font: str = "Jalnan"
    title_size: int = 70
    title_color: str = "white"
    title_colors: list[str] = field(default_factory=lambda: ["white", "#FFFF00"])
    title_sizes: list[int] = field(default_factory=lambda: [70, 90])
    title_y: int = 120

    # 자막 설정 (ASS 스타일 기준)
    subtitle_font: str = "여기어때 잘난체 2 TTF"
    subtitle_size: int = 65
    # None = 라운드 10 장르 프리셋의 색을 그대로 사용. 명시하면 프리셋 위에 덮어쓴다.
    subtitle_color: str | None = None
    subtitle_y_margin: int = 380
    # 라운드 10: 자막 스타일 프리셋 (None / "auto" → 장르 기반 자동 선택)
    subtitle_style_preset: str | None = None

    # TTS 자막 설정 (원본 자막과 별도 위치)
    tts_line_font_size: int = 70
    tts_line_color: str = "&H00EBCE87"
    tts_line_y_margin: int = 580

    # 작품명 및 이미지 설정
    work_title_y: int = 1400
    work_font_size: int = 40
    work_color: str = "#FF69B4"
    work_letter_spacing: bool = True

    work_type: str = "text"
    work_value: str | None = None
    # 라운드 24: 로고는 (width x height) 박스 안에 비율 유지로 맞춘다(contain).
    # 두 축을 동시에 제한하는 것이 핵심 — 로고 비율이 작품마다 달라(세로 캘리그래피 0.5:1 ~
    # 가로 워드마크 10:1) 한 축만 고정하면 반드시 반대 모양에서 깨진다. 세로형은 높이가,
    # 가로형은 너비가 박스에 닿는다.
    # 395x280 은 도깨비·유미 실물로 사람이 고른 크기에서 역산한 값(오차 0.3%).
    work_image_width: int = 395
    work_image_height: int | None = 280
    # top=영상 하단에 붙임 · center=영상 하단~캔버스 하단 밴드의 세로 중앙.
    # center 는 로고 높이가 달라져도 균형이 유지돼 작품별로 y 를 다시 찾지 않아도 된다.
    work_image_align: str = "center"

    overlay_image_path: str | None = None # 필요 시 이미지 경로

    # Phase 11: 화자 우선 얼굴 트래킹 (False면 "가장 큰 얼굴" 폴백)
    enable_speaker_tracking: bool = True

    # Phase 12: 인물 인식 기반 얼굴 추적 (deepface + TMDb 배우 사진)
    enable_face_recognition: bool = True

    # 리프레이밍(얼굴 추종 크롭) 자체를 끈다 — False 면 크롭 타임라인을 만들지도, 쓰지도 않고
    # 원본을 가운데 정렬로 넣는다. 위 두 플래그는 '누구를 따라갈지'를 고르는 것이라 꺼도 크롭은
    # 계속 일어난다는 점에서 다르다.
    # 왜 필요한가(2026-08-10 커리어데이 실측): 인물이 고정된 인터뷰 소재는 얼굴로 확대할 이유가
    # 적은데, 확대하면 **원본에 박힌 자막이 잘려나간다**. 크롭 타임라인 생성은 resources 단계에서
    # 가장 느린 축이라 끄면 생성도 그만큼 빨라진다.
    enable_reframe: bool = True

@dataclass(frozen=True)
class AppConfig:
    chunk_seconds: int = 600
    chunk_overlap: int = 180  # 라운드 19B: 30 → 180. chunks 1+에서 앞 청크 3분 prepend로 경계 사건 손실 방지.
    # 길이 노브 — A/B 에서 env 로 조정 (기본값은 기존 동작 유지). 시장 승자 평균 ~46s.
    target_duration_sec: int = field(default_factory=lambda: int(os.environ.get("TARGET_DURATION_SEC", "50")))
    target_duration_tolerance_sec: int = 10
    min_duration_sec: int = field(default_factory=lambda: int(os.environ.get("MIN_DURATION_SEC", "40")))
    max_duration_sec: int = field(default_factory=lambda: int(os.environ.get("MAX_DURATION_SEC", "60")))  # 라운드 11: 100 → 60
    # storyline 총 길이 상한 배수 (validate_story_clips). 1.5=레거시(최대 90s 허용 → 100s 클립 원인).
    # env MAX_DURATION_TOLERANCE 로 조정. aggressive A/B 는 1.1 권장(≈66s 상한).
    max_duration_tolerance: float = field(default_factory=lambda: float(os.environ.get("MAX_DURATION_TOLERANCE", "1.5")))
    # 무음 컷 공격성 프로파일 — "conservative"(베이스라인) | "aggressive"(가설). env SILENCE_CUT_PROFILE.
    silence_cut_profile: str = field(default_factory=lambda: os.environ.get("SILENCE_CUT_PROFILE", "conservative"))
    canvas_width: int = 1080
    canvas_height: int = 1920
    top_title_height: int = 250
    bottom_label_height: int = 200
    subtitle_margin_top: int = 320
    subtitle_margin_bottom: int = 400 
    subtitle_max_chars_per_line: int = 15
    subtitle_max_lines: int = 2
    crop_sample_interval_sec: float = 1.0
    crop_smoothing_window: int = 5
    scene_snap_threshold_sec: float = 0.8
    tts_gain_db: int = -3
    original_gain_db: int = -3
    bgm_gain_db: int = -20
    render_preset: str = "balanced"
    enable_hwaccel: bool = True
    # 바이럴 최적화 설정
    viral_scroll_stop_threshold: float = 0.7
    viral_score_min_threshold: float = 0.4
    max_storyline_candidates: int = 6
    gemini_story_max_retries: int = 3
    max_shorts_count: int = 3


@dataclass(frozen=True)
class Paths:
    app_root: Path

    @property
    def assets_dir(self) -> Path:
        return self.app_root / "assets"

    @property
    def outputs_dir(self) -> Path:
        return self.app_root.parent / "outputs"


# ASS는 이름 색상을 지원하지 않으므로 자주 쓰는 것만 hex로 매핑.
_NAMED_COLORS = {
    "white": "FFFFFF",
    "black": "000000",
    "yellow": "FFFF00",
    "red": "FF0000",
    "green": "00FF00",
    "blue": "0000FF",
    "skyblue": "87CEEB",
    "pink": "FF69B4",
}


def to_ass_color(value: str) -> str:
    """사용자 입력 색상을 ASS 형식(&HAABBGGRR)으로 정규화.

    - "&H..." → 그대로 통과 (기존 사용자 호환)
    - "#RRGGBB" / "RRGGBB" → 채널을 뒤집어 "&H00BBGGRR" (알파 00=불투명)
    - "white" 등 이름 색상 → 위 맵으로 변환
    """
    s = (value or "").strip()
    if s.lower().startswith("&h"):
        return s
    s = _NAMED_COLORS.get(s.lower(), s).lstrip("#")
    if len(s) != 6:
        raise ValueError(
            f"잘못된 색상 형식: '{value}'. #RRGGBB, &H00BBGGRR, "
            f"또는 {', '.join(sorted(_NAMED_COLORS))} 중 하나를 사용하세요."
        )
    try:
        rr, gg, bb = s[0:2], s[2:4], s[4:6]
        int(s, 16)
    except ValueError:
        raise ValueError(
            f"잘못된 색상 형식: '{value}'. #RRGGBB는 16진수 6자리여야 합니다."
        ) from None
    return f"&H00{bb}{gg}{rr}".upper()


def _ensure_ascii_font_path(file_path: Path) -> str:
    """폰트 경로에 비-ASCII 문자가 있으면 시스템 임시 디렉토리로 복사 후 반환."""
    import shutil
    import tempfile
    path_str = str(file_path.resolve())
    try:
        path_str.encode("ascii")
        return path_str.replace("\\", "/")
    except UnicodeEncodeError:
        tmp_dir = Path(tempfile.gettempdir()) / "ai_video_fonts"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / file_path.name
        if not tmp_path.exists():
            shutil.copy2(file_path, tmp_path)
        return str(tmp_path).replace("\\", "/")


def get_font_path(name: str, app_root: Path) -> str:
    font_dir = app_root / "assets" / "fonts"
    font_dir.mkdir(parents=True, exist_ok=True)

    safe_name = FONT_NAME_MAP.get(name, name.replace(" ", "_"))

    file_path = font_dir / f"{safe_name}.ttf"

    if file_path.exists():
        return _ensure_ascii_font_path(file_path)

    url = REMOTE_FONTS.get(name)
    if url:
        try:
            print(f"폰트 다운로드 시도: {name} -> {safe_name}.ttf")
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            file_path.write_bytes(response.content)
            return _ensure_ascii_font_path(file_path)
        except Exception as e:
            print(f"다운로드 실패: {e}")
            return "Malgun Gothic"

    return name

def get_logo_path(name: str, app_root: Path) -> str:
    """로고 이름 → 절대경로. get_font_path 와 같은 규약이다.

    경로 구분자가 없으면 assets/logos 에서 찾는다 — 호출부(루프·작품 카드)가 이 레포의 디렉토리
    구조를 몰라도 되게 하려는 것이다(작품 카드에는 'RZsv4.png' 한 줄만 적힌다). 확장자를 생략하면
    .png 를 붙인다. 절대·상대 경로를 그대로 주면 그 경로를 쓴다.
    """
    s = str(name)
    if "/" in s or "\\" in s:
        return str(Path(s).expanduser().resolve())
    p = app_root / "assets" / "logos" / (s if s.lower().endswith(".png") else f"{s}.png")
    if not p.exists():
        raise FileNotFoundError(
            f"로고 파일이 없습니다: {p}\n"
            f"  scripts/normalize_logo.py --code <식별코드> --input <권리사 원본> 으로 먼저 생성하세요.")
    return str(p.resolve())


REMOTE_FONTS = {
    "Jalnan": "https://raw.githubusercontent.com/rht-22/font.zip/main/Jalnan.ttf",
    "JalnanGothic": "https://raw.githubusercontent.com/rht-22/font.zip/main/JalnanGothic.ttf",
    "mulmaru": "https://raw.githubusercontent.com/rht-22/font.zip/main/mulmaru.ttf",
    "Griun": "https://raw.githubusercontent.com/rht-22/font.zip/main/Griun-Polpairness.ttf",
    "Hakgyo": "https://raw.githubusercontent.com/rht-22/font.zip/main/HakgyoansimSamulhamR.ttf"
}

# ⚠️ 이건 **파일명** 맵이다 — get_font_path 가 assets/fonts/<값>.ttf 를 찾는 데 쓴다.
# ASS 자막의 Fontname 에는 쓰면 안 된다(아래 FONT_FAMILY_MAP 참고).
FONT_NAME_MAP = {
    "여기어때 잘난체 2 TTF": "Jalnan",
    "물마루": "mulmaru",
    "여기어때 잘난체 고딕 TTF": "JalnanGothic",
    "그리운 경찰공평체": "Griun"
}

# ASS 의 Fontname 은 **파일명이 아니라 폰트 내부 패밀리명**이라 별도 맵이 필요하다.
# 값은 fc-scan / PIL ImageFont.getname() 실측치다.
#
# 이 맵이 없던 동안 자막 폰트가 통째로 적용되지 않았다: _ass_header 가 위 FONT_NAME_MAP(파일명)을
# 재사용해 유효한 패밀리명 '여기어때 잘난체 2 TTF' 를 무효한 'Jalnan' 으로 바꿔 썼고, libass 는
# 그 이름을 못 찾아 시스템 기본 폰트로 조용히 대체했다. 렌더는 성공하고 글자도 보이므로 아무도
# 눈치채지 못한다 — 없는 폰트명으로 렌더한 것과 픽셀 단위로 같았다(2026-07-29 실측).
# mulmaru 만 우연히 동작했는데, 파일명과 패밀리명이 대소문자 차이뿐이라 libass 가 매칭했다.
#
# 폰트를 추가하면 여기에도 넣어야 한다 — tests/test_font_family.py 가 번들 폰트 전수를 대조한다.
FONT_FAMILY_MAP = {
    "Jalnan": "Jalnan 2 TTF",
    "여기어때 잘난체 2 TTF": "Jalnan 2 TTF",
    "JalnanGothic": "Jalnan Gothic TTF",
    "여기어때 잘난체 고딕 TTF": "Jalnan Gothic TTF",
    "mulmaru": "Mulmaru",
    "물마루": "Mulmaru",
    "Griun": "Griun PolFairness",
    "그리운 경찰공평체": "Griun PolFairness",
}


def to_font_family(name: str) -> str:
    """폰트 이름(파일명 stem·한글명·패밀리명) → ASS Fontname 용 패밀리명.

    모르는 이름은 그대로 통과시킨다 — 시스템 폰트(예: Malgun Gothic)를 지정하는 경우가 있다.
    """
    return FONT_FAMILY_MAP.get(str(name), str(name))
