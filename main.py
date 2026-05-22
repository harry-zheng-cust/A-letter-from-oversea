import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs
from typing import Any
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "letters.json"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="侨批生成器")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

templates = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "xml"]),
)

SESSION_COOKIE = "qiaopi_session"
SHARE_ACCESS_COOKIE_PREFIX = "qiaopi_share_"
LOGIN_VARIANTS = {"classic", "minimal", "poster"}
ENCRYPTED_STORE_FORMAT = "qiaopi.encrypted.v1"
APP_STATE_FORMAT = "qiaopi.app.v1"
SHARE_PASSWORD_ITERATIONS = 210_000
PASSWORD_ITERATIONS = 260_000
BAILIAN_DAILY_LIMIT = 15
APP_TZ = ZoneInfo("Asia/Shanghai")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?\d{6,20}$")
INTERNAL_LETTER_FIELDS = {
    "share_password",
    "share_password_hash",
    "share_password_salt",
    "share_password_iterations",
}


class GenerateRequest(BaseModel):
    cover_text: str = Field(default="平安批", max_length=12)
    recipient: str = Field(default="吾妻", max_length=16)
    sender: str = Field(default="亲启", max_length=16)
    prompt: str = Field(default="", max_length=600)
    blessing: str = Field(default="", max_length=120)


class LetterPayload(BaseModel):
    cover_text: str = Field(default="平安批", max_length=12)
    recipient: str = Field(default="吾妻", max_length=16)
    sender: str = Field(default="亲启", max_length=16)
    body: str = Field(min_length=1, max_length=1800)
    amount: str = Field(default="", max_length=20)
    blessing: str = Field(default="", max_length=120)
    share_password: str = Field(default="", max_length=128)


def default_state() -> dict[str, Any]:
    return {
        "__app_state__": APP_STATE_FORMAT,
        "letters": {},
        "users": {},
        "stats": {
            "daily": {},
            "user_daily": {},
        },
    }


