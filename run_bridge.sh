#!/bin/bash
# LAN bridge supervisor (tmux session "pbridge"; started @reboot from crontab)
cd "$(dirname "$0")"
while true; do venv/bin/python bridge.py >> bridge.log 2>&1; sleep 5; done