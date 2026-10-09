#!/usr/bin/env python3
"""Mail the operator when an account a review role uses has stopped working.

A run refuses to start on an account that is signed out, which is safe but
silent: on 2026-10-09 ~/.claude lost its login at 18:05 and the only sign was
an `all` run that logged wrong-claude-account and a dashboard with nothing new.

    check-auth.py            check, and mail $REVIEW_ALERT_MAIL on a problem
    check-auth.py --dry-run  print the mail instead of sending it

Two sources. It opens no session of its own: asking an account to do anything
can refresh its token, and two refreshes at once can sign it out.

  review-auth.sh status   the same sign-in checks a run makes before it starts
  quota.jsonl             the hourly probe, which really uses each account: it
                          catches a login the server no longer accepts, which
                          still looks signed in locally

Mails on every check that finds a problem, so the reminder repeats until the
account is signed in again. Mail goes through the local sendmail, from
$REVIEW_ALERT_FROM when set, which needs a relay the recipient accepts
(docs/review-box.md).
"""
import datetime
import os
import socket
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quota                                                      # noqa: E402

BIN = os.path.dirname(os.path.abspath(__file__))
# A probe reading older than this says nothing about the account now: the probe
# runs hourly, so a gap this long means it has stopped, which is not this check's
# business to report.
PROBE_AGE = datetime.timedelta(hours=3)
# What review-auth.sh status says about a role that runs cannot use.
BAD = ("NOT SIGNED IN", "refuse", "REFUSE", "(not set)")


def account_dir(tool, shown):
    """The real directory behind status's ACCOUNT-DIR column."""
    if shown in ("." + tool, "~/." + tool):
        return os.path.realpath(os.path.join(quota.HOME, "." + tool))
    return os.path.realpath(os.path.join(quota.AUTH, shown))


def login_hint(tool, directory):
    if tool == "claude":
        return "BROWSER=echo CLAUDE_CONFIG_DIR=%s claude auth login" % directory
    return "CODEX_HOME=%s codex login --device-auth" % directory


def check(now=None):
    """(problems, status text). A problem is (role, account dir, why)."""
    now = now or datetime.datetime.now().astimezone()
    out = subprocess.run([os.path.join(BIN, "review-auth.sh"), "status"],
                         capture_output=True, text=True)
    status = (out.stdout + out.stderr).strip()
    if out.returncode != 0:
        return [("-", None, "review-auth.sh status exited %d" % out.returncode)], status
    problems, used = [], {}
    for row in out.stdout.splitlines()[1:]:
        f = row.split()
        if len(f) < 2:
            continue
        role, shown = f[0], f[1]
        tool = role.split("-", 1)[0]
        directory = None if shown == "-" else account_dir(tool, shown)
        if any(b in row for b in BAD):
            problems.append((role, directory, " ".join(f[2:])))
        elif directory:
            used.setdefault((tool, directory), []).append(role)
    probes = quota.recorded()
    for (tool, directory), roles in sorted(used.items()):
        rec = probes.get((tool, directory))
        if not rec or not rec.get("error") or rec.get("busy"):
            continue
        try:
            at = datetime.datetime.fromisoformat(rec["at"])
        except (KeyError, TypeError, ValueError):
            continue
        if now - at > PROBE_AGE:
            continue
        for role in roles:
            problems.append((role, directory, "hourly probe at %s failed: %s"
                             % (at.strftime("%H:%M"), rec["error"])))
    return problems, status


def message(to, problems, status, sender=None):
    host = os.environ.get("REVIEW_BOX_NAME") or socket.gethostname()
    lines = ["Runs on %s cannot use these accounts:" % host, ""]
    hints = []
    for role, directory, why in problems:
        lines.append("  %-16s %s  %s" % (role, directory or "-", why))
        if directory:
            hint = login_hint(role.split("-", 1)[0], directory)
            if hint not in hints:
                hints.append(hint)
    if hints:
        lines += ["", "Sign in again on %s with:" % host, ""] + ["  " + h for h in hints]
    lines += ["", "review-auth.sh status:", "", status, ""]
    head = "From: APReview %s <%s>\n" % (host, sender) if sender else ""
    return ("%sTo: %s\nSubject: APReview %s: %d account role(s) cannot run\n\n%s"
            % (head, to, host, len(problems), "\n".join(lines)))


def main(argv):
    dry = "--dry-run" in argv
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    problems, status = check()
    if not problems:
        print("%s ok" % stamp)
        return 0
    print("%s %s" % (stamp, "; ".join("%s: %s" % (r, w) for r, _, w in problems)))
    to = os.environ.get("REVIEW_ALERT_MAIL")
    if not to:
        print("%s not mailed: REVIEW_ALERT_MAIL is not set (etc/local.conf)" % stamp)
        return 1
    sender = os.environ.get("REVIEW_ALERT_FROM")
    mail = message(to, problems, status, sender)
    if dry:
        print(mail)
        return 1
    sendmail = os.environ.get("REVIEW_SENDMAIL") or "/usr/sbin/sendmail"
    # the envelope sender too: it is what a relay signs for and what SPF checks
    cmd = [sendmail, "-t", "-oi"] + (["-f", sender] if sender else [])
    sent = subprocess.run(cmd, input=mail, text=True, capture_output=True)
    print("%s mailed %s%s" % (stamp, to, "" if sent.returncode == 0 else
                              ": sendmail exited %d %s" % (sent.returncode,
                                                            sent.stderr.strip())))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
