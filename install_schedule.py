#!/usr/bin/env python3
"""Install the tracker as a recurring background job.

Picks the right mechanism for the machine:

* macOS   launchd. Unlike cron it survives sleep and fires a missed run on
          wake, which matters on a laptop that is not always awake.
* Linux   systemd user timer with Persistent=true, which also catches up
          after downtime. Falls back to plain cron if systemd is absent.
* Windows prints the schtasks command to run.

Default is six runs a day, the maximum `spread_hours` will place. Blocking
risk is driven by *requests*, not by how many times the job wakes up, and
several small runs make fewer requests than a few large ones while
re-checking the cheapest fares more often. Six was chosen over four because
the departure grid now prices every day in the window: at four runs the
cold pool needed ~19 days to come round, which is a long time to discover a
new fare. Six brings that down without raising the per-run budget.

The last run lands at 21:00, deliberately after `last_call_hour` (20:00),
so the held second email of the day still has a run to go out on. Moving
LAST_HOUR earlier than `last_call_hour` would strand it.
See README, "How often should this run".
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path

LABEL = "com.flighttracker.sjo-japan"
# Spread across the day rather than round the clock: fares are reloaded
# during business hours in the markets that matter, and a 03:00 run mostly
# just looks like a bot.
FIRST_HOUR = 6
LAST_HOUR = 21


def spread_hours(runs: int) -> list[int]:
    """`runs` times spread evenly between FIRST_HOUR and LAST_HOUR."""
    runs = max(1, min(runs, 6))
    if runs == 1:
        return [FIRST_HOUR]
    span = LAST_HOUR - FIRST_HOUR
    return sorted({FIRST_HOUR + round(i * span / (runs - 1)) for i in range(runs)})


def project_root() -> Path:
    return Path(__file__).resolve().parent


def python_exe() -> str:
    return sys.executable or "python3"


# --- macOS ----------------------------------------------------------------


def launchd_plist(hours: list[int], root: Path) -> str:
    intervals = "\n".join(
        "        <dict><key>Hour</key><integer>%d</integer>"
        "<key>Minute</key><integer>%d</integer></dict>" % (h, (h * 7) % 60)
        for h in hours
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_exe()}</string>
        <string>-m</string>
        <string>tracker.cli</string>
        <string>--runs-per-day</string>
        <string>{len(hours)}</string>
    </array>
    <key>WorkingDirectory</key><string>{root}</string>
    <key>StartCalendarInterval</key>
    <array>
{intervals}
    </array>
    <key>StandardOutPath</key><string>{root}/tracker.log</string>
    <key>StandardErrorPath</key><string>{root}/tracker.log</string>
    <key>RunAtLoad</key><false/>
</dict>
</plist>
"""


def install_launchd(hours: list[int], root: Path, dry: bool) -> int:
    target = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    content = launchd_plist(hours, root)
    if dry:
        print(f"--- would write {target} ---\n{content}")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(["launchctl", "unload", str(target)],
                   capture_output=True, check=False)
    res = subprocess.run(["launchctl", "load", str(target)],
                         capture_output=True, text=True, check=False)
    if res.returncode != 0:
        print(f"launchctl load failed: {res.stderr.strip()}")
        return 1
    print(f"Installed {target}")
    print(f"Runs at {', '.join(f'{h:02d}:xx' for h in hours)} local time.")
    print(f"Logs: {root}/tracker.log")
    print(f"Remove with: launchctl unload {target} && rm {target}")
    return 0


# --- Linux ----------------------------------------------------------------


