import uuid
from sqlalchemy import Column, String, DateTime, Enum, func,ForeignKey
from sqlalchemy.dialects.mysql import CHAR # MySQL/MariaDB용 UUID 저장 방식
from app.core.database import Base
from sqlalchemy.orm import relationship
import enum

class ChannelStatus(enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    DELETED = "deleted"

class Channel(Base):
    __tablename__ = "channels"

    channel_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("user.id"), nullable=False, default=lambda: str(uuid.uuid4()))
    channel_name = Column(String(100), nullable=False)

    
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    
    status = Column(Enum(ChannelStatus), nullable=False, default=ChannelStatus.ACTIVE)
    user = relationship("User", backref="channels")