def normalize_state(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and value.get("__app_state__") == APP_STATE_FORMAT:
        state = value
    else:
        state = default_state()
        state["letters"] = value if isinstance(value, dict) else {}

    state.setdefault("__app_state__", APP_STATE_FORMAT)
    state.setdefault("letters", {})
    state.setdefault("users", {})
    state.setdefault("stats", {})
    state["stats"].setdefault("daily", {})
    state["stats"].setdefault("user_daily", {})
    return state


def data_encryption_secret() -> str:
    configured_secret = (
        os.getenv("QIAOPI_DATA_KEY", "").strip()
        or os.getenv("QIAOPI_DATA_SECRET", "").strip()
    )
    if configured_secret:
        return configured_secret
    return f"{session_secret()}:qiaopi-data"


def fernet_key_from_secret(secret: str) -> bytes:
    raw = secret.encode("utf-8")
    try:
        decoded = base64.urlsafe_b64decode(raw)
        if len(decoded) == 32:
            return raw
    except Exception:
        pass

    return base64.urlsafe_b64encode(hashlib.sha256(raw).digest())


def data_cipher() -> Fernet:
    return Fernet(fernet_key_from_secret(data_encryption_secret()))


def encrypted_store(data: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {
        "__format__": ENCRYPTED_STORE_FORMAT,
        "data": data_cipher().encrypt(payload).decode("utf-8"),
    }


def decrypt_store(raw: Any) -> tuple[dict[str, Any], bool]:
    if isinstance(raw, dict) and raw.get("__format__") == ENCRYPTED_STORE_FORMAT:
        try:
            payload = data_cipher().decrypt(str(raw.get("data", "")).encode("utf-8"))
            letters = json.loads(payload.decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="侨批数据解密失败，请检查 QIAOPI_DATA_KEY 是否正确",
            ) from exc

        return (letters if isinstance(letters, dict) else {}), True

    return (raw if isinstance(raw, dict) else {}), False


def read_state() -> dict[str, Any]:
    if not DATA_FILE.exists():
        return default_state()
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default_state()
    data, encrypted = decrypt_store(raw)
    state = normalize_state(data)
    if not encrypted or data.get("__app_state__") != APP_STATE_FORMAT:
        write_state(state)
    return state


def write_state(state: dict[str, Any]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(encrypted_store(normalize_state(state)), ensure_ascii=False, indent=2), encoding="utf-8")


def read_letters() -> dict[str, Any]:
    return read_state()["letters"]


def write_letters(letters: dict[str, Any]) -> None:
    state = read_state()
    state["letters"] = letters
    write_state(state)


def public_url(path: str) -> str:
    base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if base_url:
        return f"{base_url}{path}"
    return path


def admin_username() -> str:
    return os.getenv("QIAOPI_USERNAME", "").strip()


def admin_password() -> str:
    return os.getenv("QIAOPI_PASSWORD", "").strip()


def admin_credentials_configured() -> bool:
    return bool(admin_username() and admin_password())


def auth_configured() -> bool:
    return admin_credentials_configured() or bool(read_state()["users"])


def session_secret() -> str:
    return (
        os.getenv("QIAOPI_SESSION_SECRET", "").strip()
        or admin_password()
        or "qiaopi-dev-secret"
    )


def safe_next_path(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//") and not value.startswith("/login") and not value.startswith("/register"):
        return value
    return "/"


def sign_session(role: str, actor_id: str, issued_at: int) -> str:
    message = f"v2:{role}:{actor_id}:{issued_at}".encode("utf-8")
    return hmac.new(session_secret().encode("utf-8"), message, hashlib.sha256).hexdigest()


def make_session_token(role: str, actor_id: str) -> str:
    issued_at = int(time.time())
    signature = sign_session(role, actor_id, issued_at)
    return f"v2:{role}:{actor_id}:{issued_at}:{signature}"


def hash_password(password: str) -> dict[str, Any]:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return {
        "password_salt": base64.urlsafe_b64encode(salt).decode("ascii"),
        "password_hash": digest.hex(),
        "password_iterations": PASSWORD_ITERATIONS,
    }


def verify_password(password: str, record: dict[str, Any]) -> bool:
    try:
        salt = base64.urlsafe_b64decode(str(record["password_salt"]).encode("ascii"))
        expected = bytes.fromhex(str(record["password_hash"]))
        iterations = int(record.get("password_iterations", PASSWORD_ITERATIONS))
    except (KeyError, ValueError, TypeError, binascii.Error):
        return False

    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return secrets.compare_digest(actual, expected)


def normalize_identity(value: str) -> str:
    cleaned = value.strip()
    if "@" in cleaned:
        return cleaned.lower()
    return re.sub(r"[\s\-()]+", "", cleaned)


def classify_identity(value: str) -> str:
    if EMAIL_RE.match(value):
        return "email"
    if PHONE_RE.match(value):
        return "phone"
    return "invalid"


def normalize_user_record(user: dict[str, Any]) -> dict[str, Any]:
    user.setdefault("id", uuid.uuid4().hex[:12])
    user.setdefault("identity", "")
    user.setdefault("identity_type", "unknown")
    user.setdefault("role", "user")
    user.setdefault("created_at", datetime.utcnow().isoformat(timespec="seconds") + "Z")
    user.setdefault("generated_body_count", 0)
    user.setdefault("generated_share_count", 0)
    user.setdefault("last_generate_at", "")
    user.setdefault("last_share_at", "")
    user.setdefault("last_login_at", "")
    return user


def get_user_by_identity(identity: str) -> dict[str, Any] | None:
    normalized = normalize_identity(identity)
    for user in read_state()["users"].values():
        if normalize_identity(str(user.get("identity", ""))) == normalized:
            return normalize_user_record(dict(user))
    return None


def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    user = read_state()["users"].get(user_id)
    return normalize_user_record(dict(user)) if isinstance(user, dict) else None


def update_user_record(user: dict[str, Any]) -> None:
    state = read_state()
    state["users"][user["id"]] = normalize_user_record(dict(user))
    write_state(state)


def session_identity(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None

    parts = token.split(":")
    if len(parts) == 5 and parts[0] == "v2":
        _, role, actor_id, issued_at_text, signature = parts
        try:
            issued_at = int(issued_at_text)
        except ValueError:
            return None

        max_age = int(os.getenv("QIAOPI_SESSION_MAX_AGE", "604800"))
        if int(time.time()) - issued_at > max_age:
            return None

        expected_signature = sign_session(role, actor_id, issued_at)
        if not secrets.compare_digest(signature, expected_signature):
            return None

        if role == "admin":
            if not admin_credentials_configured():
                return None
            return {"role": "admin", "actor_id": actor_id, "identity": admin_username()}
        if role == "user":
            user = get_user_by_id(actor_id)
            if not user:
                return None
            return {
                "role": "user",
                "actor_id": user["id"],
                "identity": user["identity"],
                "identity_type": user.get("identity_type", "unknown"),
            }
        return None

    if len(parts) == 3:
        token_username, issued_at_text, signature = parts
        try:
            issued_at = int(issued_at_text)
        except ValueError:
            return None

        max_age = int(os.getenv("QIAOPI_SESSION_MAX_AGE", "604800"))
        if int(time.time()) - issued_at > max_age:
            return None

        message = f"{token_username}:{issued_at}".encode("utf-8")
        expected_signature = hmac.new(session_secret().encode("utf-8"), message, hashlib.sha256).hexdigest()
        if not secrets.compare_digest(signature, expected_signature):
            return None
        if not admin_credentials_configured():
            return None
        if not secrets.compare_digest(token_username, admin_username()):
            return None
        return {"role": "admin", "actor_id": "admin", "identity": admin_username()}

    return None


def current_actor(request: Request) -> dict[str, Any] | None:
    if not auth_configured():
        return {"role": "guest", "actor_id": "guest", "identity": "guest", "identity_type": "guest", "unlimited": True}
    return session_identity(request.cookies.get(SESSION_COOKIE))


def is_authenticated(request: Request) -> bool:
    return current_actor(request) is not None


def current_actor_or_raise(request: Request) -> dict[str, Any]:
    actor = current_actor(request)
    if actor is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="需要登录后访问侨批生成器")
    return actor


def attach_session_cookie(response: RedirectResponse, actor: dict[str, Any]) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        make_session_token(actor["role"], actor["actor_id"]),
        max_age=int(os.getenv("QIAOPI_SESSION_MAX_AGE", "604800")),
        httponly=True,
        samesite="lax",
        secure=cookie_secure(),
    )


def verify_credentials(identity: str, password: str) -> dict[str, Any] | None:
    if admin_credentials_configured() and secrets.compare_digest(identity, admin_username()) and secrets.compare_digest(password, admin_password()):
        return {"role": "admin", "actor_id": "admin", "identity": admin_username(), "identity_type": "admin"}

    user = get_user_by_identity(identity)
    if user and verify_password(password, user):
        user["last_login_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        update_user_record(user)
        return {
            "role": "user",
            "actor_id": user["id"],
            "identity": user["identity"],
            "identity_type": user.get("identity_type", "unknown"),
        }
    return None


def verify_generator_access(request: Request) -> dict[str, Any]:
    return current_actor_or_raise(request)


def cookie_secure() -> bool:
    return os.getenv("QIAOPI_COOKIE_SECURE", "").lower() == "true"


def share_access_secret() -> str:
    return os.getenv("QIAOPI_SHARE_SECRET", "").strip() or f"{session_secret()}:qiaopi-share"


def share_access_max_age() -> int:
    return int(os.getenv("QIAOPI_SHARE_ACCESS_MAX_AGE", os.getenv("QIAOPI_SESSION_MAX_AGE", "604800")))


def hash_share_password(password: str) -> dict[str, Any]:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        SHARE_PASSWORD_ITERATIONS,
    )
    return {
        "share_password_salt": base64.urlsafe_b64encode(salt).decode("ascii"),
        "share_password_hash": digest.hex(),
        "share_password_iterations": SHARE_PASSWORD_ITERATIONS,
    }


def letter_requires_password(letter: dict[str, Any]) -> bool:
    return bool(letter.get("share_password_hash") or letter.get("share_password_salt"))


def verify_share_password(letter: dict[str, Any], password: str) -> bool:
    if not letter_requires_password(letter):
        return True

    try:
        salt = base64.urlsafe_b64decode(str(letter["share_password_salt"]).encode("ascii"))
        expected = bytes.fromhex(str(letter["share_password_hash"]))
        iterations = int(letter.get("share_password_iterations", SHARE_PASSWORD_ITERATIONS))
    except (KeyError, ValueError, TypeError, binascii.Error):
        return False

    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return secrets.compare_digest(actual, expected)


def public_letter(letter: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in letter.items() if key not in INTERNAL_LETTER_FIELDS}


def share_access_cookie_name(letter_id: str) -> str:
    safe_id = "".join(ch for ch in letter_id if ch.isalnum())[:48] or "letter"
    return f"{SHARE_ACCESS_COOKIE_PREFIX}{safe_id}"


def sign_share_access(letter_id: str, expires_at: int, letter: dict[str, Any]) -> str:
    message = f"{letter_id}:{expires_at}".encode("utf-8")
    secret = f"{share_access_secret()}:{letter.get('share_password_hash', '')}".encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def make_share_access_token(letter_id: str, letter: dict[str, Any]) -> str:
    expires_at = int(time.time()) + share_access_max_age()
    signature = sign_share_access(letter_id, expires_at, letter)
    return f"{expires_at}:{signature}"


def share_access_granted(request: Request, letter_id: str, letter: dict[str, Any]) -> bool:
    if not letter_requires_password(letter):
        return True

    token = request.cookies.get(share_access_cookie_name(letter_id))
    if not token:
        return False

    try:
        expires_at_text, signature = token.split(":", 1)
        expires_at = int(expires_at_text)
    except ValueError:
        return False

    if expires_at < int(time.time()):
        return False

    expected_signature = sign_share_access(letter_id, expires_at, letter)
    return secrets.compare_digest(signature, expected_signature)


def grant_share_access(response: RedirectResponse, letter_id: str, letter: dict[str, Any]) -> None:
    response.set_cookie(
        share_access_cookie_name(letter_id),
        make_share_access_token(letter_id, letter),
        max_age=share_access_max_age(),
        httponly=True,
        samesite="lax",
        secure=cookie_secure(),
    )


def today_key() -> str:
    return datetime.now(APP_TZ).date().isoformat()


def actor_key(actor: dict[str, Any]) -> str:
    return f"{actor.get('role', 'user')}:{actor.get('actor_id', actor.get('identity', 'unknown'))}"


def is_admin_actor(actor: dict[str, Any] | None) -> bool:
    return bool(actor and actor.get("role") == "admin")


def event_bucket(container: dict[str, Any], key: str) -> dict[str, int]:
    bucket = container.setdefault(key, {})
    bucket.setdefault("generate_count", 0)
    bucket.setdefault("share_count", 0)
    return bucket


def user_day_bucket(state: dict[str, Any], actor: dict[str, Any], day: str) -> dict[str, int]:
    per_user = state["stats"].setdefault("user_daily", {}).setdefault(actor_key(actor), {})
    return event_bucket(per_user, day)


def quota_status(actor: dict[str, Any], state: dict[str, Any] | None = None) -> dict[str, Any]:
    state = state or read_state()
    day = today_key()
    used = int(user_day_bucket(state, actor, day).get("generate_count", 0))
    if actor.get("role") in {"admin", "guest"} or actor.get("unlimited"):
        return {"limit": None, "used": used, "remaining": None, "unlimited": True}

    remaining = max(0, BAILIAN_DAILY_LIMIT - used)
    return {"limit": BAILIAN_DAILY_LIMIT, "used": used, "remaining": remaining, "unlimited": False}


def increment_event(state: dict[str, Any], actor: dict[str, Any], event_name: str) -> None:
    day = today_key()
    event_bucket(state["stats"].setdefault("daily", {}), day)[event_name] += 1
    user_day_bucket(state, actor, day)[event_name] += 1

    if actor.get("role") == "user":
        user = state["users"].get(actor["actor_id"])
        if isinstance(user, dict):
            if event_name == "generate_count":
                user["generated_body_count"] = int(user.get("generated_body_count", 0)) + 1
                user["last_generate_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            if event_name == "share_count":
                user["generated_share_count"] = int(user.get("generated_share_count", 0)) + 1
                user["last_share_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"


def consume_generate_quota(actor: dict[str, Any]) -> dict[str, Any]:
    state = read_state()
    quota = quota_status(actor, state)
    if not quota["unlimited"] and quota["remaining"] <= 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="今日百炼正文生成次数已用完，请明天再试",
        )

    increment_event(state, actor, "generate_count")
    write_state(state)
    return quota_status(actor, state)


def record_share_event(actor: dict[str, Any], state: dict[str, Any]) -> None:
    increment_event(state, actor, "share_count")


def user_context(actor: dict[str, Any] | None) -> dict[str, Any]:
    if not actor:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "identity": actor.get("identity", ""),
        "role": actor.get("role", "user"),
        "is_admin": is_admin_actor(actor),
        "quota": quota_status(actor),
    }


def admin_summary() -> dict[str, Any]:
    state = read_state()
    rows: list[dict[str, Any]] = []
    day = today_key()

    admin_actor = {"role": "admin", "actor_id": "admin", "identity": admin_username() or "admin"}
    actors = [admin_actor]
    actors.extend(
        {
            "role": "user",
            "actor_id": user_id,
            "identity": normalize_user_record(dict(user)).get("identity", ""),
            "identity_type": normalize_user_record(dict(user)).get("identity_type", "unknown"),
        }
        for user_id, user in state["users"].items()
        if isinstance(user, dict)
    )

    for actor in actors:
        key = actor_key(actor)
        per_days = state["stats"].get("user_daily", {}).get(key, {})
        total_generate = sum(int(item.get("generate_count", 0)) for item in per_days.values() if isinstance(item, dict))
        total_share = sum(int(item.get("share_count", 0)) for item in per_days.values() if isinstance(item, dict))
        today_stats = per_days.get(day, {}) if isinstance(per_days, dict) else {}
        rows.append(
            {
                "identity": actor.get("identity", ""),
                "role": "管理员" if actor.get("role") == "admin" else "用户",
                "identity_type": actor.get("identity_type", "admin"),
                "today_generate": int(today_stats.get("generate_count", 0)),
                "today_share": int(today_stats.get("share_count", 0)),
                "total_generate": total_generate,
                "total_share": total_share,
                "remaining": "不限" if actor.get("role") == "admin" else max(0, BAILIAN_DAILY_LIMIT - int(today_stats.get("generate_count", 0))),
            }
        )

    daily_share_totals = [
        {"date": date, "share_count": int(item.get("share_count", 0)), "generate_count": int(item.get("generate_count", 0))}
        for date, item in sorted(state["stats"].get("daily", {}).items(), reverse=True)
        if isinstance(item, dict)
    ]
    return {"rows": rows, "daily_share_totals": daily_share_totals, "today": day}


def fallback_letter(payload: GenerateRequest) -> str:
    recipient = payload.recipient or "吾妻"
    sender = payload.sender or "远人"
    detail = payload.prompt or "家书抵万金，愿以侨批传平安、寄相思。"
    blessing = f"末了再添一句：{payload.blessing}" if payload.blessing else "愿阖家安康，岁岁平安。"
    return (
        f"{recipient}亲启：\n"
        f"离乡数载，海风隔岸，时时念及家中灯火。{detail}\n"
        "近来一切尚安，劳作虽辛，幸有同乡相互照应，衣食无缺。"
        "所寄银钱虽薄，皆是寸心，望可添补家用，勿为我多虑。\n"
        f"{blessing}\n"
        f"{sender}谨上"
    )


async def call_bailian(payload: GenerateRequest) -> str:
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key or api_key == "your-api-key-here":
        return fallback_letter(payload)

    model = os.getenv("DASHSCOPE_MODEL", "qwen-plus")
    system = (
        "你是一位熟悉岭南侨批文化的中文书信作者。"
        "请生成一封短而真挚的侨批正文，语言含蓄、家常、带旧式家书气质。"
        "不要输出标题，不要使用 Markdown，分段自然，控制在 260 字以内。"
    )
    user = (
        f"封面大字：{payload.cover_text}\n"
        f"收信人：{payload.recipient}\n"
        f"落款：{payload.sender}\n"
        f"用户补充：{payload.prompt or '表达平安、思念、寄银钱补贴家用'}\n"
        f"祝福语：{payload.blessing or '阖家安康'}"
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.75,
                },
            )
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"].strip()
            return content or fallback_letter(payload)
    except Exception:
        return fallback_letter(payload)


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
):
    actor = current_actor(request)
    if actor is None:
        return RedirectResponse(url=f"/login?next={request.url.path}", status_code=status.HTTP_303_SEE_OTHER)

    template = templates.get_template("qiaopi.html")
    return HTMLResponse(
        template.render(
            request=request,
            share=None,
            share_json="null",
            user_json=json.dumps(user_context(actor), ensure_ascii=False),
            user=user_context(actor),
            protected_share=False,
            locked_share=False,
            letter_id="",
            unlock_error="",
        )
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/") -> HTMLResponse:
    return render_login_page(request, safe_next_path(next), variant="classic", mode="login")


@app.get("/login/{variant}", response_class=HTMLResponse)
async def login_variant_page(request: Request, variant: str, next: str = "/") -> HTMLResponse:
    return render_login_page(request, safe_next_path(next), variant=variant, mode="login")


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, next: str = "/") -> HTMLResponse:
    return render_login_page(request, safe_next_path(next), variant="classic", mode="register")


