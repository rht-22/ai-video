from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.domain.video import Video, VideoStatus
# pipeline.py에서 정의한 입력 클래스와 스타일 리스트를 가져옵니다.
from app.pipeline import PipelineInput, run_pipeline, STYLE_PRESETS 

router = APIRouter()

# 결과물 저장 경로
OUTPUT_ROOT = Path("storage/outputs")
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

@router.post("/generate/shorts")
async def generate_shorts(
    video_id: str,
    style_id: str = "IMPACT_YELLOW",  # 기본값 설정
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: Session = Depends(get_db)
):
    # 1. DB에서 영상 정보 조회
    video = db.query(Video).filter(Video.video_id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="영상을 찾을 수 없습니다. 먼저 업로드해 주세요.")

    # 2. pipeline에 정의된 스타일 프리셋 확인
    preset = STYLE_PRESETS.get(style_id)
    if not preset:
        raise HTTPException(
            status_code=400, 
            detail=f"존재하지 않는 스타일입니다. 가능 목록: {list(STYLE_PRESETS.keys())}"
        )

    # 3. 파이프라인 입력 객체(Payload) 구성
    # 디자인 설정 전체(preset)를 그대로 넘깁니다.
    payload = PipelineInput(
        video_path=Path(video.storage_path),
        work_title=video.title,
        topic=video.topic,
        outdir=OUTPUT_ROOT,
        design=preset  # PipelineInput에 추가한 design 필드에 할당
    )

    # 4. DB 상태 업데이트
    video.status = VideoStatus.READY 
    video.log += f"\n[INFO] {preset.name} 스타일로 분석 요청 접수"
    db.commit()

    # 5. 비동기 실행 (BackgroundTasks)
    background_tasks.add_task(
        run_pipeline, 
        payload=payload, 
        job_id=video.video_id,
        db=db 
    )

    return {
        "status": "processing",
        "video_id": video_id,
        "style_name": preset.name,
        "message": "숏츠 생성이 시작되었습니다."
    }

@router.get("/styles")
async def list_styles():
    """프론트엔드에서 선택 가능한 스타일 목록을 보여주기 위한 API"""
    return {key: val.name for key, val in STYLE_PRESETS.items()}