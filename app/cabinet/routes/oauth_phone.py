"""Phone login wired into the cabinet's OAuth flow — without touching the frontend.

The cabinet's login page builds its provider buttons from ``GET /auth/oauth/providers``
and, on click, walks the usual OAuth dance: fetch an authorize URL, redirect the
browser there, then hand ``code`` + ``state`` back to ``/auth/oauth/<provider>/callback``.

Phone verification is not OAuth, but it fits that dance exactly, so we reuse it:

    /auth/oauth/phone/authorize  -> URL of a page WE serve (number input + countdown)
    /auth/oauth/phone/page       -> that page
    /auth/oauth/phone/callback   -> exchanges a one-time code for a session

Why this file instead of extending the existing OAuth routes: their provider
argument is a ``Literal`` guarded by a start-up check that it matches the OAuth
account-link columns in the database. Adding 'phone' there would mean inventing a
column for something that is not an account link. Registering these literal paths
*before* the parameterised ``/auth/oauth/{provider}/...`` router keeps upstream files
untouched — FastAPI matches routes in registration order.

The browser never learns which service places the calls: it only ever sees our
own opaque identifiers.
"""

import secrets

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.user import create_user_by_phone, get_user_by_phone
from app.utils.cache import cache

from ..auth.flashcall import mask_phone
from ..dependencies import get_cabinet_db
from ..schemas.auth import AuthResponse
from .auth import _create_auth_response, _store_refresh_token

logger = structlog.get_logger(__name__)

# Префикс обязан совпадать с OAuth-роутером кабинета ('/auth/oauth'), иначе
# фронтенд позовёт /auth/oauth/phone/authorize и попадёт в параметризованный
# роут, который наш литерал не знает и ответит 422.
router = APIRouter(prefix='/auth/oauth/phone', tags=['Cabinet Phone Auth'])

#: One-time codes are short-lived: they only have to survive a redirect.
EXCHANGE_TTL_SECONDS = 120


def _ensure_enabled() -> None:
    if not settings.PHONE_AUTH_ENABLED:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Phone authentication is disabled')


class AuthorizeResponse(BaseModel):
    authorize_url: str
    state: str


class CallbackBody(BaseModel):
    code: str = Field(min_length=1, max_length=128)
    state: str = Field(min_length=1, max_length=128)


@router.get('/authorize', response_model=AuthorizeResponse)
async def authorize():
    """Hand the cabinet the URL of our number-entry page.

    Built from ``CABINET_URL``, not from the incoming request: behind nginx the
    request sees plain http and no ``/api`` prefix, so a URL derived from it
    lands in the SPA instead of the bot.

    Serving the page on the cabinet's own origin (through its ``/api`` proxy)
    also removes the cross-origin hop entirely — the final redirect back to
    ``/auth/oauth/callback`` becomes a same-origin one.
    """
    _ensure_enabled()
    state = secrets.token_urlsafe(24)
    base = (settings.CABINET_URL or '').rstrip('/')
    return AuthorizeResponse(
        authorize_url=f'{base}/api/cabinet/auth/oauth/phone/page?state={state}',
        state=state,
    )


@router.post('/exchange')
async def exchange(body: dict, db: AsyncSession = Depends(get_cabinet_db)):
    """Called by our page once the call is confirmed: issue a one-time code.

    Tokens cannot be handed over here — the page lives on the bot's host while
    the cabinet stores its session on the client domain. The code travels in the
    redirect and is spent by ``/callback`` on the cabinet's side.
    """
    _ensure_enabled()
    session_id = str(body.get('session_id', ''))[:64]
    state = str(body.get('state', ''))[:128]
    if not session_id or not state:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'session_id and state are required')

    from ..auth.flashcall import poll_verification

    phone, confirmed = await poll_verification(db, session_id)
    if not confirmed:
        raise HTTPException(status.HTTP_202_ACCEPTED, 'Ожидаем звонок')

    code = secrets.token_urlsafe(32)
    await cache.set(f'phone_auth:code:{code}', {'phone': phone, 'state': state}, EXCHANGE_TTL_SECONDS)
    return {'code': code}


@router.post('/callback', response_model=AuthResponse)
async def callback(body: CallbackBody, db: AsyncSession = Depends(get_cabinet_db)):
    """Spend the one-time code and return the same session shape as email login."""
    _ensure_enabled()

    key = f'phone_auth:code:{body.code}'
    payload = await cache.get(key)
    if not payload or payload.get('state') != body.state:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Ссылка устарела, начните вход заново')
    await cache.delete(key)  # single use

    phone = payload['phone']
    user = await get_user_by_phone(db, phone)
    if user is None:
        user = await create_user_by_phone(db, phone)
        logger.info('Cabinet user registered by phone', user_id=user.id, phone=mask_phone(phone))

    response = await _create_auth_response(user, db)
    await _store_refresh_token(db, user.id, response.refresh_token)
    return response


