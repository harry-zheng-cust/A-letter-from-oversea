import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs
from typing import Any

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
LOGIN_VARIANTS = {"classic", "minimal", "poster"}


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


def read_letters() -> dict[str, Any]:
    if not DATA_FILE.exists():
        return {}
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def write_letters(letters: dict[str, Any]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(letters, ensure_ascii=False, indent=2), encoding="utf-8")


def public_url(path: str) -> str:
    base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if base_url:
        return f"{base_url}{path}"
    return path


def auth_configured() -> bool:
    return bool(os.getenv("QIAOPI_USERNAME", "").strip() or os.getenv("QIAOPI_PASSWORD", "").strip())


def session_secret() -> str:
    return (
        os.getenv("QIAOPI_SESSION_SECRET", "").strip()
        or os.getenv("QIAOPI_PASSWORD", "").strip()
        or "qiaopi-dev-secret"
    )


def safe_next_path(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//") and not value.startswith("/login"):
        return value
    return "/"


def sign_session(username: str, issued_at: int) -> str:
    message = f"{username}:{issued_at}".encode("utf-8")
    return hmac.new(session_secret().encode("utf-8"), message, hashlib.sha256).hexdigest()


def make_session_token(username: str) -> str:
    issued_at = int(time.time())
    signature = sign_session(username, issued_at)
    return f"{username}:{issued_at}:{signature}"


def verify_session_token(token: str | None) -> bool:
    username = os.getenv("QIAOPI_USERNAME", "").strip()
    if not auth_configured():
        return True
    if not token or not username:
        return False

    try:
        token_username, issued_at_text, signature = token.split(":", 2)
        issued_at = int(issued_at_text)
    except ValueError:
        return False

    max_age = int(os.getenv("QIAOPI_SESSION_MAX_AGE", "604800"))
    if int(time.time()) - issued_at > max_age:
        return False

    expected_signature = sign_session(token_username, issued_at)
    return secrets.compare_digest(token_username, username) and secrets.compare_digest(signature, expected_signature)


def is_authenticated(request: Request) -> bool:
    return verify_session_token(request.cookies.get(SESSION_COOKIE))


def verify_credentials(username: str, password: str) -> bool:
    expected_username = os.getenv("QIAOPI_USERNAME", "").strip()
    expected_password = os.getenv("QIAOPI_PASSWORD", "").strip()
    if not auth_configured():
        return True
    return (
        secrets.compare_digest(username, expected_username)
        and secrets.compare_digest(password, expected_password)
    )


def verify_generator_access(request: Request) -> None:
    if is_authenticated(request):
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="需要登录后访问侨批生成器",
    )


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
    if not is_authenticated(request):
        return RedirectResponse(url=f"/login?next={request.url.path}", status_code=status.HTTP_303_SEE_OTHER)

    template = templates.get_template("qiaopi.html")
    return HTMLResponse(template.render(request=request, share=None, share_json="null"))


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/") -> HTMLResponse:
    return render_login_page(request, safe_next_path(next), variant="classic")


@app.get("/login/{variant}", response_class=HTMLResponse)
async def login_variant_page(request: Request, variant: str, next: str = "/") -> HTMLResponse:
    return render_login_page(request, safe_next_path(next), variant=variant)


@app.post("/login")
async def login_submit(request: Request):
    body = (await request.body()).decode("utf-8")
    form = parse_qs(body)
    username = form.get("username", [""])[0].strip()
    password = form.get("password", [""])[0]
    next_path = safe_next_path(form.get("next", ["/"])[0])
    variant = form.get("variant", ["classic"])[0]

    if verify_credentials(username, password):
        response = RedirectResponse(url=next_path, status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            SESSION_COOKIE,
            make_session_token(username),
            max_age=int(os.getenv("QIAOPI_SESSION_MAX_AGE", "604800")),
            httponly=True,
            samesite="lax",
            secure=os.getenv("QIAOPI_COOKIE_SECURE", "").lower() == "true",
        )
        return response

    return render_login_page(
        request,
        next_path,
        variant=variant,
        error="用户名或密码不正确",
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


@app.get("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/s/{letter_id}", response_class=HTMLResponse)
async def share_page(request: Request, letter_id: str) -> HTMLResponse:
    letters = read_letters()
    letter = letters.get(letter_id)
    if not letter:
        raise HTTPException(status_code=404, detail="侨批不存在")
    template = templates.get_template("qiaopi.html")
    return HTMLResponse(
        template.render(
            request=request,
            share=letter,
            share_json=json.dumps(letter, ensure_ascii=False),
        )
    )


@app.post("/api/generate")
async def generate(
    payload: GenerateRequest,
    _: None = Depends(verify_generator_access),
) -> dict[str, str]:
    return {"body": await call_bailian(payload)}


@app.post("/api/letters")
async def create_letter(
    payload: LetterPayload,
    _: None = Depends(verify_generator_access),
) -> dict[str, str]:
    letters = read_letters()
    letter_id = uuid.uuid4().hex[:10]
    letters[letter_id] = {
        **payload.model_dump(),
        "id": letter_id,
        "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    write_letters(letters)
    path = f"/s/{letter_id}"
    return {"id": letter_id, "path": path, "url": public_url(path)}


@app.get("/api/letters/{letter_id}")
async def get_letter(letter_id: str) -> dict[str, Any]:
    letter = read_letters().get(letter_id)
    if not letter:
        raise HTTPException(status_code=404, detail="侨批不存在")
    return letter


def render_login_page(
    request: Request,
    next_path: str,
    variant: str = "classic",
    error: str = "",
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse | RedirectResponse:
    if is_authenticated(request):
        return RedirectResponse(url=next_path, status_code=status.HTTP_303_SEE_OTHER)

    safe_variant = variant if variant in LOGIN_VARIANTS else "classic"
    template = templates.get_template("login.html")
    return HTMLResponse(
        template.render(
            request=request,
            next_path=next_path,
            variant=safe_variant,
            error=error,
        ),
        status_code=status_code,
    )
