from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import create_access_token
from app.domain.channels import Channel, ChannelStatus 
from app.schemas.auth import AuthRequest

router = APIRouter()

@router.post("/")
def verify_system_access(request: AuthRequest, db: Session = Depends(get_db)):
    # 1. DB에서 인증키와 사용자 ID 대조
    channel = db.query(Channel).filter(
        Channel.user_id == request.userId,
        Channel.auth_key == request.authKey
    ).first()

    # 2. 인증키 불일치 (401)
    if not channel:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="인증키 불일치"
        )

    # 3. 채널 상태 확인 (ACTIVE가 아니면 403)
    if channel.status != ChannelStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="권한 없는 채널 접근 (상태: " + channel.status + ")"
        )

    # 4. 성공 시 JWT 발급
    access_token = create_access_token(data={"sub": channel.channel_id})
    
    # 명세서대로 성공 시 바디에 토큰 포함 (헤더로만 주고 싶다면 Response 사용)
    return {"access_token": access_token, "token_type": "bearer"}