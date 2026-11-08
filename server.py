"""Printer hub server (runs on a public server).

Browsers  <-- HTTPS/WSS :PORT -->  server  <-- WSS /bridge (outbound from LAN) --  bridge.py (LAN, talks to printers)

Env: HUB_PASSWORD, HUB_BRIDGE_TOKEN, HUB_COOKIE_KEY, HUB_PORT (8446), HUB_CERT, HUB_KEY
"""
import asyncio, hashlib, hmac, json, logging, os, pathlib, ssl, time
from aiohttp import web, WSMsgType

HERE = pathlib.Path(__file__).resolve().parent
PW = os.environ["HUB_PASSWORD"]
BTOK = os.environ["HUB_BRIDGE_TOKEN"]
KEY = os.environ["HUB_COOKIE_KEY"].encode()
log = logging.getLogger("hub")

printers = {}      # id -> {"meta": {...}, "state": {...}, "online": bool, "ts": float}
owner = {}         # id -> bridge ws
frames = {}        # id -> (seq, ts, jpeg bytes)
browsers = {}      # browser ws -> {id: fps}
sent_fps = {}      # id -> fps last sent to bridge
pending = {}       # req id -> browser ws
_req = 0


def sign(exp):
    return hmac.new(KEY, str(exp).encode(), hashlib.sha256).hexdigest()


def authed(req):
    try:
        exp, sig = req.cookies.get("ph", "").split(".")
        return int(exp) > time.time() and hmac.compare_digest(sig, sign(exp))
    except ValueError:
        return False


async def send(ws, obj):
    if not ws.closed:
        try:
            await ws.send_str(json.dumps(obj, separators=(",", ":")))
        except ConnectionError:
            pass


def public(pid):
    p = printers[pid]
    f = frames.get(pid)
    return {"id": pid, **p["meta"], "online": p["online"], "state": p["state"], "ts": p["ts"],
            "frame": {"seq": f[0], "ts": f[1]} if f else None}


async def broadcast(obj, only=None):
    for ws, watch in list(browsers.items()):
        if only is None or watch.get(only):
            await send(ws, obj)


async def sync_watch():
    want = {}
    for watch in browsers.values():
        for pid, fps in watch.items():
            want[pid] = max(want.get(pid, 0), float(fps))
    for pid, b in list(owner.items()):
        fps = want.get(pid, 0.0)
        if sent_fps.get(pid) != fps:
            sent_fps[pid] = fps
            await send(b, {"type": "watch", "id": pid, "fps": fps})


