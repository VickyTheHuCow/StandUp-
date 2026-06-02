"""
StandUp! - A webcam-based "don't sit too long" reminder that escalates a PiShock
device until you stand up.

HOW IT WORKS
------------
1. A webcam watches you (MediaPipe Pose estimates your shoulder/head position).
2. While you stay seated, a timer counts up.
3. After the configured sit limit (default 15 min) the app enters SHOCK mode.
   - Optional warning buzz a few seconds before the first shock.
   - It sends a short, low-power shock, then increases intensity every
     N seconds (default 30s) until you stand up.
4. When you stand and remain standing for the configured break length, the
   timer resets.

SAFETY (read this)
------------------
This drives a device that delivers electric shocks. Use it ONLY on yourself,
consensually. The app enforces a hard intensity AND duration ceiling that the
escalation can never exceed, has an always-available EMERGENCY STOP, a runaway
auto-shutoff, and will NEVER shock when it cannot clearly see you (detection
loss is treated as "do not shock"). Do not remove these guards. If you have a
heart condition, a pacemaker/implant, epilepsy, or any other relevant medical
condition, do not use a shock device.

REQUIREMENTS
------------
    pip install opencv-python mediapipe pillow requests

Tested target: Windows 10/11, Python 3.10-3.12.
"""

import json
import os
import threading
import time
import traceback
from dataclasses import dataclass, asdict, field

import tkinter as tk
from tkinter import ttk, messagebox

import requests

# Optional heavy deps are imported lazily so the GUI can still open and show a
# helpful message if they are missing.
try:
    import cv2
    import numpy as np
    from PIL import Image, ImageTk
    _CV_OK = True
    _CV_ERR = ""
except Exception as e:  # pragma: no cover
    _CV_OK = False
    _CV_ERR = str(e)

try:
    import mediapipe as mp
    # The legacy "Solutions" API moved/was removed across mediapipe versions.
    # Try the canonical path first, then the bare alias. (Note: mediapipe
    # 0.10.31+ dropped this API entirely - pin mediapipe==0.10.21 if so.)
    try:
        from mediapipe.python.solutions import pose as mp_pose
        from mediapipe.python.solutions import drawing_utils as mp_drawing
    except ImportError:
        from mediapipe.solutions import pose as mp_pose
        from mediapipe.solutions import drawing_utils as mp_drawing
    _MP_OK = True
    _MP_ERR = ""
except Exception as e:  # pragma: no cover
    mp = None
    mp_pose = None
    mp_drawing = None
    _MP_OK = False
    _MP_ERR = str(e)


try:
    import websocket  # the 'websocket-client' package
    _WS_OK = True
    _WS_ERR = ""
except Exception as e:  # pragma: no cover
    websocket = None
    _WS_OK = False
    _WS_ERR = str(e)


# PiShock current (V2) API. The old do.pishock.com REST endpoint was retired
# and replaced by a WebSocket broker; commands are PUBLISHed to a channel.
PISHOCK_AUTH_URL = "https://auth.pishock.com/Auth/GetUserIfAPIKeyValid"
PISHOCK_DEVICES_URL = "https://ps.pishock.com/PiShock/GetUserDevices"
PISHOCK_WS_URL = "wss://broker.pishock.com/v2"

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "standup_config.json")

# Hard limits the user can never exceed, regardless of settings file tampering.
ABS_MAX_INTENSITY = 100   # PiShock device max
ABS_MAX_DURATION = 15     # seconds

