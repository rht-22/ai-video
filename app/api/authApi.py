from fastapi import APIRouter, Depends,HTTPException,status
from sqlalchemy.orm import Session
from app.core.database import get_db

from app.core.security import (
    get_current_api_key, 
    check_admin_role, 
    verify_admin_by_user_id, 
    generate_api_key  
)
from app.domain.authKey import AuthApiKey, UserStatus, Role


router = APIRouter()

# ---------------------------------------------------------
# [API 1] 일반 유저 인증 확인
# ---------------------------------------------------------
@router.get("/user-check")
def check_user_auth(
    # get_current_api_key가 성공하면 인증된 record를 반환합니다
    record = Depends(get_current_api_key) 
):
    return {
        "status": "success",
        "message": "인증 성공",
        "data": {
            "user_id": record.user_id,
            "channel_id": record.channel_id,
            "role": record.role, # Enum일 경우 record.role.value
            "status": record.status
        }
    }

# ---------------------------------------------------------
# [API 2] 어드민 전용 인증 확인
# ---------------------------------------------------------
@router.get("/admin-check")
def check_admin_auth(
    # check_admin_role이 내부적으로 get_current_api_key를 호출하고 권한까지 검사합니다
    record = Depends(check_admin_role)
):
    return {
        "status": "success",
        "message": "어드민 인증 성공",
        "data": {
            "admin_id": record.user_id,
            "role": "ADMIN"
        }
    }

# 어드민 키 생성
@router.post("/admin/initial-key")
def create_initial_admin_key(
    user_id: str, 
    db: Session = Depends(get_db)
):
    # 1. User 테이블 직접 조회해서 어드민인지 검증
    admin_user = verify_admin_by_user_id(user_id, db)
    
    # 이미 해당 유저에게 활성(ACTIVE) 상태의 키가 있는지 확인
    existing_key = db.query(AuthApiKey).filter(
        AuthApiKey.user_id == user_id,
        AuthApiKey.status == UserStatus.ACTIVE
    ).first()

    if existing_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="이미 활성화된 API Key가 존재합니다. 기존 키를 사용하거나 무효화하세요."
        )

    # 2. 새로운 API Key 생성 (기존 로직 동일)
    api_key_plain, hashed_key = generate_api_key()
    
    # 3. AuthApiKey 테이블에 저장
    new_key_record = AuthApiKey(
        user_id=admin_user.id,
        api_key_hash=hashed_key,
        role=Role.ADMIN,
        status=UserStatus.ACTIVE
    )
    
    try:
        db.add(new_key_record)
        db.commit()
        db.refresh(new_key_record)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"DB 저장 오류: {str(e)}")

    return {
        "status": "success",
        "message": "최초 어드민 API Key가 발급되었습니다.",
        "data": {
            "api_key": api_key_plain,
            "user_id": admin_user.id,
            "role": "ADMIN"
        }
    }