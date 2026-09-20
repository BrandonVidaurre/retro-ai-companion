#!/usr/bin/env python3
"""
chord_hotkey.py — L+R double-tap trigger for the Retro AI Companion
===================================================================

Watches the GPi Case 2W's controller and fires RetroArch's AI Service
when both back triggers are double-tapped.

Why this exists instead of a RetroArch hotkey binding
-----------------------------------------------------
RetroArch has exactly one global "hotkey enable" button. Every combo it
supports is <enable> + <action>, so getting L+R would mean making L the
enable button — which moves quit from Select+Start to L+Start, moves the
menu from Select+X to L+X, and makes the hotkey layer swallow L inside
SNES and GBA games that actually use it.

So the chord is detected here instead, and the trigger is delivered to
RetroArch over its network command interface (UDP 55355). Select-based
hotkeys are untouched, and the chord shape is ours to tune.

Reads /dev/input/js0 (the legacy joystick API), which allows multiple
concurrent readers — RetroArch keeps its own handle open and neither
side interferes with the other. Stdlib only, Python 3.7+ (RetroPie 4.8
ships 3.7.3).

    # on the Pi, see what the daemon sees — button indices, chord state
    python3 chord_hotkey.py --watch

    # dry run: log the trigger, send nothing
    python3 chord_hotkey.py --dry-run -v

    # for real
    python3 chord_hotkey.py

Button indices are the ones verified on hardware with `jstest --event`
on Sep 18: A=0 B=1 X=2 Y=3 L=4 R=5 Select=8 Start=9.
"""

from __future__ import annotations

import argparse
import errno
import logging
import os
import select
import signal
import socket
import struct
import sys
import time

# ──────────────────────────────────────────────────────────────────────
#  Defaults
# ──────────────────────────────────────────────────────────────────────

DEVICE = "/dev/input/js0"
BTN_L, BTN_R = 4, 5              # verified with jstest on the GPi Case 2W

HOST, PORT = "127.0.0.1", 55355  # RetroArch network command interface
COMMAND = "AI_SERVICE"

# 1.0s, not the 0.5 this started at: 0.5 is a desk-keyboard number. Two hands
# releasing and re-squeezing the GPi's stiff shoulder buttons takes 600-800ms,
# measured on the real case — at 0.5 the second tap always aged out and the
# chord never completed.
TAP_WINDOW = 1.00                # both taps must land inside this, seconds
CHORD_SLOP = 0.12                # L and R count as simultaneous within this
# Both triggers must stay down together this long for the press to count.
# This is the chatter defence: a bouncing switch flickers every ~50ms and can
# never hold a steady contact, while a real finger holds for 100ms+ without
# trying. Anything below the chatter period would let the noise through.
HOLD = 0.08
COOLDOWN = 3.0                   # ignore the chord this long after firing
# Release smoothing — the setting that makes this work on the real case.
#
# The GPi's triggers chatter *while held*: one deliberate press arrives as a
# stream of down/up pairs rather than a clean edge. So an "up" is not believed
# straight away. If the contact closes again within RELEASE seconds it was
# chatter, and the button is treated as having stayed down throughout; only a
# quiet gap longer than this counts as the player letting go.
#
# Must be longer than the chatter period (~50ms, measured on the case) and
# shorter than the gap between two deliberate taps (150ms+).
RELEASE = 0.06

# Edge-level debounce. Superseded by RELEASE smoothing and off by default:
# discarding edges here would swallow the re-close that proves a release was
# only chatter. Kept as a flag in case other hardware needs it.
DEBOUNCE = 0.0
REOPEN_DELAY = 2.0               # wait between attempts to reopen the device

# Legacy joystick event: uint32 time, int16 value, uint8 type, uint8 number
JS_EVENT = struct.Struct("IhBB")
JS_EVENT_BUTTON = 0x01
JS_EVENT_INIT = 0x80             # synthetic events sent on open — ignore them

log = logging.getLogger("chord")


# ──────────────────────────────────────────────────────────────────────
#  Chord detection
# ──────────────────────────────────────────────────────────────────────