# ---------------- pages ----------------
LOGIN = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>打印机</title>
<style>body{background:#111;color:#eee;font:16px system-ui;display:grid;place-items:center;height:90vh}
input,button{font-size:18px;padding:10px;border-radius:8px;border:1px solid #444;background:#222;color:#eee;margin:4px}</style>
<form method=post action=login><input type=password name=pw placeholder=密码 autofocus><button>进入</button>%s</form>"""


async def login_get(req):
    return web.Response(text=LOGIN % "", content_type="text/html")


async def login_post(req):
    pw = (await req.post()).get("pw", "")
    if not hmac.compare_digest(pw.encode(), PW.encode()):
        await asyncio.sleep(1.5)
        return web.Response(text=LOGIN % "<p style=color:#f66>密码错误</p>", content_type="text/html", status=401)
    exp = int(time.time()) + 30 * 86400
    r = web.HTTPFound("./")
    r.set_cookie("ph", f"{exp}.{sign(exp)}", max_age=30 * 86400, httponly=True, secure=True, samesite="Lax")
    raise r


async def index(req):
    if not authed(req):
        raise web.HTTPFound("login")
    return web.FileResponse(HERE / "index.html", headers={"Cache-Control": "no-cache"})


async def snap(req):
    if not authed(req):
        raise web.HTTPUnauthorized()
    f = frames.get(req.match_info["id"])
    if not f:
        raise web.HTTPNotFound()
    return web.Response(body=f[2], content_type="image/jpeg",
                        headers={"Cache-Control": "no-store", "X-Frame-Ts": str(f[1])})


# ---------------- browser ws ----------------
async def ws_browser(req):
    global _req
    if not authed(req):
        raise web.HTTPUnauthorized()
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(req)
    browsers[ws] = {}
    await send(ws, {"type": "all", "printers": [public(p) for p in printers]})
    try:
        async for m in ws:
            if m.type != WSMsgType.TEXT:
                continue
            d = json.loads(m.data)
            if d.get("type") == "watch":
                browsers[ws] = {k: min(float(v), 3.0) for k, v in d.get("ids", {}).items()
                                if k in printers and float(v) > 0}
                await sync_watch()
            elif d.get("type") == "cmd":
                pid = d.get("id")
                b = owner.get(pid)
                if not b or b.closed:
                    await send(ws, {"type": "ack", "ok": False, "msg": "打印机离线"})
                    continue
                _req += 1
                pending[_req] = ws
                log.info("cmd %s %s %s from %s", pid, d.get("cmd"), d.get("arg"),
                         req.headers.get("X-Forwarded-For", req.remote))
                await send(b, {"type": "cmd", "req": _req, "id": pid, "cmd": d.get("cmd"), "arg": d.get("arg")})
    finally:
        browsers.pop(ws, None)
        await sync_watch()
    return ws


# ---------------- bridge ws ----------------
async def ws_bridge(req):
    if not hmac.compare_digest(req.headers.get("X-Token", "").encode(), BTOK.encode()):
        raise web.HTTPUnauthorized()
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=8 * 2**20)
    await ws.prepare(req)
    mine = set()
    log.info("bridge connected %s", req.remote)
    try:
        async for m in ws:
            if m.type == WSMsgType.BINARY:
                pid, _, jpg = m.data.partition(b"\n")
                pid = pid.decode()
                if pid in mine:
                    seq = frames[pid][0] + 1 if pid in frames else 1
                    frames[pid] = (seq, time.time(), jpg)
                    await broadcast({"type": "frame", "id": pid, "seq": seq, "ts": frames[pid][1]}, only=pid)
                continue
            if m.type != WSMsgType.TEXT:
                continue
            d = json.loads(m.data)
            t = d.get("type")
            if t == "hello":
                for meta in d["printers"]:
                    pid = meta.pop("id")
                    mine.add(pid)
                    owner[pid] = ws
                    sent_fps.pop(pid, None)
                    printers.setdefault(pid, {"state": {}, "online": False, "ts": 0})["meta"] = meta
                await broadcast({"type": "all", "printers": [public(p) for p in printers]})
                await sync_watch()
            elif t == "state" and d.get("id") in mine:
                p = printers[d["id"]]
                p.update(state=d["data"], online=d["online"], ts=time.time())
                await broadcast({"type": "state", "id": d["id"], "online": d["online"], "state": d["data"],
                                 "ts": p["ts"]})
            elif t == "ack":
                b = pending.pop(d.get("req"), None)
                if b:
                    await send(b, {"type": "ack", "ok": d.get("ok"), "msg": d.get("msg", "")})
    finally:
        log.info("bridge gone %s", req.remote)
        for pid in mine:
            if owner.get(pid) is ws:
                owner.pop(pid)
                sent_fps.pop(pid, None)
                printers[pid]["online"] = False
                await broadcast({"type": "state", "id": pid, "online": False, "state": printers[pid]["state"],
                                 "ts": printers[pid]["ts"]})
    return ws


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/login", login_get)
    app.router.add_post("/login", login_post)
    app.router.add_get("/ws", ws_browser)
    app.router.add_get("/bridge", ws_bridge)
    app.router.add_get("/snap/{id}", snap)
    ctx = None
    if os.environ.get("HUB_CERT"):
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(os.environ["HUB_CERT"], os.environ["HUB_KEY"])
    web.run_app(app, port=int(os.environ.get("HUB_PORT", 8446)), ssl_context=ctx, access_log=None)


if __name__ == "__main__":
    main()
