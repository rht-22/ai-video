import uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy import Column, Integer, String, Text, DateTime,Enum,ForeignKey
from sqlalchemy.orm import relationship
from app.core.database import Base

KST = timezone(timedelta(hours=9))

# 비디오 아이디, 채널 아이디 , 상태, 영상 타입
# 영상제목, 길이, 해상도, 포멧, 사이즈, 저장 위치
# 로그
#생성일자
#삭제 일자
import enum

# 영상 상태 정의
class VideoStatus(str, enum.Enum):
    READY = "READY"
    PROCESSING ="PROCESSING"
    COMPLETED ="COMPLETED"
    FAILED = "FAILED"

class VideoSourceType(str, enum.Enum):
    FILE = "FILE"
    YOUTUBE = "YOUTUBE"

class Video(Base):
    __tablename__ = "videos"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    video_id = Column(String(10), nullable=False, default=lambda: str(uuid.uuid4()))
    channel_id = Column(String(36),ForeignKey("channels.channel_id"), nullable=False,default=lambda: str(uuid.uuid4()))

    status = Column(Enum(VideoStatus), default=VideoStatus.READY)
    source_type = Column(Enum(VideoSourceType), nullable=False)

    title = Column(String(255), nullable=False)
    thumbnail_url = Column(String(500), nullable=False)
    topic = Column(String(50), nullable=False)

    format = Column(String(20), nullable=False)
    duration = Column(Integer, nullable=False)
    size = Column(String(50), nullable=False)
    resolution = Column(String(20), nullable=False)

    storage_path =Column(String(500), nullable=False)

    log =Column(Text, nullable=False)

    created_at = Column(DateTime, default=datetime.now(KST))
    expired_at = Column(DateTime, default=lambda: datetime.now(KST) + timedelta(days=30))

    channels = relationship("Channel", backref="videos")
    