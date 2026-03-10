from sqlalchemy import Column, String, DateTime,Enum
import uuid
from app.core.database import Base
import enum


class Role(enum.Enum):
    USER = "user"
    ADMIN = "admin"




class User(Base):
    __tablename__ = "user"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    role = Column(Enum(Role), nullable=False, default=Role.USER)
