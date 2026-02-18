# RBSoft AutoChat (Render Worker)

This project runs a background worker that:
- discovers connected devices/SIMs from RBSoft SMSGateway
- pairs SIM numbers automatically (works with any count: odd/even, <100 or >100)
- sends a simple "taking news" SMS conversation
- stops after MAX_TURNS messages per conversation
- stores state in a local JSON file (rbsoft_state.json)

## Deploy on Render (Background Worker)

1) Upload this project to GitHub (or upload zip then push).
2) Create a **Background Worker** on Render.
3) Build command:
   `pip install -r requirements.txt`
4) Start command:
   `python rbsoft_auto_chat.py`
5) Set environment variable:
   - `RBSOFT_TOKEN` = your RBSoft API token

Optional env vars:
- MAX_TURNS (default 10)
- POLL_INTERVAL_S (default 4)
- SIM_REFRESH_INTERVAL_S (default 30)
- GLOBAL_SEND_PER_MIN (default 120)
- PER_SIM_SEND_PER_MIN (default 30)

## Notes
- The JSON state file is stored on the service filesystem. If you redeploy/restart, state may reset unless you attach a Persistent Disk.
