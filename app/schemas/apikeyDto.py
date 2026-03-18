from pydantic import BaseModel
from typing import Optional
from uuid import UUID

class ApiKeyResponse(BaseModel):
    api_key: str 
    role: str
    status: str

    class Config:
        from_attributes = True