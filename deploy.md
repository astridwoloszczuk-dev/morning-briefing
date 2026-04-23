# Deploy to Hetzner VPS

## Setup

```bash
cd ~/morning-briefing
pip install -r requirements.txt
cp config.example.json config.json
# edit config.json with your keys
```

## Gmail app password

Gmail requires an app-specific password (not your main password):
1. Google Account → Security → 2-Step Verification → App passwords
2. Create one named "Morning Briefing"
3. Use that 16-char password in config.json

## Test run

```bash
python3 briefing.py
```

## Cron (7am Vienna time = UTC+2 in summer, UTC+1 in winter)

```bash
crontab -e
# Add:
0 5 * * 1-5 cd /root/morning-briefing && python3 briefing.py >> /var/log/morning-briefing.log 2>&1
# (5am UTC = 7am CEST / 6am CET)
```
