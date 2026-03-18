#pydantic
# - json 파싱, 데이터 검증
from pydantic import BaseModel

class AuthRequest(BaseModel):
    userId: str
    authKey: str

    class Config:
        from_attributes = True