# bambu-printer-hub

A tiny self-hosted web dashboard for Bambu Lab printers (H2D, X1, P1, A1, A1 mini …) that you can open from anywhere — phone or desktop.

极简的拓竹打印机远程监控网页：实时状态、摄像头画面、暂停 / 继续 / 停止 / 调速，HMS 与错误码自动翻译成中文。手机和电脑都能用，加打印机只需改一个 JSON。

```
browser ──HTTPS/WSS──▶ server.py (public VPS) ◀──WSS (outbound only)── bridge.py (LAN) ──MQTT 8883 / camera──▶ printers
```

* **No port forwarding at home**: the bridge only makes an outbound connection to the server, so printers are never exposed.
* **Status**: state, task, progress, remaining time / ETA, layers, nozzle (dual) / bed / chamber temps, print error and HMS codes with Chinese text (link to the Bambu wiki).
* **Control**: pause, resume, stop (with confirm), speed level (silent / standard / sport / ludicrous).
* **Camera, bandwidth-aware**: a printer's camera is opened only while someone is watching it (+20 s grace). Cards visible on screen refresh at 0.5 fps, full-screen at 2 fps, background tabs at 0. Frames are downscaled to 960 px before upload.
  * `rtsps` – X1 / H2D / P2S style RTSP on port 322 (needs `ffmpeg` on the bridge host)
  * `tcp6000` – A1 / A1 mini / P1 JPEG stream on port 6000
* Single password login (HMAC cookie, 30 days). Plain Python, no build step, no database.

Uses the printers' **LAN mode MQTT** (access code from the printer screen); "LAN only" mode is not required.

## Files

| file | runs on | what |
|---|---|---|
| `server.py` | public server | aiohttp web + WebSocket hub, serves `index.html` |
| `index.html` | browser | the whole UI (vanilla JS) |
| `bridge.py` | a machine on the printer LAN | MQTT + camera ↔ server |
| `build_hms.py` | bridge host | builds `hms_zh.json` (HMS / error code → Chinese) from Bambu's public data |
| `run_bridge.sh` | bridge host | restart loop for the bridge |
| `deploy/printer-hub.service` | public server | systemd unit |

## Setup

### 1. Server (public VPS, Python ≥ 3.9)

```bash
git clone https://github.com/NextBG/bambu-printer-hub ~/printer-hub && cd ~/printer-hub
pip install aiohttp                       # or: apt install python3-aiohttp
cp secrets.example.env secrets.env && chmod 600 secrets.env   # fill in random values
# TLS: use an existing Let's Encrypt cert of a domain pointing here, or self-signed:
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -keyout key.pem -out cert.pem -subj "/CN=your.domain.com"
sed "s/USER/$USER/g" deploy/printer-hub.service | sudo tee /etc/systemd/system/printer-hub.service
sudo systemctl daemon-reload && sudo systemctl enable --now printer-hub
```

Open TCP 8446 in the firewall / cloud security group. Then browse to `https://your.domain.com:8446`.

Using a certbot certificate: copy `fullchain.pem` / `privkey.pem` into the folder in a deploy hook (`/etc/letsencrypt/renewal-hooks/deploy/printer-hub.sh`) and `systemctl restart printer-hub`, then point `HUB_CERT` / `HUB_KEY` at the copies.
The login cookie is `Secure`, so serve over HTTPS (directly, or plain HTTP behind an HTTPS reverse proxy).

### 2. Bridge (any always-on Linux box on the printer LAN)

```bash
git clone https://github.com/NextBG/bambu-printer-hub ~/printer-hub && cd ~/printer-hub
python3 -m venv venv && venv/bin/pip install -r requirements.txt   # ffmpeg needed for rtsps cameras
cp printers.example.json printers.json && chmod 600 printers.json  # server URL, token, printers
venv/bin/python build_hms.py                                       # optional: Chinese HMS / error texts
venv/bin/python bridge.py --dump h2d                               # sanity check: prints the raw MQTT report
tmux new -d -s pbridge ./run_bridge.sh
(crontab -l; echo "@reboot sleep 30; tmux new -d -s pbridge $HOME/printer-hub/run_bridge.sh") | crontab -
```

### Add a printer

Add an entry to `printers.json` (`id`, `name`, `model`, `ip`, `serial`, LAN access `code`, `camera`; `"chamber": false` hides the chamber temperature on printers without a sensor) and restart the bridge (`pkill -f "bridge.py$"`; `run_bridge.sh` restarts it).
Several bridges (e.g. different sites) can connect to the same server as long as printer ids are unique.

## Notes

* Speed levels map to Bambu `print_speed` 1–4 (50 / 100 / 124 / 166 %).
* HMS text lookup: model-specific table (by serial prefix, e.g. `094` = H2D) from Bambu Studio's `resources/hms`, falling back to Bambu's cloud error DB. Re-run `build_hms.py` occasionally for new codes.
* Not affiliated with Bambu Lab.

## License

MIT