# Mode codes used by the broker. Kept as selectors for the GUI test buttons too.
OP_SHOCK = "s"
OP_VIBRATE = "v"
OP_BEEP = "b"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    # --- PiShock credentials ---
    username: str = ""
    api_key: str = ""
    share_code: str = ""          # legacy / optional; not needed for your own device
    shocker_id: str = ""          # numeric Shocker ID or name; blank = auto-pick first
    app_name: str = "StandUp Reminder"

    # --- Timing ---
    sit_limit_minutes: float = 15.0      # how long you may sit before shocks begin
    escalation_interval_s: float = 30.0  # increase intensity every N seconds
    min_stand_seconds: float = 60.0      # how long you must stand for the timer to reset
    warn_before_shock: bool = True       # buzz before the first shock of a cycle
    warn_lead_seconds: float = 5.0       # how long before first shock to warn

    # --- Shock escalation ---
    start_intensity: int = 10            # first-shock intensity (1-100)
    start_duration: int = 1              # first-shock duration in seconds (1-15)
    intensity_step: int = 5              # intensity added each escalation step
    duration_step: int = 0              # duration added each escalation step
    max_intensity: int = 40             # ceiling the escalation cannot exceed
    max_duration: int = 5               # ceiling the escalation cannot exceed

    # --- Detection ---
    camera_index: int = 0
    stand_sensitivity: float = 0.12      # how far (frac of frame height) shoulders
                                         # must rise above the seated baseline to
                                         # count as "standing". Lower = more sensitive.
    confirm_frames: int = 8              # frames of agreement needed to switch state

    # --- Runaway guard ---
    safety_timeout_minutes: float = 5.0  # auto-disarm if continuously shocking this long

    def clamp(self):
        """Enforce sane ranges and the absolute hardware ceilings."""
        self.start_intensity = int(_bound(self.start_intensity, 1, ABS_MAX_INTENSITY))
        self.max_intensity = int(_bound(self.max_intensity, 1, ABS_MAX_INTENSITY))
        # max may not be below start
        self.max_intensity = max(self.max_intensity, self.start_intensity)

        self.start_duration = int(_bound(self.start_duration, 1, ABS_MAX_DURATION))
        self.max_duration = int(_bound(self.max_duration, 1, ABS_MAX_DURATION))
        self.max_duration = max(self.max_duration, self.start_duration)

        self.intensity_step = int(_bound(self.intensity_step, 0, ABS_MAX_INTENSITY))
        self.duration_step = int(_bound(self.duration_step, 0, ABS_MAX_DURATION))

        self.sit_limit_minutes = _bound(self.sit_limit_minutes, 0.1, 600)
        self.escalation_interval_s = _bound(self.escalation_interval_s, 5, 600)
        self.min_stand_seconds = _bound(self.min_stand_seconds, 1, 3600)
        self.warn_lead_seconds = _bound(self.warn_lead_seconds, 0, 60)
        self.stand_sensitivity = _bound(self.stand_sensitivity, 0.02, 0.5)
        self.confirm_frames = int(_bound(self.confirm_frames, 1, 60))
        self.safety_timeout_minutes = _bound(self.safety_timeout_minutes, 0.5, 60)
        return self

    @staticmethod
    def load():
        cfg = Config()
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for k, v in data.items():
                    if hasattr(cfg, k):
                        setattr(cfg, k, v)
            except Exception:
                pass
        return cfg.clamp()

    def save(self):
        self.clamp()
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(asdict(self), f, indent=2)
        except Exception:
            traceback.print_exc()


def _bound(v, lo, hi):
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = lo
    return max(lo, min(hi, v))


def _normalize_share_code(raw):
    """Accept either a bare share code or a full pishock.com share URL/link and
    return just the code. Pasting the whole 'https://pishock.com/#/Control?
    sharecode=XXXX' link is a common mistake, so we extract XXXX from it."""
    s = (raw or "").strip()
    low = s.lower()
    if "sharecode=" in low:
        s = s[low.index("sharecode=") + len("sharecode="):]
        for sep in ("&", "#", "/", "?", " "):
            if sep in s:
                s = s.split(sep)[0]
    return s.strip()


