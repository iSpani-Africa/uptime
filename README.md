# iSpani uptime check

Checks that https://ispani.africa is up, from outside its own server, and
texts the people on call when it is not.

- `.github/workflows/uptime.yml` runs `uptime_check.py` on a schedule against
  the site's two health endpoints.
- Two failed runs in a row send one "down" SMS. The first good run after that
  sends one "recovered" SMS.
- `.github/workflows/keepalive.yml` makes one empty commit a month, because
  GitHub switches off scheduled workflows in a public repository that has had
  no activity for 60 days.

The SMS credentials and the phone numbers are repository secrets. Nothing
private is in this repository or in its run logs; phone numbers are logged by
their last three digits only.

`uptime_check.py` is a copy. The tested original lives in the main iSpani
repository at `scripts/uptime_check.py`; change it there first, then copy it
here.
