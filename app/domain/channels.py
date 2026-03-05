# from sqlalchemy import Column, Integer, String, create_engine
import enum
from sqlalchemy import Column, Integer, String, Text, DateTime,Enum,func

from app.core.database import Base

class ChannelStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    DELETED = "DELETED"

class Channel(Base):
    __tablename__ = "channels"

    channel_id = Column(Integer, primary_key=True)
    user_id = Column(String(10), nullable=False)
    channel_name = Column(String(100), nullable=False)
    auth_key = Column(String(255), unique=True, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    
    status = Column(Enum(ChannelStatus), nullable=False,default=ChannelStatus.ACTIVE)