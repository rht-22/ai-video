from __future__ import annotations

import json
import mimetypes
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.modules.editorial import format_editorial_block

from dotenv import load_dotenv


def _safe_upload_path(file_path: Path) -> tuple[Path, bool]:
    """한글 등 비-ASCII 경로를 Gemini File API 호환 경로로 변환.

    Returns:
        (사용할 경로, 임시파일 여부). 임시파일이면 사용 후 삭제 필요.
    """
    path_str = str(file_path)
    try:
        path_str.encode("ascii")
        return file_path, False
    except UnicodeEncodeError:
        # 비-ASCII 경로 → 임시 디렉토리에 심볼릭 링크 생성
        suffix = file_path.suffix
        tmp = Path(tempfile.mktemp(suffix=suffix, prefix="gemini_upload_"))
        shutil.copy2(file_path, tmp)
        return tmp, True


def _max_tokens_usage(response: Any) -> str | None:
    """응답이 출력 한도(MAX_TOKENS)에서 끊겼으면 토큰 사용량 요약, 아니면 None.

    Gemini 는 thinking 토큰도 max_output_tokens 예산에서 함께 쓴다. 추론이 길어지면 JSON 을
    끝맺지 못한 채 finish_reason=MAX_TOKENS 로 끊기는데, 그 잘린 조각을 그대로 파싱하면
    `Expecting ',' delimiter: line N column 1` 같은 **엉뚱한 JSONDecodeError** 로 보인다.
    2026-07-30·31 생성 실패 3건(커리어데이·B급 스튜디오·유미의 세포들 시즌3)이 전부 이것이었고,
    원인을 찾는 데 로그를 거슬러 올라가야 했다. 파싱 전에 잘림을 잘림이라고 말한다.

    ※ 사용량은 진단용이라 없으면 없는 대로 둔다 — 여기서 예외를 내면 원래 오류를 덮는다.
    """
    try:
        candidates = getattr(response, "candidates", None) or []
        finish = getattr(candidates[0], "finish_reason", None) if candidates else None
    except Exception:  # noqa: BLE001 — 진단 경로가 본 오류를 가리지 않게
        return None
    name = getattr(finish, "name", None) or (str(finish) if finish is not None else "")
    if "MAX_TOKENS" not in name.upper():
        return None
    usage = getattr(response, "usage_metadata", None)
    parts = []
    for label, attr in (("프롬프트", "prompt_token_count"), ("추론", "thoughts_token_count"),
                        ("출력", "candidates_token_count"), ("합계", "total_token_count")):
        value = getattr(usage, attr, None) if usage is not None else None
        if value is not None:
            parts.append(f"{label} {value}")
    return "토큰 " + ", ".join(parts) if parts else "사용량 정보 없음"


def _extract_json_from_markdown(text: str) -> str:
    """마크다운 코드 블록에서 JSON을 추출합니다."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def _finish_reason(response: Any) -> str:
    """응답이 왜 끝났는지(STOP/MAX_TOKENS/SAFETY…). 안전차단·토큰한도를 파싱 잡음과 구분하는 데 쓴다."""
    try:
        reason = response.candidates[0].finish_reason
        return str(getattr(reason, "name", reason))
    except Exception:
        return "unknown"


def _dump_gemini_response(raw_text: str, response: Any, payload: dict[str, Any], kind: str) -> Path | None:
    """파싱이 흔들린 응답 원문을 파일로 남긴다.

    지금까지 실패 원문이 어디에도 안 남아(성공분만 run_log_gemini.json 에 기록) 'JSON이 깨졌다'는
    사실만 알고 무엇이 왜 깨졌는지는 볼 수 없었다. 원인 추적의 전제라 실패·구제 양쪽 다 남긴다.
    """
    try:
        base = Path(os.getenv("AI_VIDEO_ROOT") or ".") / "outputs" / "_gemini_failures"
        base.mkdir(parents=True, exist_ok=True)
        path = base / (
            f"{time.strftime('%Y%m%d_%H%M%S')}_chunk{payload.get('chunk_index', 'na')}"
            f"_{kind}_{os.getpid()}.txt"
        )
        header = [
            f"# kind={kind}",
            f"# chunk_index={payload.get('chunk_index')}",
            f"# chunk={payload.get('chunk_start_sec')}~{payload.get('chunk_end_sec')}초",
            f"# finish_reason={_finish_reason(response)}",
            f"# usage={getattr(response, 'usage_metadata', None)}",
            f"# raw_len={len(raw_text)}",
            "---- raw response.text ----",
        ]
        path.write_text("\n".join(header) + "\n" + raw_text, encoding="utf-8")
        return path
    except Exception as dump_err:  # 진단용이므로 실패해도 본 흐름을 막지 않는다
        print(f"    [WARN] 응답 원문 보존 실패: {type(dump_err).__name__}: {dump_err}")
        return None


def _loads_first_json(text: str) -> tuple[Any, int]:
    """맨 앞의 완결된 JSON 값 하나만 읽고, 뒤에 붙은 것은 버린다. → (값, 버린 길이)

    Gemini 응답이 한 덩어리가 아니라 두 덩어리로 이어붙어 오는 사고가 잦다(2026-08-03 실측:
    분석 호출 22회 중 12회 파싱 실패). SDK 의 response.text 가 답변 파트를 구분자 없이 이어붙여
    `{...}{...}` 가 되기 때문이고, json.loads 는 이걸 'Extra data' 로 거부한다. 첫 값만 취하면
    그 유형은 그대로 살아난다. 이어붙은 자리가 컨테이너 **안쪽**이면(=Expecting ',' delimiter)
    이 방법으로도 못 살리므로, 그때는 원문을 남기고 종전대로 재시도한다.
    """
    decoder = json.JSONDecoder()
    data, end = decoder.raw_decode(text)
    return data, len(text) - end



# ─────────────────────────────────────────────
# 청크 분석 프롬프트 (멀티모달 분석 + 핵심 필드 보존)
# ─────────────────────────────────────────────
GEMINI_PROMPT_TEMPLATE = """
[ROLE]
당신은 한국 최고의 숏폼 전문 AI 에디터이자 멀티모달 스토리 분석 전문가다.
텍스트, 영상, 오디오를 통합적으로 이해하여
바이럴 가능성이 높은 쇼츠 콘텐츠를 기획하고 분석한다.
반드시 JSON만 출력한다. 코드블록 금지.
영상에 없는 내용은 절대 창작하지 말 것.

---

[입력 정보]
- 작품명: {work_title}
- 주제: {topic}
- **현재 청크 번호 (chunk_index): {chunk_index}** ← 출력의 모든 chunk_index 필드는 이 값을 사용할 것
- 청크 범위: {chunk_start_sec} ~ {chunk_end_sec} 초
{work_context_block}
{previous_episodes_context_block}
{character_appearances_block}
- ⚠️ 모든 start_sec / end_sec는 반드시 첨부된 영상 파일의 시작(0초)을 기준으로 한 상대값으로 반환할 것

- 자막(있으면): {transcript_text}
- 씬 경계(있으면): {scene_boundaries}
{previous_context}

---

[인물 식별 단계]

- 분석 시작 전, 아래 수단을 통해 등장 인물의 이름을 먼저 파악한다:
    - **face_id 사전 인식 결과** (있을 경우): 위 `[입력 정보]`의 face_id 블록은 외부 얼굴 인식기가 추정한 캐릭터 등장 구간이다. 같은 인물명을 라벨로 일관되게 사용하라.
    - 화면 자막 또는 이름 자막 (예능/드라마 자막 포함)
    - 대사 내 호칭 (예: "야 민준아~")
    - 화면 내 텍스트 (명찰, 이름표 등 OCR 가능한 텍스트)
- 위 수단으로 이름 확인이 불가능한 인물은 "인물A", "인물B" 등 고유 레이블을 부여한다.
- 이후 모든 분석 항목에서 확정된 이름 또는 레이블을 일관되게 사용한다.
- ❌ 복장/헤어스타일 기반 레이블링 금지 (예: "갈색 재킷 남자"). 동일 인물이 다른 씬에 다른 옷으로 등장해도 같은 이름/레이블을 유지하라.

(※ 자막 화자태그 우선 / 입 모양 동기화 없으면 화자 = 불명 등 화자 판정 규칙은 아래 [원칙 P1] 단서 1 참조)

[열린 라벨 허용 — 명명 편향(Named-Character Bias) 차단]
- `characters_in_scene`, `character_focus`, `characters_tracking[].character`, `event_template.subject/target` 등 *모든 인물 필드*에는 식별된 주요 인물명 외에 다음 열린 라벨도 사용 가능하다:
    - `"엑스트라"` — 이름 없는 단일 행인·조연
    - `"엑스트라(다수)"` — 군중·여러 행인이 함께 등장·행동
    - `"행인"` — 배경 통행자
    - `"불명"` — 화면에 인물은 있으나 누구인지 단정 불가
- ⚠️ **행위자가 식별된 주요 인물 중 *반드시* 한 명일 필요는 없다.** 이름이 부여된 인물에게 *그럴듯하다는 이유로* 행동·발화를 귀속시키지 마라. 엑스트라가 실제 행위자일 가능성을 항상 열어두어라.
- ⚠️ **확신 부족 시 디폴트는 단정이 아닌 열린 라벨**: 행위자·화자 확신도가 낮으면 `subject = "불명"` 또는 `"엑스트라"`로 두는 것이, 잘못된 주요 인물명을 채워넣는 것보다 우선한다. 빈 칸 회피용으로 디폴트 인물을 쓰지 마라.

---

[원칙 P1 — 시각 단서 근거 추적]

⚠️ 자막·호칭 단서가 없는 장면에서 행위자·화자·행동을 *서사적으로 그럴듯한 인물*에 디폴트로 귀속시키는 결함이 반복 관찰되었다. 이를 막기 위한 자기검토 절차다.

**적용 범위**: description, transcript의 화자 추정, `characters_in_scene`, `character_focus`, `event_template.subject/target/action`, `scene_location`, `event_template.location`에 행위자·화자·행동·관계·상태·장소를 기록할 때마다 적용. 자막 유무와 무관.

**자기검토 절차**: 각 진술 직전, 그 진술이 다음 시각 단서 중 어느 것에 근거하는지 식별하라.

1. **입 모양 동기화** — 화자 결정 시. 대사가 들리는 시점에 *누구의 입이 실제로 움직이는가*. 화면에 입이 보이지 않거나 동기화 확인 불가 → 화자 = `"불명"`. 자막에 화자 태그가 있으면 그것이 우선.
2. **시선·얼굴 방향** — 행위자로 추정한 인물의 시선·얼굴이 행동 대상을 향하는가. 두리번거리거나 다른 방향을 보면 그 인물은 행위자가 아닐 가능성이 높다.
3. **신체 방향·동선** — 접근·이동·다가감 같은 *움직임이 있는 행동*은 화면 안에서 *실제로 움직이는 인물*이 누구인가. 정지해 있는 주요 인물에게 이동 행동을 귀속시키지 마라.
4. **공간 거리·위치** — 행동 대상과의 거리가 그 행동과 부합하는가. 멀리 떨어진 인물 사이에 *근접 행동* — "다가갔다"·"속삭였다"·"건넸다"·"손을 잡았다"·"어깨를 두드렸다" — 을 부여하지 마라.
5. **카메라 컷·앵글** — 컷 사이 인물 동일성 가정 금지. 컷이 바뀌면 새로 식별하라. 반응 컷에서 화면에 보이는 인물이 *직전* 행동·발화의 주체라고 단정 금지.

**근거 부재 시 처리**:
- 행위자·화자가 불명확 → 디폴트 주요 인물 채우지 말고 `"불명"` 또는 `"엑스트라"`로 두어라.
- 행동의 주체가 화면에 명확히 안 보이거나 추정 불가 → 행동을 디폴트 인물에 귀속시키지 말고 *수동태·관찰형*으로 표현하라 (예: "누군가 다가온다", "주변 인물이 모여든다", "어디선가 목소리가 들린다").

⚠️ **장소(`scene_location` / `event_template.location`)는 서사 맥락이 아닌 화면·대사 단서로 결정하라**:
- "외국에서 한국으로 왔다" → "공항 입국장" ❌
- "의사다" → "병원" ❌
- 화면·대사에 단서 부재 시 `"불명"`, `"실내(불명)"`, `"실외(불명)"` 같은 일반 라벨 사용.

---

[description 규칙]
- description은 compose story 단계를 위한 정보이므로 영상과 자막을 기반으로 시간 순서에 맞게 정확하고 객관적으로 최소 5 문장 이상 자세하게.
- description은 실제 장면 묘사를 유지하고, 앞 장면이 나중 내용에 의해 재해석되는 경우 재해석된 의미는 reason 필드에만 반영할 것. 과대해석 및 과장 금지. ⚠️ reason도 [원칙 P2]의 양방 단서 규칙에서 면제되지 않는다 — 드라마가 의도적으로 모호하게 연출한 관계·인지 상태를 한 방향으로 단정하지 말 것.
- description에서 행동의 범주를 바꾸지 마라. 장면에서 명확히 관찰된 행동(발화·동작·표정)만 그 종류 그대로 기술하고, 확인되지 않은 의도·감정·결과를 덧씌우지 마라.
- ⚠️ description에서 내레이션·독백·보이스오버(VO)는 반드시 "~의 내레이션", "~가 속으로 독백한다", "VO로 ~가 말한다" 등으로 명시하여 실제 대화와 혼동되지 않도록 할 것.
- ⚠️ OST·삽입곡·배경음악의 가사는 내레이션/독백/VO가 아니다. transcript에 옮기거나 description에서 인물 발화로 기술하지 말고, 필요하면 "배경음악(OST) 가사"로만 언급할 것.
- ⚠️ **description의 모든 사건·동작은 같은 candidate의 `start_sec` ~ `end_sec` 구간 안에서 일어난 것만 적어라.** 시간 범위 밖 사건은 *서사 인과로 자연스럽게 이어져 보여도* 별개 candidate로 분리하라. 한 candidate description에 두 개 이상의 시·공간적으로 분리된 비트(예: 사건 발생 + 한참 뒤 후일담)를 압축해 넣지 마라.

---

[원칙 P2 — 관찰 비약 금지 (Observation-to-Relation Leap)]

⚠️ 한쪽 인물의 단편 관찰을 *둘 사이의 상호작용·관계*로 비약하는 결함이 반복 관찰되었다 (예: 찬은 하란을 보고 하란은 두리번거리는 장면을 "서로 바라본다"고 단정).

**원칙**: description은 *개별 인물의 관찰 가능한 행동·표정·시선·동선*을 나열하는 것이 원칙이다. 둘 이상 인물 사이의 *관계·상호작용·공동 의도·상호 인지*를 표현하려면 **양쪽 인물의 시각 단서가 모두 명시적으로 확인**되어야 한다.

**양방 단서 필수 표현 — 카테고리별 예시 (열린 목록)**:

- **상호 시선**: "서로 본다", "마주본다", "눈이 마주친다", "시선이 마주친다", "응시한다" → 양쪽 시선·얼굴 방향이 *서로를 향함*이 동시에 확인되어야 사용
- **상호 발화**: "대화한다", "이야기를 나눈다", "말다툼한다", "주고받는다" → 양쪽 모두의 발화 단서(입 모양·교대)가 확인되어야 사용. 한쪽만 말하면 `"A가 B에게 말한다"`
- **상호 인지**: "서로 알아본다", "서로를 발견한다", "마주친다" → 양쪽 인지 반응(표정·시선 이동)이 모두 확인되어야 사용
- **공동 행동**: "함께 걷는다", "같이 간다", "동행한다", "함께 X한다" → 같은 방향·근접 동선이 양쪽 모두 확인되어야 사용
- **신체 접촉**: "포옹한다", "악수한다", "키스한다", "손을 잡는다", "어깨동무한다" → 양쪽 신체가 실제로 접촉하는 것이 화면에 명시적으로 보여야 사용
- **감정·관계 상호**: "교감한다", "공감한다", "서로 의지한다", "유대를 느낀다" → 표면 단서로 단정 금지. 명시적 상호 행동(끄덕임·미소 교환·시선 교환 등)이 *양방* 확인되어야 사용

⚠️ 위 목록은 **닫힌 차단 리스트가 아니다.** 어떤 행동 표현이든 "두 인물 사이의 관계·상호작용·공동 의도"를 함의하면 양방 단서 확인 후 사용하라.

**확신 부족 시 처리 — 단편 관찰 나열**:

상호작용 표현 대신 *각 인물의 관찰을 개별 문장으로 분해*하여 적어라.

- ❌ "찬과 하란이 서로 바라본다." (한쪽만 단서 확인)
- ✅ "찬이 멀리서 하란을 바라본다. 하란은 주변을 두리번거리며 누군가를 찾는다."

- ❌ "두 사람이 대화한다." (한쪽 발화만 확인)
- ✅ "A가 B에게 말을 건다. B는 표정 변화 없이 듣고 있다."

- ❌ "함께 걸어간다." (방향·동선 미확인)
- ✅ "A가 앞장서서 걷는다. B는 뒤에서 천천히 따라간다."

**reason 필드에도 동일 원칙 적용**:

⚠️ `reason` 필드는 "재해석된 의미"를 적는 출구이지만, *드라마가 의도적으로 모호하게 연출한 관계·인지 상태*까지 한 방향으로 단정해도 된다는 뜻은 아니다. 한쪽 인물의 단서만 확인되는데 양방의 인지·관계·과거 인연을 단정하는 표현은 reason에서도 금지. 후속 장면 단서 없이 이 candidate 안에서 단정 불가능한 관계·인지는 *모호성 자체*를 reason의 매력 포인트로 기술하라.

- ❌ "서로를 알면서도 모르는 척해야 하는 두 사람의 엇갈린 관계" (양방의 인지를 단정 — 한쪽만 알 수도, 진짜 모르는 사이일 수도, 둘 다 모를 수도 있음)
- ❌ "오랜 연인의 재회" / "헤어진 옛 연인의 우연한 마주침" (관계 종류를 단서 없이 확정)
- ❌ "처음 만난 두 사람의 운명적 첫 마주침" (반대 방향 단정도 동일하게 금지)
- ✅ "찬의 씁쓸한 표정과 하란의 사무적 자기소개가 관계를 단정할 수 없게 만드는 긴장" (모호성 자체를 매력 포인트로)
- ✅ "찬의 표정에서 과거 인연이 암시되지만 하란 쪽 인지 단서는 비어 있어 관계가 열려 있음" (한쪽 단서만 있음을 명시)

**판단 기준**: reason에 적은 관계·인지 표현이 *이 candidate 안의 시각·대사 단서만으로* 단정 가능한가? 단정 불가면 모호성을 보존하라. 같은 청크 내 다른 candidate나 이전 청크 결과로 단정 가능해진 경우만 단정형 표현 허용.

---

[타임스탬프 정확도 — 절대 규칙 / 어기면 그 후보는 폐기됨]

🚫 가장 중요한 규칙. 어기면 다운스트림에서 자막·TTS·렌더가 모두 어긋난다.