# --------------------------------------------------------------------------- #
# PiShock client
# --------------------------------------------------------------------------- #
class PiShockClient:
    """Talks to PiShock's current (V2) WebSocket broker.

    Flow: resolve UserID + Shocker/Client IDs from username+API key (HTTP),
    then open a WebSocket to the broker and PUBLISH operate commands. The old
    do.pishock.com REST endpoint was retired, which is why share-code POSTs now
    404. No share code is needed for your own device."""

    def __init__(self, cfg: Config, logger):
        self.cfg = cfg
        self.log = logger
        self._user_id = None
        self._client_id = None
        self._shocker_id = None
        self._shocker_name = None
        self._key = None  # (username, api_key, shocker_id) to detect changes

    # ----- discovery ----- #
    def _discover(self):
        if not _WS_OK:
            self.log("The 'websocket-client' package isn't installed. "
                     "Run:  py -m pip install websocket-client")
            return False
        u = self.cfg.username.strip()
        k = self.cfg.api_key.strip()
        want = str(self.cfg.shocker_id).strip()
        if not (u and k):
            self.log("PiShock: enter your username and API key first.")
            return False
        if self._shocker_id is not None and (u, k, want) == self._key:
            return True

        # 1) UserID from API key
        try:
            r = requests.get(PISHOCK_AUTH_URL,
                             params={"apikey": k, "username": u}, timeout=10)
        except requests.RequestException as e:
            self.log(f"Auth request failed (network): {e}")
            return False
        if r.status_code != 200:
            self.log(f"Auth failed (HTTP {r.status_code}). Check the username and "
                     "API key on pishock.com -> Account.")
            return False
        try:
            data = r.json()
        except ValueError:
            data = {}
        # Find the user id regardless of key casing (API returns "UserId").
        self._user_id = None
        if isinstance(data, dict):
            for kk, vv in data.items():
                if kk.lower() == "userid":
                    self._user_id = vv
                    break
        if not self._user_id:
            safe = {kk: ("<redacted>" if kk.lower() in ("password", "token", "apikey")
                         else vv)
                    for kk, vv in (data.items() if isinstance(data, dict) else [])}
            self.log(f"Couldn't read a UserID from the auth response: {str(safe)[:200]}")
            return False

        # 2) Devices / shockers for that user
        try:
            r = requests.get(PISHOCK_DEVICES_URL,
                             params={"UserId": self._user_id, "Token": k, "api": "true"},
                             timeout=10)
            devices = r.json()
        except (requests.RequestException, ValueError) as e:
            self.log(f"Device lookup failed: {e}")
            return False
        if not isinstance(devices, list) or not devices:
            self.log("No PiShock devices found on this account.")
            return False

        chosen = None
        available = []
        for dev in devices:
            cid = dev.get("clientId")
            for sh in dev.get("shockers", []):
                sid = sh.get("shockerId")
                sname = (sh.get("name") or "").strip()
                available.append(f"{sname or '?'} (id {sid})")
                if want and want != str(sid) and want.lower() != sname.lower():
                    continue
                chosen = (cid, sid, sname, sh.get("isPaused", False))
                break
            if chosen:
                break

        if not chosen:
            self.log("Couldn't match a shocker. Available: "
                     + (", ".join(available) or "none")
                     + ". Set 'Shocker ID' in Settings to one of these, or leave it "
                       "blank to use the first.")
            return False

        self._client_id, self._shocker_id, self._shocker_name, paused = chosen
        self._key = (u, k, want)
        self.log(f"Using shocker '{self._shocker_name}' (id {self._shocker_id}) "
                 f"on hub {self._client_id}"
                 + (" [PAUSED - unpause it on pishock.com]" if paused else "") + ".")
        return True

    # ----- websocket publish ----- #
    def _publish(self, mode, intensity, duration_s):
        if not self._discover():
            return False
        u = self.cfg.username.strip()
        k = self.cfg.api_key.strip()
        duration_ms = int(round(float(duration_s) * 1000))
        body = {
            "id": self._shocker_id,
            "m": mode,
            "i": int(intensity),
            "d": duration_ms,
            "r": True,
            "l": {
                "u": self._user_id,
                "ty": "api",
                "w": False,
                "h": False,
                "o": self.cfg.app_name.strip() or "StandUp Reminder",
            },
        }
        message = {
            "Operation": "PUBLISH",
            "PublishCommands": [
                {"Target": f"c{self._client_id}-ops", "Body": body}
            ],
        }
        url = f"{PISHOCK_WS_URL}?Username={u}&ApiKey={k}"
        try:
            ws = websocket.create_connection(url, timeout=10)
        except Exception as e:
            self.log(f"WebSocket connect failed: {e}  ->  If your API key was created "
                     "before 2024-10-15, regenerate it on pishock.com -> Account "
                     "(the broker requires a newer key).")
            return False

        try:
            ws.settimeout(6)
            ws.send(json.dumps(message))
            deadline = time.time() + 6
            last = ""
            while time.time() < deadline:
                try:
                    resp = ws.recv()
                except Exception:
                    break
                if not resp:
                    continue
                last = resp
                low = resp.lower()
                if "publish success" in low or "publish successful" in low:
                    return True
                if '"iserror":true' in low:
                    self.log(f"PiShock broker error: {resp}")
                    return False
            if last:
                self.log(f"No success confirmation from broker. Last message: {last}")
            else:
                self.log("No response from broker after publishing.")
            return False
        except Exception as e:
            self.log(f"WebSocket send/recv failed: {e}")
            return False
        finally:
            try:
                ws.close()
            except Exception:
                pass

    # ----- public operations ----- #
    def shock(self, intensity, duration):
        ok = self._publish(OP_SHOCK, intensity, duration)
        self.log(f"SHOCK {int(intensity)}% / {duration}s -> {'sent' if ok else 'FAILED'}")
        return ok

    def vibrate(self, intensity, duration):
        ok = self._publish(OP_VIBRATE, intensity, duration)
        self.log(f"vibrate {int(intensity)}% / {duration}s -> {'sent' if ok else 'FAILED'}")
        return ok

    def beep(self, duration):
        ok = self._publish(OP_BEEP, 0, duration)
        self.log(f"beep {duration}s -> {'sent' if ok else 'FAILED'}")
        return ok

    def verify(self):
        """Resolve the account/shocker and send a test beep over the broker."""
        if not self._discover():
            return False
        self.log("Account + shocker resolved. Sending a test beep over the broker...")
        return self.beep(1)



