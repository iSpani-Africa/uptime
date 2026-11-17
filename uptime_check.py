"""External uptime checker for ispani.africa - runs on GitHub Actions runners.

Pings /healthz from OUTSIDE the VPS and alerts by SMS via the BulkSMS API
directly - never through the app, because the whole point is detecting the
app being down (MONITORING.md §5). Deliberately standalone: imports nothing
from the app so it runs on a bare runner with only `requests` installed.

Flap control: a JSON state file ({"consecutive_failures": N, "alerted": bool})
persisted between runs via the Actions cache. A single failed run never
alerts; UPTIME_FAIL_THRESHOLD consecutive failed runs (default 2, i.e. ~10
minutes at the 5-minute cron) send one DOWN SMS, and the first green run
after that sends one RECOVERED SMS. The check itself also retries once
in-run so a sub-second blip doesn't even count as a failed run.

Environment (workflow supplies these; secrets in GitHub, never in the repo):
  HEALTHZ_URL            URL to ping (default https://ispani.africa/healthz)
  BULKSMS_TOKEN_ID       BulkSMS API token id     (GitHub Secret)
  BULKSMS_TOKEN_SECRET   BulkSMS API token secret (GitHub Secret)
  UPTIME_ALERT_PHONES    comma-separated E.164 numbers, e.g. +27821234567
  UPTIME_STATE_FILE      state file path (default .uptime-state.json)
  UPTIME_CHECK_NAME      "site" (default) or "jobs". "jobs" pings
                         /healthz/jobs: only the app's own 503 counts as a
                         failure, and the SMS says background jobs stopped
  UPTIME_FAIL_THRESHOLD  consecutive failed runs before alerting (default 2)
  UPTIME_TIMEOUT_S       per-request timeout; slower than this counts as
                         down (default 10)

Exit code: 0 healthy, 1 down - so a down site shows as a red run in the
Actions list. The workflow saves the state file with `if: always()`.

Missing BulkSMS credentials -> messages are printed, not sent (mirrors the
dev_mode convention in utils/sms.py), so a fork or local run is harmless.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

BULKSMS_API = "https://api.bulksms.com/v1/messages"
# Same rule as utils/sms.py::sender_id(): empty means "let BulkSMS choose".
# Naming an unregistered alphanumeric sender gets messages dropped outright.
SENDER_ID = os.getenv("BULKSMS_SENDER_ID", "").strip()
DEFAULT_URL = "https://ispani.africa/healthz"

# South Africa does not observe daylight saving.
SA_TZ = timezone(timedelta(hours=2))


def sast_stamp(now=None):
    """Short SAST timestamp for SMS bodies, e.g. '14:05 SAST'."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(SA_TZ).strftime("%H:%M SAST")


def _status_word(resp):
    """The app's own {"status": "<word>"} from a health response, or "".
    A proxy's error page has no such body, which is how the app saying
    "stale" is told apart from the proxy saying 503 with nothing behind it."""
    try:
        word = resp.json().get("status")
    except Exception:
        return ""
    return word if isinstance(word, str) and word.isalpha() and len(word) <= 20 else ""


def check_once(url, timeout_s):
    """One GET against the health URL. Returns (ok, detail)."""
    try:
        resp = requests.get(url, timeout=timeout_s)
        word = _status_word(resp)
        if resp.status_code == 200:
            return True, f"200 {word}" if word and word != "ok" else "200"
        if resp.status_code == 503 and word:
            return False, f"HTTP 503 {word}"
        return False, f"HTTP {resp.status_code}"
    except requests.exceptions.Timeout:
        return False, f"timeout >{timeout_s}s"
    except requests.exceptions.RequestException as e:
        # Connection refused, DNS failure, TLS error... all read as down.
        return False, type(e).__name__


def check_health(url, timeout_s, retry_delay_s=15, sleep=time.sleep):
    """Check with one in-run retry so a momentary blip isn't a failed run."""
    ok, detail = check_once(url, timeout_s)
    if ok:
        return ok, detail
    sleep(retry_delay_s)
    return check_once(url, timeout_s)


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        return {
            "consecutive_failures": int(state.get("consecutive_failures", 0)),
            "alerted": bool(state.get("alerted", False)),
        }
    except (OSError, ValueError):
        # First run, evicted cache, or corrupt file: start clean.
        return {"consecutive_failures": 0, "alerted": False}


def save_state(path, state):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)