1. **첨부 영상의 시작 = 0.0초**. 모든 start_sec / end_sec는 이 영상의 0초 기준 상대값이다. 원본 풀 영상의 절대 시간이 아니다.
2. **첨부 영상 길이를 초과하는 시간은 절대 출력하지 마라.** 영상 길이가 600초이면 어떤 출력 시간도 600.0을 넘으면 안 된다.
3. **transcript에 적은 대사는 그 timestamp 시점에 실제로 들리는 대사여야 한다.** 영상 안 다른 시점의 대사를 베껴서 다른 시간에 붙이지 마라. transcript 시점과 화면 위치를 다시 한 번 일치시킨 뒤 출력하라.
4. **자막에서 본 시간을 그대로 베끼지 마라.** 자막 텍스트가 [12:30~12:35]에 적혀 있어도, 첨부된 영상에서 그 대사가 실제로 어느 시점에 들리는지 영상 본문을 다시 확인하고 그 시점을 적어야 한다. (영상이 원본의 일부 잘라낸 chunk이므로 자막 절대 시간과 영상 0초 기준 상대 시간은 다르다.)
5. **각 candidate_moments의 start_sec / end_sec는 반드시 영상에서 직접 확인 가능한 구간이어야 한다.** 보지 않은 시점을 추측해서 만들지 마라.
6. **context_extension.extended_start_sec / extended_end_sec 도 chunk-relative 시간**이며 동일하게 [0.0, 첨부 영상 길이] 범위 안에 있어야 한다.
   `extended_start_sec ≤ start_sec ≤ end_sec ≤ extended_end_sec`를 만족시키지 못하면 needed=false로 강등하라.
7. 출력 전 다음 룰을 적용하라 (한 진술을 쓰기 직전마다 확인 — 출력 후 수정은 불가능하므로 *생성 시점에 적용*하라):
   - 모든 start_sec / end_sec / extended_start_sec / extended_end_sec 가 [0.0, 첨부 영상 길이] 범위 안일 것
   - segments[i].end_sec == segments[i+1].start_sec (gap/overlap 금지)
   - **[원칙 P1]** description·transcript·event_template의 모든 진술은 시각 단서(입 모양·시선·신체 방향·공간 거리·카메라 컷) 중 하나에 근거를 둘 것 — 근거 없는 진술은 적지 않음
   - **[원칙 P2]** 상호작용·관계·공동 의도 표현은 description·reason 모두에서 양방 단서 확인 후에만 사용. description은 한쪽만 확인되면 단편 관찰 나열로 분해, reason은 의도적 모호성 자체를 매력 포인트로 기술 (한 방향 단정 금지)
   - **description의 사건 시간 일치**: description 안 모든 사건이 그 candidate의 start_sec ~ end_sec 안에서 일어났는지. 범위 밖 사건은 적지 않음 (별개 candidate로 분리)

---

[세그먼트(segments) 분할 — 청크 전체 커버]

청크 전체 시간(`chunk_start_sec` ~ `chunk_end_sec`)을 **빈틈 없이, 겹침 없이** 시간순 세그먼트로 분할하라.

[분할 기준 — 우선순위]
1. **장면 전환(scene cut)**: 카메라가 새로운 장소/시점/상황으로 바뀌는 지점. 입력으로 주어진 `scene_boundaries`가 있으면 1차 참고.
2. **서사 단위(beat)**: 같은 장소·등장인물이라도 화제/사건이 명확히 바뀌면 분리.
3. **대화 단위**: 한 인물이 길게 이야기하다가 다른 인물로 발화 주체가 바뀌고 화제도 바뀌면 분리.

[세그먼트 길이 가이드]
- 일반적 길이: 10~60초 권장
- 정적·전환 컷이 길게 이어지면 60초 이상도 허용
- 너무 짧은 마이크로 컷(1~3초)은 인접 세그먼트에 합쳐라

[세그먼트 연속성 제약 — 절대 규칙]
- segments[0].start_sec == chunk_start_sec
- segments[-1].end_sec == chunk_end_sec
- 모든 i에 대해 segments[i].end_sec == segments[i+1].start_sec (gap/overlap 금지)
- segments는 빠짐없이 청크 전체를 덮어야 한다 (평범한 구간도 반드시 포함)

[세그먼트 description]
- 모든 세그먼트는 [description 규칙]에 따라 객관적 묘사를 작성하라
- 평범한/조용한 구간도 짧은 묘사(2~3문장)는 작성 (예: "복도를 천천히 걷는 인물A. 별다른 대사는 없고 발걸음 소리만 들린다.")
- 주목할 만한 핵심 세그먼트는 5문장 이상 상세히 작성

---

[SHORTS TYPE DEFINITION]

candidate_moments는 다음 두 가지 유형의 쇼츠 제작에 모두 활용될 수 있도록 추출한다.

[유형 1: 하이라이트 쇼츠]
- 하나의 강렬한 장면 중심, 맥락 설명 최소화
- 단일 클립으로 시청자가 "뭐지?" → 감정반응 → 완결까지 느낄 수 있는 장면
- 이전 맥락이나 이후 결과에 대한 설명이 전혀 필요하지 않은 장면
- → highlight_eligible: true 로 표기

[유형 2: 서사형 쇼츠]
- 가장 드라마틱한 씬을 중심으로 서사가 자연스럽게 흐르도록 구성
- 다른 장면들과 함께 묶여 hook→build→payoff 흐름을 만들 수 있는 장면

⚠️ hook/build/payoff 역할 결정은 다음 단계(스토리 구성)에서 수행하므로 여기서는 미리 라벨을 붙이지 말 것.

⚠️ **모든 candidate**(highlight·서사형 무관)는 context_extension 필드로 앞뒤 맥락 필요 여부를 판단·출력하라.
다음 단계(스토리 구성)에서 highlight는 단독 클립 자연 확장에, storytelling은 hook/build/payoff 클립 사이의 시간 점프를 줄이는 데 활용된다.

---

[후보 모멘트(candidate_moments) 추출 규칙]

segments 중 **쇼츠 제작에 가치 있는 장면만** 선별해 candidate_moments로 추출한다.

- candidate_moment의 `segment_index`는 segments 배열 인덱스를 가리킨다
- candidate_moment의 start_sec/end_sec은 해당 segment 범위 안에서, 더 좁게 잡아도 된다(핵심만 발췌). 단, segment 범위 밖으로 나가면 안 된다
- 후보 최소 {min_candidates}개 이상 선별

---

[TONE & RULES]
- 모든 출력은 한국어 사용
- 최신 쇼츠 트렌드 반영: 자연스러운 톤, 짧은 문장, 강조/리액션 요소
- JSON 스키마 강제. 분석 결과 해당 항목이 없을 경우 빈 배열 대신 null로 출력
- 타이틀 시퀀스, 엔딩 크레딧은 candidate_moments에서 제외 (단, segments에는 포함)
- "start_sec"~"end_sec" 내의 상황만 봐도 이해가 가능해야 한다.
- ⚠️ 대사가 있는 장면에서 인물의 행동/의도를 묘사할 때는 대사 내용을 최우선으로 반영하라.
  캐릭터 설정(질병, 성격 등 배경 지식)이 대사와 충돌하면 대사를 믿어라.

[intro/credits/recap/sponsor 식별 — 청크 내 비-콘텐츠 구간]

청크 안에 다음 유형이 존재하면 위치를 식별해 표시하라. 시청자에게 정보 가치가 없는 비-콘텐츠 구간이라 쇼츠에서 제외돼야 한다.
- intro: 작품·회차 타이틀 시퀀스, 오프닝 영상, 로고·제목 카드
- credits: 엔딩 크레딧, 스태프롤, 출연진 자막
- recap: 지난 화 요약 (예: "지난 회에…", "previously on …")
- sponsor: 협찬·광고 컷, 스폰서 자막
- promo: 다음 화 예고편

식별 신호 (강함→약함):
1. 화면 중앙 정적 텍스트 카드 (제작진/배우/회차 정보)
2. transcript 비어있고 BGM·정적 그래픽만 보임
3. 동일 형식 자막이 화면 하단·측면을 일정 시간 채움
4. "다음 화", "지난 화" 같은 명시 키워드

[출력 표시 방법]
- chunk-level: `chunk_intro_credits_ranges` 배열에 모든 식별 구간을 `{{start_sec, end_sec, kind, confidence}}` 형식으로 기록. 없으면 빈 배열 `[]`.
- candidate-level: 그런데도 해당 구간이 candidate_moment 로 출력되면 (segment 분할 후 핵심 장면처럼 잘못 추출된 경우) 반드시 `is_intro_credits: true` + `intro_credits_reason` 한 줄을 함께 표시. 정상 콘텐츠 candidate 는 `is_intro_credits: false` / `intro_credits_reason: null`.
- 후처리에서 `is_intro_credits=true` candidate 와 `chunk_intro_credits_ranges` 안의 candidate 는 모두 자동 제외된다.

⚠️ 이 식별은 segments 분할에는 영향을 주지 않는다 (segments 는 청크 전체를 빈틈없이 덮어야 함). 다만 비-콘텐츠 segment 의 description 은 한두 문장으로 짧게 작성하라.

---

[chunk_intro_credits_ranges 필드 정의 (chunk-level)]
- 위 [intro/credits/recap/sponsor 식별] 섹션에서 식별한 모든 비-콘텐츠 구간 배열.
- 각 항목: `{{"start_sec": float, "end_sec": float, "kind": str, "confidence": float}}`
    - kind: `"intro"` | `"credits"` | `"recap"` | `"sponsor"` | `"promo"`
    - confidence: 0.0~1.0 (1.0이 가장 확실)
- 없으면 빈 배열 `[]`. null 출력 금지.

[characters_tracking 필드 정의 (chunk-level)]
- 청크 전체에 등장하는 인물별 등장 타임스탬프 및 주요 행동을 정리한다.
- character: 인물명 또는 레이블 (인물 식별 단계에서 확정한 이름과 일관되게). [열린 라벨 허용] 규칙에 따라 `"엑스트라"`·`"엑스트라(다수)"`·`"행인"`·`"불명"`도 사용 가능
- appearances[]: 해당 인물이 등장하는 구간 목록
    - start_sec / end_sec: 등장 구간
    - action: 그 구간에서 인물의 핵심 행동/발화 요약 한 문장. [원칙 P1] 시각 단서 근거 없는 추정 금지

[segment 필드 정의]
- segment_index: 0부터 시작하는 정수 (segments 배열 인덱스)
- start_sec / end_sec: 이 세그먼트의 시간 범위 (인접 세그먼트와 정확히 맞물려야 함)
- description: 장면 설명(묘사 위주). 위 [description 규칙] 및 [원칙 P2 — 관찰 비약 금지]를 엄수. 평범한 구간은 2~3문장, 핵심 구간은 5문장 이상
- transcript: 이 구간의 핵심 발화. 내레이션/독백/VO인 경우 '[내레이션]' 접두. 발화 없으면 빈 문자열
- characters_in_scene: 화면에 등장하는 인물 이름 배열. 엑스트라 다수 등장 시 `"엑스트라(다수)"` 가능. 화면 안 모든 인물을 식별된 주요 인물명으로 채우지 마라
- scene_location: 장면 배경 장소
- timeline_position: "현재" | "과거" | "불명"

[candidate_moment 필드 정의]
- segment_index: 이 후보가 속한 segments 배열의 인덱스 (필수)
- chunk_index: **반드시 위 [입력 정보]의 "현재 청크 번호" 값과 동일**해야 한다. 절대 0으로 고정하거나 임의의 값을 쓰지 말 것.
- candidate_index: 이 청크 내 후보 순서 (0부터 시작)
- continues_from의 chunk_index: 같은 청크 내 후보를 참조하면 위 "현재 청크 번호"와 동일. 이전 청크의 후보를 참조할 때만 [이전 청크들의 분석 결과] 블록에 명시된 chunk_index 값을 그대로 사용할 것.
- start_sec / end_sec: 해당 segment 범위 안의 좁은 핵심 구간 (segment 경계를 넘지 말 것)
- characters_in_scene: 화면에 등장하는 인물 이름 배열. 엑스트라 다수면 `"엑스트라(다수)"` 가능. 디폴트로 주요 인물명을 채우지 마라
- character_focus: 이 장면의 주요(핵심) 인물 이름 배열. 행위자가 엑스트라면 `"엑스트라"`, 불명확하면 `"불명"`
- description: 장면 설명. segment의 description을 그대로 복사하거나, 더 상세하게 보강 가능. [원칙 P2 — 관찰 비약 금지] 엄수
- reason: 후보 선정 이유. 재해석된 의미는 여기에만 적되, [원칙 P2]의 양방 단서 규칙은 reason에도 적용 — 한쪽 단서만으로 양방 관계·인지를 단정 금지. 의도적 모호성은 모호성 자체를 매력 포인트로 기술
- transcript: '단 한 명'의 주요 발화. 내레이션/독백/VO인 경우 '[내레이션]' 접두
- scene_location / timeline_position: segment에서 복사
- continues_from: 다른 candidate_moment와 직접 이어지면 {{"chunk_index": N, "candidate_index": M}}, 독립이면 null
  - 같은 청크 내 후보를 참조 → chunk_index = 위 [입력 정보]의 '현재 청크 번호'와 동일하게
  - 이전 청크 후보를 참조 → chunk_index = 위 [이전 청크들의 분석 결과] 블록에 명시된 chunk_index 값을 그대로 사용
  - ⚠️ 응답 예시의 (0, 3) 같은 좌표를 그대로 베껴 쓰지 마라. 실제 참조 대상의 좌표를 정확히 입력하라.
- requires_context: 이 클립을 이해하려면 다른 장면이 필요하면 true
- highlight_eligible: requires_context가 false이고 클립 자체에 감정적 완결성이 있으면 true
- highlight_reason: highlight_eligible이 true인 경우에만 1문장 (false면 null)
- visual_essential: 이 candidate의 *핵심 의미가 대사가 아닌 시각 단서*(인물 표정·동작·소품·자막·시선·교차편집·문자·그래픽 등)에 있으면 true. true면 후속 무음 컷 단계에서 대사 없는 무음 구간도 그대로 유지된다(시각 비트 보호). 대사가 의미를 좌우하면 false.
  - 예: 편지·메모를 읽는 장면, 침묵 속 시선 교환, 화면 자막·그래픽 강조 등
- is_intro_credits: 이 candidate 가 intro/credits/recap/sponsor/promo 등 비-콘텐츠 구간이면 true. 위 [intro/credits/recap/sponsor 식별] 섹션 기준 적용. 후처리에서 자동 제외됨. 정상 콘텐츠 candidate 면 false.
- intro_credits_reason: is_intro_credits=true 일 때만 한 줄 사유 (예: "엔딩 크레딧 스태프롤", "지난 화 요약 인서트", "다음 화 예고편"). false 면 null.
- context_extension: **모든 candidate**에 대해 다음을 판단·출력 (highlight 여부와 무관)
    - needed: 이 candidate를 단독 클립으로 보여줬을 때 시청자가 "뭐지?" 없이 흐름을 따라가려면 앞뒤 맥락이 필요한가?
    - extended_start_sec: needed=true면 인접 segment까지 확장한 시작점 (보통 핵심 직전 5~25초). false면 start_sec와 동일
    - extended_end_sec: needed=true면 인접 segment까지 확장한 종료점 (보통 핵심 직후 5~25초). false면 end_sec와 동일
    - before_summary / after_summary: 확장 부분에 무엇이 있는지 한 문장씩
    - reason: 왜 그 앞뒤가 필수인지 한 줄
    - 강제 조건: extended_start_sec ≤ start_sec ≤ end_sec ≤ extended_end_sec (양쪽 확장만 허용)
    - 확장 구간 안에 컷·장소 변경이 너무 많으면(>2회) needed:false 로 강등하라
    - **라운드 19C-1 기준 완화**: 다음 신호 중 하나라도 있으면 needed=true 적극 적용:
      - candidate 시작 직전 5초 안에 *도입·배경 대사·인물 등장*이 있음
      - candidate 끝 직후 5초 안에 *반응·여운 대사·결과 표정/액션*이 있음
      - 핵심 사건의 의미가 앞뒤 1~2개 segment 없이는 모호해짐
    - storytelling의 hook/build/payoff에서도 활용되어 클립 사이 시간 점프를 줄이는 데 사용된다.

- event_template: **라운드 19D — 행동 의미 라벨링 필수**. 이 후보의 *행동 의미*를 (subject, action, target, mode, location)로 명시.
    - subject: 행동의 주체 (캐릭터명; 영상 안 인물 이름과 일치). **[열린 라벨 허용]** 적용: 행위자가 엑스트라·행인이면 `"엑스트라"`, 누구인지 단정 불가하면 `"불명"`. ⚠️ 디폴트로 주요 인물명을 채우지 마라 — 잘못된 단정보다 `"불명"`이 우선.
    - action: 무슨 행동인지 간결한 동사구 (예: "맥주 권유", "병문안", "통화로 업무 위임", "고백"). [원칙 P1] 시각 단서 근거 필수
    - target: 행동의 대상 (캐릭터명·장소·물건; 없으면 null). 대상이 엑스트라·불명확하면 마찬가지로 `"엑스트라"`·`"불명"` 사용 가능
    - mode: 행동 방식 — `in_person` | `phone_call` | `narration` | `observation` | `mixed`
        - in_person: 두 인물이 같은 장소에서 직접 대면
        - phone_call: 전화·영상통화로 *떨어진 두 사람* 간 소통. 직접 대면 아님
        - narration: 내레이션·독백·VO
        - observation: 한 인물이 다른 인물·상황을 *관찰*만 함 (개입 없음)
        - mixed: 한 candidate 안에서 둘 이상 모드가 섞임 (드물게)
    - location: 장면 발생 장소 (예: "극장 로비", "병원", "야외 거리")
    - ⚠️ phone_call vs in_person 구분 매우 중요. 통화 장면을 in_person으로 잘못 라벨하면 다음 단계(스토리 구성)에서 "X가 Y에게 직접 고백" 같은 잘못된 title 생성됨.

- beats: **이 candidate의 [start_sec, end_sec] 구간을 더 잘게 쪼갠 세부 비트 배열**. 길이가 60초를 넘는 쇼츠에서 내용 흐름을 끊지 않고 줄이기 위해, 그리고 스토리 구성 단계가 장면 내부를 더 정밀하게 이해하도록 쓰인다.
    - 타일링 규칙: beats는 candidate 구간을 **빈틈 없이, 겹침 없이** 시간순으로 채운다. 첫 beat.start_sec == candidate.start_sec, 마지막 beat.end_sec == candidate.end_sec. (segments 타일링 규칙과 동일)
    - 비트 분할 기준: 대사 전환·동작 전환·컷·장소 전환·감정 전환 등 *의미 단위*로 나눠라. 보통 candidate당 2~8개. 한 문장/한 동작이 한 beat가 되는 게 이상적.
    - start_sec / end_sec: 이 beat의 시간 범위 (chunk-relative, candidate 범위 안). [타임스탬프 정확도] 규칙 동일 적용.
    - summary: 이 beat에서 무슨 일이 일어나는지 **1~2문장으로 구체적으로** 묘사 — 행동·시각 단서·감정 흐름을 담아라. 단순 한 줄 요약("전화 받음") 금지.
    - dialogue: 이 beat 구간에 들리는 **모든 발화**를 화자별로 [{{"speaker": "인물명", "line": "대사"}}] 배열로. candidate.transcript는 '단 한 명'만 담지만 beats.dialogue는 **여러 화자 전부** 담아라. 내레이션/VO/독백은 speaker="[내레이션]". 발화가 없으면 빈 배열 []. speaker는 [열린 라벨 허용] — 불명확하면 "불명".
    - mood: 이 beat의 분위기/감정 톤 한 단어~한 구 (예: "긴장", "유쾌", "슬픔", "정적", "충격"). 컷/전환으로 candidate 안에서 분위기가 바뀌면 beat마다 다르게.
    - location: 이 beat의 장소. candidate 안에서 장소가 바뀌면(컷) beat마다 다르게. scene_location과 같으면 그대로 복사.
    - characters: 이 beat에 등장하는 인물 배열. [열린 라벨 허용].
    - importance: 이 beat가 candidate 핵심 의미에 기여하는 정도 — "core"(없으면 의미 붕괴) | "supporting"(보강·연결) | "droppable"(빼도 흐름 유지). **애매하면 "supporting"** (절대 "droppable" 기본값 금지).
    - carries_payoff: 이 beat가 candidate의 *핵심 대사·반전·펀치라인*을 담으면 true, 아니면 false. (길이 단축 시 보호 대상)
    - 위 모든 내용은 [원칙 P1 시각/음성 단서 근거]·[원칙 P2 관찰 비약 금지]를 동일하게 엄수.

