"""
Google Smart Home Action Fulfillment
=====================================
Handles SYNC, QUERY, EXECUTE intents from Google Assistant.
Reports occupancy state to Google Home Graph API in real-time.

Setup:
  1. Create a Google Cloud project and enable HomeGraph API
  2. Create a Service Account with 'Home Graph API Writer' role
  3. Download the JSON key as service-account.json in this directory
  4. Set PROJECT_ID below
  5. Create an Actions Console project with Smart Home Action
  6. Set fulfillment URL to https://<your-domain>/smarthome

Usage:
  from google_home import google_home, report_state, request_sync
  app.register_blueprint(google_home)
"""
import json
import time
import threading
import secrets
import requests
from urllib.parse import urlencode
from flask import Blueprint, request, jsonify, redirect, render_template_string

google_home = Blueprint('google_home', __name__)

# ─── Configuration ───────────────────────────────────────────────
# Set these before running
PROJECT_ID = "wave-sense-05bdc"
SERVICE_ACCOUNT_KEY_PATH = "service-account.json"  # Path to service account JSON
DEVICE_ID = "wavesense-room-1"
AGENT_USER_ID = "wavesense-user-1"

# ─── Internal state ──────────────────────────────────────────────
_last_reported_state = None
_access_token = None
_token_expiry = 0
_token_lock = threading.Lock()

# ─── OAuth state (in-memory, for testing) ────────────────────────
_oauth_codes = {}      # code -> {client_id, redirect_uri, timestamp}
_oauth_tokens = {}     # token -> {user_id, timestamp}
OAUTH_CLIENT_ID = "wavesense-client"
OAUTH_CLIENT_SECRET = "wavesense-secret"
OAUTH_USER_ID = "wavesense-user-1"


# ─── Device SYNC Response ────────────────────────────────────────
def _get_device_sync(request_id: str) -> dict:
    """Return device definition for SYNC intent."""
    return {
        "requestId": request_id,
        "payload": {
            "agentUserId": AGENT_USER_ID,
            "devices": [{
                "id": DEVICE_ID,
                "type": "action.devices.types.SWITCH",
                "traits": ["action.devices.traits.OnOff"],
                "name": {
                    "name": "Room Occupancy",
                    "defaultNames": ["WaveSense Room", "Occupancy Switch"],
                    "nicknames": ["room", "occupancy", "wavesense", "office"]
                },
                "willReportState": True,
                "attributes": {},
                "roomHint": "Office",
                "deviceInfo": {
                    "manufacturer": "WaveSense",
                    "model": "CSI-Occupancy-v1",
                    "hwVersion": "1.0",
                    "swVersion": "7.3"
                }
            }]
        }
    }


# ─── Device QUERY Response ──────────────────────────────────────
def _get_device_query(request_id: str, occupied: bool) -> dict:
    """Return device state for QUERY intent."""
    return {
        "requestId": request_id,
        "payload": {
            "devices": {
                DEVICE_ID: {
                    "on": occupied,
                    "online": True,
                    "status": "SUCCESS"
                }
            }
        }
    }


# ─── JWT Access Token ───────────────────────────────────────────
def _get_access_token() -> str | None:
    """Generate JWT access token for Home Graph API using service account."""
    global _access_token, _token_expiry

    with _token_lock:
        if _access_token and time.time() < _token_expiry - 60:
            return _access_token

        try:
            with open(SERVICE_ACCOUNT_KEY_PATH) as f:
                key = json.load(f)
        except FileNotFoundError:
            print("[GoogleHome] WARNING: service-account.json not found — "
                  "Google Home state reporting disabled")
            return None
        except json.JSONDecodeError:
            print("[GoogleHome] ERROR: service-account.json is not valid JSON")
            return None

        try:
            # Use PyJWT to create signed JWT
            import jwt
            now = int(time.time())
            payload = {
                "iss": key["client_email"],
                "scope": "https://www.googleapis.com/auth/homegraph",
                "aud": "https://oauth2.googleapis.com/token",
                "iat": now,
                "exp": now + 3600,
            }
            signed_jwt = jwt.encode(payload, key["private_key"], algorithm="RS256")

            resp = requests.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": signed_jwt,
                },
                timeout=10
            )

            if resp.status_code == 200:
                _access_token = resp.json()["access_token"]
                _token_expiry = now + 3600
                return _access_token
            else:
                print(f"[GoogleHome] Token error: {resp.status_code} {resp.text[:200]}")
                return None

        except ImportError:
            print("[GoogleHome] ERROR: PyJWT not installed. pip install PyJWT")
            return None
        except Exception as e:
            print(f"[GoogleHome] Token generation error: {e}")
            return None


