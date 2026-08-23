"""현지화 실행 사양 — 경로·로케일 설정·모델·Gemini 클라이언트를 한곳에서 푼다.

원본: video-localization-project `scripts/localize_run.py` 머리말 + `work_locale_cfg`.

⚠ 이식하며 **의도적으로 바꾼 것 하나** — Flash 모델.
   vlp 는 `gemini-3-flash-preview` 를 박아 썼지만 ai-video CLAUDE.md 의 모델 규칙이
   그 모델을 금지한다(허용: Pro `gemini-3.1-pro-preview` · Flash `gemini-3.6-flash`).
   그래서 **ai-video 규칙을 따른다** — gemini_client 와 같은 환경변수를 읽는다.
   Pro 는 양쪽이 같은 모델이라 차이가 없다. Flash 가 쓰이는 곳은 L2b(텔롭 타이밍
   프레임 판독)와 제목 축약뿐이고, 둘 다 LLM 판단이라 회귀 0 측정 대상이 애초에
   아니다(기획서 §8-2: 번역 결과를 고정 입력으로 주입해 렌더 계층만 대조한다).
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = Path(__file__).resolve().parent / "data"
FONTS_DIR = REPO_ROOT / "app" / "assets" / "fonts"


def engine_path(env_key: str, sibling: str) -> Path:
    """형제 엔진 디렉토리 해석 — 환경변수 우선, 없으면 이 레포의 형제. 순수(테스트 대상).

    로컬 운영(`~/ves/<engine>`)과 워커 노드(`$VES_HOME/engines/<engine>`)가 **둘 다
    형제 배치**라 규칙 하나로 맞는다. 절대 경로를 박으면 워커에서 brain .env·폰트를
    전부 못 찾는다."""
    v = os.environ.get(env_key)
    return Path(v) if v else REPO_ROOT.parent / sibling


BRAIN = engine_path("BRAIN_ROOT", "ai-improvement-edit-video")


def model_pro() -> str:
    """정밀 분석(영상 패스·통번역). ai-video 모델 규칙과 같은 환경변수를 읽는다."""
    return os.getenv("GEMINI_MODEL_NAME", "gemini-3.1-pro-preview")


def model_flash() -> str:
    """스크리닝(프레임 판독·제목 축약). 위 머리말의 ⚠ 참조."""
    return os.getenv("GEMINI_FLASH_MODEL_NAME", "gemini-3.6-flash")


def load_locales() -> dict:
    """채널 현지화 정본. ⚠ 내부 키(작품명)는 절대 번역하지 않는다 — laeebly 조회 키다."""
    return json.loads((DATA_DIR / "locales.json").read_text(encoding="utf-8"))


def work_locale_cfg(locales: dict, work_title: str, locale: str) -> dict:
    """작품 × 로케일 설정(표기·문맥·용어집·고지). 없으면 즉시 실패 — 조용히 원어로
    나가는 것이 최악이다."""
    cfg = (locales.get("works") or {}).get(work_title, {}).get(locale)
    if not cfg:
        raise SystemExit(f"locales.json 에 작품 '{work_title}' 의 '{locale}' 항목이 없다")
    return cfg


def gemini_client():
    """GEMINI_API_KEY 는 **환경변수 우선**(워커 /etc/ves/node.env), 없을 때만 brain .env 폴백.
    워커 노드의 brain 체크아웃에는 .env 가 없다(시크릿은 git 밖) — 폴백만 있으면 즉사한다."""
    if not os.environ.get("GEMINI_API_KEY") and (BRAIN / ".env").exists():
        sys.path.insert(0, str(BRAIN / "scripts"))
        from envload import load_env
        load_env(str(BRAIN / ".env"))
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("GEMINI_API_KEY 없음 — 워커는 /etc/ves/node.env, 로컬은 brain .env 확인")
    from google import genai
    return genai.Client(api_key=key)


def read_work_title(job: Path) -> str:
    """작품 키 — 백업본이 있으면 그쪽이 정본(재실행에서 일본어로 덮인 원본을 안 읽는다).

    ⚠ 이 값은 laeebly 완전일치 조회 키라 **절대 번역하지 않는다**(기획서 §8-4)."""
    backed = job / "localize_backup_ko" / "work_title.txt"
    src = backed if backed.exists() else job / "work_title.txt"
    return src.read_text(encoding="utf-8").strip("﻿\n ")


@dataclass
class LocalizeSpec:
    """한 번의 현지화 실행에 필요한 것 전부."""
    job: Path
    locale: str
    work_title: str
    work_cfg: dict
    locale_cfg: dict
    out_dir: Path

    @property
    def backup(self) -> Path:
        return self.job / "localize_backup_ko"

    @classmethod
    def build(cls, job_dir: str | Path, locale: str = "ja") -> "LocalizeSpec":
        job = Path(job_dir).resolve()
        if not job.is_dir():
            raise SystemExit(f"job 디렉토리가 없다: {job}")
        locales = load_locales()
        if locale not in (locales.get("locales") or {}):
            raise SystemExit(f"locales.json 에 로케일 '{locale}' 없음")
        work = read_work_title(job)
        out_dir = job / f"localize_{locale}"
        out_dir.mkdir(exist_ok=True)
        return cls(job=job, locale=locale, work_title=work,
                   work_cfg=work_locale_cfg(locales, work, locale),
                   locale_cfg=locales["locales"][locale], out_dir=out_dir)
