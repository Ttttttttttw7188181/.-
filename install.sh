#!/bin/bash
apt update && apt install -y python3.11 python3.11-venv software-properties-common git || \
(add-apt-repository ppa:deadsnakes/ppa -y && apt update && apt install -y python3.11 python3.11-venv)

git clone https://github.com/Ttttttttttw7188181/.- /root/wdtt-bot 2>/dev/null || \
  (cd /root/wdtt-bot && git pull)

cd /root/wdtt-bot
python3.11 -m venv venv
venv/bin/pip install aiogram==3.7.0 paramiko==3.4.0

read -p "Введи BOT_TOKEN: " TOKEN
python3 -c "
import re
with open('bot.py','r') as f: c=f.read()
c=re.sub(r'YOUR_BOT_TOKEN_HERE', '$TOKEN', c)
with open('bot.py','w') as f: f.write(c)
"

cat > /etc/systemd/system/wdtt-bot.service <<EOF
[Unit]
Description=WDTT Bot
After=network.target

[Service]
WorkingDirectory=/root/wdtt-bot
ExecStart=/root/wdtt-bot/venv/bin/python3 bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable wdtt-bot
systemctl restart wdtt-bot
sleep 3
journalctl -u wdtt-bot -n 10 --no-pager