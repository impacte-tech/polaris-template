"""Small dashboard around Polaris. Iceberg authentication stays native to Polaris."""
import asyncio
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import quote

import httpx
from starlette.applications import Starlette
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse, RedirectResponse, StreamingResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.background import BackgroundTask

from config import settings

config = settings()
templates = Jinja2Templates(directory="templates")
attempts = {}
HOP = {"host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailer", "transfer-encoding", "upgrade"}


@asynccontextmanager
async def lifespan(app):
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8181", timeout=60) as client:
        app.state.client = client
        yield


def session_ok(request):
    return request.session.get("expires", 0) > time.time()


def page(request, name, **context):
    return templates.TemplateResponse(request, name, context,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                 "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin"})


async def health(request):
    try:
        response = await request.app.state.client.get("http://127.0.0.1:8182/q/health/ready", timeout=3)
        healthy = response.status_code == 200
    except httpx.HTTPError:
        healthy = False
    return JSONResponse({"status": "ready" if healthy else "unavailable"}, status_code=200 if healthy else 503)


async def login(request):
    if request.method == "GET":
        request.session["csrf"] = secrets.token_urlsafe(32)
        return page(request, "login.html", csrf=request.session["csrf"], error=None)
    form = await request.form()
    if not secrets.compare_digest(str(form.get("csrf", "")), request.session.get("csrf", "missing")):
        return JSONResponse({"error": "Reload the login page and try again"}, status_code=403)
    # Bound in-memory login throttling. Restarting the service clears the counters.
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    for ip in list(attempts):
        if attempts[ip][0] < now - 60:
            del attempts[ip]
    start, count = attempts.get(key, (now, 0))
    if count >= 10 or (key not in attempts and len(attempts) >= 10000):
        return JSONResponse({"error": "Too many login attempts. Try again in a minute."}, status_code=429)
    attempts[key] = (start, count + 1)
    try:
        response = await request.app.state.client.post("/api/catalog/v1/oauth/tokens", data={
            "grant_type": "client_credentials", "client_id": config["client"],
            "client_secret": str(form.get("password", "")), "scope": "PRINCIPAL_ROLE:ALL"})
        if response.status_code == 200:
            token = response.json()
            request.session.clear()
            request.session.update(token=token["access_token"], expires=time.time() + token.get("expires_in", 3600) - 10,
                                   csrf=secrets.token_urlsafe(32))
            return RedirectResponse("/", status_code=303)
    except httpx.HTTPError:
        return page(request, "login.html", csrf=request.session["csrf"], error="Catalog is unavailable. Try again shortly.")
    await asyncio.sleep(0.5)
    return page(request, "login.html", csrf=request.session["csrf"], error="The password was not accepted.")


async def logout(request):
    form = await request.form()
    if not secrets.compare_digest(str(form.get("csrf", "")), request.session.get("csrf", "missing")):
        return JSONResponse({"error": "Invalid session"}, status_code=403)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


async def dashboard(request):
    if not session_ok(request):
        return RedirectResponse("/login", status_code=303)
    headers = {"Authorization": "Bearer " + request.session["token"]}
    namespaces, tables, error = [], [], None
    selected = request.query_params.get("namespace", "")
    path = "/api/catalog/v1/" + quote(config["name"], safe="")
    try:
        response = await request.app.state.client.get(path + "/namespaces", headers=headers)
        if response.status_code == 401:
            request.session.clear()
            return RedirectResponse("/login", status_code=303)
        response.raise_for_status()
        namespaces = response.json().get("namespaces", [])
        if selected:
            response = await request.app.state.client.get(path + "/namespaces/" + quote(selected, safe="") + "/tables", headers=headers)
            response.raise_for_status()
            tables = response.json().get("identifiers", [])
    except httpx.HTTPError:
        error = "Could not load catalog contents. Check the service logs and try again."
    return page(request, "index.html", config=config, namespaces=namespaces, tables=tables,
                selected=selected, error=error, csrf=request.session["csrf"])


async def proxy(request):
    # Preserve escaped namespace separators and query strings; never forward dashboard cookies.
    path = request.scope["raw_path"].decode("ascii")
    query = request.scope.get("query_string", b"").decode("ascii")
    url = "http://127.0.0.1:8181" + path + ("?" + query if query else "")
    connection_tokens = {x.strip().lower() for x in request.headers.get("connection", "").split(",")}
    blocked = HOP | connection_tokens | {"cookie", "forwarded", "x-forwarded-host", "x-forwarded-proto"}
    headers = [(k, v) for k, v in request.headers.items() if k.lower() not in blocked]
    try:
        upstream = request.app.state.client.build_request(request.method, url, headers=headers, content=request.stream())
        response = await request.app.state.client.send(upstream, stream=True)
    except httpx.HTTPError:
        return JSONResponse({"error": {"message": "Catalog unavailable", "type": "ServiceUnavailableException", "code": 503}}, status_code=503)
    blocked_response = HOP | {x.strip().lower() for x in response.headers.get("connection", "").split(",")}
    result = StreamingResponse(response.aiter_raw(), status_code=response.status_code,
        background=BackgroundTask(response.aclose))
    result.raw_headers = [(k, v) for k, v in response.headers.raw if k.decode().lower() not in blocked_response]
    return result


app = Starlette(lifespan=lifespan, routes=[
    Route("/health", health), Route("/login", login, methods=["GET", "POST"]),
    Route("/logout", logout, methods=["POST"]), Route("/", dashboard),
    Mount("/static", StaticFiles(directory="static"), name="static"),
    Route("/api/catalog/{path:path}", proxy, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]),
    Route("/api/management/{path:path}", proxy, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]),
])
app.add_middleware(SessionMiddleware, secret_key=config["signing"], session_cookie="polaris_session",
                   max_age=3600, same_site="strict", https_only=config["public"].startswith("https://"))