def decide(state, ok, threshold=2):
    """Pure flap-control transition: (state, check result) -> (new state, action).

    action is None, "alert_down" (threshold consecutive failures reached and
    not yet alerted) or "alert_recovery" (healthy again after an alert).
    Kept side-effect free so Phase 6 can unit test it exhaustively.
    """
    if ok:
        action = "alert_recovery" if state["alerted"] else None
        return {"consecutive_failures": 0, "alerted": False}, action

    failures = state["consecutive_failures"] + 1
    if failures >= threshold and not state["alerted"]:
        return {"consecutive_failures": failures, "alerted": True}, "alert_down"
    return {"consecutive_failures": failures, "alerted": state["alerted"]}, None


def counts_as(name, ok, detail):
    """What a check result means for the named check: True (healthy), False
    (failed) or None (says nothing; leave the state alone).

    The "jobs" check asks the app whether its background jobs are running,
    and only the app's own word answers that:
      "200" (ok)            healthy
      "HTTP 503 stale"      failed: the jobs have stopped
      "200 disabled"        failed: the scheduler is switched off, so every
                            watcher is off and nothing in the app can say so
      "200 starting",       no verdict: just restarted, or the heartbeat
      "200 unknown"         could not be read; wait for a real answer
      anything else         no verdict: a timeout, a connection error or a
                            proxy's own 502 / 503 is the SITE being down,
                            which the "site" check already texts about.
                            Counting it here too would send two SMSes for
                            one outage."""
    if name != "jobs":
        return bool(ok)
    if detail == "200":
        return True
    if detail in ("HTTP 503 stale", "200 disabled"):
        return False
    return None


def message(name, action, failures, detail, now=None):
    """The SMS text for an action, 160 characters at most."""
    stamp = sast_stamp(now)
    if name == "jobs":
        if action == "alert_down":
            body = ("iSpani: background jobs have stopped. Watchers, reminders "
                    f"and email are not running. Restart the app. {stamp}")
        else:
            body = f"iSpani: background jobs are running again. {stamp}"
    elif action == "alert_down":
        body = f"iSpani DOWN: health check failed {failures}x ({detail}). {stamp}"
    else:
        body = f"iSpani RECOVERED: health check OK again. {stamp}"
    return body[:160]


def send_sms(phones, body, token_id, token_secret):
    """Send one SMS per recipient via BulkSMS. Returns count of accepted sends.

    Without credentials, print instead of send (dev_mode) - never fails the
    run over a message: the red run + logs are themselves the fallback signal.
    """
    if not token_id or not token_secret:
        print(f"[dev_mode] SMS not sent (no BulkSMS credentials): {body}")
        return 0

    sent = 0
    for phone in phones:
        to_number = phone if phone.startswith("+") else "+" + phone
        try:
            resp = requests.post(
                BULKSMS_API,
                json=({"to": to_number, "body": body, "from": SENDER_ID}
                      if SENDER_ID else {"to": to_number, "body": body}),
                auth=(token_id, token_secret),
                timeout=15,
            )
            resp.raise_for_status()
            sent += 1
            print(f"SMS sent to {_masked(to_number)}")
        except requests.exceptions.RequestException as e:
            print(f"SMS to {_masked(to_number)} failed: {e}")
    return sent


def _masked(phone):
    """Last three digits only: the run log is not a place for a full number
    (GitHub masks a secret's exact value, not one number out of a list)."""
    return "..." + phone[-3:]


def main():
    url = os.getenv("HEALTHZ_URL", DEFAULT_URL)
    state_file = os.getenv("UPTIME_STATE_FILE", ".uptime-state.json")
    threshold = int(os.getenv("UPTIME_FAIL_THRESHOLD", "2"))
    timeout_s = int(os.getenv("UPTIME_TIMEOUT_S", "10"))
    phones = [p.strip() for p in os.getenv("UPTIME_ALERT_PHONES", "").split(",") if p.strip()]
    token_id = os.getenv("BULKSMS_TOKEN_ID", "")
    token_secret = os.getenv("BULKSMS_TOKEN_SECRET", "")

    name = os.getenv("UPTIME_CHECK_NAME", "site")

    ok, detail = check_health(url, timeout_s)
    verdict = counts_as(name, ok, detail)
    if verdict is None:
        # Not this check's question to answer; state stays as it was.
        print(f"check[{name}]: no verdict (detail={detail}); state unchanged")
        return 0

    state = load_state(state_file)
    new_state, action = decide(state, verdict, threshold)
    save_state(state_file, new_state)

    print(f"check[{name}]: ok={ok} detail={detail} state={new_state} action={action}")

    if action in ("alert_down", "alert_recovery"):
        send_sms(phones, message(name, action, new_state["consecutive_failures"],
                                 detail), token_id, token_secret)

    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
