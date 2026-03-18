import uuid
from pathlib import Path
from typing import Optional # Optional 추가
from fastapi import APIRouter, Depends, HTTPException, Form, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.domain.video import Video, VideoStatus, VideoSourceType
from app.modules.media_probe import probe_media

from app.core.security import get_current_api_key 
from app.domain.authKey import Role 

router = APIRouter()

@router.post("/upload/file")
async def upload_video_file(
    work_title: str = Form(...),
    topic: str = Form(...),
    video_path: str = Form(...),
    # [수정] 어드민은 직접 채널 ID를 입력할 수 있도록 추가
    input_channel_id: Optional[str] = Form(None, alias="channel_id"), 
    db: Session = Depends(get_db), 
    current_key_info = Depends(get_current_api_key) 
):
    # 1. 계정 상태 체크
    if current_key_info.status.value != "active":
        raise HTTPException(status_code=403, detail="계정이 비활성화 상태입니다.")

    # -----------------------------------------------------------
    # [권한별 채널 ID 결정 로직]
    # -----------------------------------------------------------
    target_channel_id = None

    if current_key_info.role == Role.ADMIN:
        # 어드민인 경우: 입력받은 channel_id를 우선 사용, 없으면 자신의 키에 연결된 것 사용
        target_channel_id = input_channel_id or current_key_info.channel_id
        
        # 어드민인데 둘 다 없다면 에러 (어디엔가는 속해야 하므로)
        if not target_channel_id:
            raise HTTPException(
                status_code=400, 
                detail="어드민 업로드 시 대상 channel_id를 입력하거나 지정해야 합니다."
            )
    else:
        # 일반 유저인 경우: 무조건 자신의 API Key에 연결된 채널만 사용 (보안)
        target_channel_id = current_key_info.channel_id
        if not target_channel_id:
            raise HTTPException(status_code=403, detail="연결된 채널 정보가 없습니다.")
    # -----------------------------------------------------------

    file_path = Path(video_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="파일이 존재하지 않습니다.")

    try:
        # 미디어 검증 로직 (동일)
        media_info = probe_media(file_path)
        duration = int(media_info.duration_sec)

        # 5. DB 데이터 생성
        new_video = Video(
            video_id=uuid.uuid4().hex[:10],
            channel_id=target_channel_id, # 결정된 채널 ID 삽입
            status=VideoStatus.READY,
            source_type=VideoSourceType.FILE,
            title=work_title,
            topic=topic,
            thumbnail_url="temp/url", 
            format=file_path.suffix.replace(".", ""),
            duration=duration,
            size=f"{file_path.stat().st_size / (1024*1024):.2f} MB",
            resolution=f"{media_info.width}x{media_info.height}",
            storage_path=str(file_path),
            log=f"Registered by {current_key_info.role.value}: {current_key_info.user_id}", 
        )
        
        db.add(new_video)
        db.commit()
        db.refresh(new_video)
        
        return {
            "status": "success",
            "data": {
                "video_id": new_video.video_id,
                "channel_id": new_video.channel_id,
                "owner_role": current_key_info.role.value
            }
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"등록 실패: {str(e)}")