def systemd_units(hours: list[int], root: Path) -> tuple[str, str]:
    oncal = "\n".join(f"OnCalendar=*-*-* {h:02d}:{(h * 7) % 60:02d}:00"
                      for h in hours)
    service = f"""[Unit]
Description=SJO-Japan flight price tracker

[Service]
Type=oneshot
WorkingDirectory={root}
ExecStart={python_exe()} -m tracker.cli --runs-per-day {len(hours)}
StandardOutput=append:{root}/tracker.log
StandardError=append:{root}/tracker.log
"""
    timer = f"""[Unit]
Description=Run the SJO-Japan flight tracker several times a day

[Timer]
{oncal}
RandomizedDelaySec=600
Persistent=true

[Install]
WantedBy=timers.target
"""
    return service, timer


def install_systemd(hours: list[int], root: Path, dry: bool) -> int:
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    service, timer = systemd_units(hours, root)
    if dry:
        print(f"--- {unit_dir}/flighttracker.service ---\n{service}")
        print(f"--- {unit_dir}/flighttracker.timer ---\n{timer}")
        return 0
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "flighttracker.service").write_text(service, encoding="utf-8")
    (unit_dir / "flighttracker.timer").write_text(timer, encoding="utf-8")
    for cmd in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "flighttracker.timer"],
    ):
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            print(f"{' '.join(cmd)} failed: {res.stderr.strip()}")
            return 1
    print(f"Installed timer in {unit_dir}")
    print(f"Runs at {', '.join(f'{h:02d}:xx' for h in hours)} local time.")
    print("Status: systemctl --user list-timers flighttracker.timer")
    print("Remove: systemctl --user disable --now flighttracker.timer")
    print("\nIf the machine is a laptop that logs out, also run:")
    print(f"  sudo loginctl enable-linger {os.getenv('USER', 'youruser')}")
    return 0


# --- cron fallback --------------------------------------------------------


def cron_lines(hours: list[int], root: Path) -> str:
    return "\n".join(
        f"{(h * 7) % 60} {h} * * * cd {root} && {python_exe()} -m tracker.cli "
        f"--runs-per-day {len(hours)} >> {root}/tracker.log 2>&1"
        for h in hours
    )


def install_cron(hours: list[int], root: Path, dry: bool) -> int:
    block = cron_lines(hours, root)
    marker = "# flight-tracker"
    if dry:
        print(f"--- would add to crontab ---\n{marker}\n{block}")
        return 0
    current = subprocess.run(["crontab", "-l"], capture_output=True,
                             text=True, check=False).stdout
    kept = [ln for ln in current.splitlines()
            if marker not in ln and "tracker.cli" not in ln]
    new = "\n".join([*kept, marker, block, ""])
    res = subprocess.run(["crontab", "-"], input=new, text=True,
                         capture_output=True, check=False)
    if res.returncode != 0:
        print(f"crontab install failed: {res.stderr.strip()}")
        return 1
    print("Installed cron entries:")
    print(block)
    print("\nNote: plain cron does not catch up runs missed while the machine")
    print("was off or asleep. systemd timers and launchd do.")
    return 0


# --- Windows --------------------------------------------------------------


