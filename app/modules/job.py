"""job 디렉토리·run_log·지문 규약 — v1·v3·v4 공용 지향 (V4-M1 계약 §2).

v3 는 이 규약을 `app/v3/pipeline.py` 안에 인라인으로 들고 있다(job 디렉토리 :131 ·
run_log 신규/이어쓰기 :143~166 · step 클로저 :168 · finally 기록 :439 · `_write_json`
:58). M1 은 **v4 가 쓸 형태로 추출**한다 — v3 를 이 함수로 바꾸는 것은 M1 잔여다
(1773개 테스트가 걸린 표면이라 같은 커밋에서 갈아타지 않는다). 그래서 이 파일의
기본값·파일명·job_id 형식은 **v3 와 같은 모양의 run_log** 를 내도록 맞춰져 있고
(`tests/test_v4_job.py` 가 v3 인라인 구현과 대조해 고정한다), 딱 한 가지만
의도적으로 다르다:

🛑 **run_log 를 step 마다 즉시·원자적으로 쓴다.** v3 는 `finally` 한 곳에서만 썼다 —
   SIGKILL·OOM·노드 리부트로 프로세스가 죽으면 그 실행의 감사 기록이 통째로 사라진다
   (조사 gotcha 1). v4 는 호출별 usage·elapsed 를 남기고 그 양이 O7 승인 편수만큼
   늘어나는데, 죽을 때 다 날아가면 **비용을 되짚을 수 없다**(무인 노드 6대에서 어느
   편이 얼마를 태웠는지 사후에 물어볼 데가 없다). 그래서 기록은 누적이 아니라
   매 단계 디스크에 확정된다.

원자성은 "같은 디렉토리 임시 파일 → os.replace" 하나로 얻는다. 같은 디렉토리여야
rename 이 같은 파일시스템 안에서 원자적이고, 쓰는 중에 죽어도 독자는 **이전 판**을
온전히 본다(반쯤 쓰인 JSON 을 읽고 '깨진 run_log' 로 오진하는 경로가 없다).
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

# run_log 스키마 이름 — 자료 모양이 바뀌면 이 값을 올린다(읽는 쪽이 판별한다).
RUN_LOG_SCHEMA = "run_log/v1"
RUN_LOG_NAME = "run_log.json"

# v3·v1 이 쓰는 것과 **같은** 직렬화 형태다(indent=1 · ensure_ascii=False).
# 바꾸면 같은 내용의 run_log 가 바이트로 달라져 산출물 대조가 안 된다.
JSON_INDENT = 1

# sha1 앞 16자 — 지문은 캐시 무효화 판정용이라 충돌 확률보다 로그 가독성이 중요하다
# (v3 의 체크포인트 지문들과 같은 자릿수).
FINGERPRINT_LEN = 16

_JOB_ID_HEX = 8   # v3 규약: f"{제목}_{uuid4().hex[:8]}"


# ── 원자적 기록 ─────────────────────────────────────────────────────────────

def _atomic_write_text(path: Path, text: str) -> None:
    """같은 디렉토리 임시 파일 → fsync → os.replace.

    fsync 까지 하는 이유: os.replace 는 프로세스가 죽는 경우를 막아 주지만 머신이
    죽으면 내용이 비어 있는 새 파일로 갈리는 경우가 있다. run_log 는 킬로바이트급이라
    비용이 무의미하고, 이 기록의 존재 이유가 '죽을 때 남는 것'이다.
    임시 파일은 실패해도 남기지 않는다 — 남으면 다음 실행이 남의 조각을 보고 헷갈린다.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path: Path, doc: Any) -> None:
    """원자적 JSON 기록. 직렬화 불가한 값은 그대로 터뜨린다(조용한 str 강제 금지)."""
    _atomic_write_text(Path(path),
                       json.dumps(doc, ensure_ascii=False, indent=JSON_INDENT))


def read_json(path: Path) -> Any:
    """JSON 읽기. 파일이 없으면 FileNotFoundError · 깨졌으면 JSONDecodeError.

    ⚠ 조용한 기본값 금지 — 캐시가 없는 것과 캐시가 깨진 것은 다른 사건이고, 둘 다
    부르는 쪽이 알아야 한다(v3 `_read_json` 규약 승계).
    """
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ── job 디렉토리 ────────────────────────────────────────────────────────────