class ChordDetector:
    """Double-tap of two buttons pressed and briefly held together.

    A 'tap' is both buttons staying down together for `hold` seconds. Two
    taps inside TAP_WINDOW fire the trigger.

    The hold requirement is what makes this survive real hardware. An
    earlier version counted a tap the instant both buttons were down, and
    on a case whose left trigger chatters under pressure that fired
    continuously: every chatter edge that landed near a held R looked like
    a fresh chord. Requiring the contact to stay closed for 80ms discards
    that noise, because a bouncing switch cannot hold and a finger cannot
    help it.

    A tap is only counted once per press, so leaning on both shoulders
    through a fight does nothing — which is also what keeps a game's own
    L+R input from summoning the companion.
    """

    def __init__(self, left=BTN_L, right=BTN_R, window=TAP_WINDOW,
                 slop=CHORD_SLOP, cooldown=COOLDOWN, taps_needed=2,
                 debounce=DEBOUNCE, hold=HOLD, release=RELEASE):
        self.left = left
        self.right = right
        self.window = window
        self.slop = slop
        self.cooldown = cooldown
        self.taps_needed = taps_needed
        self.debounce = debounce
        self.hold = hold
        self.release = release

        self.down = {left: None, right: None}   # button -> press timestamp
        self.pending_up = {}                    # button -> when it opened
        self.last_change = {}                   # button -> last accepted edge
        self.bounces = 0                        # chatter edges absorbed
        self.chord_since = None                 # both down since when
        self.chord_counted = False              # this press already scored
        self.taps = []                          # timestamps of recent taps
        # None, not 0.0: time.monotonic() is small right after boot, and a
        # zero epoch would make the daemon deaf for its first `cooldown`
        # seconds — exactly when someone is testing it.
        self.last_fire = None

    def reset(self):
        """Forget all state. Called when the device disappears, so a press
        that straddles a controller dropout can't complete a chord."""
        self.down = {self.left: None, self.right: None}
        self.pending_up = {}
        self.last_change = {}
        self.chord_since = None
        self.chord_counted = False
        self.taps = []

    def _break_chord(self):
        self.chord_since = None
        self.chord_counted = False

    def button(self, number, pressed, now):
        """Feed one button event. Updates state only — firing is decided by
        tick(), because the hold requirement is a matter of elapsed time
        rather than of any single event arriving."""
        if number not in self.down:
            return False

        # Contact bounce filter. Swallowing a real edge here is self-correcting:
        # the next genuine transition is always >30ms later and gets through.
        last = self.last_change.get(number)
        if last is not None and now - last < self.debounce:
            self.bounces += 1
            log.debug("button %d edge discarded as bounce (%.0fms)",
                      number, (now - last) * 1000)
            return False
        self.last_change[number] = now

        if not pressed:
            # Don't believe it yet — tick() decides, once RELEASE has passed
            # with the contact still open.
            if self.down[number] is not None:
                self.pending_up[number] = now
            return False

        # A press. If the contact re-closed while a release was pending, the
        # open was chatter: cancel it and leave the original press time alone,
        # so the chord's hold clock is not restarted by noise.
        if self.pending_up.pop(number, None) is not None:
            self.bounces += 1
            log.debug("button %d re-closed within %.0fms — chatter, still down",
                      number, self.release * 1000)
            return False
        if self.down[number] is not None:
            return False                        # already down; duplicate edge

        self.down[number] = now

        lt, rt = self.down[self.left], self.down[self.right]
        if lt is None or rt is None:
            self._break_chord()
            return False
        if abs(lt - rt) > self.slop:
            # One trigger was held long before the other — a deliberate
            # hold, not a chord. Don't count it.
            self._break_chord()
            return False
        if self.chord_since is None:
            self.chord_since = max(lt, rt)      # clock starts when BOTH are in
        return False

    def pending(self):
        """True while a chord is waiting out its hold."""
        return self.chord_since is not None and not self.chord_counted

    def busy(self):
        """True when a timer is running — the daemon polls fast in this state
        so holds and releases resolve on schedule instead of at the next
        button event, which might never come."""
        return self.pending() or bool(self.pending_up)

    def tick(self, now):
        """Call frequently. Returns True when the trigger should fire."""
        # Settle any release that has now stayed open long enough to be real.
        for b in list(self.pending_up):
            if now - self.pending_up[b] >= self.release:
                del self.pending_up[b]
                self.down[b] = None
                self._break_chord()

        if not self.pending():
            return False
        if now - self.chord_since < self.hold:
            return False
        self.chord_counted = True
        return self._tap(now)

    def _tap(self, now):
        if self.last_fire is not None and now - self.last_fire < self.cooldown:
            log.debug("tap ignored — %.1fs of cooldown left",
                      self.cooldown - (now - self.last_fire))
            return False

        self.taps = [t for t in self.taps if now - t <= self.window]
        self.taps.append(now)
        log.debug("tap %d/%d", len(self.taps), self.taps_needed)

        if len(self.taps) >= self.taps_needed:
            self.taps = []
            self.last_fire = now
            return True
        return False


# ──────────────────────────────────────────────────────────────────────
#  Trigger delivery
# ──────────────────────────────────────────────────────────────────────

class Trigger:
    """Sends a RetroArch network command. Connectionless and fire-and-forget:
    if RetroArch isn't running the datagram is simply dropped, which is the
    correct behaviour — the chord should never crash the daemon."""

    def __init__(self, host=HOST, port=PORT, command=COMMAND, dry_run=False):
        self.addr = (host, port)
        self.command = command
        self.dry_run = dry_run
        self.sock = None if dry_run else socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def fire(self):
        if self.dry_run:
            log.info("FIRE (dry run) -> %s to %s:%d", self.command, *self.addr)
            return
        try:
            self.sock.sendto((self.command + "\n").encode(), self.addr)
            log.info("FIRE -> %s to %s:%d", self.command, *self.addr)
        except OSError as exc:
            log.warning("send failed (%s) — is RetroArch running with "
                        "network_cmd_enable = true?", exc)


