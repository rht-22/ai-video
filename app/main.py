from fastapi import FastAPI, Depends
from app.core.security import get_current_channel
from app.domain.channels import Channel
from app.api import auth
from app.api import channelApi

app = FastAPI()

#인증 관련 API
app.include_router(auth.router, prefix="/auth/verify", tags=["Authentication"])

#채널 관련
app.include_router(channelApi.router, prefix="/channel", tags=["Authentication"])
