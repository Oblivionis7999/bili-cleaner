"""认证相关路由：扫码登录、登录态查询、退出。"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

router = APIRouter(prefix="/api", tags=["auth"])


def _ctx(request: Request):
    """从 app.state 取运行上下文（避免路由模块与 app 循环导入）。"""
    return request.app.state.ctx


@router.post("/auth/qrcode")
def create_qrcode(request: Request) -> dict:
    """生成登录二维码。"""
    return _ctx(request).qrcode_generate()


@router.get("/auth/qrcode/poll")
def poll_qrcode(request: Request, key: str = Query(..., min_length=1)) -> dict:
    """轮询扫码状态；confirmed 时由上下文保存登录态。"""
    return _ctx(request).qrcode_poll(key)


@router.get("/me")
def me(request: Request) -> dict:
    """当前登录用户。"""
    return _ctx(request).me()


@router.post("/auth/logout")
def logout(request: Request) -> dict:
    """退出登录（清掉本地加密保存的 Cookie）。"""
    return _ctx(request).logout()