# ─── Report State to Home Graph ─────────────────────────────────
def report_state(occupied: bool) -> bool:
    """
    Push device state to Google Home Graph API.
    Call this whenever occupancy changes.
    Returns True if successful, False otherwise.
    """
    global _last_reported_state

    # Skip if state hasn't changed
    if occupied == _last_reported_state:
        return True

    token = _get_access_token()
    if not token:
        return False

    try:
        resp = requests.post(
            "https://homegraph.googleapis.com/v1/devices:reportStateAndNotification",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "requestId": f"wavesense-{int(time.time() * 1000)}",
                "agentUserId": AGENT_USER_ID,
                "payload": {
                    "devices": {
                        "states": {
                            DEVICE_ID: {
                                "on": occupied,
                                "online": True
                            }
                        }
                    }
                }
            },
            timeout=10
        )

        if resp.status_code == 200:
            _last_reported_state = occupied
            state_str = "ON (occupied)" if occupied else "OFF (empty)"
            print(f"[GoogleHome] State reported -> {state_str}")
            return True
        else:
            print(f"[GoogleHome] Report error: {resp.status_code} {resp.text[:200]}")
            return False

    except requests.RequestException as e:
        print(f"[GoogleHome] Report request failed: {e}")
        return False


# ─── Request Sync ────────────────────────────────────────────────
def request_sync() -> bool:
    """
    Tell Google to re-sync the device list.
    Call this once after initial setup or when devices change.
    """
    token = _get_access_token()
    if not token:
        return False

    try:
        resp = requests.post(
            "https://homegraph.googleapis.com/v1/devices:requestSync",
            headers={"Authorization": f"Bearer {token}"},
            json={"agentUserId": AGENT_USER_ID},
            timeout=10
        )
        if resp.status_code == 200:
            print("[GoogleHome] RequestSync sent successfully")
            return True
        else:
            print(f"[GoogleHome] RequestSync error: {resp.status_code} {resp.text[:200]}")
            return False
    except requests.RequestException as e:
        print(f"[GoogleHome] RequestSync failed: {e}")
        return False


# ─── Fulfillment Endpoints ───────────────────────────────────────
@google_home.route('/smarthome', methods=['POST'])
def smarthome_fulfillment():
    """
    Main Smart Home fulfillment endpoint.
    Google sends intents here: SYNC, QUERY, EXECUTE.
    """
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "invalid request"}), 400

    inputs = body.get("inputs", [])
    if not inputs:
        return jsonify({"error": "no inputs"}), 400

    intent = inputs[0].get("intent", "")
    request_id = body.get("requestId", "")

    print(f"[GoogleHome] Fulfillment intent: {intent}")

    # ── SYNC: Return device list ──
    if intent == "action.devices.SYNC":
        return jsonify(_get_device_sync(request_id))

    # ── QUERY: Return current device state ──
    elif intent == "action.devices.QUERY":
        try:
            from app import csi
            occupied = csi.is_occupied()
        except Exception:
            occupied = False
        return jsonify(_get_device_query(request_id, occupied))

    # ── EXECUTE: Handle commands ──
    elif intent == "action.devices.EXECUTE":
        # For a read-only sensor, we acknowledge the command
        # but don't actually change state
        try:
            exec_commands = inputs[0].get("payload", {}).get("commands", [])
            if exec_commands:
                execution = exec_commands[0].get("execution", [{}])[0]
                command = execution.get("command", "")
                params = execution.get("params", {})
                device_ids = [d["id"] for d in exec_commands[0].get("devices", [])]

                # Handle OnOff command
                if command == "action.devices.commands.OnOff":
                    return jsonify({
                        "requestId": request_id,
                        "payload": {
                            "commands": [{
                                "ids": device_ids,
                                "status": "SUCCESS",
                                "states": {
                                    "on": params.get("on", False),
                                    "online": True
                                }
                            }]
                        }
                    })
        except Exception as e:
            print(f"[GoogleHome] EXECUTE error: {e}")

        return jsonify({
            "requestId": request_id,
            "payload": {
                "commands": [{
                    "ids": [DEVICE_ID],
                    "status": "ERROR",
                    "errorCode": "functionNotSupported"
                }]
            }
        })

    # ── DISCONNECT: Handle account unlink ──
    elif intent == "action.devices.DISCONNECT":
        return jsonify({})

    return jsonify({"error": f"unknown intent: {intent}"}), 400


