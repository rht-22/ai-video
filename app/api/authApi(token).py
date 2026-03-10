import hashlib
from datetime import datetime
from fastapi import APIRouter, HTTPException, status, Security, Depends, Header, Body
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from jose import jwt, JWTError

from app.core.database import get_db
from app.domain.authKey import AuthApiKey, UserStatus, Role

router = APIRouter()
security = HTTPBearer()

# --- 설정 ---
SECRET_KEY = "your-secret-key"
ALGORITHM = "HS256"

# 테스트용 토큰 발급
@router.post("/test-login", tags=["Test"])
def test_login(
    user_id: str = Body("test-user-uuid", description="DB에 등록된 user_id"),
    channel_id: str = Body("test-channel-uuid", description="DB에 등록된 channel_id"),
    role: str = Body("user", description="user 또는 admin"),
    status: str = Body("ACTIVE", description="ACTIVE 또는 INACTIVE")
):
    """
    입력한 정보를 바탕으로 테스트용 JWT 토큰을 생성합니다.
    """
    payload = {
        "sub": user_id,
        "channel_id": channel_id,
        "role": role.lower(),
        "status": status.upper(),
        # "exp": datetime.utcnow() + timedelta(days=1) # 유효기간 1일
    }
    
    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    
    return {
        "access_token": token,
        "token_type": "bearer",
        "message": "위 access_token을 복사해서 상단 'Authorize' 버튼에 넣으세요."
    }

# [1] 토큰 까서 유저 정보 추출 (공통 함수)
def get_info_from_token(credentials: HTTPAuthorizationCredentials = Security(security)):
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        
        # 토큰 내부 데이터 추출
        user_id: str = payload.get("sub")
        channel_id: str = payload.get("channel_id")
        role: str = payload.get("role")
        user_status: str = payload.get("status", "ACTIVE") # 상태값 포함
        
        if not user_id:
            raise HTTPException(status_code=401, detail="토큰에 사용자 정보가 없습니다.")
            
        return {
            "user_id": user_id,
            "channel_id": channel_id,
            "role": role.lower() if role else "user",
            "status": user_status.upper()
        }
    except JWTError:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다.")

# [2] 핵심 검증 로직 (DB 정보와 대조)
def verify_access(db: Session, token_data: dict, x_api_key: str, required_role: str = None):
    # 1. 토큰의 상태가 ACTIVE인지 확인
    if token_data["status"] != "ACTIVE":
        raise HTTPException(status_code=403, detail="비활성화된 사용자 계정입니다.")

    # 2. 어드민 체크 (필요한 경우)
    if required_role == "admin" and token_data["role"] != "admin":
        raise HTTPException(status_code=403, detail="어드민 권한이 필요합니다.")

    # 3. 입력받은 인증키 해싱
    hashed_key = hashlib.sha256(x_api_key.encode()).hexdigest()

    # 4. DB 교차 검증: [해시 + 유저ID + 채널ID + 권한 + 활성상태] 모두 일치해야 함
    query = db.query(AuthApiKey).filter(
        AuthApiKey.api_key_hash == hashed_key,
        AuthApiKey.user_id == token_data["user_id"],
        AuthApiKey.status == UserStatus.ACTIVE
    )
    
    # 어드민이 아닐 경우 채널 ID까지 엄격하게 체크
    if token_data["role"] != "admin":
        query = query.filter(AuthApiKey.channel_id == token_data["channel_id"])
        
    key_record = query.first()

    if not key_record:
        raise HTTPException(status_code=401, detail="인증키가 유효하지 않거나 본인 소유가 아닙니다.")

    # 사용 기록 업데이트
    key_record.last_used_at = datetime.utcnow()
    db.commit()
    
    return key_record

# ---------------------------------------------------------
# [API 1] 일반 유저 인증 확인
# ---------------------------------------------------------
@router.get("/user-check")
def check_user_auth(
    x_api_key: str = Header(..., description="인증키 입력"),
    db: Session = Depends(get_db),
    token_data: dict = Depends(get_info_from_token)
):
    record = verify_access(db, token_data, x_api_key)
    return {
        "status": "success",
        "user_id": record.user_id,
        "channel_id": record.channel_id,
        "role": record.role.value
    }

# ---------------------------------------------------------
# [API 2] 어드민 전용 인증 확인
# ---------------------------------------------------------
@router.get("/admin-check")
def check_admin_auth(
    x_api_key: str = Header(..., description="어드민 인증키 입력"),
    db: Session = Depends(get_db),
    token_data: dict = Depends(get_info_from_token)
):
    # required_role="admin"을 넘겨서 토큰 권한까지 강제 확인
    record = verify_access(db, token_data, x_api_key, required_role="admin")
    return {
        "status": "success",
        "admin_id": record.user_id,
        "role": "ADMIN"
    }