@app.post("/login")
async def login_submit(request: Request):
    body = (await request.body()).decode("utf-8")
    form = parse_qs(body)
    username = form.get("username", [""])[0].strip()
    password = form.get("password", [""])[0]
    next_path = safe_next_path(form.get("next", ["/"])[0])
    variant = form.get("variant", ["classic"])[0]

    actor = verify_credentials(username, password)
    if actor:
        response = RedirectResponse(url=next_path, status_code=status.HTTP_303_SEE_OTHER)
        attach_session_cookie(response, actor)
        return response

    return render_login_page(
        request,
        next_path,
        variant=variant,
        mode="login",
        error="用户名或密码不正确",
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


@app.post("/register")
async def register_submit(request: Request):
    body = (await request.body()).decode("utf-8")
    form = parse_qs(body)
    raw_identity = form.get("identity", [""])[0].strip()
    password = form.get("password", [""])[0]
    password_confirm = form.get("password_confirm", [""])[0]
    next_path = safe_next_path(form.get("next", ["/"])[0])
    variant = form.get("variant", ["classic"])[0]
    identity = normalize_identity(raw_identity)
    identity_type = classify_identity(identity)

    if identity_type == "invalid":
        return render_login_page(
            request,
            next_path,
            variant=variant,
            mode="register",
            error="请输入有效的手机号码或邮箱",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if len(password) < 6:
        return render_login_page(
            request,
            next_path,
            variant=variant,
            mode="register",
            error="密码至少需要 6 位",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if password != password_confirm:
        return render_login_page(
            request,
            next_path,
            variant=variant,
            mode="register",
            error="两次输入的密码不一致",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if admin_credentials_configured() and secrets.compare_digest(identity, normalize_identity(admin_username())):
        return render_login_page(
            request,
            next_path,
            variant=variant,
            mode="register",
            error="该账号为管理员账号，请直接登录",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    state = read_state()
    if any(normalize_identity(str(user.get("identity", ""))) == identity for user in state["users"].values() if isinstance(user, dict)):
        return render_login_page(
            request,
            next_path,
            variant=variant,
            mode="register",
            error="该手机号码或邮箱已注册",
            status_code=status.HTTP_409_CONFLICT,
        )

    user_id = uuid.uuid4().hex[:12]
    user = {
        "id": user_id,
        "identity": identity,
        "identity_type": identity_type,
        "role": "user",
        "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "last_login_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        **hash_password(password),
    }
    state["users"][user_id] = normalize_user_record(user)
    write_state(state)

    actor = {"role": "user", "actor_id": user_id, "identity": identity, "identity_type": identity_type}
    response = RedirectResponse(url=next_path, status_code=status.HTTP_303_SEE_OTHER)
    attach_session_cookie(response, actor)
    return response


@app.get("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    actor = current_actor(request)
    if actor is None:
        return RedirectResponse(url=f"/login?next={request.url.path}", status_code=status.HTTP_303_SEE_OTHER)
    if not is_admin_actor(actor):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")

    template = templates.get_template("admin.html")
    return HTMLResponse(template.render(request=request, summary=admin_summary(), user=actor))


@app.get("/s/{letter_id}", response_class=HTMLResponse)
async def share_page(request: Request, letter_id: str) -> HTMLResponse:
    letters = read_letters()
    letter = letters.get(letter_id)
    if not letter:
        raise HTTPException(status_code=404, detail="侨批不存在")
    unlocked = share_access_granted(request, letter_id, letter)
    share = public_letter(letter) if unlocked else None
    template = templates.get_template("qiaopi.html")
    return HTMLResponse(
        template.render(
            request=request,
            share=share,
            share_json=json.dumps(share, ensure_ascii=False) if share else "null",
            user_json=json.dumps({"authenticated": False}, ensure_ascii=False),
            user=None,
            protected_share=letter_requires_password(letter),
            locked_share=letter_requires_password(letter) and not unlocked,
            letter_id=letter_id,
            unlock_error="",
        )
    )


@app.post("/s/{letter_id}/unlock")
async def unlock_share(request: Request, letter_id: str):
    letters = read_letters()
    letter = letters.get(letter_id)
    if not letter:
        raise HTTPException(status_code=404, detail="侨批不存在")

    body = (await request.body()).decode("utf-8")
    form = parse_qs(body)
    password = form.get("password", [""])[0]
    if verify_share_password(letter, password):
        response = RedirectResponse(url=f"/s/{letter_id}", status_code=status.HTTP_303_SEE_OTHER)
        grant_share_access(response, letter_id, letter)
        return response

    template = templates.get_template("qiaopi.html")
    return HTMLResponse(
        template.render(
            request=request,
            share=None,
            share_json="null",
            user_json=json.dumps({"authenticated": False}, ensure_ascii=False),
            user=None,
            protected_share=True,
            locked_share=True,
            letter_id=letter_id,
            unlock_error="密码不正确",
        ),
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


@app.post("/api/generate")
async def generate(
    payload: GenerateRequest,
    actor: dict[str, Any] = Depends(verify_generator_access),
) -> dict[str, Any]:
    quota = consume_generate_quota(actor)
    return {"body": await call_bailian(payload), "quota": quota}


@app.post("/api/letters")
async def create_letter(
    payload: LetterPayload,
    actor: dict[str, Any] = Depends(verify_generator_access),
) -> dict[str, Any]:
    state = read_state()
    letters = state["letters"]
    letter_id = uuid.uuid4().hex[:10]
    letter = payload.model_dump()
    share_password = letter.pop("share_password", "").strip()
    if share_password:
        letter.update(hash_share_password(share_password))
    letters[letter_id] = {
        **letter,
        "id": letter_id,
        "owner_role": actor.get("role", "user"),
        "owner_id": actor.get("actor_id", ""),
        "owner_identity": actor.get("identity", ""),
        "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    record_share_event(actor, state)
    write_state(state)
    path = f"/s/{letter_id}"
    return {
        "id": letter_id,
        "path": path,
        "url": public_url(path),
        "password_protected": letter_requires_password(letters[letter_id]),
        "quota": quota_status(actor),
    }


@app.get("/api/letters/{letter_id}")
async def get_letter(request: Request, letter_id: str) -> dict[str, Any]:
    letter = read_letters().get(letter_id)
    if not letter:
        raise HTTPException(status_code=404, detail="侨批不存在")
    if not share_access_granted(request, letter_id, letter):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="需要分享密码")
    return public_letter(letter)


def render_login_page(
    request: Request,
    next_path: str,
    variant: str = "classic",
    mode: str = "login",
    error: str = "",
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse | RedirectResponse:
    if is_authenticated(request):
        return RedirectResponse(url=next_path, status_code=status.HTTP_303_SEE_OTHER)

    safe_variant = variant if variant in LOGIN_VARIANTS else "classic"
    safe_mode = mode if mode in {"login", "register"} else "login"
    template = templates.get_template("login.html")
    return HTMLResponse(
        template.render(
            request=request,
            next_path=next_path,
            variant=safe_variant,
            mode=safe_mode,
            error=error,
        ),
        status_code=status_code,
    )