다음 스키마로만 응답 (※ 아래 예시의 숫자는 placeholder다. 실제 값은 [입력 정보]의 "현재 청크 번호"·"청크 범위"를 그대로 사용하라):
{{
  "chunk_index": <현재 청크 번호와 동일하게>,
  "chunk_start_sec": <청크 범위 시작값>,
  "chunk_end_sec": <청크 범위 종료값>,
  "summary": "해당 청크 전체의 핵심 내용 요약",
  "chunk_intro_credits_ranges": [
    {{"start_sec": 0.0, "end_sec": 12.5, "kind": "intro", "confidence": 0.95}}
  ],
  "characters_tracking": [
    {{
      "character": "인물명 또는 레이블",
      "appearances": [
        {{"start_sec": 0.0, "end_sec": 32.0, "action": "해당 구간 행동/발화 요약"}}
      ]
    }}
  ],
  "segments": [
    {{
      "segment_index": 0,
      "start_sec": 0.0,
      "end_sec": 45.3,
      "description": "이 구간 묘사 ([description 규칙] 엄수)",
      "transcript": "이 구간 핵심 발화 (없으면 \\"\\")",
      "characters_in_scene": ["인물명1"],
      "scene_location": "장면 배경 장소",
      "timeline_position": "현재|과거|불명"
    }}
  ],
  "candidate_moments": [
    {{
      "segment_index": 0,
      "chunk_index": <현재 청크 번호와 동일하게>,
      "candidate_index": <0부터 시작하는 청크 내 순번>,
      "start_sec": 12.4,
      "end_sec": 25.8,
      "characters_in_scene": ["인물명1", "인물명2"],
      "character_focus": ["인물명"],
      "description": "장면 설명(묘사 위주, 5문장 이상)",
      "reason": "선정 이유",
      "transcript": "단 한 명의 주요 발화 ([내레이션] 접두 가능)",
      "scene_location": "장면 배경 장소",
      "timeline_position": "현재|과거|불명",
      "continues_from": null,
      // 또는 이전 후보를 참조하는 경우:
      // {{"chunk_index": <참조 청크 번호 — 같은 청크면 현재 청크 번호, 이전 청크면 [이전 청크들의 분석 결과]에 명시된 chunk_index>, "candidate_index": <참조 후보의 candidate_index>}}
      "requires_context": false,
      "highlight_eligible": false,
      "highlight_reason": null,
      "visual_essential": false,
      "is_intro_credits": false,
      "intro_credits_reason": null,
      "context_extension": {{
        "needed": false,
        "extended_start_sec": 12.4,
        "extended_end_sec": 25.8,
        "before_summary": null,
        "after_summary": null,
        "reason": null
      }},
      "event_template": {{
        "subject": "주체 캐릭터명",
        "action": "행동 동사구 (예: '통화로 업무 위임', '맥주 권유', '병문안')",
        "target": "대상 캐릭터명/장소/물건 또는 null",
        "mode": "in_person | phone_call | narration | observation | mixed",
        "location": "장면 발생 장소"
      }},
      "beats": [
        {{
          "start_sec": 12.4,
          "end_sec": 16.0,
          "summary": "1~2문장 구체 묘사 (행동·시각 단서·감정 흐름)",
          "dialogue": [
            {{"speaker": "인물명 또는 [내레이션]/불명", "line": "대사"}}
          ],
          "mood": "긴장|유쾌|슬픔|정적|충격 등",
          "location": "이 beat의 장소",
          "characters": ["인물명"],
          "importance": "core | supporting | droppable",
          "carries_payoff": false
        }}
      ]
    }}
  ],
  "title_candidates": ["제목1", "제목2", "제목3"]
}}
"""

# ─────────────────────────────────────────────
# 스토리 구성 프롬프트 (레퍼런스 기반 바이럴 최적화)
# ─────────────────────────────────────────────
STORY_COMPOSITION_PROMPT = """
# Role
너는 드라마/영화/예능 기반 유튜브 쇼츠 100만 조회수 전문 편집자다.
시청자가 해당 쇼츠를 보고 재미를 느껴 작품을 궁금해하게 만든다.

# Task
제공된 영상 분석 데이터를 기반으로 스토리라인 3개를 구성하라.
해당 작품을 처음 보는 사람도 맥락을 모른채 이해하고 관심을 가질 수 있게 구성하라.
각 스토리라인은 **storytelling(멀티클립 서사형)** 또는 **highlight(단일클립 완결형)** 중 적합한 타입을 선택하라.
3개 모두 독립적으로 완성도 높은 쇼츠가 될 수 있어야 한다.
그중 가장 바이럴 성공 가능성이 높은 하나를 최종 선정하되, 나머지 2개도 사용될 수 있다.

## 스토리라인 구성 단위: "하나의 상황"

에피소드 전체 줄거리를 압축해서 담으려 하지 마라. 하나의 스토리라인은 에피소드 내 **하나의 상황**을 단위로 구성하라.
- "하나의 상황"이란: 하나의 사건, 인물의 결정·행동, 감정 전환, 관계 변화 중 **자체로 완결되는 한 단위**
- 에피소드 전체를 요약하듯 구성하면 작품을 모르는 시청자는 맥락이 너무 많아 따라가지 못한다
- 한 상황에 집중하면 배경 지식 없이도 흐름과 감정을 따라갈 수 있다

## 장면 사실성 원칙 (절대 규칙) — description은 영상의 "요약"이지 "본질"이 아니다

너(Gemini)는 candidate_moment의 description을 텍스트로 읽지만, 시청자는 그 description의 출처가 된 **영상 자체를 본다.**
- 시청자는 장면의 "의미"를 추론하지 않는다. **눈앞에서 일어나는 사건**을 본다.
- 따라서 "이 장면이 storyline 주제 X를 상징/대비/복선으로 표현한다"는 너의 해석은 **편집된 쇼츠에서는 전달되지 않을 가능성이 높다.**

[장면 선택 시 자문해야 할 것]
1. "음소거하고 자막 없이 이 클립만 본 사람"이 무슨 일이 일어나는지 즉시 알 수 있는가?
   → 알 수 없다면 그 description의 의미는 너의 머릿속에만 있는 것.
2. hook → build → payoff를 순서대로 본 시청자에게 "방금 본 영상 뭐였어?"라고 물으면 storyline.topic과 같은 답이 나올까?
   → 단순히 "사건 A, B, C가 차례로 나왔다"로만 보인다면 의미적 연결이 작동하지 않는 것.
3. 두 장면 사이의 연결이 "의미적 도약"인지 "사건의 흐름"인지 구분하라.
   - "쾌활한 셀카 → 화상 흉터 발견"은 의미적 대비지만 사건상으로는 단절돼 있음.
   - 같은 인물의 같은 시점/공간에서 이어지거나, 동작·대사·시선이 직접 연결되는 장면이어야 시청자도 한 흐름으로 본다.

[피해야 할 패턴]
- description의 **형용사**("밝다", "쾌활하다", "따뜻하다")에 매칭해서 장면 고르기 → 시청자는 형용사를 안 본다, 사건을 본다.
- "이 장면이 반전을 위한 빌드업이다"처럼 **너만 아는 의도로** 장면을 끼워넣기.

# 타입 선택 기준

후보 클립 목록에서 highlight_eligible: true인 클립 수와 전체 클립 수를 직접 세어 비율을 계산하라.

- 비율 **90% 이상**: 전부 highlight로 구성해도 된다.
- 비율 **20% 이하**: storytelling을 반드시 둘 이상 구성하라.
- 그 외 (20%~90%): storytelling과 highlight를 적절히 혼합하라.
- highlight 타입은 반드시 highlight_eligible: true인 클립에만 사용하라.

## storytelling 타입 — 멀티클립 서사형

- 모든 chunk의 candidate_moments 중에서 여러 장면들을 선정해 원본의 서사를 반영한 기승전결이 있도록 여러 장면을 유기적으로 연결할 것.
- 캐릭터의 행적을 조명하거나 영상의 주요 서사를 요약.
- sequence_type: "여정몰입형" / "결과선공개형" / "반전형" / "시퀀스블록형" 중 선택 (storytelling만 해당)

### sequence_type 선택 결정 트리 (반드시 이 순서로 검토)

**1단계 — 시퀀스블록형 우선 검토**:
- candidate 입력에 같은 `sequence_id`를 가진 candidate가 **3개 이상** 묶여 있고, 그 묶음이 자체로 hook→발전→결말을 모두 포함하면 → **시퀀스블록형 선택**
- 한 sequence_id의 묶음이 자연스러운 코너/씬이므로 build·payoff 구분 없이 통째로 사용
- 예: SNL 한 콩트의 시작~중간~끝이 한 sequence_id (같은 sequence_id 4~5개)
- ⚠️ 이 조건이 충족되는데 다른 sequence_type을 선택하면 시퀀스 정보를 낭비하는 것이다.

**2단계 — 반전형 검토**:
- 1단계에 해당 안 하고, candidate description에서 **hook/build와 payoff의 결말 방향이 반대**이면 → **반전형 선택**
- 판별: hook/build에서 *주인공 승리·정상·평온* 묘사 → payoff에서 *반전 패배·굴욕·대참사·발각·실패*
- 시간 순서는 자연스러움 (결과선공개형처럼 시간 역순 아님)
- ⚠️ **반전형 title_line2는 반드시 payoff 결말 방향을 따른다** — hook/build의 점수·결과 패턴을 외삽해서 payoff와 반대 방향 title을 출력하면 안 된다.
- 예: hook(주인공 1대0 승) → build(2대0 승) → payoff(셀프 제모 발각 굴욕)
  - 올바른 line2: "막판 대참사" / "한순간 대굴욕" / "역대급 굴욕"
  - ❌ 잘못된 line2: "3대0으로 박살낸 주인공" — 패턴 외삽으로 payoff 반전 무시

**3단계 — 결과선공개형 검토**:
- 1·2단계 해당 안 하고, 핵심 결과 장면을 **hook에 *시간 역순*으로 미리 노출**해 "이게 왜?" 궁금증 유발하면 → **결과선공개형 선택**
- 판별: hook이 시간상 build·payoff *뒤*에 일어난 장면 (시간 역순 hook)
- ⚠️ **결과선공개형 hook 제약**: hook은 반드시 build/payoff와 동일 인물이 1명 이상 겹치거나, hook의 상황이 build/payoff의 직접적 결과여야 한다.

**4단계 — 여정몰입형 (디폴트)**:
- 1·2·3단계 해당 안 하면 → 여정몰입형. hook~payoff 시간 순서, 같은 방향 흐름.

### 결과선공개형 vs 반전형 (가장 헷갈리는 차이)

| 구분 | 시간 순서 | 결말 방향 | 예 |
|------|-----------|-----------|-----|
| 결과선공개형 | hook이 시간 *역순* (결말 미리) | hook과 build·payoff 결말이 *동일* | hook(셀프 제모 발각) → build(1대0 승) → payoff(2대0 승) |
| 반전형 | hook→build→payoff 시간 *순서* 그대로 | hook/build와 payoff 결말이 *반대* | hook(1대0 승) → build(2대0 승) → payoff(셀프 제모 발각) |

**핵심 차이**: 결과선공개형은 "결말을 hook에 미리 보여주는 시간 역순", 반전형은 "시간 순서대로지만 끝에 반전".

### 시퀀스블록형 schema

   - sequence_block 필드: `[{{"chunk_index": N, "candidate_index": M}}, ...]` 형태로 같은 sequence_id 안 candidate 참조
   - hook(선택) + sequence_block(필수) 구성
   - ⚠️ sequence_block 안 candidate들은 **같은 sequence_id**여야 한다.

### sequence_id — 인접 배치 시 안전 여부 판별용 (강제 규칙 아님)

각 클립에는 `sequence_id` 정수 필드가 있다. 같은 숫자 = 원본 영상에서 직접 이어지는 장면들의 집합.
이 정보는 **클립을 인접 배치할 때 자연스러운 조합인지 판별하는 참고용**이다. "같은 sequence_id끼리 묶어 쓰라"는 지시가 아니다.

⚠️ TTS 나레이션은 별도 단계(tts_planner)에서 결정한다. 이 단계에서는 클립 시간/제목/리듬만 결정하라. tts_line 필드를 출력하지 마라.

### candidate.description vs transcript — 음성 우선 (라운드 15)

각 candidate에는 `description`(시각 분석)과 `transcript`(실제 음성 전사) 두 필드가 있다.
**transcript는 영상 안 실제 음성을 Whisper로 전사한 정확한 텍스트**다.
description은 LLM의 시각 분석 추정이라 *통화 상대*, *대사 인물*, *화면 전환 후 등장 인물*
같은 음성 의존 정보가 부정확할 수 있다.

⚠️ **transcript와 description이 충돌하면 transcript를 신뢰하라**.

예 (실제 발생한 LLM 오류):
- description: "유미가 통화 후 주호가 등장 → 주호한테 전화한 것"
- transcript: "여보세요? 피디님, 저예요. 그 영화 같이 볼까요?"
- 옳은 라벨: 유미가 *피디(순록)*에게 전화 (transcript 명시), 주호 등장은 별개 장면
- 잘못된 title (X): "주호한테 전화한 대참사"
- 옳은 title (O): "피디한테 영화 같이 보자고" 같이 transcript 기반

title_line2에 통화 상대·대사 인물·등장 인물 같은 *음성 의존 정보*를 단정적으로
사용하려면 반드시 transcript에서 그 인물명·대사가 명확히 확인되어야 한다. transcript에
명시 없는 추정 라벨은 *부정확 위험* 라벨로 간주하고 보다 일반적인 표현으로 대체하라.

### candidate.event_template — 행동 의미 라벨링 (라운드 19D)

각 candidate에는 `event_template` 필드가 있다 (subject, action, target, mode, location).
이는 분석 단계에서 결정된 *행동 의미*다. title 작성 시 이 정보를 1순위로 사용하라.

⚠️ **phone_call 모드 라벨 규칙 — 발신자 행동만 라벨링**:
- mode == "phone_call"이면 subject = 발신자, target = 수신자.
- title은 **발신자가 수신자에게 *시킨/알린 행동* 위주**로 작성하라.
  - ✅ 옳은 예: "유미가 PD에게 일을 맡김", "유미가 작가에게 전화로 통보"
  - ❌ 잘못된 예: "유미에게 고백", "PD에게 마음을 전한 유미" (수신자 시점 라벨)

⚠️ **confession/사랑/제안 단어 차단 (mode=phone_call)**:
- phone_call에서 발화된 대사라도 직접 대면 아니면 title에 '고백/사랑/제안/프러포즈' 단어 사용 금지.
- 대신 위임/통보/알림/지시/확인 같은 *기능적 행동* 단어 사용.

⚠️ **검증 단계 — title 작성 후 event_template 재확인**:
1. 선택된 storyline의 모든 clip의 event_template.mode 목록을 확인
2. mode=phone_call이 1개 이상 포함됐고, title_line1 또는 title_line2에 '고백/사랑/제안/프러포즈' 단어가 있으면 **재작성**
3. 재작성 시: 발신자 subject가 target에게 행한 *기능적 action*을 위주로

예 (실제 케이스 — 라운드 18 결함):
- 장면: 유미가 *병원 병문안 와서* 다른 사람에게 *전화로 일을 맡김*
- event_template: {{subject:"유미", action:"통화로 업무 위임", target:"PD", mode:"phone_call", location:"병원"}}
- ❌ 잘못된 title: "유미에게 고백한 PD" (mode 무시)
- ✅ 옳은 title: "병문안 중 통화로 일 맡긴 유미" (subject·action·mode 반영)

### 원본 타임라인 순서 원칙 (절대 규칙)

- **build 클립들과 payoff는 반드시 원본 영상의 시간 순서(start_sec 오름차순)대로 배치해야 한다**
  - build[0].start_sec < build[1].start_sec < ... < payoff.start_sec 를 반드시 만족
- 점수가 높다고 해서 뒤에 나온 장면을 앞으로 당기거나 순서를 임의로 섞는 것은 절대 금지
- 원본 영상의 전개 흐름을 무시한 뒤죽박죽 구성은 시청자에게 혼란을 줌

### hook 시간 위치 검증 (절대 규칙) — hook 이중 사용 결정

hook을 결정한 뒤 build / payoff와 시간을 비교해 다음 케이스를 적용하라:

[케이스 1] hook.start_sec < build[0].start_sec  (시간순)
[케이스 2] hook.start_sec > payoff.end_sec      (끝-결과 선공개)
[케이스 3] build[0].start_sec ≤ hook.start_sec ≤ payoff.end_sec  (hook이 build/payoff 사이)

→ 케이스 1, 2: hook_preview = null. 시퀀스: hook → build → payoff
→ 케이스 3: ⚠️ hook_preview 필수 (3~7초 발췌). 후처리 시퀀스: hook_preview → build → hook(시간순 자리) → payoff
   (build → hook → payoff 가 시간순으로 자연 연결되어 인과 끊김 사라짐)

### 클립 경계 자연 연결 (storytelling)

각 클립(hook / build[*] / payoff)을 결정할 때 해당 candidate의 `context_extension`을 검토하라.
candidate.context_extension.needed=true면 그 클립의 start_sec/end_sec을 extended 시간으로 설정해
인접 segments를 자연스럽게 흡수할 수 있다. 결과: 클립 사이 시간 점프가 줄어 흐름이 부드러워짐.

다만 다음을 지켜라:
- build[i].extended_end_sec 이 build[i+1].start_sec 을 넘으면 안 됨 (시간 역전 금지) → 충돌 시 충돌 없는 범위로 잘라 쓰거나 needed=false로 폴백
- 모든 클립 시간 합이 max_duration_sec를 초과하지 않도록 확장 우선순위:
  payoff > hook > 가장 클라이맥스인 build > 나머지 build
- segments 컨텍스트(`[전체 장면 흐름 (segments)]` 블록)을 함께 참고해 인접 segment 묘사가 현재 클립과 자연스럽게 이어지는지 확인

extended 시간을 적용한 클립에는 `"context_extended": true` 플래그를 표기 (디버그용).

## highlight 타입 — 단일클립 완결형

단일 클립만으로 시청자가 "뭐지?" → 감정반응 → 완결까지 느낄 수 있는 경우 선택하라.
멀티클립으로 이으면 오히려 흐름이 끊기거나 불필요해지는 경우가 여기에 해당한다.
**반드시 highlight_eligible: true인 클립만 사용할 수 있다.**

### context_extension 활용 (highlight 시간 자동 확장)

⭐ highlight candidate 후보 중 **`context_extension.needed=true`인 candidate를 우선 채택하라**.
이 candidate들은 setup → 핵심 → 결과 흐름이 자동으로 묶여서 완결성·맥락이 강해진다.
needed=true 후보가 동일한 점수대에서 경쟁하면, 일반 candidate보다 needed=true 쪽을 선택하라.

needed=true 채택 시:
- storyline 출력에 `"context_extended": true` 플래그만 표기
- start_sec/end_sec은 후처리에서 candidate.context_extension.extended_start_sec / extended_end_sec로 자동 적용됨 (LLM이 시간 변형 금지)

needed=false면 candidate의 원래 start_sec/end_sec 그대로 사용 (`"context_extended": false`).

⚠️ extended 구간이 max_duration_sec를 초과하면 needed=false 폴백 후보로 교체.

# 공통 규칙

## topic-title-스토리 일관성 (최상위 원칙)

topic은 이 쇼츠가 결국 무엇에 관한 이야기인지를 정의하는 척추다.
topic이 바뀌지 않는 한 제목과 결말은 같은 이야기를 가리켜야 한다.

