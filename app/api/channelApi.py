from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import create_access_token
from app.domain.channels import Channel, ChannelStatus 
from app.schemas.channelDto import ChanneCreatelRequest
# ,ChanneSelectRequest,ChanneUpdateRequest,ChanneDeleteRequest



#채널 생성, 수정 , 삭제
# 이미 있는 계정인지
# 계정 상태 조회 및 관리
# 생성 시 암호화 처리
# 삭제 시 소프트 딜리트 처리
# 채널명 / 인증키 수정

router = APIRouter()

#채널 생성
@router.post("/create")
def channel_create(request: ChanneCreatelRequest, db: Session = Depends(get_db)):
   
    channel = Channel(channel_name = request.channel_name,auth_key = request.authKey, user_id = request.user_id)
    db.add(channel)
    db.commit()      
    db.refresh(channel)
    
    return {"message": "생성 완료", "channel": channel.channel_name}