def job_dir_for(outdir: Path, work_title: str, job_id: str | None) -> Path:
    """job 디렉토리를 정한다(신규는 만들고, 재개는 존재를 확인한다).

    신규: f"{work_title 공백→_}_{uuid4().hex[:8]}" · `exist_ok=False`.
    ⚠ `exist_ok=False` 가 계약이다 — 같은 이름이 이미 있으면 크게 실패해야 한다.
    조용히 재사용하면 남의 체크포인트 위에 다른 소재의 산출물이 섞인다.

    job_id 를 주면 **이미 있는** 디렉토리여야 한다(없으면 FileNotFoundError).
    ⚠ '특정 job_id 로 신규 생성'은 v3 와 같이 불가다 — job_id 를 만드는 것은
    엔진이고 오케스트레이터는 그것을 되돌려 받아 쓴다는 규약이라, 임의 id 로 새
    디렉토리를 열어 주면 큐가 모르는 job 이 디스크에 생긴다.
    """
    outdir = Path(outdir)
    if job_id:
        d = outdir / job_id
        if not d.is_dir():
            raise FileNotFoundError(f"재개할 job 디렉토리 없음: {d}")
        return d
    safe_title = str(work_title).replace(" ", "_")
    d = outdir / f"{safe_title}_{uuid.uuid4().hex[:_JOB_ID_HEX]}"
    d.mkdir(parents=True, exist_ok=False)
    return d


# ── provenance ──────────────────────────────────────────────────────────────

def _minimal_provenance() -> dict:
    """config 가 없을 때의 최소 provenance — git_sha·host·machine 만.

    ⚠ 값을 만드는 코드를 여기 베끼지 않고 `app.modules.provenance` 의 것을 부른다
    (베낀 수식은 언젠가 한쪽만 고쳐진다 — E13 교훈). 그 모듈의 helper 가 비공개
    이름이라 예외적으로 직접 부르되, 공개 API 인 `build_provenance` 는 AppConfig 를
    요구하므로(설정을 모르는 호출자도 감사 기록은 남겨야 한다) 여기서 대신 쓴다.
    """
    from app.modules import provenance as _prov

    return {
        "git_sha": _prov._git_sha(_prov._REPO_ROOT),
        "host": _prov._host(),
        "machine": _prov._machine(),
        "config": None,
        "note": "config 미전달 — 최소 provenance(git_sha·host·machine)만 기록",
    }


def _provenance_for(config: Any | None) -> dict:
    if config is None:
        return _minimal_provenance()
    from app.modules.provenance import build_provenance

    return build_provenance(config)


# ── run_log ─────────────────────────────────────────────────────────────────

def new_run_log(*, pipeline: str, job_id: str, config: Any | None = None) -> dict:
    """신규 run_log 골격. v3 가 인라인으로 만드는 것과 **같은 필수 키**를 낸다.

    ⚠ `input`(video_path·work_title·episode…)은 여기 없다 — 무엇을 입력으로 봤는지는
    파이프라인마다 어휘가 달라서 부르는 쪽이 얹는다(v3 는 얹는다. M0 리플레이 로더의
    레이블 매칭이 `input.video_path` 를 읽으므로 v4 배선도 반드시 얹어야 한다).
    """
    return {
        "schema": RUN_LOG_SCHEMA,
        "pipeline": pipeline,
        "job_id": job_id,
        "provenance": _provenance_for(config),
        "steps": [],
    }


