import os
from passlib.context import CryptContext
from itsdangerous import URLSafeSerializer, BadSignature

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

SECRET_KEY = os.getenv("SECRET_KEY", "DEV_ONLY_CHANGE_ME")
serializer = URLSafeSerializer(SECRET_KEY, salt="session")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def make_session_token(user_id: int) -> str:
    return serializer.dumps({"user_id": user_id})


def read_session_token(token: str) -> int | None:
    try:
        data = serializer.loads(token)
        return int(data.get("user_id"))
    except (BadSignature, ValueError, TypeError):
        return None
