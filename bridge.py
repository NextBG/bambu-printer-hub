"""LAN bridge: Bambu printers (MQTT 8883 + camera) <-> printer hub server (outbound WSS only).

printers.json: {"server": "wss://HOST:8446/bridge", "token": "...",
                "printers": [{"id": "h2d", "name": "H2D", "model": "H2D", "ip": "...", "serial": "...",
                              "code": "...", "camera": "rtsps" | "tcp6000", "chamber": false}]}
camera: rtsps = X1/H2D/P2S-style RTSP on :322, tcp6000 = A1/A1 mini/P1 JPEG stream; chamber false hides chamber temp.
Add a printer = add an entry and restart. `python bridge.py --dump h2d` prints the merged raw report.
"""
import argparse, asyncio, io, json, logging, pathlib, socket, ssl, struct, subprocess, sys, threading, time
import aiohttp
import paho.mqtt.client as mqtt

HERE = pathlib.Path(__file__).resolve().parent
log = logging.getLogger("bridge")
CAM_W = 960          # downscale before upload (phones / 4G)
CAM_GRACE = 20       # keep camera open this long after the last viewer leaves
try:
    HMS = json.loads((HERE / "hms_zh.json").read_text("utf-8"))     # built by build_hms.py
except (OSError, ValueError):
    HMS = {}


def explain(serial, kind, code):
    """kind 'hms' (16 hex) / 'err' (8 hex) -> text; model table (serial prefix) first, then default."""
    for t in (HMS.get(serial[:3], {}), HMS.get("default", {})):
        s = t.get(kind, {}).get(code)
        if s:
            return s
    return ""


def tls():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            merge(dst[k], v)
        else:
            dst[k] = v


def temp_pair(v):
    """H2D packs target<<16 | current into one int."""
    if v is None:
        return None, None
    v = int(v)
    return (v & 0xFFFF, v >> 16) if v > 0xFFFF else (v, None)


class Printer:
    def __init__(self, cfg, loop):
        self.c, self.loop = cfg, loop
        self.raw, self.online, self.dirty = {}, False, True
        self.fps, self.cam_until, self.latest = 0.0, 0.0, None
        self.cam_thread = None
        m = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"hub-{cfg['id']}-{int(time.time())}")
        m.username_pw_set("bblp", cfg["code"])
        m.tls_set_context(tls())
        m.reconnect_delay_set(2, 30)
        m.on_connect, m.on_disconnect, m.on_message = self._conn, self._disc, self._msg
        self.m = m
        m.connect_async(cfg["ip"], 8883, 30)
        m.loop_start()
        self.last_push = 0

    # ---- mqtt (paho thread) ----
    def _conn(self, cl, u, f, rc, p=None):
        log.info("%s mqtt connected rc=%s", self.c["id"], rc)
        cl.subscribe(f"device/{self.c['serial']}/report")
        self.pushall()

    def _disc(self, *a):
        log.warning("%s mqtt disconnected", self.c["id"])
        self.online, self.dirty = False, True

    def _msg(self, cl, u, msg):
        try:
            d = json.loads(msg.payload)
        except ValueError:
            return
        merge(self.raw, d)
        self.online = self.dirty = True

    def pushall(self):
        self.last_push = time.time()
        self.publish({"pushing": {"sequence_id": "0", "command": "pushall"}})

    def publish(self, obj):
        return self.m.publish(f"device/{self.c['serial']}/request", json.dumps(obj))

    def command(self, cmd, arg):
        if cmd in ("pause", "resume", "stop"):
            body = {"print": {"sequence_id": "0", "command": cmd, "param": ""}}
        elif cmd == "speed" and str(arg) in "1234":
            body = {"print": {"sequence_id": "0", "command": "print_speed", "param": str(arg)}}
        else:
            return False, f"未知命令 {cmd}"
        r = self.publish(body)
        r.wait_for_publish(5)
        return r.is_published(), ""

    def summary(self):
        p = self.raw.get("print", {})
        dev = p.get("device", {})
        noz = []
        for e in dev.get("extruder", {}).get("info", []) or []:
            cur, tgt = temp_pair(e.get("temp"))
            noz.append([round(cur or 0), tgt and round(tgt)])
        if not noz and "nozzle_temper" in p:
            noz = [[round(p["nozzle_temper"]), round(p.get("nozzle_target_temper", 0))]]
        bed = [p.get("bed_temper"), p.get("bed_target_temper")]
        if bed[0] is None and "bed" in dev:
            bed = list(temp_pair(dev["bed"].get("info", {}).get("temp")))
        ch = p.get("chamber_temper") if self.c.get("chamber", True) else None
        if "ctc" in dev:
            ch = temp_pair(dev["ctc"].get("info", {}).get("temp"))[0]
        hms = []
        for h in p.get("hms", []) or []:
            a, c = int(h.get("attr", 0)), int(h.get("code", 0))
            hms.append([f"{a >> 16:04X}_{a & 0xFFFF:04X}_{c >> 16:04X}_{c & 0xFFFF:04X}",
                        explain(self.c["serial"], "hms", f"{a:08X}{c:08X}")])
        err = int(p.get("print_error") or 0)
        err_text = explain(self.c["serial"], "err", f"{err:08X}") if err else ""
        return {"gcode_state": p.get("gcode_state"), "percent": p.get("mc_percent"),
                "remain_min": p.get("mc_remaining_time"), "layer": p.get("layer_num"),
                "layers": p.get("total_layer_num"), "speed": p.get("spd_lvl"), "task": p.get("subtask_name"),
                "nozzle": noz, "bed": [bed[0] and round(bed[0]), bed[1] and round(bed[1])],
                "chamber": ch and round(ch), "err": err, "err_text": err_text, "hms": hms,
                "stage": p.get("stg_cur"), "wifi": p.get("wifi_signal")}

    # ---- camera (own thread while someone watches) ----
    def want_cam(self):
        return self.fps > 0 or time.time() < self.cam_until

    def ensure_cam(self):
        if self.want_cam() and not (self.cam_thread and self.cam_thread.is_alive()):
            self.cam_thread = threading.Thread(target=self._cam, daemon=True)
            self.cam_thread.start()

    def _cam(self):
        log.info("%s camera on", self.c["id"])
        self.latest = None
        try:
            (self._cam_rtsps if self.c.get("camera") == "rtsps" else self._cam_tcp6000)()
        except Exception as e:
            log.warning("%s camera: %s", self.c["id"], e)
            time.sleep(3)
        log.info("%s camera off", self.c["id"])

    def _cam_tcp6000(self):
        s = tls().wrap_socket(socket.create_connection((self.c["ip"], 6000), 10))
        s.settimeout(15)
        s.sendall(struct.pack("<IIII", 0x40, 0x3000, 0, 0) + b"bblp".ljust(32, b"\0")
                  + self.c["code"].encode().ljust(32, b"\0"))

        def rd(n):
            b = bytearray()
            while len(b) < n:
                k = s.recv(n - len(b))
                if not k:
                    raise ConnectionError("eof")
                b += k
            return bytes(b)
        try:
            while self.want_cam():
                n = struct.unpack("<I", rd(16)[:4])[0]
                self.latest = shrink(rd(n))
        finally:
            s.close()

    def _cam_rtsps(self):
        url = f"rtsps://bblp:{self.c['code']}@{self.c['ip']}:322/streaming/live/1"
        pr = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", url,
                               "-vf", f"fps=2,scale={CAM_W}:-2", "-q:v", "6", "-f", "image2pipe", "-vcodec", "mjpeg",
                               "-"], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
        buf = b""
        try:
            while self.want_cam():
                k = pr.stdout.read1(65536)
                if not k:
                    raise ConnectionError("ffmpeg exited")
                buf += k
                while True:
                    a = buf.find(b"\xff\xd8")
                    e = buf.find(b"\xff\xd9", a + 2)
                    if a < 0 or e < 0:
                        break
                    self.latest, buf = buf[a:e + 2], buf[e + 2:]
        finally:
            pr.kill()
            pr.wait()


