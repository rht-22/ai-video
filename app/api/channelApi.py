import secrets
from fastapi import APIRouter, Depends, HTTPException,Query,Form
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import check_admin_role, generate_api_key
from app.domain.channels import Channel, ChannelStatus 
from app.domain.authKey import AuthApiKey,UserStatus,Role
from app.schemas.channelDto import ChanneCreatelRequest
from typing import List, Optional


router = APIRouter()

# -----------------------------------------------------------
# 채널 생성
# -----------------------------------------------------------
@router.post("/create")
def channel_create(
    request: ChanneCreatelRequest, 
    db: Session = Depends(get_db),
    admin_record = Depends(check_admin_role) 
):
    # 1. 중복 확인
    existing_channel = db.query(Channel).filter(Channel.user_id == request.user_id).first()
    if existing_channel:
        raise HTTPException(status_code=400, detail="이미 해당 유저의 채널이 존재합니다.")

    try:
        # 2. 채널(Channel) 생성
        new_channel = Channel(
            channel_name = request.channel_name,
            user_id = request.user_id,
            status = ChannelStatus.ACTIVE
        )
        db.add(new_channel)
        db.flush() # ID를 미리 확보하기 위함

        # 3. 공통 함수를 통한 API Key 생성 및 해싱
        plain_key, hashed_key = generate_api_key()

        # 4. AuthApiKey 테이블에 저장
        new_auth_key = AuthApiKey(
            channel_id = new_channel.channel_id, 
            api_key_hash = hashed_key,           
            user_id = request.user_id,
            status = UserStatus.ACTIVE,          
            role = Role.USER                     
        )
        db.add(new_auth_key)
        
        db.commit() 
        db.refresh(new_channel)
        
        # 5. 최종 결과 반환 
        return {
            "status": "success",
            "message": "채널 및 인증키 생성 완료",
            "data": {
                "channel_id": new_channel.channel_id,
                "channel_name": new_channel.channel_name,
                "user_id": new_channel.user_id,
                "api_key": plain_key 
            },
            "admin_info": {
                "created_by": admin_record.user_id
            }
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"생성 중 오류 발생: {str(e)}")

# -----------------------------------------------------------
# 채널 조회 (전체 / 조건 조회)
# -----------------------------------------------------------
@router.get("/list")
def get_channels(
    status: Optional[ChannelStatus] = None,
    channel_name: Optional[str] = None,
    db: Session = Depends(get_db),
    admin_record = Depends(check_admin_role)
):
    query = db.query(Channel)
    
    # 조건 조회: 상태별
    if status:
        query = query.filter(Channel.status == status)
    
    # 조건 조회: 채널명 검색 (부분 일치)
    if channel_name:
        query = query.filter(Channel.channel_name.contains(channel_name))
        
    channels = query.all()
    return {"status": "success", "data": channels}

# -----------------------------------------------------------
# 채널 단건 조회
# -----------------------------------------------------------
@router.get("/{channel_id}")
def get_channel_detail(
    channel_id: str,
    db: Session = Depends(get_db),
    admin_record = Depends(check_admin_role)
):
    channel = db.query(Channel).filter(Channel.channel_id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="채널을 찾을 수 없습니다.")
    
    return {"status": "success", "data": channel}

# -----------------------------------------------------------
# 채널 수정 (채널명 변경 / 인증키 갱신)
# -----------------------------------------------------------
@router.patch("/{channel_id}/update")
def update_channel(
    channel_id: str,
    new_name: Optional[str] = Form(None),
    renew_api_key: bool = Form(False), # 인증키 갱신 여부
    db: Session = Depends(get_db),
    admin_record = Depends(check_admin_role)
):
    channel = db.query(Channel).filter(Channel.channel_id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="채널을 찾을 수 없습니다.")

    result_data = {}

    # 채널명 변경
    if new_name:
        channel.channel_name = new_name
        result_data["updated_name"] = new_name

    # 인증키 갱신 (어드민만 가능하며 요청 시에만 수행)
    if renew_api_key:
        plain_key, hashed_key = generate_api_key()
        auth_key_record = db.query(AuthApiKey).filter(AuthApiKey.channel_id == channel_id).first()
        
        if auth_key_record:
            auth_key_record.api_key_hash = hashed_key
            result_data["new_api_key"] = plain_key # 평문 키 반환
        else:
            # 혹시나 연결된 키 레코드가 없을 경우 새로 생성
            new_auth_key = AuthApiKey(
                channel_id=channel.channel_id,
                api_key_hash=hashed_key,
                user_id=channel.user_id,
                status=UserStatus.ACTIVE,
                role=Role.USER
            )
            db.add(new_auth_key)
            result_data["new_api_key"] = plain_key

    db.commit()
    return {
        "status": "success", 
        "message": "수정 완료", 
        "data": result_data
    }

# -----------------------------------------------------------
# 채널 상태 변경 (정지/삭제)
# -----------------------------------------------------------
@router.delete("/{channel_id}")
def delete_or_freeze_channel(
    channel_id: str,
    target_status: ChannelStatus = Query(ChannelStatus.DELETED), # 기본값 DELETED
    db: Session = Depends(get_db),
    admin_record = Depends(check_admin_role)
):
    channel = db.query(Channel).filter(Channel.channel_id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="채널을 찾을 수 없습니다.")

    # 채널 상태 변경
    channel.status = target_status

    # 채널 상태가 DELETED 또는 INACTIVE가 되면 인증키도 함께 정지 처리
    auth_key_record = db.query(AuthApiKey).filter(AuthApiKey.channel_id == channel_id).first()
    if auth_key_record:
        if target_status == ChannelStatus.DELETED:
            auth_key_record.status = UserStatus.DELETED
        elif target_status == ChannelStatus.INACTIVE:
            auth_key_record.status = UserStatus.INACTIVE

    db.commit()
    return {
        "status": "success", 
        "message": f"채널 상태가 {target_status.value}로 변경되었습니다."
    }