# ─── OAuth Endpoints (for Account Linking) ───────────────────────
OAUTH_LOGIN_PAGE = """
<!DOCTYPE html>
<html>
<head><title>WaveSense - Account Linking</title></head>
<body style="font-family:Arial,sans-serif;max-width:400px;margin:60px auto;text-align:center;">
  <h2>🔗 WaveSense Account Linking</h2>
  <p>Click below to link your WaveSense account to Google Home.</p>
  <form method="POST" action="/oauth/authorize">
    <input type="hidden" name="client_id" value="{{ client_id }}">
    <input type="hidden" name="redirect_uri" value="{{ redirect_uri }}">
    <input type="hidden" name="state" value="{{ state }}">
    <input type="hidden" name="response_type" value="{{ response_type }}">
    <button type="submit" style="padding:12px 32px;font-size:16px;background:#4285F4;color:white;border:none;border-radius:4px;cursor:pointer;">
      Link WaveSense Account
    </button>
  </form>
</body>
</html>
"""

@google_home.route('/oauth/authorize', methods=['GET', 'POST'])
def oauth_authorize():
    """OAuth2 authorization endpoint."""
    if request.method == 'GET':
        # Show the login/consent page
        client_id = request.args.get('client_id', '')
        redirect_uri = request.args.get('redirect_uri', '')
        state = request.args.get('state', '')
        response_type = request.args.get('response_type', 'code')

        if client_id != OAUTH_CLIENT_ID:
            return jsonify({"error": "invalid_client"}), 400

        return render_template_string(OAUTH_LOGIN_PAGE,
                                       client_id=client_id,
                                       redirect_uri=redirect_uri,
                                       state=state,
                                       response_type=response_type)

    # POST: User clicked "Link" — generate auth code and redirect
    client_id = request.form.get('client_id', '')
    redirect_uri = request.form.get('redirect_uri', '')
    state = request.form.get('state', '')

    # Generate authorization code
    auth_code = secrets.token_urlsafe(32)
    _oauth_codes[auth_code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "timestamp": time.time(),
    }

    # Redirect back to Google with the auth code
    params = urlencode({"code": auth_code, "state": state})
    return redirect(f"{redirect_uri}?{params}")


@google_home.route('/oauth/token', methods=['POST'])
def oauth_token():
    """OAuth2 token endpoint."""
    grant_type = request.form.get('grant_type', '')
    client_id = request.form.get('client_id', '')
    client_secret = request.form.get('client_secret', '')
    code = request.form.get('code', '')

    # Validate client credentials
    if client_id != OAUTH_CLIENT_ID or client_secret != OAUTH_CLIENT_SECRET:
        return jsonify({"error": "invalid_client"}), 401

    if grant_type == 'authorization_code':
        # Exchange auth code for tokens
        if code not in _oauth_codes:
            return jsonify({"error": "invalid_grant"}), 400

        # Remove used code
        del _oauth_codes[code]

        # Generate access and refresh tokens
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)

        _oauth_tokens[access_token] = {
            "user_id": OAUTH_USER_ID,
            "timestamp": time.time(),
        }

        return jsonify({
            "token_type": "Bearer",
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": 3600,
        })

    elif grant_type == 'refresh_token':
        # Refresh token flow
        refresh_token = request.form.get('refresh_token', '')
        access_token = secrets.token_urlsafe(32)

        _oauth_tokens[access_token] = {
            "user_id": OAUTH_USER_ID,
            "timestamp": time.time(),
        }

        return jsonify({
            "token_type": "Bearer",
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": 3600,
        })

    return jsonify({"error": "unsupported_grant_type"}), 400


# ─── Health Check ─────────────────────────────────────────────────
@google_home.route('/smarthome/health', methods=['GET'])
def health_check():
    """Health check endpoint for verification."""
    return jsonify({
        "status": "ok",
        "service": "WaveSense Smart Home",
        "device_id": DEVICE_ID,
        "project_configured": bool(PROJECT_ID),
        "key_configured": _check_key_exists(),
    })


def _check_key_exists() -> bool:
    """Check if service account key file exists."""
    try:
        with open(SERVICE_ACCOUNT_KEY_PATH):
            return True
    except FileNotFoundError:
        return False
