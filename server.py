"""
server.py — Flask WebApp server
Serves webapp/index.html and REST API for profile, tariffs, stats.
Runs in a daemon thread alongside bot.py.
"""

import logging
import os
import time

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder="webapp", static_url_path="")
CORS(app)


# ── Serve WebApp ──────────────────────────────────────────────────────────────
@app.route("/")
@app.route("/webapp/")
@app.route("/webapp/index.html")
def index():
    return send_from_directory("webapp", "index.html")


# ── Health ────────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"ok": True, "ts": int(time.time())})


# ── API: User profile ─────────────────────────────────────────────────────────
@app.route("/api/profile")
def api_profile():
    from db import Database
    from xui_api import XUIApi

    db  = Database()
    xui = XUIApi()

    user_id = request.args.get("user_id", type=int)
    if not user_id:
        return jsonify({"error": "no user_id"}), 400

    user = db.get_user(user_id)
    if not user:
        return jsonify({"error": "user not found"}), 404

    now_ms    = int(time.time() * 1000)
    sub_end   = user.get("sub_end", 0)
    active    = bool(sub_end and sub_end > now_ms)
    days_left = max(0, int((sub_end - now_ms) / 86_400_000)) if active else 0

    traffic_up = traffic_down = 0.0
    if user.get("client_email"):
        stats = xui.get_client_stats(user["client_email"])
        if stats:
            traffic_up   = round(stats.get("up",   0) / (1024 ** 3), 2)
            traffic_down = round(stats.get("down", 0) / (1024 ** 3), 2)

    ref_stats = db.get_referral_stats(user_id)
    payments  = db.get_user_payments(user_id)

    return jsonify({
        "user_id":      user_id,
        "username":     user.get("username"),
        "uuid":         user.get("uuid", ""),
        "client_email": user.get("client_email", ""),
        "sub_end":      sub_end,
        "sub_active":   active,
        "days_left":    days_left,
        "trial_used":   bool(user.get("trial_used")),
        "balance":      round(user.get("balance", 0), 4),
        "traffic_up":   traffic_up,
        "traffic_down": traffic_down,
        "ref_count":    ref_stats["count"],
        "ref_earned":   round(ref_stats["earned"], 4),
        "payments":     payments,
        "created_at":   user.get("created_at", 0),
    })


# ── API: Tariffs ──────────────────────────────────────────────────────────────
@app.route("/api/tariffs")
def api_tariffs():
    from db import Database
    return jsonify(Database().get_all_tariffs())


# ── API: Stats (admin) ────────────────────────────────────────────────────────
@app.route("/api/admin/stats")
def api_admin_stats():
    admin_token = os.getenv("ADMIN_API_TOKEN", "")
    if admin_token and request.args.get("token") != admin_token:
        return jsonify({"error": "forbidden"}), 403
    from db import Database
    return jsonify(Database().get_stats())


# ── Run ───────────────────────────────────────────────────────────────────────
def run_server(host: str = "0.0.0.0", port: int = 8080):
    logger.info("WebApp server starting on %s:%s", host, port)
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