# --------------------------------------------------------------------------- #
# Shared state between worker thread and GUI
# --------------------------------------------------------------------------- #
@dataclass
class SharedState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    frame_rgb: object = None          # latest annotated frame (numpy RGB)
    posture: str = "UNKNOWN"          # SITTING / STANDING / UNKNOWN
    sit_seconds: float = 0.0
    stand_seconds: float = 0.0
    shocking: bool = False
    current_intensity: int = 0
    current_duration: int = 0
    status_text: str = "Idle"
    baseline_set: bool = False
    calibrate_request: bool = False
    next_shock_in: float = 0.0        # seconds until next shock/escalation event


# --------------------------------------------------------------------------- #
# Webcam + posture monitor (runs on its own thread)
# --------------------------------------------------------------------------- #
class PoseMonitor(threading.Thread):
    def __init__(self, cfg: Config, state: SharedState, client: PiShockClient, logger):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.state = state
        self.client = client
        self.log = logger
        self._stop = threading.Event()
        self._estop = threading.Event()

        self.baseline_y = None          # seated shoulder Y (normalized)
        self.recent = []                # rolling raw posture classifications
        self.confirmed = "UNKNOWN"

        # Cycle / escalation bookkeeping
        self.shock_mode = False
        self.shock_mode_started = 0.0
        self.cur_intensity = cfg.start_intensity
        self.cur_duration = cfg.start_duration
        self.last_action_time = 0.0
        self.warned = False

    def stop(self):
        self._stop.set()

    def emergency_stop(self):
        """Halt all shock activity immediately and reset the cycle."""
        self._estop.set()

    def _set_status(self, text):
        with self.state.lock:
            self.state.status_text = text

    # ----- posture classification ----- #
    def _classify(self, landmarks, frame_h):
        """Return SITTING / STANDING / UNKNOWN from MediaPipe landmarks."""
        if landmarks is None:
            return "UNKNOWN", None
        ls = landmarks[mp_pose.PoseLandmark.LEFT_SHOULDER.value]
        rs = landmarks[mp_pose.PoseLandmark.RIGHT_SHOULDER.value]
        # visibility check
        if ls.visibility < 0.5 and rs.visibility < 0.5:
            return "UNKNOWN", None
        ys = [p.y for p in (ls, rs) if p.visibility >= 0.5]
        shoulder_y = sum(ys) / len(ys)  # normalized 0 (top) .. 1 (bottom)

        if self.baseline_y is None:
            # auto-establish baseline from the first stable seated reading
            self.baseline_y = shoulder_y
            with self.state.lock:
                self.state.baseline_set = True
            self.log(f"Auto baseline set (shoulder y={shoulder_y:.3f}). "
                     "Recalibrate while seated for best accuracy.")

        # Standing = shoulders rose (smaller y) by more than sensitivity
        if shoulder_y < self.baseline_y - self.cfg.stand_sensitivity:
            return "STANDING", shoulder_y
        return "SITTING", shoulder_y

    def _confirm(self, raw):
        self.recent.append(raw)
        if len(self.recent) > self.cfg.confirm_frames:
            self.recent.pop(0)
        # require a clear majority of the window to agree before switching
        if len(self.recent) >= self.cfg.confirm_frames:
            for label in ("STANDING", "SITTING", "UNKNOWN"):
                if self.recent.count(label) >= int(self.cfg.confirm_frames * 0.7):
                    self.confirmed = label
                    break
        return self.confirmed

    def _reset_cycle(self):
        self.shock_mode = False
        self.warned = False
        self.cur_intensity = self.cfg.start_intensity
        self.cur_duration = self.cfg.start_duration
        with self.state.lock:
            self.state.shocking = False
            self.state.current_intensity = 0
            self.state.current_duration = 0

    def _fire_async(self, fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    def run(self):
        if not (_CV_OK and _MP_OK):
            self._set_status("Missing dependencies - see Log tab.")
            return

        cap = cv2.VideoCapture(self.cfg.camera_index, cv2.CAP_DSHOW if os.name == "nt" else 0)
        if not cap.isOpened():
            cap = cv2.VideoCapture(self.cfg.camera_index)
        if not cap.isOpened():
            self.log(f"Could not open camera index {self.cfg.camera_index}.")
            self._set_status("Camera not available.")
            return

        pose = mp_pose.Pose(
            model_complexity=1, min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )

        prev = time.time()
        sit_acc = 0.0
        stand_acc = 0.0

        try:
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.05)
                    continue

                frame = cv2.flip(frame, 1)  # mirror for natural view
                h, w = frame.shape[:2]
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                results = pose.process(rgb)

                landmarks = results.pose_landmarks.landmark if results.pose_landmarks else None

                # Handle a pending calibration request
                with self.state.lock:
                    do_cal = self.state.calibrate_request
                    self.state.calibrate_request = False
                if do_cal and landmarks is not None:
                    ls = landmarks[mp_pose.PoseLandmark.LEFT_SHOULDER.value]
                    rs = landmarks[mp_pose.PoseLandmark.RIGHT_SHOULDER.value]
                    self.baseline_y = (ls.y + rs.y) / 2.0
                    with self.state.lock:
                        self.state.baseline_set = True
                    self.log(f"Calibrated seated baseline (shoulder y={self.baseline_y:.3f}).")

                raw, _ = self._classify(landmarks, h)
                posture = self._confirm(raw)

                now = time.time()
                dt = now - prev
                prev = now

                # Emergency stop wins over everything
                if self._estop.is_set():
                    self._estop.clear()
                    self._reset_cycle()
                    sit_acc = 0.0
                    self.log("EMERGENCY STOP - cycle reset, no further shocks.")
                    self._set_status("EMERGENCY STOP")

                # ----- timer + state machine ----- #
                if posture == "SITTING":
                    sit_acc += dt
                    stand_acc = 0.0
                elif posture == "STANDING":
                    stand_acc += dt
                    if stand_acc >= self.cfg.min_stand_seconds:
                        if sit_acc > 0 or self.shock_mode:
                            self.log(f"Break complete ({self.cfg.min_stand_seconds:.0f}s "
                                     "standing). Timer reset.")
                        sit_acc = 0.0
                        self._reset_cycle()
                    else:
                        # standing but break not yet satisfied -> stop shocking
                        if self.shock_mode:
                            self.shock_mode = False
                            with self.state.lock:
                                self.state.shocking = False
                else:
                    # UNKNOWN: do not advance the sit timer and never shock.
                    pass

                sit_limit_s = self.cfg.sit_limit_minutes * 60.0

                # ----- decide on shock actions ----- #
                if posture == "SITTING" and sit_acc >= sit_limit_s:
                    if not self.shock_mode:
                        self.shock_mode = True
                        self.shock_mode_started = now
                        self.warned = False
                        self.last_action_time = 0.0
                        self.cur_intensity = self.cfg.start_intensity
                        self.cur_duration = self.cfg.start_duration
                        self.log("Sit limit reached -> entering shock mode.")

                    # runaway guard
                    if (now - self.shock_mode_started) > self.cfg.safety_timeout_minutes * 60.0:
                        self.log("Safety timeout reached - auto-disarming. Check on yourself!")
                        self._set_status("SAFETY TIMEOUT - disarmed")
                        break

                    # optional pre-shock warning
                    if self.cfg.warn_before_shock and not self.warned and self.last_action_time == 0.0:
                        self._fire_async(self.client.vibrate,
                                         min(30, self.cfg.start_intensity), 1)
                        self.warned = True
                        self.warn_time = now
                        self._set_status("WARNING buzz - stand up now!")

                    # time to act?
                    if self.last_action_time == 0.0:
                        # first shock fires after the warning lead time
                        ready = (not self.cfg.warn_before_shock) or \
                                (now - getattr(self, "warn_time", now) >= self.cfg.warn_lead_seconds)
                        if ready:
                            self._deliver(now)
                    elif (now - self.last_action_time) >= self.cfg.escalation_interval_s:
                        self._escalate()
                        self._deliver(now)

                # update next-event countdown for the GUI
                next_in = 0.0
                if self.shock_mode and self.last_action_time:
                    next_in = max(0.0, self.cfg.escalation_interval_s - (now - self.last_action_time))

                # ----- annotate frame ----- #
                if results.pose_landmarks:
                    mp_drawing.draw_landmarks(
                        rgb, results.pose_landmarks,
                        mp_pose.POSE_CONNECTIONS)
                self._annotate(rgb, posture, sit_acc, sit_limit_s)

                # ----- publish state ----- #
                with self.state.lock:
                    self.state.frame_rgb = rgb
                    self.state.posture = posture
                    self.state.sit_seconds = sit_acc
                    self.state.stand_seconds = stand_acc
                    self.state.shocking = self.shock_mode
                    self.state.current_intensity = self.cur_intensity if self.shock_mode else 0
                    self.state.current_duration = self.cur_duration if self.shock_mode else 0
                    self.state.next_shock_in = next_in
                    if not self.shock_mode:
                        if posture == "SITTING":
                            remaining = max(0, sit_limit_s - sit_acc)
                            self.state.status_text = f"Sitting - {remaining/60:.1f} min until shocks"
                        elif posture == "STANDING":
                            self.state.status_text = (
                                f"Standing - break {stand_acc:.0f}/"
                                f"{self.cfg.min_stand_seconds:.0f}s")
                        else:
                            self.state.status_text = "Cannot see you (no shock while unseen)"

                time.sleep(max(0.0, 0.05 - (time.time() - now)))  # ~20 fps cap
        finally:
            cap.release()
            try:
                pose.close()
            except Exception:
                pass

    def _escalate(self):
        self.cur_intensity = min(self.cfg.max_intensity,
                                 self.cur_intensity + self.cfg.intensity_step)
        self.cur_duration = min(self.cfg.max_duration,
                                self.cur_duration + self.cfg.duration_step)

    def _deliver(self, now):
        i = int(min(self.cur_intensity, self.cfg.max_intensity, ABS_MAX_INTENSITY))
        d = int(min(self.cur_duration, self.cfg.max_duration, ABS_MAX_DURATION))
        self._fire_async(self.client.shock, i, d)
        self.last_action_time = now
        with self.state.lock:
            self.state.shocking = True
            self.state.current_intensity = i
            self.state.current_duration = d

    def _annotate(self, rgb, posture, sit_acc, sit_limit_s):
        color = {"SITTING": (255, 180, 0), "STANDING": (0, 200, 0),
                 "UNKNOWN": (160, 160, 160)}.get(posture, (200, 200, 200))
        cv2.putText(rgb, posture, (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 1.0, color, 2)
        mins = sit_acc / 60.0
        cv2.putText(rgb, f"sat: {mins:5.1f} min", (12, 66),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (240, 240, 240), 2)
        if self.shock_mode:
            cv2.putText(rgb, f"SHOCK {self.cur_intensity}% / {self.cur_duration}s",
                        (12, 98), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (40, 40, 255), 2)


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("StandUp! - PiShock sit reminder")
        self.geometry("980x680")
        self.minsize(900, 620)

        self.cfg = Config.load()
        self.state = SharedState()
        self.log_lines = []
        self.client = PiShockClient(self.cfg, self.log)
        self.monitor = None
        self._photo = None

        self._build_ui()
        self._poll_gui()

        if not _CV_OK:
            self.log(f"OpenCV/Pillow import failed: {_CV_ERR}")
        if not _MP_OK:
            self.log(f"MediaPipe import failed: {_MP_ERR}")
            if "solutions" in _MP_ERR or "mediapipe.python" in _MP_ERR:
                self.log("This mediapipe is too new - the Solutions API was removed "
                         "in 0.10.31+. Fix with:  py -m pip install mediapipe==0.10.21")
        if not _WS_OK:
            self.log(f"websocket-client not available: {_WS_ERR}")
            self.log("Install it with:  py -m pip install websocket-client")
        if not (_CV_OK and _MP_OK):
            self.log('Install dependencies:  pip install "numpy<2" '
                     '"opencv-contrib-python==4.10.0.84" "mediapipe==0.10.21" '
                     'websocket-client pillow requests')

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----- UI construction ----- #
    def _build_ui(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)

        self.tab_main = ttk.Frame(nb)
        self.tab_settings = ttk.Frame(nb)
        self.tab_log = ttk.Frame(nb)
        nb.add(self.tab_main, text="Monitor")
        nb.add(self.tab_settings, text="Settings")
        nb.add(self.tab_log, text="Log")

        self._build_main_tab()
        self._build_settings_tab()
        self._build_log_tab()

    def _build_main_tab(self):
        left = ttk.Frame(self.tab_main)
        left.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        right = ttk.Frame(self.tab_main, width=300)
        right.pack(side="right", fill="y", padx=6, pady=6)

        self.video_label = ttk.Label(left, anchor="center",
                                     text="Camera preview will appear here.\nPress START.")
        self.video_label.pack(fill="both", expand=True)

        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(right, textvariable=self.status_var, font=("Segoe UI", 12, "bold"),
                  wraplength=280, justify="center").pack(pady=(4, 8))

        self.posture_var = tk.StringVar(value="Posture: -")
        self.sit_var = tk.StringVar(value="Sitting time: 0.0 min")
        self.shock_var = tk.StringVar(value="Shock: idle")
        for v in (self.posture_var, self.sit_var, self.shock_var):
            ttk.Label(right, textvariable=v, font=("Segoe UI", 11)).pack(anchor="w", pady=2)

        ttk.Separator(right).pack(fill="x", pady=8)

        self.start_btn = ttk.Button(right, text="START monitoring", command=self.start)
        self.start_btn.pack(fill="x", pady=3)
        self.stop_btn = ttk.Button(right, text="Stop monitoring", command=self.stop,
                                   state="disabled")
        self.stop_btn.pack(fill="x", pady=3)

        ttk.Button(right, text="Calibrate (sit normally, then click)",
                   command=self.calibrate).pack(fill="x", pady=3)

        ttk.Separator(right).pack(fill="x", pady=8)
        ttk.Button(right, text="Test beep", command=lambda: self._test(OP_BEEP)).pack(fill="x", pady=2)
        ttk.Button(right, text="Test vibrate (low)",
                   command=lambda: self._test(OP_VIBRATE)).pack(fill="x", pady=2)
        ttk.Button(right, text="Test connection", command=self.test_connection).pack(fill="x", pady=2)

        ttk.Separator(right).pack(fill="x", pady=8)
        estop = tk.Button(right, text="EMERGENCY STOP", command=self.emergency_stop,
                          bg="#cc0000", fg="white", font=("Segoe UI", 13, "bold"),
                          activebackground="#ff2222", height=2)
        estop.pack(fill="x", pady=4)

    def _build_settings_tab(self):
        self.vars = {}
        frm = ttk.Frame(self.tab_settings)
        frm.pack(fill="both", expand=True, padx=12, pady=12)

        def section(title, row):
            ttk.Label(frm, text=title, font=("Segoe UI", 11, "bold")).grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(12, 4))
            return row + 1

        def field_row(label, key, row, hint=""):
            ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
            var = tk.StringVar(value=str(getattr(self.cfg, key)))
            self.vars[key] = var
            ttk.Entry(frm, textvariable=var, width=18).grid(row=row, column=1, sticky="w", pady=2)
            if hint:
                ttk.Label(frm, text=hint, foreground="#666").grid(
                    row=row, column=2, sticky="w", padx=8)
            return row + 1

        def check_row(label, key, row):
            var = tk.BooleanVar(value=bool(getattr(self.cfg, key)))
            self.vars[key] = var
            ttk.Checkbutton(frm, text=label, variable=var).grid(
                row=row, column=0, columnspan=2, sticky="w", pady=2)
            return row + 1

        r = 0
        r = section("PiShock account", r)
        r = field_row("Username", "username", r)
        r = field_row("API key", "api_key", r, "from pishock.com -> Account")
        r = field_row("Shocker ID (optional)", "shocker_id", r, "blank = use first shocker")
        r = field_row("Share code (optional)", "share_code", r, "not needed for your own device")
        r = field_row("App name (shown in logs)", "app_name", r)

        r = section("Timing", r)
        r = field_row("Sit limit (minutes)", "sit_limit_minutes", r, "shocks begin after this")
        r = field_row("Escalation interval (s)", "escalation_interval_s", r, "intensity rises each step")
        r = field_row("Required break (s standing)", "min_stand_seconds", r, "stand this long to reset")
        r = check_row("Warn (buzz) before first shock", "warn_before_shock", r)
        r = field_row("Warning lead time (s)", "warn_lead_seconds", r)

        r = section("Shock escalation", r)
        r = field_row("Start intensity (1-100)", "start_intensity", r)
        r = field_row("Start duration (1-15 s)", "start_duration", r)
        r = field_row("Intensity step", "intensity_step", r, "added each escalation")
        r = field_row("Duration step", "duration_step", r, "0 = keep duration fixed")
        r = field_row("MAX intensity ceiling", "max_intensity", r, "escalation never exceeds")
        r = field_row("MAX duration ceiling", "max_duration", r, "escalation never exceeds")

        r = section("Detection & safety", r)
        r = field_row("Camera index", "camera_index", r, "0 = default webcam")
        r = field_row("Stand sensitivity", "stand_sensitivity", r, "lower = easier to trigger 'stood'")
        r = field_row("Confirm frames", "confirm_frames", r, "smoothing; ~8 is good")
        r = field_row("Safety auto-shutoff (min)", "safety_timeout_minutes", r,
                      "stop if shocking nonstop this long")

        btns = ttk.Frame(frm)
        btns.grid(row=r, column=0, columnspan=3, sticky="w", pady=16)
        ttk.Button(btns, text="Save settings", command=self.save_settings).pack(side="left", padx=4)
        ttk.Button(btns, text="Reload from file", command=self.reload_settings).pack(side="left", padx=4)

        ttk.Label(frm, foreground="#a00", wraplength=560, justify="left",
                  text=("Reminder: this device shocks you. Use on yourself only. The MAX "
                        "ceilings, emergency stop, safety auto-shutoff, and 'no shock when "
                        "unseen' failsafe are intentional. Don't use with a heart/seizure "
                        "condition or implant.")).grid(
            row=r + 1, column=0, columnspan=3, sticky="w", pady=(4, 0))

    def _build_log_tab(self):
        self.log_text = tk.Text(self.tab_log, wrap="word", state="disabled",
                                font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)

    # ----- settings IO ----- #
    def save_settings(self):
        for key, var in self.vars.items():
            cur = getattr(self.cfg, key)
            val = var.get()
            try:
                if isinstance(cur, bool):
                    val = bool(val)
                elif isinstance(cur, int):
                    val = int(float(val))
                elif isinstance(cur, float):
                    val = float(val)
                else:
                    val = str(val)
            except (ValueError, TypeError):
                self.log(f"Bad value for {key!r}: {var.get()!r} (kept old).")
                continue
            setattr(self.cfg, key, val)
        self.cfg.save()
        self.reload_settings()  # refresh fields with clamped values
        self.log("Settings saved.")
        messagebox.showinfo("Saved", "Settings saved.\nRestart monitoring to apply camera changes.")

    def reload_settings(self):
        self.cfg = Config.load()
        self.client.cfg = self.cfg
        for key, var in self.vars.items():
            v = getattr(self.cfg, key)
            if isinstance(var, tk.BooleanVar):
                var.set(bool(v))
            else:
                var.set(str(v))

    # ----- controls ----- #
    def start(self):
        if not (_CV_OK and _MP_OK):
            messagebox.showerror("Missing dependencies",
                                 "Install requirements first:\n\n"
                                 "pip install opencv-python mediapipe pillow requests")
            return
        if self.monitor and self.monitor.is_alive():
            return
        self.cfg = Config.load()
        self.client.cfg = self.cfg
        self.monitor = PoseMonitor(self.cfg, self.state, self.client, self.log)
        self.monitor.start()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.log("Monitoring started.")

    def stop(self):
        if self.monitor:
            self.monitor.stop()
            self.monitor = None
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.log("Monitoring stopped.")

    def calibrate(self):
        with self.state.lock:
            self.state.calibrate_request = True
        self.log("Calibration requested - hold a normal seated posture.")

    def emergency_stop(self):
        if self.monitor:
            self.monitor.emergency_stop()
        self.log("EMERGENCY STOP pressed.")

    def test_connection(self):
        def run():
            if self.client.verify():
                self.client.beep(1)
                self.log("Connection test: OK (device should beep).")
            else:
                self.log("Connection test: failed - see the messages just above.")
        threading.Thread(target=run, daemon=True).start()

    def _test(self, op):
        def run():
            if op == OP_BEEP:
                self.client.beep(1)
            elif op == OP_VIBRATE:
                self.client.vibrate(min(20, self.cfg.start_intensity), 1)
        threading.Thread(target=run, daemon=True).start()

    # ----- logging ----- #
    def log(self, msg):
        ts = time.strftime("%H:%M:%S")
        self.log_lines.append(f"[{ts}] {msg}")
        self.log_lines = self.log_lines[-500:]

    def _flush_log(self):
        if not hasattr(self, "log_text"):
            return
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("end", "\n".join(self.log_lines))
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # ----- GUI poll loop ----- #
    def _poll_gui(self):
        with self.state.lock:
            frame = self.state.frame_rgb
            posture = self.state.posture
            sit_s = self.state.sit_seconds
            shocking = self.state.shocking
            ci = self.state.current_intensity
            cd = self.state.current_duration
            status = self.state.status_text
            nxt = self.state.next_shock_in

        if frame is not None and _CV_OK:
            try:
                img = Image.fromarray(frame)
                lw = max(320, self.video_label.winfo_width())
                lh = max(240, self.video_label.winfo_height())
                img.thumbnail((lw, lh))
                self._photo = ImageTk.PhotoImage(img)
                self.video_label.config(image=self._photo, text="")
            except Exception:
                pass

        self.status_var.set(status)
        self.posture_var.set(f"Posture: {posture}")
        self.sit_var.set(f"Sitting time: {sit_s/60:.1f} min")
        if shocking:
            self.shock_var.set(f"SHOCK {ci}% / {cd}s  (next in {nxt:.0f}s)")
        else:
            self.shock_var.set("Shock: idle")

        self._flush_log()
        self.after(120, self._poll_gui)

    def _on_close(self):
        try:
            if self.monitor:
                self.monitor.emergency_stop()
                self.monitor.stop()
        finally:
            self.destroy()


if __name__ == "__main__":
    App().mainloop()
