import hashlib
import secrets
from fastapi import HTTPException, status, Security, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.domain.user import User, Role
from app.domain.authKey import AuthApiKey,Role,UserStatus

security = HTTPBearer()



# Admin API Key 생성
def verify_admin_by_user_id(user_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    
    if not user:
        raise HTTPException(status_code=404, detail="존재하지 않는 사용자입니다.")
    
    # User 테이블의 role 컬럼이 'admin'인지 확인
    if user.role != Role.ADMIN:
        raise HTTPException(status_code=403, detail="해당 유저는 관리자 권한이 없습니다.")
        
    return user

def get_current_api_key(
    credentials: HTTPAuthorizationCredentials = Security(security),
    db: Session = Depends(get_db)
):
    # 헤더에서 평문 추출
    api_key_plain = credentials.credentials
    
    # 서버 내부에서 해싱 수행
    hashed_key = hashlib.sha256(api_key_plain.encode()).hexdigest()

    # 터미널(콘솔) 확인용 로그
    print("\n" + "="*30)
    print(f"DEBUG: 클라이언트 평문 -> {api_key_plain}")
    print(f"DEBUG: 생성된 해시값 -> {hashed_key}")
    print("="*30 + "\n")

    # DB 조회
    key_record = db.query(AuthApiKey).filter(
        AuthApiKey.api_key_hash == hashed_key,
        AuthApiKey.status == "active",
    ).first()

    if not key_record:
        print(f"인증 실패: DB에 {hashed_key} 값이 없습니다.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="유효하지 않은 API Key입니다."
        )
    
    print(f"인증 성공: User ID {key_record.user_id}")
    return key_record


def check_admin_role(key_record = Depends(get_current_api_key)):
    if key_record.role != Role.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="관리자 권한이 필요합니다."
        )
    return key_record

def generate_api_key(prefix: str = "sk_live") -> tuple[str, str]:
  
    # 1. 랜덤 평문 키 생성
    raw_token = secrets.token_hex(32)
    api_key_plain = f"{prefix}_{raw_token}"
    
    # 2. SHA256 해싱
    api_key_hash = hashlib.sha256(api_key_plain.encode()).hexdigest()
    
    return api_key_plain, api_key_hash