- **title_line1 / title_line2**는 topic을 시청자의 언어로 압축한 것이어야 한다
- **hook → build → payoff**의 흐름은 topic이 전개되고 완결되는 과정이어야 한다
- 셋 중 하나라도 topic에서 벗어난다면 topic을 재정의하거나 클립/제목을 교체하라

## 3개 스토리라인 독립성

- 3개 스토리라인은 **서로 다른 장면을 사용**해야 한다 (동일 씬 중복 사용 금지)
- 각 스토리라인마다 **독립적인 서사 아크 + 제목(title_line1+title_line2)**을 갖추어야 한다

## 제목 구조 (2줄 필수)

레퍼런스 분석 결과 고조회수 쇼츠 제목은 2줄 구조 권장:
- **title_line1** (위·흰색, 10자 권장, 최대 15자): **상황/배경/도입 설명**.
  인물 자체보다 *무슨 일이 일어났는지* 또는 *어떤 상황인지*를 압축.
- **title_line2** (아래·노란색 강조, 10자 권장, 최대 15자): **캐릭터·반전·핵심 후킹**.
  시청자 시선을 잡는 핵심 문구. 강조 자리.

글자 수 가이드 (라운드 22): 13자 이내 가독성 최고 (폰트 sqrt 자동 축소).

두 줄의 의미 역할을 일관되게 유지 권장 — 강조 자리(line2)에 캐릭터·반전이 와야 후킹 효과↑.

### line1 ↔ line2 문법적 연결 규칙 (라운드 18 — 필수)

두 줄을 **이어 읽었을 때 한 호흡으로 자연스럽게 연결**되어야 한다. 별도 단위로 작성해 어색하게 붙이지 마라.

**조건절-결과절 패턴**: line1이 연결어미(...면, ...니, ...만, ...는데, ...자)로 끝나면
line2는 그 **결과·반응·결말을 완결하는 절**이어야 한다 (동사 종결: ...된다 / ...한다 / ...버린다).
- ✅ "비 오는 날 우산 같이 쓰면 / 철벽남도 무장해제 된다"
- ❌ "비 오는 날 우산 같이 쓰면 / 철벽남 무장해제 시킨 유미" (조건→라벨 불일치)
- ✅ "사랑에 빠진 걸 분석하면 / 호감의 흔적이 보인다"
- ❌ "사랑에 빠지는 걸 분석하면 / 엑스레이에 찍힌 사랑의 증거" (조건→명사구 불일치)

**병렬 라벨 패턴**: line1이 *명사구·완결절*로 끝나면 line2도 같은 구조 유지.
- ✅ "모두 기피하는 깡치사건 / 클리어하는 이한영" (둘 다 명사구·동명사)
- ✅ "일만 하는 꼰대인 줄 알았는데 / 알고보니 29살 연하남" (반전 연결, 자연스러운 호흡)

**시간순·인과 흐름 패턴**: line1이 시점·도입을 잡고 line2가 그 시점에 *벌어진 결과*를 드러낸다.
- ✅ "비 오는 밤 우산 속에서 / 철벽남 마음 흔든 한 마디"
- ✅ "엑스레이로 마음을 들여다보니 / 보이는 사랑의 증거"

### 검증 방법
title_line1과 title_line2를 *공백 한 칸*으로 이어 읽어 본다. 어색하면 둘 중 하나를 다시 작성.
조건절(...면)으로 끝났다면 line2는 반드시 동사 종결 결과절이어야 한다.

## 제목 사실성 원칙 (절대 규칙) — 입력 데이터에 없는 내용은 제목에 쓰지 마라
(transcript / event_template / description / characters_in_scene / segments 등)만
제목의 사실 근거로 사용할 수 있다. **입력에 없는 행동·감정·관계·결과를 만들어내지 마라.**

[금지 — "한 단계 앞선" 라벨링]
- 행동의 범주 변경 금지:
  - description에 "서류 건넴"만 있고 transcript에 서류 종류가 없으면 → "사표 제출"로 쓰지 마라
  - description에 "마주 본다"만 있고 transcript에 고백 대사가 없으면 → "사랑 고백"으로 쓰지 마라
  - event_template.mode == "phone_call"이면 → "직접 만남/대면"으로 쓰지 마라 (기존 규칙 재확인)
- 감정·강도 과장 금지:
  - description의 "표정이 굳는다" → "분노 폭발", "당황한다" → "충격 대참사" 같이 강도를 한 칸 올리지 마라
  - 형용사 강화(놀라움 → 충격, 의외 → 반전, 좋아함 → 사랑)는 transcript나 event_template에 명백한 근거가 있을 때만
- 결과·인과 외삽 금지:
  - description에서 1→2 추이만 확인되면 "3대0 압승" 라벨 금지 (외삽)
  - "곧 ~할 것이다" 식 미래 예단 금지 — payoff description이 결과를 명시한 경우에만 결말 라벨링
- 인물 관계·지위 단정 금지:
  - transcript의 호칭이나 event_template의 target 외 정보로 "연인/형제/적/상사" 같은 관계를 단정하지 마라
- highlight title 합성 금지 (선택 candidate 단일 출처 원칙):
  - highlight의 topic/title/viral_titles는 *선택한 (chunk_index, candidate_index)*의 description·transcript·event_template에서만 근거 추출 — 같은 청크의 다른 candidate 모티프를 빌려오지 말 것. description에 명시되지 않은 시각 명사(흉터·문신·상처·반지·눈물·피 등) 임의 추가 금지

## title_line2 ↔ payoff 결말 일관성 (필수)

title_line2는 단순 후킹 문구가 아니라, **payoff 클립의 실제 결말 방향과 의미가 일치**해야 한다.
hook/build의 흐름 패턴을 외삽해서 *반대 방향* 결말을 라벨링하지 마라.

### 올바른 예 (결말과 일치)
- payoff: 주인공이 셀프 제모 발각으로 굴욕 (반전 패배)
  - line2: "막판 대참사로 무너진" / "마지막에 무너진 주인공" / "한순간 대굴욕"
  - ❌ 잘못: "3대0으로 박살낸 주인공" — hook/build 패턴(1→2→3) 외삽, payoff 반전 무시
- payoff: 유미가 마음 인정 (긍정 결말)
  - line2: "사랑에 빠진걸 인정한 유미"
  - ❌ 잘못: "철벽 친 유미" — payoff는 마음 인정인데 line2는 정반대

### 검증 방법
title_line2를 작성한 후 payoff description을 다시 읽어 결말 방향이 일치하는지 확인.
불일치하면 line2를 payoff 결말에 맞게 재작성. **반전형은 반드시 payoff 결말을 따른다.**

이모지는 사용하지 않음.

## Duration Constraint (요즘 쇼츠 표준)

총 클립 길이 합계: **{min_duration_sec}초 ~ {max_duration_sec}초** (이상적: 50초 부근).

### 타입형별 구성 가이드

**highlight형** (shorts_type="highlight"):
- 1개 클립이 자체로 후킹 + 본문 + 결말을 포함하는 강한 장면
- 길이: 40~60초 (이상적 50초)
- candidate가 30초 미만이면 context_extended=true로 인접 영역까지 확장 요청

**storytelling형** (shorts_type="storytelling"):
- **총 3~4 클립**으로 hook → build → payoff 구성
- 클립 길이 가이드:
  - hook: **5~8초** (강한 후킹 한 장면)
  - build: **1~2개** × 각 **15~20초** (서사 풀이의 핵심 1~2 장면만 — 너무 많이 넣지 말 것)
  - payoff: **15~20초** (감정 정점 / 반전 결말)
- 합계 40~60초 안에 들어오게 클립 수와 길이 조절
- 5개 이상 build로 늘이지 말 것 — 1~2개 핵심 build만 선택

⚠️ 합계가 60초 초과 시 후처리에서 점수 낮은 build부터 자동 제거됨.
   처음부터 핵심만 선택해 60초 이내로 출력하는 것이 가장 효과적.
⚠️ 합계가 40초 미만 시 후처리에서 인접 candidate로 자동 확장 시도.
   가능한 40초 이상 확보.

각 클립의 start_sec, end_sec는 원본 영상 타임라인 기준.
각 장면의 (end_sec - start_sec)를 합산하여 범위 내인지 반드시 확인.

# Input Data
- 작품명: {work_title}
- 주제: {topic}
{story_topic_line}
{work_context_block}
{episodes_context_block}
{segments_summary_block}

- 후보 장면 및 분석 데이터:
{candidates_str}

## 후보의 beats 활용 (장면 내부 정밀 이해)
각 candidate에는 구간을 더 잘게 쪼갠 `beats[]`가 있고, 각 beat는 `summary`·`dialogue`(여러 화자 전부)·`mood`·`location`을 담는다.
- candidate.transcript는 '단 한 명'의 발화만 담지만, `beats[].dialogue`는 그 장면의 **모든 대사**를 담으니 흐름·관계 판단에 우선 활용하라.
- `beats[].mood`/`location`으로 인접 클립 사이 분위기·장소 연결이 자연스러운지(급격한 단절이 없는지) 평가하라.
- hook→build→payoff 배치 시 beats의 감정 흐름을 보고 감정 낙차가 큰 지점을 hook/payoff로 삼아라.
- ⚠️ beats는 *이해용 참고 자료*다. 클립 선택 단위는 여전히 candidate(chunk_index/candidate_index)이며, beat 단위로 시간을 쪼개 출력하지 마라.

# Constraints & Rules

⚠️⚠️ **시간 절대 고정 정책 — 가장 중요** ⚠️⚠️
- candidate_moments에 있는 start_sec, end_sec, context_extension.extended_start_sec, extended_end_sec는
  **절대 변형하지 마라.** 1초도 줄이거나 늘리지 마라.
- LLM 출력의 start_sec/end_sec 값은 후처리 단계에서 chunk_index/candidate_index 기반 lookup으로 덮어씀 →
  네가 수치를 적었어도 무시된다. 그러므로 candidate에 적힌 값을 정확히 인용하기만 하라.
- 클립 시간 합이 max_duration_sec를 초과해도 **시간을 줄여서 맞추지 마라.**
  대신 (a) build 후보 개수를 줄이거나 (b) 더 짧은 다른 candidate로 교체해서 맞춰라.
- context_extension 적용은 "context_extended": true 플래그 표기로만 표현. 시간은 후처리에서 ext 적용.

1. 각 클립의 chunk_index, candidate_index만 정확히 명시 (start_sec/end_sec는 candidate 그대로 인용)
2. 작품의 전체 맥락을 모르는 사람도 한 번 보고 재미를 느낄 수 있는 장면을 선정
3. score는 description, reason, requires_context, highlight_eligible 등 후보의 의미적 평가를 종합해 0.0~1.0으로 정직하게 평가하라
4. 'continues_from'을 참고하여 맥락이 끊기지 않게 하라
5. 위 # Input Data의 "이전 에피소드 요약"이 비어있지 않으면, 이전 회차에서 묘사된 인물 관계·미해결 갈등을 후킹 포인트로 활용하여 연속극 시청자에게 자연스럽게 이어지도록 hook/payoff를 구성하라
6. (예약됨 — narrative_skeleton 단계 제거됨)
7. 위 # Input Data의 "[전체 장면 흐름 (segments)]"이 비어있지 않으면, 다음 용도로만 활용하라:
   - candidate_moments가 영상 어느 흐름에 위치하는지 맥락 파악
   - 두 candidate 사이 갭이 큰 경우 그 사이 segments 묘사를 참고해 자연스러운 흐름 작성
   - hook 직전·payoff 직후 장면을 segments에서 확인해 후보 선택 시 참고
   ⚠️ segments에 있는 평범한 구간을 새 candidate로 만들지 마라. 후보는 반드시 candidate_moments에서만 선택.
8. (storytelling) 각 클립(hook / build[*] / payoff)도 해당 candidate.context_extension을 검토하라.
   - needed=true면 "context_extended": true 플래그만 출력 (시간은 후처리에서 ext 적용)
   - 클립 시간 합 ≤ max_duration_sec 초과 시: candidate를 줄이거나 교체. **절대 시간을 줄이지 마라.**
9. (storytelling) hook이 build와 payoff 시간대 사이에 있으면(케이스 3) 반드시 hook_preview를 출력하라.
   - hook_preview.start_sec/end_sec은 hook 시간 안 짧은 발췌 (3~7초 권장)
   - 같은 장면이 두 번 등장하므로 hook_preview는 임팩트 핵심만 짧게
   - hook 본체는 시퀀스 후처리에서 build 다음 자리(시간순)로 자동 배치된다
   - 케이스 1·2(hook이 시간순 앞 또는 끝-결과)에서는 hook_preview = null
10. (storylines 다양성) storylines 배열을 2개 이상 만들 경우, **서로 다른 사건/장면/코너**에서 추출하라.
    - 같은 인물·같은 장소·같은 사건의 다른 각도 변형은 다양성 위반 (예: "의사 캐릭터 A의 이중성" + "의사 캐릭터 A의 기싸움" + "의사 캐릭터 A 환자 응대" 셋 다 같은 코너 → 1개만 채택)
    - 가능하면 각 storyline의 hook/build/payoff가 서로 다른 chunk_index에서 시작되도록 배분
    - narrative_skeleton.emotional_arc[]가 주어졌다면 서로 다른 phase의 장면을 우선 선택
    - 같은 코너만 반복되면 시청자 입장에서 "다 비슷한 영상" 인상을 주므로 점수 0.05~0.10 감점
11. (storytelling 클립 수) 모든 storytelling storyline은 hook + build(1개 이상) + payoff = **최소 3개 클립** 필수.
    - 1~2개 클립으로는 스토리 흐름이 만들어지지 않음 → 후처리에서 reject 됨
    - build를 충분히 찾기 어려우면 해당 storyline의 score를 낮추고, 다른 storyline 우선 추천

## TTS cue 작성 (각 storyline 본체에 `tts_cues` 배열 출력)

각 storyline 마다 `tts_cues` 배열을 함께 출력하라. cue 는 **클립 앵커** 기준으로 적는다 —
"어느 클립(`clip_index`)의 시작에서 몇 초 지점(`offset_sec`)"만 말하면, 절대 시간은
타임라인이 확정된 뒤 파이프라인이 계산한다. **절대 시간(start_sec/end_sec)은 적지 않는다.**

### 작성 규칙

1. **TTS 는 꼭 필요한 곳에만**. 모든 컷에 다는 것 금지. 보통 클립 1개당 0~2개, **storyline 전체 0~5개 cue**. 5개 초과 금지 (응답 토큰 제어).
2. `clip_index` = 그 storyline 클립 배열의 0-based 인덱스. storytelling 은 hook(0) → build(순서대로) → payoff(마지막) 순, 시퀀스블록형은 sequence_block 배열 순서, highlight 는 단일 클립이므로 항상 0. `clip_role` 은 검증·가독용("hook"/"build"/"payoff"/"sequence"/"highlight") — clip_index 와 불일치 시 clip_index 우선. `offset_sec` = 그 클립 시작으로부터의 초 (≥0, 클립 길이 이내). `duration_sec` = cue 길이 (보통 2~6초).
3. 같은 clip_index 안에서 cue 끼리 offset 구간이 겹치지 않게. (서로 다른 클립 간 겹침은 후처리가 정리한다)
4. cue 텍스트가 클립의 핵심 transcript 와 동시에 충돌하지 않도록 배치. ★**그 클립에서 화자가 그 내용을 말하기 *전에* cue 가 먼저 말하지 않게 한다. 요약·선언 cue 는 화자의 해당 발화 *뒤*(offset 을 그 뒤로) 배치한다.**
5. **결과선공개형 타임점프 cue**: sequence_type == "결과선공개형" 이면 build[0] 클립 시작 지점(그 clip_index, offset_sec 0 부근)에 타임점프를 알리는 cue 를 반드시 배치하라. 의문형 유도 금지 ("대체 무슨 일이?", "어떻게 이렇게 됐을까?"). 명사형 종결 또는 단언체로 시점·맥락을 단언하라.
6. **맥락 연속성**: 각 cue 는 그 시점 영상의 *실제 사건*을 짧게 설명하면서, 인접 cue 와 한 호흡으로 이어져야 한다. cue 들을 한 줄씩 이어 읽었을 때 시청자가 "무슨 영상인지" 한 줄로 답할 수 있어야 한다.

### candidate.tts_draft 활용 + 사실성 원칙

- candidate 입력에 `tts_draft` (analyze_chunk 가 영상을 직접 보면서 적은 한 컷 단독 내레이션 초안) 가 있다. *사실성*은 신뢰하되 storyline 흐름·인접 cue 와의 맥락 연결은 너의 책임 — 각 컷의 tts_draft 를 그대로 베끼지 말고 storyline 흐름에 맞춰 다듬어 cue.text 로 옮겨라. 첫 cue 가 도입을 깔면 다음 cue 는 그 도입을 받는 전개·반전이 되도록.
- 위 [transcript 우선] (라운드 15) 과 [제목 사실성 원칙] 이 cue 텍스트에도 동일하게 적용. transcript 와 tts_draft 가 충돌하면 transcript 가 우선. event_template.mode == "phone_call" cue 에 '고백/사랑/제안/프러포즈' 금지.
- tts_draft 가 비어 있거나 storyline 흐름에 안 맞으면 cue 를 만들지 않아도 된다.

### 텍스트 톤 (가장 중요)

쇼츠 내레이션이다. **뉴스 헤드라인체도, 예능·슬랭 톤도 둘 다 금지.**
방향: "상황을 짧게 설명해 다음 장면이 궁금해지게 만든다" — 후킹·여운·인물 명사화.

[금지]
- ❌ 격식체 / 헤드라인체: "~합니다, ~됩니다, ~입니다, 마침내, 비로소, 새로운 ~의 탄생"
- ❌ 가벼운 슬랭·예능톤·반말: "~네, ~함, ~임, ㅋㅋ, 헐, 미친 설계, 통째로 먹었네, 한 방에 다 뒤집힘"
- ❌ 시청자 직접 호명: "봐봐, 잘 봐, 이거 진짜?"

[권장 — 다음 셋 중 하나의 결로]
1. **명사형 종결**: "결국 시장을 통째로 장악한 희로." / "이 판을 뒤집을 한 사람."
2. **상황 설명 + 여운 (~다 / ~된다 / ~인 셈)**: "조용히 판을 다시 짠다." / "그가 노린 건 시장 그 자체였다."
3. **궁금증 유발 (~는데? / 근데~)**: "근데 이게 진짜 끝이 아니다." / "그가 진짜 노린 건 따로 있는데?"

[길이·구조]
- 한 cue = **한 문장**, 12~25자 권장. 평서 위주, 의문은 cue 전체의 1/3 이내.
- 어미: "~다 / ~ㄴ다 / ~인 셈 / ~의 X / ~는데? / 명사형 점." — "~네 / ~함 / ~임" 금지.

[좋은 예 vs 나쁜 예]
- ❌ "결국 시장을 통째로 먹었네." → ✅ "결국 시장을 통째로 장악한 희로."
- ❌ "한 방에 다 뒤집힘." → ✅ "이 한 수로 판세가 뒤집힌다."
- ❌ "근데 진짜 노림수는 이거였음." → ✅ "그가 진짜 노린 건 따로 있는데?"

### voice 프리셋 (정확히 이 라벨만 사용)

[자연스러운 한국어 — 우선]
- `ko_female` : 기본 한국 여성 (차분, 자연스러운 발음)
- `ko_female_high` : 밝은 한국 여성 (피치 높음, 트렌드·임팩트)
- `ko_male` : 기본 한국 남성 (차분 다큐풍)
- `ko_male_low` : 낮은 한국 남성 (피치 낮음, 묵직·진지)

