import httpx
from nicegui import app

from app.core.config import settings


class APIError(RuntimeError):
    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message)
        self.status_code = status_code


def _raise_response(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        detail = response.json().get("detail", response.text)
        if isinstance(detail, dict):
            detail = detail.get("message", str(detail))
    except Exception:
        detail = response.text
    raise APIError(str(detail), response.status_code)


def request(method: str, path: str, *, json=None, params=None, files=None, data=None, authenticated: bool = True):
    headers = {}
    token = app.storage.user.get("access_token") if authenticated else None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{settings.api_internal_url.rstrip('/')}{path}"
    try:
        with httpx.Client(timeout=25) as client:
            response = client.request(method, url, json=json, params=params, files=files, data=data, headers=headers)
            if response.status_code == 401 and authenticated:
                refresh_token = app.storage.user.get("refresh_token")
                if refresh_token:
                    refreshed = client.post(f"{settings.api_internal_url.rstrip('/')}/api/auth/refresh", json={"refresh_token": refresh_token})
                    if refreshed.is_success:
                        result = refreshed.json()
                        app.storage.user["access_token"] = result["access_token"]
                        app.storage.user["refresh_token"] = result["refresh_token"]
                        headers["Authorization"] = f"Bearer {result['access_token']}"
                        response = client.request(method, url, json=json, params=params, files=files, data=data, headers=headers)
        _raise_response(response)
        if not response.content:
            return None
        return response.json()
    except httpx.RequestError as exc:
        raise APIError(f"Backend unavailable: {exc.__class__.__name__}", 503) from exc


def get(path: str, **kwargs):
    return request("GET", path, **kwargs)


def post(path: str, **kwargs):
    return request("POST", path, **kwargs)


def patch(path: str, **kwargs):
    return request("PATCH", path, **kwargs)


def delete(path: str, **kwargs):
    return request("DELETE", path, **kwargs)
