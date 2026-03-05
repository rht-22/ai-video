from pydantic import BaseModel

#채널 생성
class ChanneCreatelRequest(BaseModel):
    channel_name: str
    authKey: str
    user_id:str

    class Config:
        from_attributes = True

#채널 조회
class ChanneSelectRequest(BaseModel):
    channel_id: str
    channel_name: str
    status: str

    class Config:
        from_attributes = True

#채널 수정
#- 채널명, 인증키, 채널 상태
class ChanneUpdateRequest(BaseModel):
    channel_id: str
    channel_name: str
    status: str

    class Config:
        from_attributes = True

#채널 삭제
class ChanneDeleteRequest(BaseModel):
    channel_id: str
    status: str

    class Config:
        from_attributes = True