def print_windows(hours: list[int], root: Path) -> int:
    """Print PowerShell that registers the jobs. Deliberately not schtasks.

    This project lives at "D:\\Levi\\Japan flight scanner\\..." and schtasks
    mangles a /TR value containing spaces - it reads the path as separate
    arguments and dies with "Invalid argument/option - 'flight'". Escaping
    it well enough to survive both cmd.exe and the task scheduler is
    possible and unreadable.

    Register-ScheduledTask takes the executable and its arguments as
    separate parameters, so quoting stops being the caller's problem. It
    also registers daily jobs without administrator rights.
    """
    py = python_exe()
    n_runs = len(hours)
    hour_list = ", ".join(str(h) for h in hours)

    print("Paste this into PowerShell. No administrator rights needed.\n")
    print(f'$root = "{root}"')
    print(f'$py   = "{py}"')
    print(f"$hours = @({hour_list})")
    print("for ($i = 0; $i -lt $hours.Count; $i++) {")
    print("  $h = $hours[$i]; $m = ($h * 7) % 60; $n = $i + 1")
    print("  $a = New-ScheduledTaskAction -Execute $py "
          f"-Argument \"-m tracker.cli --runs-per-day {n_runs}\" "
          "-WorkingDirectory $root")
    print("  $t = New-ScheduledTaskTrigger -Daily "
          "-At ([datetime]::Today.AddHours($h).AddMinutes($m))")
    # -StartWhenAvailable is the Task Scheduler checkbox for "run as soon as
    # possible after a missed start", which is what matters on a laptop that
    # was asleep at 06:00.
    print("  $s = New-ScheduledTaskSettingsSet -StartWhenAvailable "
          "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
          "-ExecutionTimeLimit (New-TimeSpan -Hours 1)")
    print('  Register-ScheduledTask -TaskName "FlightTracker$n" -Action $a '
          "-Trigger $t -Settings $s -Force | Out-Null")
    print("}")
    print(f'"registered {n_runs} daily runs"')

    # The sweep is a service, not a scheduled job: it runs continuously and
    # resumes itself. An AtLogOn trigger would need admin rights; a file in
    # the Startup folder does not, and achieves the same thing.
    print("\n\nThen, so the full-coverage sweep survives a reboot:\n")
    print('$startup = [Environment]::GetFolderPath("Startup")')
    print(f'$root = "{root}"')
    print(f'$py   = "{py}"')
    print("$lines = @(")
    print('  "@echo off",')
    print('  "cd /d \\"$root\\"",')
    print('  "start \\"\\" /min \\"$py\\" -u sweep_forever.py '
          '--batch 25 --delay 6 >> \\"$root\\sweep.log\\" 2>&1"')
    print(")")
    print('Set-Content -Path (Join-Path $startup "FlightTrackerSweep.cmd") '
          "-Value $lines -Encoding ASCII")
    print('"sweep will start at every logon"')

    print("\n\nTo start the sweep now rather than waiting for a logon:")
    print(f'  cd "{root}"')
    print(f'  "{py}" sweep_forever.py --batch 25 --delay 6')
    print("\nCheck on it any time with:  python sweep_forever.py --status")
    return 0


# --- entry point ----------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install the tracker as a recurring job.")
    parser.add_argument("--runs", type=int, default=6, choices=range(1, 7),
                        metavar="1-6", help="runs per day (default 6)")
    parser.add_argument("--hours", type=str, default="",
                        help="explicit local hours, e.g. 6,11,16,21")
    parser.add_argument("--method",
                        choices=("auto", "launchd", "systemd", "cron", "windows"),
                        default="auto")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be installed and stop")
    args = parser.parse_args()

    if args.hours:
        try:
            hours = sorted({int(h) % 24 for h in args.hours.split(",") if h.strip()})
        except ValueError:
            print("--hours must be a comma-separated list of hours, e.g. 6,11,16,21")
            return 2
    else:
        hours = spread_hours(args.runs)

    root = project_root()
    if not (root / "preferences.json").exists():
        print("preferences.json not found. Run this first:\n")
        print("  python setup_tracker.py")
        return 2

    method = args.method
    if method == "auto":
        system = platform.system()
        if system == "Darwin":
            method = "launchd"
        elif system == "Windows":
            method = "windows"
        elif _has("systemctl"):
            method = "systemd"
        else:
            method = "cron"

    print(f"Method: {method}   Runs/day: {len(hours)}   "
          f"Hours: {', '.join(str(h) for h in hours)}\n")

    if method == "launchd":
        return install_launchd(hours, root, args.dry_run)
    if method == "systemd":
        return install_systemd(hours, root, args.dry_run)
    if method == "cron":
        return install_cron(hours, root, args.dry_run)
    return print_windows(hours, root)


def _has(cmd: str) -> bool:
    from shutil import which
    return which(cmd) is not None


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # Output was piped into something like `head`; that is not an error.
        os._exit(0)