_PAGE = r"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Вход по номеру телефона</title>
<style>
 *{box-sizing:border-box} body{margin:0;min-height:100vh;display:flex;align-items:center;
   justify-content:center;background:#0b0f17;color:#e6e9ef;
   font:16px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
 .card{width:100%;max-width:380px;padding:28px;background:#131926;border-radius:16px}
 h1{margin:0 0 6px;font-size:20px} p{margin:0 0 20px;color:#8b93a7;font-size:14px}
 input{width:100%;padding:14px;font-size:18px;border-radius:10px;border:1px solid #2a3346;
   background:#0e1420;color:#fff;letter-spacing:.5px}
 button{width:100%;margin-top:12px;padding:14px;font-size:16px;font-weight:600;border:0;
   border-radius:10px;background:#2563eb;color:#fff;cursor:pointer}
 button:disabled{opacity:.5;cursor:default}
 .dial{font-size:26px;font-weight:700;letter-spacing:1px;text-align:center;margin:8px 0 4px}
 .hint{text-align:center;color:#8b93a7;font-size:14px}
 .timer{text-align:center;font-size:15px;color:#f59e0b;margin-top:10px}
 .err{color:#f87171;font-size:14px;margin-top:10px;min-height:20px}
 a.call{display:block;text-align:center;margin-top:14px;color:#60a5fa;text-decoration:none}
 .hidden{display:none}
</style></head><body>
<div class="card">
  <div id="step1">
    <h1>Вход по номеру</h1>
    <p>Введите номер телефона — мы покажем, куда позвонить. Звонок бесплатный, отвечать не нужно.</p>
    <input id="phone" type="tel" inputmode="tel" placeholder="+7 999 123-45-67" autocomplete="tel">
    <button id="go">Продолжить</button>
    <div class="err" id="err1"></div>
  </div>

  <div id="step2" class="hidden">
    <h1>Позвоните на номер</h1>
    <p>Звонок сбросится сам — просто дождитесь соединения.</p>
    <div class="dial" id="dial"></div>
    <a class="call" id="calllink" href="#">Позвонить</a>
    <div class="timer" id="timer"></div>
    <div class="hint" id="wait">Ждём звонок…</div>
    <div class="err" id="err2"></div>
  </div>
</div>
<script>
const qs = new URLSearchParams(location.search);
const state = qs.get('state') || '';
let sessionId = null, timer = null;

async function post(url, body) {
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
                             body: JSON.stringify(body)});
  return {status: r.status, data: await r.json().catch(() => ({}))};
}

document.getElementById('go').onclick = async () => {
  const btn = document.getElementById('go'); const err = document.getElementById('err1');
  err.textContent = ''; btn.disabled = true;
  const {status, data} = await post('/api/cabinet/auth/phone/call',
                                    {phone: document.getElementById('phone').value});
  btn.disabled = false;
  if (status !== 200) { err.textContent = data.detail || 'Не удалось начать проверку'; return; }

  sessionId = data.session_id;
  document.getElementById('dial').textContent = data.dial_number;
  document.getElementById('calllink').href = 'tel:' + data.dial_number.replace(/[^+\d]/g,'');
  document.getElementById('step1').classList.add('hidden');
  document.getElementById('step2').classList.remove('hidden');
  startCountdown(data.expires_in);
  poll();
};

function startCountdown(seconds) {
  const el = document.getElementById('timer');
  let left = seconds;
  timer = setInterval(() => {
    left--; el.textContent = left > 0 ? 'Осталось ' + left + ' сек' : '';
    if (left <= 0) {
      clearInterval(timer);
      document.getElementById('wait').textContent = '';
      document.getElementById('err2').textContent = 'Время истекло. Обновите страницу и попробуйте снова.';
    }
  }, 1000);
}

async function poll() {
  // Опрос каждые 2 секунды: окно всего минута, чаще незачем, реже — потеряем время.
  const {status, data} = await post('/api/cabinet/auth/oauth/phone/exchange', {session_id: sessionId, state});
  if (status === 200 && data.code) {
    clearInterval(timer);
    // Относительный путь: страница и кабинет на одном origin.
    location.href = '/auth/oauth/callback?code=' + encodeURIComponent(data.code) +
                    '&state=' + encodeURIComponent(state);
    return;
  }
  if (status === 410 || status === 400) {
    clearInterval(timer);
    document.getElementById('wait').textContent = '';
    document.getElementById('err2').textContent = data.detail || 'Проверка не удалась';
    return;
  }
  setTimeout(poll, 2000);
}
</script></body></html>"""


@router.get('/page', response_class=HTMLResponse)
async def page():
    """Number entry and countdown.

    Served by the bot but reachable on the cabinet's origin through its ``/api``
    proxy, so the frontend stays untouched and nothing crosses origins.
    """
    _ensure_enabled()
    return HTMLResponse(_PAGE)