def shrink(jpg):
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(jpg))
        if im.width > CAM_W:
            im = im.resize((CAM_W, round(im.height * CAM_W / im.width)))
        out = io.BytesIO()
        im.convert("RGB").save(out, "JPEG", quality=70)
        return out.getvalue()
    except Exception:
        return jpg


async def run(cfg):
    loop = asyncio.get_running_loop()
    ps = {c["id"]: Printer(c, loop) for c in cfg["printers"]}
    backoff = 2
    while True:
        try:
            async with aiohttp.ClientSession() as http, http.ws_connect(
                    cfg["server"], headers={"X-Token": cfg["token"]}, ssl=False, heartbeat=20,
                    max_msg_size=2**20) as ws:
                log.info("connected to %s", cfg["server"])
                backoff = 2
                await ws.send_json({"type": "hello", "printers": [
                    {"id": c["id"], "name": c.get("name", c["id"]), "model": c.get("model", "")} for c in cfg["printers"]]})
                for p in ps.values():
                    p.dirty, p.fps = True, 0.0
                await asyncio.gather(rx(ws, ps), tx(ws, ps))
        except Exception as e:
            log.warning("server link: %r", e)
        for p in ps.values():
            p.fps = 0.0
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


async def rx(ws, ps):
    async for m in ws:
        if m.type != aiohttp.WSMsgType.TEXT:
            continue
        d = json.loads(m.data)
        p = ps.get(d.get("id"))
        if not p:
            continue
        if d["type"] == "watch":
            if p.fps > 0 and d["fps"] == 0:
                p.cam_until = time.time() + CAM_GRACE
            p.fps = float(d["fps"])
            p.ensure_cam()
        elif d["type"] == "cmd":
            log.info("cmd %s %s %s", p.c["id"], d["cmd"], d.get("arg"))
            ok, msg = await asyncio.to_thread(p.command, d["cmd"], d.get("arg"))
            await ws.send_json({"type": "ack", "req": d["req"], "ok": ok, "msg": msg})
            await asyncio.sleep(1)
            p.pushall()
    raise ConnectionError("server closed")


async def tx(ws, ps):
    last_state, last_frame, sent_latest = {}, {}, {}
    while not ws.closed:
        now = time.time()
        for pid, p in ps.items():
            if p.online and now - p.last_push > 600:
                p.pushall()
            if p.dirty and now - last_state.get(pid, 0) >= 1 or now - last_state.get(pid, 0) > 15:
                p.dirty = False
                last_state[pid] = now
                await ws.send_json({"type": "state", "id": pid, "online": p.online, "data": p.summary()})
            p.ensure_cam()
            if p.fps > 0 and p.latest is not None and p.latest is not sent_latest.get(pid) \
                    and now - last_frame.get(pid, 0) >= 1 / p.fps:
                last_frame[pid] = now
                sent_latest[pid] = p.latest
                await ws.send_bytes(pid.encode() + b"\n" + p.latest)
        await asyncio.sleep(0.1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "printers.json"))
    ap.add_argument("--dump", help="print merged raw MQTT report of this printer id and exit")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = json.loads(pathlib.Path(a.config).read_text())
    if a.dump:
        p = Printer(next(c for c in cfg["printers"] if c["id"] == a.dump), None)
        time.sleep(8)
        json.dump({"raw": p.raw, "summary": p.summary()}, sys.stdout, indent=1, default=str)
        return
    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
