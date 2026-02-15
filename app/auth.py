import hmac
import logging

from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings

logger = logging.getLogger(__name__)

_serializer = URLSafeTimedSerializer(settings.secret_key)
_SESSION_COOKIE = "session"
_MAX_AGE = 86400  # 24 hours


class AuthRequired(Exception):
    """Raised when a valid session is not present."""


def create_session(username: str, display_name: str) -> str:
    """Create a signed session cookie value."""
    return _serializer.dumps({"username": username, "display_name": display_name})


def verify_session(cookie_value: str) -> dict[str, str] | None:
    """Verify and decode a session cookie. Returns user dict or None."""
    try:
        data = _serializer.loads(cookie_value, max_age=_MAX_AGE)
        return data
    except (BadSignature, SignatureExpired):
        return None


def authenticate(username: str, password: str) -> dict[str, str] | None:
    """Validate credentials against partner accounts. Returns user dict or None."""
    accounts = settings.get_partner_accounts()
    account = accounts.get(username)
    if account and account["password"] and hmac.compare_digest(account["password"], password):
        return {"username": username, "display_name": account["display_name"]}
    return None


async def get_current_user(request: Request) -> dict[str, str]:
    """FastAPI dependency. Returns user dict or raises AuthRequired."""
    cookie = request.cookies.get(_SESSION_COOKIE)
    if not cookie:
        raise AuthRequired()
    user = verify_session(cookie)
    if not user:
        raise AuthRequired()
    return user