def resume_run_log(path: Path, *, pipeline: str, job_id: str,
                   from_step: str | None, config: Any | None = None) -> dict:
    """기존 run_log 를 **이어 쓴다** — steps 에 resume 한 줄을 append 하고 돌려준다.

    ⚠ 통째로 새로 만들면 전사 실패 창·휴리스틱 불일치 같은 감사 기록이 지워진다
    (v3 재개 규약 승계). 파일이 깨져 있으면 그대로 터뜨린다 — **조용한 초기화 금지**가
    의도다(깨진 기록을 덮어쓰면 무엇을 잃었는지조차 모른다).

    ⚠ `provenance` 는 최초 생성분을 유지한다 — 재개마다 다시 스탬핑하면 그 job 을
    *처음* 무엇으로 만들었는지가 사라진다(A/B 대조의 기준이 이 값이다). 최초 판에
    provenance 가 아예 없던 옛 job 만 지금 값으로 채운다.

    파일이 없으면 신규를 만든다(재개 지점을 잃지 않게 resume 한 줄은 그대로 남긴다).
    `job_id` 가 파일의 것과 다르면 크게 실패한다 — 남의 job 의 감사 기록에 이어 쓰는
    것이라, 조용히 통과시키면 두 편의 비용이 한 파일에 섞인다.
    `pipeline` 은 파일의 값을 이긴다고 보지 않는다(마일스톤마다 이름이 바뀌어 왔다 —
    v3_m1 → v3_m3). 다르면 resume 줄에 요청값을 함께 남겨 보이게만 한다.
    """
    path = Path(path)
    if not path.exists():
        run_log = new_run_log(pipeline=pipeline, job_id=job_id, config=config)
    else:
        run_log = read_json(path)
        if not isinstance(run_log, dict):
            raise ValueError(
                f"run_log 가 객체가 아니다({type(run_log).__name__}): {path} — "
                "조용히 새로 만들지 않는다(감사 기록 유실 금지)")
        was_job_id = run_log.get("job_id")
        if was_job_id and str(was_job_id) != str(job_id):
            raise ValueError(
                f"run_log 의 job_id 가 다르다: 파일={was_job_id!r} 요청={job_id!r} "
                f"({path}) — 남의 감사 기록에 이어 쓰지 않는다")
        run_log.setdefault("schema", RUN_LOG_SCHEMA)
        run_log.setdefault("job_id", job_id)
        run_log.setdefault("pipeline", pipeline)
        if not run_log.get("provenance"):
            run_log["provenance"] = _provenance_for(config)
    entry: dict[str, Any] = {"step": "resume", "from_step": from_step}
    if str(run_log.get("pipeline")) != str(pipeline):
        entry["pipeline_requested"] = pipeline
    run_log.setdefault("steps", []).append(entry)
    return run_log


def append_step(run_log: dict, name: str, **fields) -> dict:
    """steps 에 {"step": name, **fields} 를 append 하고 **그 dict** 를 돌려준다.

    ⚠ run_log 는 제자리에서 자란다(계약 §2 — 부르는 쪽이 한 dict 를 계속 들고 있다).
    돌려준 dict 를 나중에 고치면(예: elapsed 를 뒤에 채우면) 디스크는 아직 이전 값이니
    `write_run_log` 를 다시 불러야 한다.
    """
    entry: dict[str, Any] = {"step": name, **fields}
    run_log.setdefault("steps", []).append(entry)
    return entry


def write_run_log(path: Path, run_log: dict) -> None:
    """run_log 를 원자적으로 기록한다 — **단계마다** 부르는 것이 v4 계약이다.

    v3 는 이 호출이 `finally` 한 곳뿐이라 프로세스가 죽으면 감사 기록이 통째로
    사라졌다(모듈 독스트링의 gotcha 1). 여기서 파일명·직렬화를 고정해 두는 이유는,
    호출 지점이 늘어나도 산출 형태가 한 곳에서만 정해지게 하는 것이다.
    """
    write_json(Path(path), run_log)


def make_step_logger(run_log: dict, path: Path):
    """v3 의 `step(name, **fields)` 클로저를 대신하되 **매번 디스크에 확정**한다.

    ⚠ 계약 §2 에 없는 추가 helper 다(계약의 함수는 전량 그대로 있다). v3 의 인라인
    클로저가 append 만 하고 기록을 미룬 것이 gotcha 1 의 형태였으므로, 배선이
    `append_step` + `write_run_log` 를 **짝으로** 부르는 것을 잊지 못하게 한 곳에
    묶는다. 부작용이 있는 것은 이 함수뿐이고 나머지는 전부 순수다.
    """
    path = Path(path)

    def step(name: str, **fields) -> dict:
        entry = append_step(run_log, name, **fields)
        write_run_log(path, run_log)
        return entry

    return step


# ── 지문 ────────────────────────────────────────────────────────────────────

def fingerprint(*parts: Any) -> str:
    """캐시 무효화 지문 — sha1(json.dumps(parts, sort_keys=True))[:16]. 순수·결정적.

    `sort_keys=True` 라 dict 키 순서에 무관하다(같은 재료면 같은 지문). 직렬화
    불가한 값은 TypeError 로 터뜨린다 — `default=str` 로 넘기면 객체 주소가 섞여
    같은 재료가 매 실행 다른 지문을 내고 캐시가 영구히 무효가 된다.

    ⚠ 지문 재료는 부르는 쪽이 **전량 명시**한다 — v3 는 지문 4종의 재료가 서로 달라
    각각 다른 변경을 놓쳤다(조사 gotcha 9). 이 함수는 무엇을 재료로 삼아야 하는지
    모른다.
    """
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:FINGERPRINT_LEN]