# ──────────────────────────────────────────────────────────────────────
#  Device loop
# ──────────────────────────────────────────────────────────────────────

class Daemon:
    def __init__(self, args):
        self.path = args.device
        self.watch = args.watch
        self.detector = ChordDetector(
            left=args.left, right=args.right,
            window=args.window, slop=args.slop,
            cooldown=args.cooldown, taps_needed=args.taps,
            debounce=args.debounce, hold=args.hold, release=args.release,
        )
        self.trigger = Trigger(args.host, args.port, args.command, args.dry_run)
        self.running = True
        self.fd = None

    def stop(self, *_):
        self.running = False

    def _open(self):
        """Open the joystick, retrying forever. The GPi's button MCU drops off
        the bus on a soft reboot and comes back on a power cycle, so the daemon
        has to survive the device vanishing under it."""
        warned = False
        while self.running:
            try:
                fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
                log.info("opened %s", self.path)
                return fd
            except OSError as exc:
                if not warned:
                    log.warning("waiting for %s (%s)", self.path, exc.strerror)
                    warned = True
                time.sleep(REOPEN_DELAY)
        return None

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

        while self.running:
            self.fd = self._open()
            if self.fd is None:
                break
            self.detector.reset()
            try:
                self._pump()
            finally:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self.fd = None

        if self.detector.bounces:
            log.info("discarded %d bouncing edge(s) — if that number is large "
                     "and you were barely touching the case, the switch is "
                     "chattering", self.detector.bounces)
        log.info("stopped")
        return 0

    def _pump(self):
        buf = b""
        while self.running:
            # Idle at half a second; tighten to 10ms only while a chord is
            # counting down its hold, so the tap is timed accurately without
            # waking this process 50x a second on battery for no reason.
            timeout = 0.01 if self.detector.busy() else 0.5
            ready, _, _ = select.select([self.fd], [], [], timeout)
            if not ready:
                if self.detector.tick(time.monotonic()):
                    self.trigger.fire()
                continue
            try:
                chunk = os.read(self.fd, JS_EVENT.size * 32)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    continue
                log.warning("%s went away (%s) — reopening",
                            self.path, exc.strerror)
                return
            if not chunk:
                log.warning("%s returned EOF — reopening", self.path)
                return

            buf += chunk
            while len(buf) >= JS_EVENT.size:
                frame, buf = buf[:JS_EVENT.size], buf[JS_EVENT.size:]
                self._event(*JS_EVENT.unpack(frame))
            if self.detector.tick(time.monotonic()):
                self.trigger.fire()

    def _event(self, _time, value, etype, number):
        if etype & JS_EVENT_INIT:
            return                                   # startup snapshot
        if not etype & JS_EVENT_BUTTON:
            return                                   # axes: D-pad, not ours
        if self.watch:
            log.info("button %-2d %s", number, "down" if value else "up")
        self.detector.button(number, bool(value), time.monotonic())


# ──────────────────────────────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────────────────────────────

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Fire RetroArch's AI Service on an L+R double-tap.")
    p.add_argument("--device", default=DEVICE, help="joystick device")
    p.add_argument("--left", type=int, default=BTN_L, help="left trigger index")
    p.add_argument("--right", type=int, default=BTN_R, help="right trigger index")
    p.add_argument("--taps", type=int, default=2, help="taps required to fire")
    p.add_argument("--window", type=float, default=TAP_WINDOW,
                   help="seconds both taps must land within")
    p.add_argument("--slop", type=float, default=CHORD_SLOP,
                   help="seconds L and R may differ and still count as together")
    p.add_argument("--cooldown", type=float, default=COOLDOWN,
                   help="seconds to ignore the chord after firing")
    p.add_argument("--release", type=float, default=RELEASE,
                   help="seconds a trigger must stay open before it counts as "
                        "released; absorbs chatter during a held press")
    p.add_argument("--hold", type=float, default=HOLD,
                   help="seconds both triggers must stay down together for a "
                        "press to count (raise this if a chattering switch "
                        "still gets through)")
    p.add_argument("--debounce", type=float, default=DEBOUNCE,
                   help="discard same-button edges closer together than this "
                        "(contact bounce); 0 disables")
    p.add_argument("--host", default=HOST, help="RetroArch command host")
    p.add_argument("--port", type=int, default=PORT, help="RetroArch command port")
    p.add_argument("--command", default=COMMAND, help="network command to send")
    p.add_argument("--dry-run", action="store_true", help="log instead of sending")
    p.add_argument("--watch", action="store_true",
                   help="log every button event — use this to re-verify indices")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if (args.verbose or args.watch) else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    log.info("chord: double-tap buttons %d+%d, each held %.0fms, within %.2fs "
             "(release %.0fms, cooldown %.1fs) -> %s",
             args.left, args.right, args.hold * 1000, args.window,
             args.release * 1000, args.cooldown, args.command)
    return Daemon(args).run()


if __name__ == "__main__":
    sys.exit(main())
