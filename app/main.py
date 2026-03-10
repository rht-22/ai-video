from fastapi import FastAPI, Depends
# from app.core.security import get_current_channel
from app.core.security import get_current_api_key,check_admin_role
from app.domain.channels import Channel
from app.api import authApi
from app.api import channelApi
from app.api import uploadApi
from app.api import videoApi

app = FastAPI()

# 1. 인증 관련 API (여기는 API Key 없이 접근 가능해야 함)
app.include_router(authApi.router, prefix="/auth/verify", tags=["Authentication"])

# 2. 채널 관련 (인증 필요 시 해당 라우터 파일 내부에서 Depends 사용 권장)
app.include_router(channelApi.router, prefix="/channel", tags=["Channel"])

# 3. 영상 업로드
app.include_router(uploadApi.router, prefix="/upload", tags=["Upload"])

# 4. 영상 분석
app.include_router(videoApi.router, prefix="/video", tags=["Video"])