[트렌드 multilingual — 작품 톤이 챗봇/이국·캐주얼/시크 등에 어울릴 때만]
- `chat_emma` / `chat_brian` / `chat_seraphina` / `chat_florian`

⚠️ multilingual voice 는 한국어를 처리할 수 있지만 약간의 외국 억양이 섞일 수 있다. 한국 드라마 일반(스릴러·로맨스·예능)이면 `ko_*` 우선.

### speed 라벨 (정확히 이 5개만)
- `very_slow` / `slow` / `normal` / `fast` / `very_fast`

### voice / speed 매핑

**🚫 한 쇼츠 = 한 voice (절대 규칙)**: 이 storyline 의 모든 cue 는 같은 voice 라벨을 사용해야 한다. cue 마다 voice 바꾸지 마라. voice 는 작품·storyline 전체 톤 1개를 골라 모든 cue 에 일관되게. (speed 는 cue 마다 자유 — 톤 강약은 speed 로.)

**voice 선택 가이드**:
- 한국 드라마/예능 일반 → 기본 `ko_female` 또는 `ko_male`
- 진지·묵직한 다큐·내레이션 → `ko_male_low`
- 가벼운 후킹·바이럴·코믹 → `ko_female_high`
- AI 챗봇/SF/이국적·시크 → `chat_emma` / `chat_seraphina` (여성), `chat_brian` / `chat_florian` (남성)

**speed (cue 마다 자유)**:
- 정적·진지 → `slow` / `very_slow`
- 일반 → `normal`
- 임팩트·긴박감 → `fast` / `very_fast`

---

## 출력 형식 (필수)

