# StandUp! — webcam sit-detector that escalates PiShock shockers

A Windows Python desktop app. A webcam watches your posture; if you stay seated
past a time limit it triggers your PiShock shocker(s), starting low and
increasing intensity every escalation interval until it detects you've stood up.
Multiple shockers are supported, each with its own independent escalation curve.

## ⚠️ Read before using
This drives a device that delivers electric shocks.

- Use it **only on yourself**, consensually. Never point the camera at anyone else.
- **Do not use** if you have a heart condition, pacemaker or other implant,
  epilepsy/seizure disorder, or are pregnant.
- Built-in guards you should not remove: a hard **MAX intensity & duration ceiling**
  the escalation can never exceed, an **EMERGENCY STOP** button, a **safety
  auto-shutoff** if it ever shocks continuously too long, and a failsafe that
  **never shocks when it can't clearly see you**.
- Start with the conservative defaults and only raise limits once you know how
  your device feels at each level.

## Install
Use Python 3.10–3.12 (MediaPipe 0.10.21 doesn't support 3.13+).

```
pip install -r requirements.txt
```

The versions in `requirements.txt` are pinned on purpose: OpenCV 4.13+ requires
numpy 2 while MediaPipe needs numpy 1, and MediaPipe 0.10.31+ dropped the pose
("Solutions") API this app relies on. Don't loosen the `numpy<2`,
`opencv-contrib-python==4.10.0.84`, or `mediapipe==0.10.21` pins without testing.

## Run
```
python sit_reminder.py
```

## First-time setup
1. **Settings tab → PiShock account.** Enter your `Username` and `API key`
   (from pishock.com → Account). No share code needed — the app finds your
   devices from the API key. Click **Save settings**.
   - The API key **must be generated on or after 2024-10-15** — PiShock's
     broker rejects older keys. If Test connection reports a connect failure,
     regenerate the key on pishock.com → Account and paste the new one in.
2. **Shockers tab.** Click **List my shockers** — it prints each shocker's name
   and numeric ID in the Log tab. Click **Add shocker**, type in an `ID`, give
   it a `Name`, tick `Enabled`, and set its escalation curve (Start %, Start s,
   Int step, Dur step, Max %, Max s). Repeat for each shocker you want, then
   **Save shockers**. Every enabled shocker fires together but ramps on its own
   curve and stops at its own ceiling.
3. Back on **Monitor**, click **Test connection** — it resolves your account,
   lists your shockers, and beeps the enabled ones. Follow with **Test vibrate
   (low)** to confirm they respond before arming anything.
4. Click **START monitoring**. Sit normally, then click
   **Calibrate (sit normally, then click)** so it learns your seated posture.
   (If you skip this, it auto-calibrates from the first frame it sees you, but
   manual calibration is more reliable.)

## How detection works
MediaPipe Pose tracks your shoulders. "Standing" is registered when your
shoulders rise above the seated baseline by more than the **Stand sensitivity**
fraction of the frame height. For a typical desk webcam that sees your
head/shoulders, standing up moves your shoulders up and out of frame, which
triggers reliably. Tune **Stand sensitivity** lower if standing isn't detected,
higher if leaning forward falsely counts as standing.

## Settings reference

**Global (Settings tab) — applies to all shockers:**

| Setting | Meaning |
|---|---|
| Sit limit (minutes) | How long you may sit before shocks begin (default 15). |
| Escalation interval (s) | How often every shocker escalates while you stay seated (default 30). |
| Required break (s standing) | How long you must remain standing for the timer to reset (default 60). |
| Warn before first shock | Buzzes every enabled shocker a few seconds before the first shock. |
| Warning lead time (s) | Gap between the warning buzz and the first shock. |
| Camera index | 0 = default webcam; try 1, 2… for others. |
| Stand sensitivity | Lower = easier to register "stood up". |
| Confirm frames | Smoothing window to avoid flicker (~8 is good). |
| Safety auto-shutoff (min) | Disarms if it ever shocks nonstop this long. |

**Per-shocker (Shockers tab) — independent for each shocker:**

| Field | Meaning |
|---|---|
| ID | The shocker's numeric ID (use **List my shockers** to find it). |
| Name | A label for your reference and the logs. |
| Enabled | Only enabled shockers fire. |
| Start % / Start s | This shocker's first-shock intensity (1–100) and duration (1–15 s). |
| Int step / Dur step | Added to this shocker each escalation. Dur step 0 keeps its duration fixed. |
| Max % / Max s | This shocker's ceilings; its escalation never exceeds them. |

## How PiShock control works
PiShock retired the old `do.pishock.com/api/apioperate` REST endpoint (it now
returns 404 and upgrades to WebSocket), so this app uses PiShock's current
**V2 WebSocket broker**:

1. It resolves your `UserId` from `auth.pishock.com`, then your hub `clientId`
   and `shockerId` from `ps.pishock.com/PiShock/GetUserDevices`, using your
   username + API key.
2. It opens `wss://broker.pishock.com/v2` and PUBLISHes operate commands to
   each shocker's `c{clientId}-ops` channel — the same path the PiShock website
   uses. Multiple shockers are sent in a single publish (one command each, even
   across different hubs). Durations are sent in milliseconds; modes are
   `s`/`v`/`b` for shock/vibrate/beep.

All of this lives in the `PiShockClient` class — that's the one place to touch
if PiShock changes the protocol again. Note the broker's V2 API is officially
marked "under construction," so it may shift.

## Notes
- Settings persist to `standup_config.json` next to the script. A config from
  the old single-shocker version is migrated automatically into one shocker.
- Detection runs in a worker thread; the GUI stays responsive and shows a live
  annotated preview, current posture, sitting time, and each shocker's level.
- If you ever paste your API key somewhere public, regenerate it on
  pishock.com → Account and update it in Settings.
