from sqlalchemy import Column, String, DateTime,ForeignKey,Enum
from sqlalchemy.orm import relationship
import uuid
from datetime import datetime
from app.core.database import Base
import enum


class Role(enum.Enum):
    USER = "user"
    ADMIN = "admin"

class UserStatus(enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    DELETED = "deleted"



class AuthApiKey(Base):
    __tablename__ = "auth_api_keys"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36),ForeignKey("user.id"),nullable=False,default=lambda: str(uuid.uuid4()))
    channel_id = Column(String(36),ForeignKey("channels.channel_id"))
    api_key_hash = Column(String(255), nullable=False, index=True) 
    role = Column(Enum(Role), nullable=False, default=Role.USER)
    status = Column(Enum(UserStatus), nullable=False, default=UserStatus.ACTIVE)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)

    channels = relationship("Channel", backref="auth_api_keys")
    user = relationship("User", backref="auth_api_keys")