응답은 반드시 **JSON 객체 1개**여야 하며, 최상위 키 `storylines` (배열) 가 **반드시 포함**되어야 한다.
다른 키 이름(예: shorts, proposals, options 등) 사용 금지. 마크다운 ```json 펜스는 허용되나, 텍스트 설명은 금지.

⚠️ **hook / build[*] / payoff의 `description` 필드는 입력 candidate의 `description`을 *그대로 복사*하라.**
재작성·압축·요약·재해석 금지. analyze_chunk 단계에서 작성된 원본 시각 분석을 그대로 통과시켜야 다음 단계(TTS plan 등)에서 정보 손실이 없다.

각 storyline의 `narrative_plan`을 해당 storyline의 클립 선택 전에 먼저 작성하라.
점수나 수치가 아니라 "어떤 장면/이야기를 쇼츠로 만들 것인가"를 먼저 결정한 뒤 클립을 찾아라.

⚠️ **필드 작성 순서 — title은 가장 마지막에 작성하라**:
각 storyline 객체와 최상위 객체 모두에서 `viral_titles` / `title_line1` / `title_line2` / `title_txt` 는
**스키마 말미에 위치**한다. 다른 모든 필드(특히 storyline·description·event_template 정보)를 먼저
작성한 뒤, 그 내용에 *직접 근거*해서 제목을 마지막에 작성하라. 제목을 먼저 결정하고 거기에
맞춰 내용을 끼워 넣지 마라 — 그러면 [제목 사실성 원칙]을 위반하기 쉽다.

{{
  "storylines": [
    {{
      "storyline_index": 0,
      "shorts_type": "storytelling",
      "sequence_type": "여정몰입형|결과선공개형|반전형|시퀀스블록형",
      "topic": "주제명",
      "topic_reason": "서사 구성 이유",
      "score": 0.0,
      "coherence_score": 0.0,
      "estimated_duration_sec": 0.0,
      "storyline": {{
        "hook": {{
          "chunk_index": 0, "candidate_index": 0,
          "start_sec": 0.0, "end_sec": 0.0,
          "description": "장면 설명",
          "use_original_audio": true,
          "character_focus": ["인물명"],
          "context_extended": false
        }},
        "hook_preview": null,
        "build": [
          {{
            "chunk_index": 0, "candidate_index": 0,
            "start_sec": 0.0, "end_sec": 0.0,
            "description": "장면 설명",
            "use_original_audio": true,
            "character_focus": ["인물명"],
            "context_extended": false
          }}
        ],
        "payoff": {{
          "chunk_index": 0, "candidate_index": 0,
          "start_sec": 0.0, "end_sec": 0.0,
          "description": "장면 설명",
          "use_original_audio": true,
          "character_focus": ["인물명"],
          "context_extended": false
        }},
        "sequence_block": []
      }},
      // ▼ TTS cue 는 storyline 구성 결정 후 작성. 0~5개. 한 storyline 안 모든 cue 는 같은 voice 사용 (절대 규칙).
      "tts_cues": [
        {{"clip_index": 0, "clip_role": "hook", "offset_sec": 0.5, "duration_sec": 4.0,
          "text": "예: 황궁마켓의 유일한 법.",
          "voice": "ko_male_low", "speed": "slow",
          "voice_rationale": "디스토피아·스릴러 톤", "speed_rationale": "긴장 고조 직전이라 천천히"}},
        {{"clip_index": 1, "clip_role": "build", "offset_sec": 8.0, "duration_sec": 3.5,
          "text": "근데 진짜 노림수는 따로 있는데?",
          "voice": "ko_male_low", "speed": "fast",
          "voice_rationale": "같은 storyline 이므로 voice 유지", "speed_rationale": "반전 임팩트라 빠르게"}}
      ],
      // ▼ 제목은 위 내용을 모두 작성한 뒤 마지막에 작성 (제목 사실성 원칙 준수)
      "viral_titles": ["제목1", "제목2", "제목3"],
      "title_line1": "상황/배경 설명 (13자 이내, 초과 금지)",
      "title_line2": "캐릭터/사건 중심 후킹 (13자 이내, 초과 금지)"
    }},
    {{
      "storyline_index": 1,
      "shorts_type": "storytelling",
      "sequence_type": "시퀀스블록형",
      "topic": "시퀀스블록형 예: 한 콩트 통째 사용",
      "topic_reason": "같은 sequence_id의 candidate가 자체 완결",
      "score": 0.0,
      "storyline": {{
        "hook": null,
        "hook_preview": null,
        "build": [],
        "payoff": null,
        "sequence_block": [
          {{"chunk_index": 1, "candidate_index": 0}},
          {{"chunk_index": 1, "candidate_index": 2}},
          {{"chunk_index": 1, "candidate_index": 3}}
        ]
      }},
      "tts_cues": [],
      // ▼ 제목은 마지막
      "title_line1": "상황 설명",
      "title_line2": "후킹 강조"
    }},
    {{
      "storyline_index": 2,
      "shorts_type": "highlight",
      "chunk_index": 0,
      "candidate_index": 0,
      "start_sec": 0.0,
      "end_sec": 0.0,
      "context_extended": false,
      "topic": "주제명",
      "topic_reason": "단독 선정 이유",
      "score": 0.0,
      "coherence_score": 0.0,
      "estimated_duration_sec": 0.0,
      "description": "장면 설명 (candidate.description 그대로 또는 더 상세하게 — 후처리에서 자막으로 사용됨)",
      "character_focus": ["인물명"],
      "use_original_audio": true,
      "tts_cues": [
        {{"clip_index": 0, "clip_role": "highlight", "offset_sec": 1.0, "duration_sec": 4.5,
          "text": "이 한 컷의 후킹.",
          "voice": "ko_female_high", "speed": "normal",
          "voice_rationale": "가벼운 바이럴 톤", "speed_rationale": "기본"}}
      ],
      // ▼ 제목은 위 내용(특히 description)을 작성한 뒤 마지막에 작성
      "viral_titles": ["제목1", "제목2", "제목3"],
      "title_line1": "상황/배경 설명 (13자 이내, 초과 금지)",
      "title_line2": "캐릭터/사건 중심 후킹 (13자 이내, 초과 금지)"
    }}
  ],
  "selected_storyline_index": 0,
  "shorts_type":"storytelling"|"highlight",
  "selection_reason": "이 스토리라인을 선택한 이유",
  "selected_storyline": {{ "선정된 인덱스의 객체를 그대로 복사해서 출력": "" }},
  "title_line1": "최종 제목 1줄 (맥락)",
  "title_line2": "최종 제목 2줄 (후킹)",
  "title_txt": "title_line1 + title_line2 합친 전체 제목"
}}
"""



RELATIONSHIP_EXTRACTION_PROMPT = """
# Role
너는 영상 편집 전문가다. 쇼츠 편집을 위해 후보 장면들 사이의 관계를 분석한다.

# Task
아래 후보 장면 목록을 읽고, 장면들 사이에 존재하는 관계(엣지)를 추출하라.

각 후보에는 `continues_from` 필드가 있다. 이 값은 Pro 모델이 청크별로 개별 분석할 때 기록한 것이다.
- **같은 chunk 내** `continues_from`은 대체로 정확하다.
- **다른 chunk를 가리키는** `continues_from`은 틀린 경우가 많다. 반드시 재검증하라.

# 관계 타입 정의

| type | 의미 |
|---|---|
| `continuous` | 원본 영상에서 1~5초 이내로 물리적으로 이어지는 장면 |
| `setup_payoff` | A의 맥락이 없으면 B의 의미가 반감되는 인과 관계 |
| `consequence` | A의 결과로 B가 발생 (시간 간격이 있어도 인과 명확) |
| `sequence` | 같은 서사 라인의 순차적 단계 (반드시 같이 쓸 필요는 없음) |
| `duplicate` | 같은 장면이나 개그를 중복으로 포함한 경우 — 하나만 선택 |
| `character_arc` | 같은 인물의 감정/상태 변화 흐름 (편집 순서 지켜야 함) |
| `contrast` | 두 장면의 감정 낙차가 바이럴 포인트를 만드는 대조 관계 |

# 출력 규칙

- 명확한 관계만 출력하라. 추측성 관계는 제외.
- `continuous`는 start_sec 차이가 5초 이내이고 narrative가 이어지는 경우만 표시.
- `required` 필드: 두 클립을 반드시 함께 사용해야 하면 true, 아니면 false.
- 같은 chunk 내 `continues_from`이 올바르다고 판단되면 별도 엣지 출력 불필요.
- cross-chunk `continues_from`이 **오류**라고 판단되면 note에 명시하라.

# 후보 목록
{candidates_str}

# 출력 JSON 형식 (다른 내용 없이 JSON만 출력)
{{
  "edges": [
    {{
      "from": {{"chunk_index": 0, "candidate_index": 0}},
      "to": {{"chunk_index": 0, "candidate_index": 1}},
      "type": "continuous|setup_payoff|consequence|sequence|duplicate|character_arc|contrast",
      "required": false,
      "note": "관계 설명 한 문장"
    }}
  ]
}}
"""


# ─────────────────────────────────────────────
# E15 스타일 구성 (2026-08-23) — 스토리 구성 뒤 편 단위 연출 플랜
# ─────────────────────────────────────────────
# 기획: ves-orchestrator docs/prompts/e15-style-compose.md.
# 계약·검증 정본은 app/modules/style_compose.py 다 — 이 프롬프트는 그 계약을 말로 옮긴
# 것이고, 어긋나면 검증기가 거절한다(프롬프트가 아니라 검증기가 계약이다).
# ⚠ 이름이 `_PROMPT` 로 끝나므로 provenance._prompt_versions() 가 자동으로 해시에 싣는다.
STYLE_COMPOSITION_PROMPT = """너는 한국어 쇼츠의 **연출 감독**이다. 편집(어떤 장면을 쓸지·자막 문구·
내레이션 문구)은 이미 끝났다. 너는 그 위에 **보이는 연출**만 얹는다.

[절대 규칙]
- 장면·구간·자막 문구·내레이션 문구를 **바꾸지 마라**. 네가 정하는 것은 '어떻게 보이는가' 뿐이다.
- 모든 시각 좌표는 **원본 영상의 절대초**(source_time_sec)다. 아래 타임라인 표에 적힌
  '원본' 값을 그대로 써라. 편집본 시각(0초 시작)을 쓰면 그 항목은 버려진다.
- 아래 [타임라인]에 없는 시각을 쓰면 그 항목은 버려진다. 반드시 표 안의 구간에서 골라라.
- **적을수록 좋다.** 한 편에 효과 텍스트는 2~5개면 충분하다. 매 장면에 넣지 마라 —
  다 넣으면 아무것도 강조되지 않는다. 넣을 이유가 없으면 그 배열을 비워라.

[연출 수단]
1) texts — 화면에 얹는 짧은 글자(의성어·의태어·감탄·강조). 대사 자막이 아니다.
   x,y 는 화면 비율(0~1, **글자 중심**), 캔버스는 세로 9:16 이다.
   ⚠ **y 는 반드시 {text_y_lo}~{text_y_hi} 안에 둬라.** 이 편의 영상이 실제로 그려지는
     띠가 그 구간이다. 그 위는 **제목 자리**, 그 아래는 대사 자막·작품명 자리라 글자가
     겹쳐 읽을 수 없게 된다(범위 밖 값은 엔진이 이 구간으로 당긴다 — 네 의도와 달라진다).
   fx: none|pop|shake (pop=톡 튀어나옴, shake=흔들림). size 는 48~160 이 보통이다.
2) subtitle_styles — **핵심 대사 한두 줄**만 크게/색으로 강조. 그 줄이 이 쇼츠의 승부처일 때만.
3) images — 스티커. 아래 [스티커] 목록의 id 만 쓸 수 있다. 목록이 비어 있으면 쓰지 마라.
4) title_segments — 시간대별 제목. 구간(from_anchor~to_anchor, 원본 절대초)마다 **제목 문구를
   바꾼다**. ⚠ **제목은 편 내내 반드시 떠 있어야 한다** — 창이 못 덮은 시간에는 엔진이 기본
   제목을 되돌려 넣는다(제목이 없는 시간은 만들 수 없다). 그러니 '중간에 제목을 없애는'
   용도로는 쓰지 마라. 창끼리 겹치면 안 된다. 문구를 안 바꿀 거면 배열을 비워라
   (그러면 기본 제목이 처음부터 끝까지 나온다).
5) tts — 이미 정해진 내레이션의 **목소리·속도만** 장면 톤에 맞게 바꾼다. 문구는 못 바꾼다.
6) design — 이 편 전체에 걸리는 것. 제목 배경 박스·굵게 정도만.
   ⚠ **제목은 기울이지 않는다.** `title_rotate` 는 쓰지 마라 — 보내도 엔진이 버린다.
     제목 기울기는 채널·편집실이 정하는 값이다. 내레이션 자막 기울기(tts_rotate)는 쓸 수 있다.

[출력 형식 — 이 JSON 만, 설명 금지]
{{
  "schema": "style_plan/v1",
  "texts": [
    {{"text": "쿵!", "source_time_sec": 743.2, "duration_sec": 1.2,
      "x": 0.7, "y": 0.25, "size": 96, "color": "#FFDD00",
      "stroke": "dark", "fx": "pop", "rotate": -8, "reason": "왜 여기인지 한 줄"}}
  ],
  "subtitle_styles": [
    {{"source_time_sec": 745.0, "style": {{"size": 78, "color": "#FF4444"}}, "reason": "한 줄"}}
  ],
  "images": [
    {{"sticker": "목록의 id", "source_time_sec": 748.0, "duration_sec": 1.5,
      "x": 0.55, "y": 0.30, "w": 0.2, "layer": 0, "rotate": 0, "reason": "한 줄"}}
  ],
  "title_segments": [
    {{"text": "제목\\n둘째 줄", "from_anchor": 743.0, "to_anchor": 756.0}}
  ],
  "tts": [
    {{"source_time_sec": 743.0, "voice": "ko_male_low", "speed": "slow", "reason": "한 줄"}}
  ],
  "design": {{"title_box": "round", "title_box_color": "#000000"}},
  "notes": "이 편의 연출 컨셉 한 줄"
}}

[값 규칙 — 어기면 그 항목이 버려지거나 전체가 거절된다]
- texts: y {text_y_lo}~{text_y_hi} · size 12~400 · color "#RRGGBB" · stroke dark|none|white · fx none|pop|shake ·
  rotate -180~180(시계방향 양수) · font 는 {fonts} 중 하나(생략하면 기본) · text 60자 이내
- subtitle_styles.style 은 **size 와 color 만** (위치·회전은 사람이 정한다). size {sub_lo}~{sub_hi}
- images: x,y 는 좌상단, w 는 가로 비율(0~1) · layer 0=자막 아래, 1=자막 위
- tts.voice: {voices}
- tts.speed: {speeds}
- design.title_rotate: **쓸 수 없다**(보내면 그 키만 버려진다)
- design.tts_rotate: -180~180
- design.title_box(2)/title_box_color(2)/title_bold(2): 박스는 none|round|rect
- 상한: 효과 텍스트 {max_texts}개 · 스티커 {max_images}개 · 자막 강조 {max_subs}개 · 제목 창 {max_titles}개

[작품] {work_title}
[제목] {title_text}

[타임라인 — 이 구간들만 쓸 수 있다]
{timeline_block}

[대사 (원본 절대초)]
{transcript_block}

[내레이션 cue (원본 절대초 — 목소리·속도만 바꿀 수 있다)]
{cues_block}

[스티커]
{stickers_block}
"""


# ─────────────────────────────────────────────
# 프롬프트 입력 블록 빌더 (analyze_chunk / compose_story / plan_tts_cues 공용)
# ─────────────────────────────────────────────
# use_case 별로 경고문/헤더가 달라 한 함수에서 분기. PR-1(라운드 25) 정리.
# 기존 3곳에 인라인으로 흩어져 있던 블록 빌딩 코드와 *완전히 동일한 문자열* 을 반환한다.


def _format_work_context(work_context: str | None, *, use_case: str = "analysis") -> str:
    """work_context 블록을 반환. use_case: analysis|story|tts."""
    if not work_context:
        return ""
    if use_case == "analysis":
        return (
            f"\n[작품 정보 — 인물 식별·스토리 이해 참고용]\n{work_context}\n"
            "⚠️ 위 정보는 인물명·관계·장르를 정확히 파악하기 위한 참고용입니다.\n"
            "장면 선택 기준은 리서치가 아니라 오직 현재 영상 내 감정 강도·반응·의외성으로 판단하세요.\n"
        )
    if use_case == "story":
        return (
            f"\n[작품 정보 — 인물 식별·장르 이해 참고용]\n{work_context}\n"
            "⚠️ 위 정보는 인물명·관계·장르를 정확히 파악하기 위한 참고용입니다.\n"
            "스토리라인 구성 기준은 리서치가 아니라 후보 장면들의 감정 강도·반응·의외성입니다.\n"
        )
    # use_case == "tts" — 경고문 없는 간소화 버전
    return f"\n[작품 정보]\n{work_context}\n"


def _format_episodes_context(prev_episodes_context: str | None, *, use_case: str = "analysis") -> str:
    """previous_episodes_context 블록을 반환. use_case: analysis|story|tts."""
    if not prev_episodes_context:
        return ""
    if use_case == "analysis":
        return (
            f"\n[이전 에피소드 배경 정보 — 오해 방지 전용]\n{prev_episodes_context}\n"
            "⚠️ 위 정보는 인물명·관계·사건을 올바르게 식별하기 위한 참고용입니다.\n"
            "장면 선택 기준은 오직 현재 첨부 영상 안에서의 재미·흥미도·화제성입니다.\n"
            "이전 화와의 연관성이 높다는 이유만으로 장면을 선택하거나 높게 평가하지 마세요.\n"
            "타임스탬프는 반드시 현재 첨부 영상의 시작(0초) 기준으로 계산하세요.\n"
        )
    if use_case == "story":
        return (
            f"\n[이전 에피소드 배경 정보 — 오해 방지 전용]\n{prev_episodes_context}\n"
            "⚠️ 위 정보는 인물명·관계·사건을 올바르게 파악하기 위한 참고용입니다.\n"
            "스토리라인 구성 기준은 이번 화 후보 장면들의 재미·흥미도·화제성입니다.\n"
            "이전 화와의 연관성이 높다는 이유만으로 장면을 선택하거나 높게 평가하지 마세요.\n"
        )
    return f"\n[이전 에피소드 요약]\n{prev_episodes_context}\n"


def _format_segments_summary(chunk_meta: list[dict] | None, *, use_case: str = "story") -> str:
    """chunk_meta(청크별 summary+segments) 를 시간순 텍스트 블록으로. use_case: story|tts."""
    if not chunk_meta:
        return ""
    parts: list[str] = []
    for cm in chunk_meta:
        ci = cm.get("chunk_index", 0)
        summary = (cm.get("summary") or "").strip().replace("\n", " ")
        segs = cm.get("segments") or []
        lines: list[str] = [f"\n[chunk {ci}] 요약: {summary[:120]}"]
        for s in segs:
            ss = float(s.get("start_sec", 0))
            ee = float(s.get("end_sec", 0))
            desc = (s.get("description") or "").strip().replace("\n", " ")
            if len(desc) > 100:
                desc = desc[:100] + "…"
            lines.append(f"  - {ss:>6.1f}~{ee:>6.1f}s  {desc}")
        parts.append("\n".join(lines))
    if not parts:
        return ""
    joined = "\n".join(parts).replace("{", "{{").replace("}", "}}")
    if use_case == "story":
        return (
            "\n\n[전체 장면 흐름 (segments) — 청크별 시간순]\n"
            "(이 요약은 영상 전체 흐름을 빈틈없이 보여준다. candidate_moments는 그 중 가치 있는 일부만 추린 것.)\n"
            + joined
        )
    # use_case == "tts"
    return (
        "\n\n[현재 회차 전체 흐름 (segments) — 청크별 시간순]\n"
        "(시간은 원본 영상 절대 시간 — 위 [클립 시퀀스]의 '원본 X.X~Y.Y초'와 같은 축. "
        "선정된 클립은 이 전체 흐름의 일부다. 클립 사이의 행간을 메우는 cue를 작성할 때 "
        "이 정보로 *그 사이에 무엇이 있었는지* 파악하라.)\n"
        + joined
    )


# ─────────────────────────────────────────────
# PR-4: storyline.tts_cues 정규화 (STORY_COMPOSITION_PROMPT 새 스키마 출력 검증)
# ─────────────────────────────────────────────
# 기존 plan_tts_cues 후처리 로직과 동일 패턴을 함수로 분리.
# STORY_COMPOSITION_PROMPT 가 storyline 본체에 직접 tts_cues 를 출력하기 시작했으므로
# 그 응답도 동일한 검증·정규화를 거쳐야 한다. PR-5 에서 plan_tts_cues 제거 시 단일화.

_VALID_TTS_VOICES: frozenset[str] = frozenset({
    "ko_female", "ko_female_high", "ko_male", "ko_male_low",
    "chat_emma", "chat_brian", "chat_seraphina", "chat_florian",
})
_VALID_TTS_SPEEDS: frozenset[str] = frozenset({
    "very_slow", "slow", "normal", "fast", "very_fast",
})


def _anchor_clip_fields(clip) -> tuple[float, float, int, int]:
    """anchor_clips 원소(StoryClip 또는 dict)에서 (start, end, chunk_index, candidate_index)."""
    if isinstance(clip, dict):
        return (float(clip.get("start_sec", 0.0)), float(clip.get("end_sec", 0.0)),
                int(clip.get("chunk_index", -1)), int(clip.get("candidate_index", -1)))
    return (float(getattr(clip, "start_sec", 0.0)), float(getattr(clip, "end_sec", 0.0)),
            int(getattr(clip, "chunk_index", -1)), int(getattr(clip, "candidate_index", -1)))


def _normalize_storyline_tts_cues(
    raw_cues,
    *,
    anchor_clips=None,
    max_cues: int | None = 5,
) -> list[dict[str, Any]]:
    """LLM 응답의 tts_cues 배열(클립 앵커 스키마)을 검증·정규화.

    앵커 스키마: clip_index(int ≥0) / offset_sec(float ≥0) / duration_sec(float >0) / text 필수.
    anchor_clips (story 단계, beat trim *이전* 클립 리스트) 기준으로:
    - clip_index >= len(anchor_clips) 인 cue 는 드롭 (LLM 환각 방지)
    - offset_sec 을 [0, 클립 길이] 로 클램프
    - source_time_sec = 클립.start_sec + offset_sec (원본 영상 절대시간) 을 계산해 저장
      → 하류 _resolve_cue_anchors 가 최종 타임라인 확정 후 편집 절대시간으로 변환한다.
    - 클립의 (chunk_index, candidate_index) 를 함께 저장 (해석 시 스냅/동점 판별 키)

    하위호환: clip_index 없이 start_sec/end_sec(구 스키마, story 타임라인 절대시간)만 있으면
    anchor_clips 누적 길이로 역산해 앵커로 변환한다. 역산 불가(범위 밖·anchor_clips 없음)면 드롭.

    공통: voice/speed 라벨 검증(fallback ko_female/normal), majority voice 통일,
    (clip_index, offset_sec) 정렬, max_cues 절단. cue 간 겹침 제거는 해석 시점으로 이동
    (절대시간이 없어 여기서는 판단 불가).

    Returns: 정규화된 앵커 cue dict 리스트 (rationale 필드 보존)
    """
    if not raw_cues:
        return []
    anchors = [_anchor_clip_fields(c) for c in (anchor_clips or [])]
    # story 타임라인 누적 시작점 (구 스키마 역산용)
    cum_starts: list[float] = []
    _acc = 0.0
    for a_start, a_end, _, _ in anchors:
        cum_starts.append(_acc)
        _acc += max(0.0, a_end - a_start)
    _story_total = _acc

    cues: list[dict[str, Any]] = []
    dropped_hallucination = 0
    dropped_legacy = 0
    for c in raw_cues:
        if not isinstance(c, dict) or "text" not in c:
            continue

        clip_index: int | None = None
        offset: float | None = None
        duration: float | None = None

        if "clip_index" in c and "offset_sec" in c:
            try:
                clip_index = int(c["clip_index"])
                offset = float(c["offset_sec"])
                duration = float(c.get("duration_sec", 0.0))
            except (TypeError, ValueError):
                continue
        elif "start_sec" in c and "end_sec" in c:
            # 구 스키마 폴백 — story 타임라인 절대시간을 앵커로 역산
            try:
                s = float(c["start_sec"])
                e = float(c["end_sec"])
            except (TypeError, ValueError):
                continue
            if e <= s:
                continue
            if not anchors or s < -0.5 or s >= _story_total + 0.5:
                dropped_legacy += 1
                continue
            s = max(0.0, s)
            clip_index = 0
            for i in range(len(anchors) - 1, -1, -1):
                if s >= cum_starts[i]:
                    clip_index = i
                    break
            offset = s - cum_starts[clip_index]
            duration = e - s
        else:
            continue

        if clip_index is None or clip_index < 0 or clip_index >= len(anchors):
            dropped_hallucination += 1
            continue
        if offset is None or offset < 0.0:
            offset = 0.0
        if duration is None or duration <= 0.0:
            continue
        a_start, a_end, a_chunk, a_cand = anchors[clip_index]
        clip_dur = max(0.0, a_end - a_start)
        offset = min(offset, clip_dur)

        voice = str(c.get("voice", "ko_female"))
        if voice not in _VALID_TTS_VOICES:
            voice = "ko_female"
        speed = str(c.get("speed", "normal"))
        if speed not in _VALID_TTS_SPEEDS:
            speed = "normal"
        out: dict[str, Any] = {
            "clip_index": clip_index,
            "offset_sec": offset,
            "duration_sec": duration,
            "source_time_sec": a_start + offset,
            "chunk_index": a_chunk,
            "candidate_index": a_cand,
            "text": str(c["text"]).strip(),
            "voice": voice,
            "speed": speed,
        }
        if c.get("clip_role"):
            out["clip_role"] = str(c["clip_role"])
        # rationale 필드 보존 (디버깅·로그 용)
        if c.get("voice_rationale"):
            out["voice_rationale"] = str(c["voice_rationale"])
        if c.get("speed_rationale"):
            out["speed_rationale"] = str(c["speed_rationale"])
        cues.append(out)

    if dropped_hallucination:
        print(f"  [TTS cue] clip_index 범위 밖 cue {dropped_hallucination}개 드롭 (환각 방지)")
    if dropped_legacy:
        print(f"  [TTS cue] 구 스키마 cue {dropped_legacy}개 — 앵커 역산 실패, 드롭")

    # (clip_index, offset) 정렬
    cues.sort(key=lambda x: (x["clip_index"], x["offset_sec"]))

    # 한 쇼츠 = 한 voice 강제 (majority 통일)
    if cues:
        voice_count: dict[str, int] = {}
        for c in cues:
            voice_count[c["voice"]] = voice_count.get(c["voice"], 0) + 1
        if len(voice_count) > 1:
            majority = max(voice_count.items(), key=lambda kv: kv[1])[0]
            for c in cues:
                c["voice"] = majority

    # max_cues 클램프 (마지막 단계 — 앞쪽 N 개 유지)
    if max_cues is not None and len(cues) > max_cues:
        cues = cues[:max_cues]

    return cues


@dataclass(frozen=True)
class GeminiConfig:
    api_key: str
    # ⚠ model_name 은 **영상 분석 전용 슬롯**이다(모델 정책 2026-08-23): Pro 를 쓰는 호출은
    # analyze_chunk 하나뿐이고, 나머지 텍스트-온리 호출은 전부 flash_model_name 을 쓴다.
    # 기본값은 팩토리(load_gemini_client)의 env 기본값과 같아야 한다 — 종전 기본값은
    # 금지 모델 'gemini-3.5-flash' 였다(팩토리가 늘 덮어써서 무해했지만, GeminiConfig 를
    # 직접 만드는 코드·테스트는 그 값을 먹었다).
    model_name: str = "gemini-3.1-pro-preview"
    flash_model_name: str = "gemini-3.6-flash"
    max_retries: int = 3
    # Google 공식 가이드(Gemini 3.x): temperature/top_p/top_k 같은 샘플링 매개변수는
    # 설정하지 말고 기본값을 따르도록 권장. 카테고리별 thinking_level만 제어한다.
    analysis_thinking_level: str = "high"         # "minimal" | "low" | "medium" | "high"
    relationship_thinking_level: str = "medium"     # "minimal" | "low" | "medium" | "high"
    story_thinking_level: str = "medium"            # "minimal" | "low" | "medium" | "high"
    tts_cues_thinking_level: str = "medium"         # "minimal" | "low" | "medium" | "high"
    shorten_thinking_level: str = "medium"          # "minimal" | "low" | "medium" | "high"
    research_thinking_level: str = "medium"         # "minimal" | "low" | "medium" | "high"
    style_thinking_level: str = "medium"            # "minimal" | "low" | "medium" | "high" (E15)



def _format_reject_note(reject_note, use_case: str = "analysis") -> str:
    """사람이 직전 결과를 반려한 사유 → 프롬프트 뒤에 붙일 '재작업 지시' 블록.

    빈 값이면 빈 문자열을 돌려준다 — 반려가 없던 실행은 프롬프트가 종전과 한 글자도 다르지 않다.
    (VES 검수함에서 '영상 분석'·'스토리 구성' 유형으로 반려하면 그 사유가 여기로 들어온다.)
    """
    note = (reject_note or "").strip()
    if not note:
        return ""
    what = {"analysis": "이번 분석에서",
            "style": "이번 연출 구성에서"}.get(use_case, "이번 스토리 구성에서")
    return (
        "\n\n[재작업 지시 — 사람이 직전 결과를 반려했다]\n"
        f"반려 사유: {note}\n"
        f"{what} 위 지적을 반드시 반영하라. 같은 실수를 되풀이한 결과는 다시 반려된다.\n"
    )

class GeminiClient:
    def __init__(self, config: GeminiConfig) -> None:
        self.config = config
        from google import genai
        from google.genai import types
        self.client = genai.Client(api_key=config.api_key)
        self.types = types

    # ─────────────────────────────────────────
    # 청크 분석
    # ─────────────────────────────────────────
    def analyze_chunk(self, payload: dict[str, Any]) -> dict[str, Any]:
        previous_episodes_context_block = _format_episodes_context(
            payload.get("previous_episodes_context"), use_case="analysis",
        )

        previous_context = ""
        if payload.get("previous_analyses"):
            prev_analyses = payload["previous_analyses"]
            context_parts = []
            for prev in prev_analyses:
                prev_chunk_idx = prev.get("chunk_index", "?")
                context_parts.append(f"\n[이전 청크 분석 결과 — chunk_index={prev_chunk_idx}]")
                if prev.get("summary"):
                    context_parts.append(f"요약: {prev['summary']}")
                if prev.get("candidate_moments"):
                    moments_text = "\n".join([
                        f"  - candidate_index={m.get('candidate_index', '?')}, "
                        f"{m.get('start_sec', 0)}~{m.get('end_sec', 0)}초: {m.get('description', '')}"
                        for m in prev["candidate_moments"][:3]
                    ])
                    context_parts.append(f"주요 모멘트:\n{moments_text}")
                if prev.get("segments"):
                    seg_lines = [
                        f"  - {s.get('start_sec', 0):.1f}~{s.get('end_sec', 0):.1f}초: {s.get('description', '')[:120]}"
                        for s in prev["segments"][:8]
                    ]
                    context_parts.append("전체 타임라인 묘사 (segments):\n" + "\n".join(seg_lines))
            if context_parts:
                previous_context = (
                    "\n\n이전 청크들의 분석 결과 (전체 흐름 이해용 — continues_from 참조 시 위 chunk_index/candidate_index 값을 그대로 사용할 것):"
                    + "\n".join(context_parts)
                )

        chunk_start = payload["chunk_start_sec"]
        chunk_end = payload["chunk_end_sec"]

        # 자막 텍스트 (origin/test: transcript_text)
        raw_segments = payload.get("transcript_segments") or []
        if raw_segments:
            filtered = [
                s for s in raw_segments
                if hasattr(s, "start_sec") and s.start_sec >= chunk_start and s.end_sec <= chunk_end
            ]
            transcript_text_str = "\n".join(
                f"[{s.start_sec:.1f}~{s.end_sec:.1f}] {s.text}" for s in filtered
            ) or "없음"
            transcript_text_str = transcript_text_str.replace("{", "{{").replace("}", "}}")
        else:
            transcript_text_str = "없음"

        # 작품 컨텍스트 (시놉시스/장르/핵심 요소)
        work_context_block = _format_work_context(payload.get("work_context"), use_case="analysis")

        # 청크 길이에 비례한 최소 후보 수 (1분당 1개, 올림, 최소 1개)
        chunk_duration = chunk_end - chunk_start
        min_candidates = max(1, -(-int(chunk_duration) // 60))

        # narrative_skeleton 블록 — 라운드 6a에서 단계 자체 제거. payload의 skeleton은 무시.

        # face_id 사전 인식 결과 블록 (선택적 — 인덱스가 없으면 빈 문자열)
        character_appearances_block = ""
        _appearances = payload.get("character_appearances") or []
        if _appearances:
            ap_lines = [
                f"- {a.get('character', '?')}: {float(a.get('start_sec', 0)):.1f}~{float(a.get('end_sec', 0)):.1f}초"
                for a in _appearances
            ]
            character_appearances_block = (
                "\n[face_id 사전 인식 결과 — 참고용]\n"
                "외부 얼굴 인식기가 추정한 캐릭터 등장 구간이다. 같은 인물명을 라벨로 일관되게 사용하되, "
                "픽셀에서 명백히 다르게 보이는 경우 영상 분석 결과를 우선한다.\n"
                + "\n".join(ap_lines)
            )

        prompt = GEMINI_PROMPT_TEMPLATE.format(
            work_title=payload["work_title"],
            topic=payload["topic"],
            chunk_index=payload.get("chunk_index", 0),
            chunk_start_sec=chunk_start,
            chunk_end_sec=chunk_end,
            transcript_text=transcript_text_str,
            scene_boundaries=payload.get("scene_boundaries") or "없음",
            previous_context=previous_context,
            previous_episodes_context_block=previous_episodes_context_block,
            work_context_block=work_context_block,
            character_appearances_block=character_appearances_block,
            min_candidates=min_candidates,
        )
        prompt += _format_reject_note(payload.get("reject_note"), use_case="analysis")
        # 작품별 편집 지침(권리사 가이드/운영 지시) — 청크 단계는 태깅·상세 기술만 시키고
        # 절대 좁히지 않는다(여기서 잘린 정보는 하류에서 복구 불가). editorial.py 가 정본.
        prompt += format_editorial_block(payload.get("editorial"), use_case="analysis")

        video_path = payload.get("video_path")
        content_parts = [prompt]
        uploaded_file = None

        if video_path:
            video_path_obj = Path(video_path) if isinstance(video_path, str) else video_path
            if video_path_obj.exists():
                safe_path, is_tmp = _safe_upload_path(video_path_obj)
                for upload_attempt in range(self.config.max_retries):
                    try:
                        uploaded_file = self.client.files.upload(file=str(safe_path))
                        while uploaded_file.state.name == "PROCESSING":
                            time.sleep(2)
                            uploaded_file = self.client.files.get(name=uploaded_file.name)
                        if uploaded_file.state.name == "FAILED":
                            raise RuntimeError("Gemini File API 업로드 실패")
                        content_parts.append(self.types.Part(
                            file_data=self.types.FileData(
                                file_uri=uploaded_file.uri,
                                mime_type="video/mp4",
                            ),
                        ))
                        break
                    except Exception as upload_err:
                        if upload_attempt == self.config.max_retries - 1:
                            raise RuntimeError(
                                f"비디오 업로드 {self.config.max_retries}회 모두 실패 — 분석을 중단합니다.\n"
                                f"원인: {upload_err}"
                            )
                        wait = 2 ** upload_attempt
                        print(f" [WARN] 업로드 오류 (시도 {upload_attempt + 1}/{self.config.max_retries}), {wait}초 후 재시도: {upload_err}")
                        time.sleep(wait)
                if is_tmp and safe_path.exists():
                    safe_path.unlink()

        try:
            for attempt in range(self.config.max_retries):
                try:
                    response = self.client.models.generate_content(
                        model=self.config.model_name,
                        contents=content_parts,
                        config=self.types.GenerateContentConfig(
                            response_mime_type="application/json",
                            # beats[] 추가로 분석 출력이 커져 기본 한도에서 JSON이 잘리는 문제 방지.
                            max_output_tokens=65536,
                            thinking_config=self.types.ThinkingConfig(
                                thinking_level=self.config.analysis_thinking_level,
                            ),
                        ),
                    )

                    if not response or not response.text:
                        error_msg = "Gemini API가 빈 응답을 반환했습니다."
                        if attempt == self.config.max_retries - 1:
                            raise RuntimeError(error_msg)
                        print(f"    [WARN] {error_msg} 재시도 중... ({attempt + 1}/{self.config.max_retries})")
                        continue

                    text = response.text.strip()
                    if not text:
                        error_msg = "Gemini API 응답이 빈 문자열입니다."
                        if attempt == self.config.max_retries - 1:
                            raise RuntimeError(error_msg)
                        print(f"    [WARN] {error_msg} 재시도 중... ({attempt + 1}/{self.config.max_retries})")
                        continue

                    # 잘린 응답을 파싱하면 원인이 JSONDecodeError 로 둔갑한다 — 먼저 걸러낸다.
                    truncated = _max_tokens_usage(response)
                    if truncated is not None:
                        error_msg = (
                            f"Gemini 응답이 출력 한도에서 잘렸습니다(finish_reason=MAX_TOKENS) — {truncated}. "
                            f"max_output_tokens=65536 · thinking_level={self.config.analysis_thinking_level} · "
                            f"받은 길이 {len(text)}자. thinking 토큰이 같은 예산을 쓰므로 "
                            f"thinking_level 을 낮추거나 출력량(segments·candidate_moments)을 줄여야 합니다."
                        )
                        if attempt == self.config.max_retries - 1:
                            raise RuntimeError(error_msg)
                        print(f"    [WARN] {error_msg} 재시도 중... ({attempt + 1}/{self.config.max_retries})")
                        continue

                    json_text = _extract_json_from_markdown(text)
                    if not json_text:
                        error_msg = f"Gemini 응답에서 JSON을 추출할 수 없습니다. (원문: {text[:200]!r})"
                        if attempt == self.config.max_retries - 1:
                            raise RuntimeError(error_msg)
                        print(f"    [WARN] {error_msg} 재시도 중... ({attempt + 1}/{self.config.max_retries})")
                        continue
                    try:
                        data, dropped_tail = _loads_first_json(json_text)
                    except json.JSONDecodeError:
                        # 원문을 남기고 종전대로 재시도한다(3회 소진 시 상위로 전파).
                        dump_path = _dump_gemini_response(text, response, payload, kind="failed")
                        print(
                            f"    [WARN] 응답 파싱 실패 — finish_reason={_finish_reason(response)}"
                            f"{f', 원문 보존: {dump_path}' if dump_path else ''}"
                        )
                        raise
                    if dropped_tail:
                        dump_path = _dump_gemini_response(text, response, payload, kind="salvaged")
                        print(
                            f"    [WARN] 응답 뒤에 {dropped_tail}자가 더 붙어 있어 버리고 첫 JSON만 사용 "
                            f"(finish_reason={_finish_reason(response)})"
                            f"{f', 원문 보존: {dump_path}' if dump_path else ''}"
                        )
                    # Gemini가 JSON 배열로 응답하는 경우 첫 번째 요소 사용
                    if isinstance(data, list):
                        data = data[0] if data else {}
                    data["chunk_start_sec"] = payload["chunk_start_sec"]
                    data["chunk_end_sec"] = payload["chunk_end_sec"]
                    _validate_gemini_schema(data)
                    return data

                except Exception as e:
                    if attempt == self.config.max_retries - 1:
                        raise e
                    wait = 2 ** attempt
                    print(
                        f"    [WARN] Gemini 응답 검증 실패 "
                        f"(시도 {attempt + 1}/{self.config.max_retries}), "
                        f"{wait}초 후 재시도: {type(e).__name__}: {e}"
                    )
                    time.sleep(wait)
                    continue

            raise RuntimeError("Gemini 분석 시도 횟수 초과")

        finally:
            if uploaded_file:
                try:
                    self.client.files.delete(name=uploaded_file.name)
                    print(f" [INFO] Gemini File API 서버 파일 삭제 완료: {uploaded_file.name}")
                except Exception as del_err:
                    print(f" [WARN] Gemini File API 서버 파일 삭제 실패: {del_err}")

    # ─────────────────────────────────────────
    # 영상 의도 사전 분석 (Flash 모델, 전체 프록시 1회 스캔)
    # ─────────────────────────────────────────
    def analyze_video_intent(
        self,
        proxy_path: Path,
        work_title: str,
        topic: str,
        work_context: str | None = None,
        previous_episodes_context: str | None = None,
    ) -> dict[str, Any]:
        """전체 프록시 영상을 1회 스캔해 narrative_skeleton을 생성합니다.

        Flash 모델로 빠르게 영상의 핵심 의도/감정 아크/핵심 인물/콘텐츠 구조를 파악하고,
        이후 청크 분석 및 스토리 구성 단계에 주입되어 청크가 '전체 그림 안에서 이 장면은 뭐야?'
        를 알 수 있게 합니다.
        """
        work_context_line = f"\n[작품 정보]\n{work_context}\n" if work_context else ""
        episodes_context_line = (
            f"\n[이전 에피소드 배경 정보 — 오해 방지 전용]\n{previous_episodes_context}\n"
            "⚠️ 위 정보는 인물명·관계·사건을 올바르게 식별하기 위한 참고용입니다.\n"
        ) if previous_episodes_context else ""
        prompt = (
            "너는 영상 분석 전문가다. 아래 첨부된 전체 영상을 빠르게 스캔해 "
            "narrative_skeleton JSON을 생성하라. 반드시 JSON만 출력, 코드블록 금지.\n\n"
            f"[입력]\n- 작품명: {work_title}\n- 주제: {topic}\n"
            f"{work_context_line}"
            f"{episodes_context_line}\n"
            "[공통 출력 필드 — 포맷 무관하게 반드시 포함]\n"
            "{\n"
            '  "intent": "이 영상이 시청자에게 전달하려는 핵심 의도 (1-2문장)",\n'
            '  "emotional_arc": [\n'
            '    {"phase": "오프닝", "emotion": "호기심", "time_range": "0-120"}\n'
            "  ],\n"
            '  "key_characters": [\n'
            '    {\n'
            '      "name": "인물A (자막/대사에서 들린 실제 이름 우선, 없으면 얼굴 기반 묘사)",\n'
            '      "role": "주인공",\n'
            '      "arc": "불신 → 신뢰",\n'
            '      "name_sources": ["자막에서 직접 언급", "다른 인물이 부름", "추정"]\n'
            '    }\n'
            "  ],\n"
            "⚠️ key_characters 수집 원칙:\n"
            "  -'work_context'에 없더라도 자막/대사에서 불린 이름이 있으면 반드시 그 이름을 사용\n"
            "  - 한 번이라도 이름이 언급된 인물은 모두 포함 (주연뿐 아니라 조연/게스트 포함)\n"
            "  - 이름을 전혀 알 수 없는 경우에만 '30대 여성 주인공' 형태 사용\n"
            "  - name_sources: 이름을 어떻게 알았는지 명시 (이름 신뢰도 판단용)\n"
            '  "tone_keywords": ["묵직함", "반전 서스펜스"],\n'
            '  "story_beats": [\n'
            '    {"time_range": "0-180", "beat": "무슨 일이 벌어지는지 객관적으로 1문장"}\n'
            "  ],\n"
            '  "has_intro": false,\n'
            '  "intro_end_sec": 0,\n'
            '  "intro_description": "오프닝 타이틀/크레딧/로고 시퀀스가 있으면 true, 끝나는 시간(초). 없으면 false, 0",\n'
            '  "has_credits": false,\n'
            '  "credits_start_sec": 0,\n'
            '  "credits_description": "엔딩 크레딧/스태프롤/예고편이 시작되면 true, 시작 시간(초). 없으면 false, 0"\n'
            "}\n\n"
            "⚠️ 영상에 실제로 존재하는 정보만 담아라. 추측 금지.\n"
            "⚠️ story_beats: 영상 전체를 시간 순서대로 5~10개 구간으로 나눠 각 구간에서 실제로 벌어지는 일을 1문장으로 기술하라. "
            "감정 평가나 해석 없이 객관적 사실만 묘사한다 (예: 'A가 B에게 편지를 건네고 B는 읽지 않고 돌아선다'). "
            "행동의 범주를 바꾸지 마라 — 질문은 질문으로, 대화는 대화로, 침묵은 침묵으로 기술하고, 확인되지 않은 의도·결과를 덧씌우지 마라.\n"
            "⚠️ 인트로/크레딧 감지: 실제 타이틀 시퀀스, 스태프롤, 출연진 자막 등이 보이는 경우에만 true로 표기.\n"
        )

        uploaded_file = None
        content_parts: list = [prompt]
        proxy_path_obj = Path(proxy_path)
        if proxy_path_obj.exists():
            safe_path, is_tmp = _safe_upload_path(proxy_path_obj)
            for upload_attempt in range(self.config.max_retries):
                try:
                    uploaded_file = self.client.files.upload(file=str(safe_path))
                    while uploaded_file.state.name == "PROCESSING":
                        time.sleep(2)
                        uploaded_file = self.client.files.get(name=uploaded_file.name)
                    if uploaded_file.state.name == "FAILED":
                        raise RuntimeError("Gemini File API 업로드 실패")
                    content_parts.append(self.types.Part(
                        file_data=self.types.FileData(
                            file_uri=uploaded_file.uri,
                            mime_type="video/mp4",
                        ),
                    ))
                    break
                except Exception as upload_err:
                    if upload_attempt == self.config.max_retries - 1:
                        raise RuntimeError(
                            f"프록시 업로드 {self.config.max_retries}회 모두 실패: {upload_err}"
                        )
                    time.sleep(2 ** upload_attempt)
            if is_tmp and safe_path.exists():
                safe_path.unlink()

        try:
            for attempt in range(self.config.max_retries):
                try:
                    response = self.client.models.generate_content(
                        model=self.config.flash_model_name,
                        contents=content_parts,
                        config=self.types.GenerateContentConfig(
                            response_mime_type="application/json",
                            thinking_config=self.types.ThinkingConfig(
                                thinking_level=self.config.analysis_thinking_level,
                            ),
                        ),
                    )
                    if not response or not response.text:
                        if attempt == self.config.max_retries - 1:
                            raise RuntimeError("Gemini Flash가 빈 응답을 반환했습니다.")
                        continue
                    text = response.text.strip()
                    if not text:
                        continue
                    json_text = _extract_json_from_markdown(text)
                    data = json.loads(json_text)
                    if isinstance(data, list):
                        data = data[0] if data else {}
                    return data
                except Exception as e:
                    if attempt == self.config.max_retries - 1:
                        print(f"    [WARN] analyze_video_intent 실패: {e} — 빈 skeleton 반환")
                        return {}
                    time.sleep(2 ** attempt)
            return {}
        finally:
            if uploaded_file:
                try:
                    self.client.files.delete(name=uploaded_file.name)
                except Exception:
                    pass

    # ─────────────────────────────────────────
    # 후보 장면 관계 그래프 추출
    # ─────────────────────────────────────────
    def extract_relationships(self, all_candidates: list) -> list[dict]:
        """후보 장면들 사이의 관계 엣지를 Flash 모델로 추출한다 (텍스트 전용, 영상 업로드 없음).

        ⚠ 2026-08-23 모델 정책(사용자 결정): **Pro 는 영상을 실제로 보는 호출
        (analyze_chunk) 하나뿐**이고 나머지 텍스트-온리 호출은 전부 Flash 최신이다.
        이 호출은 영상을 올리지 않고 analyze_chunk 가 이미 뽑아 둔 후보 설명·전사만
        읽으므로 그 정책의 대상이다(전환 전에는 Pro 였다).
        """
        slim_fields = (
            "chunk_index", "candidate_index", "start_sec", "end_sec",
            "description", "characters_in_scene",
            "requires_context", "continues_from", "transcript",
        )
        candidates_str = ""
        for m in all_candidates:
            slim = {k: m[k] for k in slim_fields if k in m}
            candidates_str += f"- {json.dumps(slim, ensure_ascii=False)}\n"

        prompt = RELATIONSHIP_EXTRACTION_PROMPT.format(candidates_str=candidates_str)

        for attempt in range(self.config.max_retries):
            try:
                response = self.client.models.generate_content(
                    model=self.config.flash_model_name,   # 모델 정책 2026-08-23 (Pro 는 영상 분석만)
                    contents=[prompt],
                    config=self.types.GenerateContentConfig(
                        response_mime_type="application/json",
                        thinking_config=self.types.ThinkingConfig(
                            thinking_level=self.config.relationship_thinking_level,
                        ),
                    ),
                )
                text = (response.text or "").strip()
                if not text:
                    raise RuntimeError("빈 응답")
                data = json.loads(_extract_json_from_markdown(text))
                edges = data.get("edges", [])
                if not isinstance(edges, list):
                    raise ValueError("edges 필드가 리스트가 아님")
                return edges
            except Exception as e:
                if attempt == self.config.max_retries - 1:
                    print(f"    [WARN] 관계 그래프 추출 실패: {e} — 빈 엣지 반환")
                    return []
                time.sleep(2 ** attempt)
        return []

    # ─────────────────────────────────────────
    # 스토리 구성 v2 (바이럴 최적화)
    # ─────────────────────────────────────────
    def compose_story_with_context(
        self,
        all_candidates: list,
        work_title: str,
        topic: str,
        min_duration_sec: float = 50.0,
        max_duration_sec: float = 60.0,
        work_context: str | None = None,
        previous_episodes_context: str | None = None,
        reject_note: str | None = None,
        editorial: dict | None = None,
        relationship_edges: list[dict] | None = None,
        chunk_meta: list[dict] | None = None,
    ) -> dict[str, Any]:
        """후보 장면들로 바이럴 최적화 스토리라인을 구성합니다.

        하이라이트형 3개 + 서사형 3개를 생성하고 최적 1개를 선정합니다.
        JSON 강제 응답 + 구조 검증 + 3회 재시도 + 폴백을 적용합니다.
        """
        candidates_str = ""
        for m in all_candidates:
            candidates_str += f"- {json.dumps(m, ensure_ascii=False)}\n"

        work_context_block = _format_work_context(work_context, use_case="story")
        episodes_context_block = _format_episodes_context(previous_episodes_context, use_case="story")
        story_topic_line = f"[핵심 주제] {topic}" if topic else ""
        segments_summary_block = _format_segments_summary(chunk_meta, use_case="story")

        prompt = STORY_COMPOSITION_PROMPT.format(
            work_title=work_title,
            topic=topic,
            min_duration_sec=min_duration_sec,
            max_duration_sec=max_duration_sec,
            candidates_str=candidates_str,
            work_context_block=work_context_block,
            episodes_context_block=episodes_context_block,
            segments_summary_block=segments_summary_block,
            story_topic_line=story_topic_line,
        )

        # ── 관계 그래프 블록 (프롬프트 뒤에 추가) ──
        if relationship_edges:
            type_rules = {
                "setup_payoff": "required=true이면 두 클립을 반드시 함께 사용하거나 둘 다 제외",
                "continuous": "sequence_id에 이미 반영됨. 인접 배치 권장",
                "duplicate": "둘 중 하나만 선택. 동일 스토리라인에 중복 사용 금지",
                "character_arc": "from → to 순서 유지 필수",
                "sequence": "같은 서사 라인. 같이 쓰면 효과적이나 필수는 아님",
                "consequence": "from이 to의 감정적 원인. 인접하지 않아도 to 앞에 from이 선행돼야 함",
                "contrast": "인접 배치 시 감정 낙차 극대화",
            }
            lines = ["\n[장면 관계 그래프 — 반드시 준수]"]
            lines.append("아래 관계를 스토리라인 구성 시 반드시 참고하라.\n")
            for edge in relationship_edges:
                f = edge.get("from", {})
                t = edge.get("to", {})
                etype = edge.get("type", "")
                req = edge.get("required", False)
                note = edge.get("note", "")
                rule = type_rules.get(etype, "")
                req_str = " [REQUIRED]" if req else ""
                lines.append(
                    f"- [{f.get('chunk_index')},{f.get('candidate_index')}]"
                    f" → [{t.get('chunk_index')},{t.get('candidate_index')}]"
                    f" | {etype}{req_str} | {note}"
                )
                if rule:
                    lines.append(f"  ↳ 규칙: {rule}")
            prompt += "\n".join(lines)

        prompt += _format_reject_note(reject_note, use_case="story")
        # 작품별 편집 지침 — 이 프롬프트가 선정·제목·tts_cues 를 한 번에 내므로
        # 하드 필터(장면+문구)·랭킹 편향·문체가 전부 여기 걸린다. editorial.py 가 정본.
        prompt += format_editorial_block(editorial, use_case="story")

        for attempt in range(self.config.max_retries):
            try:
                response = self.client.models.generate_content(
                    model=self.config.flash_model_name,
                    contents=[prompt],
                    config=self.types.GenerateContentConfig(
                        response_mime_type="application/json",
                        thinking_config=self.types.ThinkingConfig(
                            thinking_level=self.config.story_thinking_level,
                        ),
                    ),
                )

                if not response or not response.text:
                    if attempt == self.config.max_retries - 1:
                        raise RuntimeError("Gemini API가 빈 응답을 반환했습니다.")
                    print(f"    [WARN] 빈 응답, 재시도 중... ({attempt + 1}/{self.config.max_retries})")
                    continue

                text = response.text.strip()
                if not text:
                    if attempt == self.config.max_retries - 1:
                        raise RuntimeError("Gemini API 응답이 빈 문자열입니다.")
                    continue

                json_text = _extract_json_from_markdown(text)
                result = json.loads(json_text)

                # 구조 검증
                _validate_story_response(result)

                # selected_storyline 자동 채우기
                selected_idx = result.get("selected_storyline_index", 0)
                storylines = result.get("storylines", [])
                if not isinstance(selected_idx, int) or selected_idx < 0 or selected_idx >= len(storylines):
                    # 최고 score로 자동 선택
                    best_idx = max(range(len(storylines)), key=lambda i: storylines[i].get("score", 0))
                    result["selected_storyline_index"] = best_idx
                    selected_idx = best_idx

                # 복합 점수 = viral*0.6 + coherence*0.4
                for _sl in storylines:
                    _v = float(_sl.get("score", 0) or 0)
                    _c = float(_sl.get("coherence_score", 0) or 0)
                    _sl["composite_score"] = _v * 0.6 + _c * 0.4

                result["selected_storyline"] = storylines[selected_idx]
                # 전체 storylines를 복합 점수 순으로 정렬 (멀티쇼츠용)
                result["ranked_storylines"] = sorted(
                    storylines, key=lambda s: s.get("composite_score", s.get("score", 0)), reverse=True
                )
                return result

            except json.JSONDecodeError as json_err:
                if attempt == self.config.max_retries - 1:
                    print(f"    [ERROR] JSON 파싱 실패: {json_err}")
                    break
                print(f"    [WARN] JSON 파싱 실패, 재시도... ({attempt + 1}/{self.config.max_retries})")
                time.sleep(2 ** attempt)
            except ValueError as val_err:
                if attempt == self.config.max_retries - 1:
                    print(f"    [ERROR] 응답 검증 실패: {val_err}")
                    break
                print(f"    [WARN] 응답 검증 실패, 재시도... ({attempt + 1}/{self.config.max_retries})")
                time.sleep(2 ** attempt)
            except Exception as e:
                if attempt == self.config.max_retries - 1:
                    print(f"    [ERROR] 스토리 구성 실패: {e}")
                    break
                time.sleep(2 ** attempt)

        # 폴백: 최고 점수 moment를 highlight 클립으로 사용
        print("    [FALLBACK] Gemini 스토리 구성 실패 — 최고 점수 moment로 하이라이트 클립 생성")
        return _build_fallback_story(all_candidates, work_title)

    # ─────────────────────────────────────────
    # 텍스트 단축 (TTS fit 용도, Flash 모델)
    # ─────────────────────────────────────────
    def shorten_text(self, text: str, *, target_chars: int) -> str:
        """target_chars 이내로 의미를 보존하며 한국어 문장 단축. Flash 모델 사용.

        TTS 합성 결과가 cue 시간 초과 시 호출되어 텍스트를 줄인다.
        실패 시 입력 그대로 반환 (호출부가 단순 절단으로 폴백).
        """
        if not text or target_chars <= 0:
            return text
        if len(text) <= target_chars:
            return text
        prompt = (
            f"다음 한국어 쇼츠 나레이션 문장을 의미·뉘앙스를 보존하며 {target_chars}자 이내로 줄여라. "
            "결과 텍스트만 출력. 따옴표·설명·접두사 금지.\n\n"
            f"{text}"
        )
        for attempt in range(2):
            try:
                response = self.client.models.generate_content(
                    model=self.config.flash_model_name,
                    contents=[prompt],
                    config=self.types.GenerateContentConfig(
                        thinking_config=self.types.ThinkingConfig(
                            thinking_level=self.config.shorten_thinking_level,
                        ),
                    ),
                )
                if response and response.text:
                    out = response.text.strip().strip('"').strip("'")
                    # 첫 줄만 사용 (Flash가 가끔 설명 추가)
                    if "\n" in out:
                        out = out.split("\n", 1)[0].strip()
                    if out and len(out) <= len(text):
                        return out
            except Exception as e:
                if attempt == 1:
                    print(f"    [WARN] shorten_text 실패: {e}")
                time.sleep(1)
        return text

    def choose_beat_drops(self, payload: dict[str, Any]) -> dict[str, Any]:
        """60초 초과 스토리라인에서 제거할 beat를 Flash가 선택. (내용 기반 trim용)

        payload: {target_max_sec, current_total_sec, must_remove_sec,
                  clips:[{clip_idx, role, duration, beats:[{beat_idx, dur, summary, mood, importance, carries_payoff}]}]}
        반환: {"drops": [{"clip_idx": int, "beat_idx": int}], "reason": str}
        실패 시 {"drops": []} (호출부가 결정적 폴백으로 처리).
        """
        prompt = (
            "너는 유튜브 쇼츠 편집자다. 아래 스토리라인은 길이가 target_max_sec를 초과한다.\n"
            "must_remove_sec 만큼 줄이도록 *덜 중요한 beat*를 골라 제거 목록을 만들어라.\n\n"
            "[규칙]\n"
            "- 제거된 beat duration 합이 must_remove_sec에 근접하도록(약간 초과 허용).\n"
            "- 스토리 흐름(hook→build→payoff)과 감정 연결이 끊기지 않게 하라.\n"
            "- carries_payoff=true 인 beat는 절대 제거 금지.\n"
            "- 각 clip의 첫 beat(도입)·마지막 beat(마무리)는 가급적 보존. hook clip 첫 beat, payoff clip 마지막 beat는 제거 금지.\n"
            "- importance가 droppable > supporting 순으로 우선 제거. core는 제거하지 마라.\n"
            "- 한 clip의 beat를 전부 제거하지 마라(최소 1개 생존).\n"
            "- drops는 *우선순위 순서*(먼저 제거할 것부터)로 나열하라.\n\n"
            "[입력]\n"
            f"{json.dumps(payload, ensure_ascii=False)}\n\n"
            '다음 JSON으로만 응답: {"drops": [{"clip_idx": 0, "beat_idx": 2}], "reason": "한 줄 사유"}'
        )
        for attempt in range(2):
            try:
                response = self.client.models.generate_content(
                    model=self.config.flash_model_name,
                    contents=[prompt],
                    config=self.types.GenerateContentConfig(
                        response_mime_type="application/json",
                        thinking_config=self.types.ThinkingConfig(
                            thinking_level=self.config.shorten_thinking_level,
                        ),
                    ),
                )
                if response and response.text:
                    result = json.loads(_extract_json_from_markdown(response.text.strip()))
                    if isinstance(result, dict) and isinstance(result.get("drops"), list):
                        return result
            except Exception as e:
                if attempt == 1:
                    print(f"    [WARN] choose_beat_drops 실패: {e}")
                time.sleep(1)
        return {"drops": []}

    def compose_style(
        self,
        *,
        work_title: str,
        title_text: str,
        timeline: list[dict[str, Any]],
        transcript_lines: list[dict[str, Any]],
        tts_cues: list[dict[str, Any]],
        sticker_catalog: str = "",
        text_y_range: tuple[float, float] = (0.35, 0.66),
        editorial: dict[str, Any] | None = None,
        reject_note: str | None = None,
    ) -> dict[str, Any] | None:
        """E15 — 편 단위 연출 플랜(style_plan/v1)을 Flash 로 구성한다.

        입력은 전부 **원본 절대초** 좌표다(timeline 이 원본↔편집 대응표를 싣는다) —
        플랜의 좌표계와 같아야 LLM 이 표를 보고 그대로 베낄 수 있다.

        `text_y_range` 는 **이 편의 실제 밴드 기하**에서 계산한 효과 텍스트 y 허용 구간이다
        (`style_compose.text_y_range`). 기본값은 13:9·꽉 찬 폭·세로 중앙 기준의 근사치라
        호출부가 채널 design 으로 계산해 넘기는 것이 정본이다 — 프롬프트에 하드코딩된
        구간이 제목 자리를 찍어서 권하고 있었다(E18, 2026-08-24).

        반환: 파싱된 dict, 또는 None(끝내 못 받음). **여기서는 계약 검증을 하지 않는다** —
        정본은 style_compose.validate_plan 이고, 호출부가 거기서 거절되면 재시도한다.
        연출은 부가물이라 예외를 올리지 않는다(본편 발행을 막지 않는다 — 호출부가
        '스타일 없이 진행'을 stdout·run_log 에 남긴다).
        """
        from app.modules import style_compose as _sc
        from app.modules.edit_overrides import TEXT_FONTS

        def _fmt(rows: list[dict[str, Any]], keys: tuple[str, ...], limit: int) -> str:
            out = []
            for r in rows[:limit]:
                out.append(" · ".join(
                    f"{k}={r[k]}" for k in keys if r.get(k) not in (None, "")))
            return "\n".join(f"- {line}" for line in out) if out else "(없음)"

        prompt = STYLE_COMPOSITION_PROMPT.format(
            fonts="/".join(TEXT_FONTS),
            sub_lo=f"{_sc.SUBTITLE_SIZE_RANGE[0]:g}", sub_hi=f"{_sc.SUBTITLE_SIZE_RANGE[1]:g}",
            voices="/".join(_sc.STYLE_VOICES),
            speeds="/".join(_sc.STYLE_SPEEDS),
            text_y_lo=f"{float(text_y_range[0]):.2f}",
            text_y_hi=f"{float(text_y_range[1]):.2f}",
            max_texts=_sc.MAX_TEXTS, max_images=_sc.MAX_IMAGES,
            max_subs=_sc.MAX_SUBTITLE_STYLES, max_titles=_sc.MAX_TITLE_SEGMENTS,
            work_title=work_title, title_text=(title_text or "").replace("\n", " / "),
            timeline_block=_fmt(timeline,
                                ("role", "source_start", "source_end", "edit_start"), 40),
            transcript_block=_fmt(transcript_lines, ("source_sec", "text"), 120),
            cues_block=_fmt(tts_cues, ("source_time_sec", "voice", "speed", "text"), 30),
            stickers_block=sticker_catalog or "(번들된 스티커 없음 — images 는 비워라)",
        )
        prompt += _format_reject_note(reject_note, use_case="style")
        prompt += format_editorial_block(editorial, use_case="story")

        for attempt in range(2):
            try:
                response = self.client.models.generate_content(
                    model=self.config.flash_model_name,
                    contents=[prompt],
                    config=self.types.GenerateContentConfig(
                        response_mime_type="application/json",
                        thinking_config=self.types.ThinkingConfig(
                            thinking_level=self.config.style_thinking_level,
                        ),
                    ),
                )
                if response and response.text:
                    result = json.loads(_extract_json_from_markdown(response.text.strip()))
                    if isinstance(result, dict):
                        return result
            except Exception as e:
                print(f"    [WARN] 스타일 구성 호출 실패({attempt + 1}/2): {e}")
                time.sleep(1)
        return None


def _build_fallback_story(all_candidates: list, work_title: str) -> dict[str, Any]:
    """Gemini 스토리 구성 실패 시 상위 3-4개 moment를 조합하여 서사형 폴백 생성."""
    if not all_candidates:
        raise RuntimeError("후보 장면이 없어 폴백도 불가능합니다.")

    # 점수 필드가 더 이상 없으므로 시간순 안정 정렬을 폴백 기준으로 사용
    sorted_candidates = sorted(
        all_candidates,
        key=lambda m: m.get("start_sec", 0),
    )

    # 상위 4개 moment 선택 (최소 3개) — 시간순으로 균등하게 샘플링
    if len(sorted_candidates) <= 4:
        top_moments = list(sorted_candidates)
    else:
        n = len(sorted_candidates)
        idxs = [0, n // 3, (2 * n) // 3, n - 1]
        top_moments = [sorted_candidates[i] for i in idxs]

    # 시간순 정렬 (서사 흐름)
    top_moments.sort(key=lambda m: m.get("start_sec", 0))

    if len(top_moments) < 3:
        # moment가 3개 미만이면 있는 것만 사용
        pass

    # hook: 첫 번째, payoff: 마지막, build: 나머지
    hook_m = top_moments[0]
    payoff_m = top_moments[-1]
    build_moments = top_moments[1:-1] if len(top_moments) > 2 else []

    # 총 길이 계산
    total_dur = sum(m.get("end_sec", 0) - m.get("start_sec", 0) for m in top_moments)

    build_clips = [
        {
            "chunk_index": m.get("chunk_index", 0),
            "candidate_index": m.get("candidate_index", 0),
            "start_sec": m["start_sec"],
            "end_sec": m["end_sec"],
            "description": m.get("description", ""),
            "use_original_audio": True,
        }
        for m in build_moments
    ]

    storyline_obj = {
        "hook": {
            "chunk_index": hook_m.get("chunk_index", 0),
            "candidate_index": hook_m.get("candidate_index", 0),
            "start_sec": hook_m["start_sec"],
            "end_sec": hook_m["end_sec"],
            "description": hook_m.get("description", ""),
            "use_original_audio": True,
        },
        "build": build_clips,
        "payoff": {
            "chunk_index": payoff_m.get("chunk_index", 0),
            "candidate_index": payoff_m.get("candidate_index", 0),
            "start_sec": payoff_m["start_sec"],
            "end_sec": payoff_m["end_sec"],
            "description": payoff_m.get("description", ""),
            "use_original_audio": True,
        },
    }

    best = sorted_candidates[0]
    fallback_storyline = {
        "storyline_index": 0,
        "shorts_type": "storytelling",
        "sequence_type": "여정몰입형",
        "topic": best.get("description", work_title)[:30],
        "topic_reason": "Gemini 스토리 구성 실패로 상위 moment 자동 조합",
        "score": 0.5,
        "estimated_duration_sec": total_dur,
        "viral_titles": best.get("viral_titles", [work_title]),
        "title_line1": work_title[:20],
        "title_line2": best.get("description", "")[:20],
        "ending_hook": "",
        "storyline": storyline_obj,
    }

    return {
        "storylines": [fallback_storyline],
        "selected_storyline_index": 0,
        "selection_reason": "폴백: Gemini 응답 실패 — 상위 moment 자동 조합",
        "title_line1": work_title[:20],
        "title_line2": best.get("description", "")[:20],
        "title_txt": f"{work_title[:20]} {best.get('description', '')[:20]}",
        "ending_hook": "",
        "selected_storyline": fallback_storyline,
    }


def _validate_story_response(data: dict[str, Any]) -> None:
    """스토리 구성 응답의 필수 구조를 검증합니다.

    라운드 9: LLM이 'storylines' 외 다른 키로 응답하는 케이스를 자동 정규화
    하여 폴백 빈도를 낮춘다.
    """
    # 라운드 9-A: alias 매핑 (LLM이 다른 키 이름으로 출력해도 정상 처리).
    if "storylines" not in data or not isinstance(data.get("storylines"), list):
        for alias in (
            "shorts", "proposals", "storyline_options", "options",
            "story_proposals", "stories", "story_list", "results", "output"
        ):
            if alias in data and isinstance(data[alias], list) and data[alias]:
                data["storylines"] = data[alias]
                break

    # 라운드 9-B: 단일 selected_storyline만 있는 경우 storylines로 wrap.
    if "storylines" not in data or not isinstance(data.get("storylines"), list):
        sel = data.get("selected_storyline")
        if isinstance(sel, dict) and sel:
            data["storylines"] = [sel]

    # 라운드 9-C: 응답 자체가 단일 storyline dict (배열 누락) 인 경우 wrap.
    # `shorts_type` 키 존재로 단일 storyline 형태 추정.
    if "storylines" not in data or not isinstance(data.get("storylines"), list):
        if "shorts_type" in data and ("storyline" in data or "start_sec" in data):
            data["storylines"] = [dict(data)]

    if "storylines" not in data or not isinstance(data["storylines"], list):
        raise ValueError("응답에 'storylines' 배열이 없습니다.")
    if len(data["storylines"]) == 0:
        raise ValueError("생성된 스토리라인이 없습니다.")

    # title_txt 또는 title_line1+title_line2 필수
    if "title_txt" not in data and "title_line1" not in data:
        raise ValueError("응답에 'title_txt' 또는 'title_line1'이 없습니다.")

    # title_line1/line2 기본값 설정 (하위 호환) — 라운드 22: 15→20자
    if "title_line1" not in data:
        title = data.get("title_txt", "")
        data["title_line1"] = title[:20] if len(title) > 20 else title
        data["title_line2"] = title[20:40] if len(title) > 20 else ""
    if "title_line2" not in data:
        data["title_line2"] = ""
    if "title_txt" not in data:
        data["title_txt"] = f"{data['title_line1']} {data['title_line2']}".strip()

    # ending_hook 기본값
    data.setdefault("ending_hook", "")

    # 라운드 12: sequence_type 미지의 값이면 "여정몰입형"으로 fallback (LLM 출력 안정성)
    _ALLOWED_SEQUENCE_TYPES = {"여정몰입형", "결과선공개형", "반전형", "시퀀스블록형"}

    for idx, sl in enumerate(data["storylines"]):
        if "shorts_type" not in sl:
            raise ValueError(f"storyline[{idx}]에 'shorts_type'이 없습니다.")
        if "score" not in sl:
            raise ValueError(f"storyline[{idx}]에 'score'가 없습니다.")

        # 라운드 12: sequence_type 검증·정규화
        if sl.get("shorts_type") == "storytelling":
            seq_type = sl.get("sequence_type")
            if seq_type not in _ALLOWED_SEQUENCE_TYPES:
                sl["sequence_type"] = "여정몰입형"

        # storyline별 title_line1/line2 기본값
        sl.setdefault("title_line1", data.get("title_line1", ""))
        sl.setdefault("title_line2", data.get("title_line2", ""))
        sl.setdefault("ending_hook", "")

        if sl["shorts_type"] == "highlight":
            for key in ("start_sec", "end_sec"):
                if key not in sl:
                    raise ValueError(f"highlight storyline[{idx}]에 '{key}'가 없습니다.")
        elif sl["shorts_type"] == "storytelling":
            storyline = sl.get("storyline")
            if not storyline:
                raise ValueError(f"storytelling storyline[{idx}]에 'storyline' 객체가 없습니다.")
            for role in ("hook", "payoff"):
                if role not in storyline:
                    raise ValueError(f"storyline[{idx}].storyline에 '{role}'이 없습니다.")
                for key in ("start_sec", "end_sec"):
                    if key not in storyline[role]:
                        raise ValueError(f"storyline[{idx}].{role}에 '{key}'가 없습니다.")


def load_gemini_client() -> GeminiClient:
    project_root = Path(__file__).resolve().parent.parent.parent
    env_path = project_root / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        load_dotenv()

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY environment variable is required. "
            "Please set it in .env file or as an environment variable."
        )
    model_name = os.getenv("GEMINI_MODEL_NAME", "gemini-3.1-pro-preview")
    flash_model_name = os.getenv("GEMINI_FLASH_MODEL_NAME", "gemini-3.6-flash")
    max_retries = int(os.getenv("GEMINI_MAX_RETRIES", "3"))
    analysis_thinking_level = os.getenv("GEMINI_ANALYSIS_THINKING_LEVEL", "medium")
    relationship_thinking_level = os.getenv("GEMINI_RELATIONSHIP_THINKING_LEVEL", "medium")
    story_thinking_level = os.getenv("GEMINI_STORY_THINKING_LEVEL", "medium")
    tts_cues_thinking_level = os.getenv("GEMINI_TTS_CUES_THINKING_LEVEL", "medium")
    shorten_thinking_level = os.getenv("GEMINI_SHORTEN_THINKING_LEVEL", "medium")
    research_thinking_level = os.getenv("GEMINI_RESEARCH_THINKING_LEVEL", "medium")
    style_thinking_level = os.getenv("GEMINI_STYLE_THINKING_LEVEL", "medium")
    return GeminiClient(GeminiConfig(
        api_key=api_key,
        model_name=model_name,
        flash_model_name=flash_model_name,
        max_retries=max_retries,
        analysis_thinking_level=analysis_thinking_level,
        relationship_thinking_level=relationship_thinking_level,
        story_thinking_level=story_thinking_level,
        tts_cues_thinking_level=tts_cues_thinking_level,
        shorten_thinking_level=shorten_thinking_level,
        research_thinking_level=research_thinking_level,
        style_thinking_level=style_thinking_level,
    ))


def _validate_gemini_schema(data: dict[str, Any]) -> None:
    """Gemini 응답의 최소 스키마를 검증한다.

    - 응답 레벨 필수 키: chunk_index, chunk_start_sec, chunk_end_sec, summary, candidate_moments
    - 모먼트 레벨 필수 키: start_sec, end_sec, description (clip 생성에 직접 필요)
    - 모먼트 레벨 옵셔널 키: reason, transcript (LLM self-explanation/참고용 — 누락 시 ""로 채움)
    - 필수 키가 빠진 모먼트는 drop. 단 candidate_moments 전체가 비면 ValueError 발생 (재시도 트리거).
    """
    required_keys = {
        "chunk_index",
        "chunk_start_sec",
        "chunk_end_sec",
        "summary",
        "candidate_moments",
    }
    missing = required_keys - data.keys()
    if missing:
        raise ValueError(f"Missing keys in Gemini response: {missing}")
    if not isinstance(data["candidate_moments"], list):
        raise ValueError("candidate_moments must be a list")

    # 필수 키 / 옵셔널 키 분리
    moment_required = ("start_sec", "end_sec", "description")
    moment_optional_defaults = {"reason": "", "transcript": ""}

    # candidate_index 결손 보정용: 이미 사용된 정수 인덱스를 모아 두고, 빠진 곳은 next-unused 로 채운다.
    # Gemini 가 일부 moment 에 candidate_index 를 빼먹는 케이스가 있어(EP07 chunk3 등) 다운스트림
    # _dedup_boundary_candidates 의 int(None) 크래시를 막는다.
    _used_ci: set[int] = {
        int(m["candidate_index"]) for m in data["candidate_moments"]
        if isinstance(m, dict) and isinstance(m.get("candidate_index"), int)
    }
    _next_ci = (max(_used_ci) + 1) if _used_ci else 0

    cleaned: list = []
    dropped: list[str] = []
    for idx, moment in enumerate(data["candidate_moments"]):
        if not isinstance(moment, dict):
            dropped.append(f"#{idx}(not-a-dict)")
            continue
        missing_required = [k for k in moment_required if k not in moment]
        if missing_required:
            dropped.append(f"#{idx}(missing={','.join(missing_required)})")
            continue
        for k, default in moment_optional_defaults.items():
            moment.setdefault(k, default)
        moment.setdefault("beats", [])  # 매 호출 fresh list (공유 방지)
        # candidate_index 결손/None 보정: 충돌 없는 다음 정수로 채움
        if not isinstance(moment.get("candidate_index"), int):
            while _next_ci in _used_ci:
                _next_ci += 1
            moment["candidate_index"] = _next_ci
            _used_ci.add(_next_ci)
            _next_ci += 1
        cleaned.append(moment)

    if dropped:
        print(
            f"    [WARN] candidate_moments 중 {len(dropped)}개 결손으로 drop: "
            + ", ".join(dropped[:6])
            + (" …" if len(dropped) > 6 else "")
        )

    data["candidate_moments"] = cleaned
    if not cleaned:
        # 모든 모먼트가 결손이면 응답 자체가 무의미 → 재시도 트리거
        raise ValueError("candidate_moments가 모두 결손되어 유효한 모먼트가 없습니다.")
