"""GitHub OAuth sign-in with a signed session cookie.

Auth is on when GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET are set. Without
them the app runs in guest mode so local demos work with no setup.
"""
import secrets
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

try:
    from backend.config import settings
except ImportError:
    from ideator.backend.config import settings

router = APIRouter()
GUEST = {"id": "guest", "login": "guest", "name": "Guest", "avatar_url": ""}


def auth_enabled() -> bool:
    return bool(settings.github_client_id and settings.github_client_secret)


def current_user(request: Request) -> Optional[dict]:
    if not auth_enabled():
        return GUEST
    return request.session.get("user")


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    return user


@router.get("/auth/me")
def me(request: Request):
    return {"auth_enabled": auth_enabled(), "user": current_user(request)}


@router.get("/auth/login")
def login(request: Request):
    if not auth_enabled():
        return RedirectResponse("/")
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    query = urlencode({
        "client_id": settings.github_client_id,
        "redirect_uri": str(request.url_for("callback")),
        "scope": "read:user",
        "state": state,
    })
    return RedirectResponse(f"https://github.com/login/oauth/authorize?{query}")


@router.get("/auth/callback", name="callback")
async def callback(request: Request, code: str = "", state: str = ""):
    expected = request.session.pop("oauth_state", None)
    if not auth_enabled() or not code or not expected or not secrets.compare_digest(state, expected):
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    async with httpx.AsyncClient(timeout=10) as client:
        tok = await client.post(
            "https://github.com/login/oauth/access_token",
            data={"client_id": settings.github_client_id, "client_secret": settings.github_client_secret,
                  "code": code, "redirect_uri": str(request.url_for("callback"))},
            headers={"Accept": "application/json"},
        )
        access = tok.json().get("access_token")
        if not access:
            raise HTTPException(status_code=400, detail="GitHub rejected the sign-in")
        prof = await client.get("https://api.github.com/user",
                                headers={"Authorization": f"Bearer {access}", "Accept": "application/json"})
    p = prof.json()
    request.session["user"] = {"id": f"gh{p['id']}", "login": p["login"], "name": p.get("name") or p["login"],
                               "avatar_url": p.get("avatar_url", "")}
    return RedirectResponse("/")


@router.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}
