"""Send one test SMS to every alert number, through the same code the real
alert uses. Run by the "Send a test SMS" step of a manual Uptime run.

Exit code 0 only when BulkSMS accepted a message for every number, so a
wrong token or a bad number shows as a red run.
"""
import os
import sys

import uptime_check


def main():
    phones = [p.strip() for p in os.getenv("UPTIME_ALERT_PHONES", "").split(",") if p.strip()]
    if not phones:
        print("test SMS: UPTIME_ALERT_PHONES is empty, nothing to send")
        return 1
    body = f"iSpani: test alert from the uptime check. Nothing is wrong. {uptime_check.sast_stamp()}"
    accepted = uptime_check.send_sms(phones, body, os.getenv("BULKSMS_TOKEN_ID", ""),
                                     os.getenv("BULKSMS_TOKEN_SECRET", ""))
    print(f"test SMS: BulkSMS accepted {accepted} of {len(phones)}")
    return 0 if accepted == len(phones) else 1


if __name__ == "__main__":
    sys.exit(main())
