from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from pathlib import Path
from pydantic import BaseModel
from app.core.database import get_db

# 필요한 모듈 임포트
from app.config import DesignConfig
from app.pipeline import run_pipeline, PipelineInput
from app.domain import Video
# from app.database import get_db, Video (실제 경로에 맞춰 수정하세요)

router = APIRouter()

class ShortsGenerateRequest(BaseModel):
    video_id: str
    topic: str
    title_config: dict = {}    # {size, y, font, color}
    video_config: dict = {}    # {aspect_ratio, width, height, y_pos}
    subtitle_config: dict = {} # {size, color, font, margin_v}
    work_config: dict = {}     # {y, size, color}
    from_step: str | None = None  # 추가: 시작할 단계 (예: "gemini", "resources")
    job_id: str | None = None

@router.post("/generate/shorts")
async def generate_shorts(
    req: ShortsGenerateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    video = db.query(Video).filter(Video.id == req.video_id).first()
    if not video: 
        raise HTTPException(status_code=404, detail="Video not found")

   
    # design = DesignConfig(
    #     aspect_ratio=req.video_config.get("aspect_ratio", "9:16"),
    #     video_width=req.video_config.get("width",  800),
    #     video_height=req.video_config.get("height", 720),
    #     video_y_pos=req.video_config.get("y_pos", 480),
        
    #     title_font=req.title_config.get("font", "Malgun Gothic"),
    #     title_size=req.title_config.get("size", 70),
    #     title_color=req.title_config.get("color", "yellow"),
    #     title_y=req.title_config.get("y", 200),
        
    #     subtitle_font=req.subtitle_config.get("font", "Malgun Gothic"),
    #     subtitle_size=req.subtitle_config.get("size", 65),
    #     subtitle_color=req.subtitle_config.get("color", "&H0000FFFF"),
    #     subtitle_y_margin=req.subtitle_config.get("margin_v", 400),
        
    #     work_font_size=req.work_config.get("size", 45),
    #     work_color=req.work_config.get("color", "white"),
    #     work_title_y=req.work_config.get("y", 1260)
    # )
    design = DesignConfig(
        aspect_ratio=req.video_config.get("aspect_ratio", "9:16"),
        video_width=req.video_config.get("width", 800),
        video_height=req.video_config.get("height", 720),
        video_y_pos=req.video_config.get("y_pos", 480),
        
        title_font=req.title_config.get("font", "Malgun Gothic"),
        title_size=req.title_config.get("size", 70),
        # title_colors (리스트) 대응
        title_colors=req.title_config.get("colors", ["white", "orange"]),
        title_color=req.title_config.get("colors", ["white"])[0],
        title_y=req.title_config.get("y", 200),
        
        subtitle_font=req.subtitle_config.get("font", "Malgun Gothic"),
        subtitle_size=req.subtitle_config.get("size", 65),
        subtitle_color=req.subtitle_config.get("color", "&H0000FFFF"),
        subtitle_y_margin=req.subtitle_config.get("margin_v", 400),
        
        # [중요] 작품명 이미지/텍스트 설정 추출
        work_type=req.work_config.get("type", "text"),         
        work_value=req.work_config.get("value", None),        
        work_image_width=req.work_config.get("image_width", 200), 
        
        work_font_size=req.work_config.get("size", 45),
        work_color=req.work_config.get("color", "white"),
        work_title_y=req.work_config.get("y", 1260)
    )

    print(f"DEBUG: API에서 받은 margin_v: {design.subtitle_y_margin}")

    # 2. 파이프라인 실행용 페이로드 구성
    # Path 객체 변환 및 저장 경로(OUTPUT_ROOT) 확인 필요
    payload = PipelineInput(
        video_path=Path(video.storage_path),
        work_title=video.title,
        topic=req.topic,
        outdir=Path("./outputs"), # 적절한 출력 경로 설정
        design=design             # 생성한 디자인 주입
    )

    # 3. 백그라운드 태스크 실행
    background_tasks.add_task(
        run_pipeline, 
        payload=payload, 
        from_step=req.from_step, # API 요청에서 받은 값 전달
        job_id=req.job_id         # API 요청에서 받은 값 전달
    )
    
    return {
        "status": "SUCCESS", 
        "message": f"'{video.title}' 영상의 렌더링이 시작되었습니다.",
        "job_id": req.video_id # 추적을 위해 ID 반환
    }