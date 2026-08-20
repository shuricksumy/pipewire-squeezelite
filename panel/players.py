"""Supervised squeezelite players.

Each player is a long-running `squeezelite` child process of this container,
bound to one PipeWire sink. Several of them live in one process tree, each with
its own environment -- which is what makes a per-player PIPEWIRE_NODE work
without running one container per DAC.

The launch recipe mirrors entrypoint.sh's ROLE=player path (same flags, same
5s->60s reconnect backoff), so a panel-managed player behaves exactly like a
single-player container.

Trade-off worth knowing: restarting this container stops every player. The
one-container-per-player approach survives a panel restart; this one does not.
"""

import json
import os
import random
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "players.json")

SQUEEZELITE = os.environ.get("SQUEEZELITE", "squeezelite")
PW_DUMP = os.environ.get("PW_DUMP", "pw-dump")
WPCTL = os.environ.get("WPCTL", "wpctl")

# Same backoff shape as entrypoint.sh: a session that stayed up for a while is
# not part of a failure streak, so the delay resets rather than carrying a 60s
# penalty over from an outage that is long since fixed.
RETRY_START = 5.0
RETRY_MAX = 60.0
HEALTHY_AFTER = 60.0

# How long to wait for a bound sink to appear before giving up on this attempt.
NODE_WAIT_SECONDS = 20.0

# Watchdog. squeezelite exits when it cannot open the device at startup, but a
# sink that disappears *mid-stream* is not guaranteed to take the process with
# it -- and a supervisor that only watches for process exit would then report a
# healthy player that cannot make a sound. So while a player runs we also watch
# its sink and restart once it has been gone long enough not to be a blip: a DAC
# re-clocking for a sample-rate change drops off the graph for a moment and must
# not count as a failure.
HEALTH_INTERVAL = 3.0
SINK_GRACE = 15.0

LOG_LINES = 200

# Any printable text, up to 64 characters. Deliberately permissive: the name is
# passed to squeezelite as a single argv element, never through a shell, so this
# is not a security boundary -- it only keeps control characters (which would
# corrupt the log stream and the JSON config) out. An ASCII-only rule would
# reject perfectly reasonable names like "Kitchen · DX5" or anything Cyrillic.
NAME_RE = re.compile(r"[^\x00-\x1f\x7f]{1,64}")
NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:_-]{0,255}$")
MAC_RE = re.compile(r"^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$")
MIXER_RE = re.compile(r"^[A-Za-z0-9 _.\-]{1,32}$")
# ALSA PCM names as squeezelite -l prints them: default, sysdefault:CARD=DX5,
# hw:CARD=DX5,DEV=0, plughw:0,0 ...
ALSA_DEVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:,=.\-]{0,127}$")
LATENCY_RE = re.compile(r"^\d{1,7}/\d{1,7}$")

# squeezelite -a <b>:<p>:<f>:<m>; f is the sample format it asks ALSA for.
ALSA_FORMATS = ("", "16", "24", "24_3", "32")


def resolve_role(env=None):
    """Which role this container runs, mirroring entrypoint.sh.

    Unset means the panel: with nothing configured, offering a UI beats failing
    for want of a SERVER_IP.
    """
    env = os.environ if env is None else env
    return (env.get("ROLE") or "").strip() or "panel"


def _env_int(name, fallback):
    try:
        return int(os.environ.get(name) or fallback)
    except ValueError:
        return fallback


# Defaults for a newly created player. Every one is overridable per player in
# the UI; these only decide what the Add-player form starts with, so a host
# whose server lives elsewhere only has to be told once.
SERVER_HOST = os.environ.get("SERVER_IP", "")
SERVER_PORT = _env_int("SERVER_PORT", 3483)

# SERVER_IP may carry its port ("192.168.1.50:3483"); split it so the form gets
# both fields right rather than putting a host:port string in the host box.
if ":" in SERVER_HOST and "//" not in SERVER_HOST:
    _host, _, _port = SERVER_HOST.rpartition(":")
    if _port.isdigit():
        SERVER_HOST, SERVER_PORT = _host, int(_port)

DEFAULTS = {
    "name": "",
    # "pipewire" plays into the host's PipeWire session and binds to one sink.
    # "alsa" talks to an ALSA device directly, which is the way out when
    # PipeWire is broken, absent, or simply not what you want on this host.
    "output_mode": "pipewire",
    "node": "",
    "alsa_device": "default",
    # Blank means "generate one". The server tells players apart by MAC, so two
    # players sharing one would fight over a single slot in its player list.
    "mac": "",
    # Blank means "discover the server", which is what Music Assistant's
    # Squeezelite provider supports on the same subnet.
    "server": SERVER_HOST,
    "port": SERVER_PORT,
    # -U <name>: hand volume to the mixer instead of attenuating in software.
    "mixer": "Master",
    # -a <buffer>:<period>:<format>:<mmap>
    "alsa_buffer": 16384,
    "alsa_period": 8,
    "alsa_format": "24",
    "alsa_mmap": 0,
    # -b <stream>:<output>
    "stream_buffer": 8000,
    "output_buffer": 12000,
    # -C <seconds>: close the output device when idle, so another player (or the
    # host) can take an exclusive DAC.
    "close_delay": 5,
    # -D: DSD over PCM / native DSD.
    "dsd": False,
    "pipewire_latency": "",
    "volume": 1.0,
    "autostart": True,
    "extra": "",
}


class PlayerError(ValueError):
    """A player definition was rejected."""

    status = 400


def random_mac():
    """A locally-administered unicast MAC, stable once stored.

    Bit 0x02 of the first octet marks it locally administered and bit 0x01 must
    stay clear (that one means multicast), so the first octet is drawn from the
    02/06/0A/0E family. Squeezebox servers key a player's saved settings off
    this address, so it is generated once and then lives in players.json.
    """
    first = (random.getrandbits(8) & 0xFC) | 0x02
    rest = [random.getrandbits(8) for _ in range(5)]
    return ":".join("%02X" % o for o in [first] + rest)


def _pw_env():
    env = dict(os.environ)
    env.setdefault("PIPEWIRE_RUNTIME_DIR", "/tmp")
    env.setdefault("PIPEWIRE_REMOTE", "pipewire-0")
    return env


def list_sinks():
    """Audio sinks PipeWire currently exposes, fresh state each call.

    Best effort: no PipeWire socket means no sinks, not an error -- the panel
    still lists players, it just cannot pre-validate their nodes.
    """
    if not shutil.which(PW_DUMP):
        return []
    try:
        raw = subprocess.run(
            [PW_DUMP], capture_output=True, text=True, timeout=10, env=_pw_env()
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if raw.returncode != 0:
        return []
    try:
        objects = json.loads(raw.stdout or "[]")
    except ValueError:
        return []

    sinks = []
    for item in objects:
        if not isinstance(item, dict):
            continue
        props = (item.get("info") or {}).get("props") or {}
        if props.get("media.class") != "Audio/Sink":
            continue
        name = props.get("node.name")
        if not name:
            continue
        sinks.append(
            {
                "id": item.get("id"),
                "node": name,
                "description": props.get("node.description") or name,
                "rate": props.get("audio.rate"),
            }
        )
    sinks.sort(key=lambda s: s["node"])
    return sinks


_alsa_cache = {"at": 0.0, "devices": []}
ALSA_CACHE_SECONDS = 30.0


def list_alsa_devices(max_age=ALSA_CACHE_SECONDS):
    """ALSA outputs, straight from `squeezelite -l`.

    This is the escape hatch for a host where PipeWire is broken or was never
    set up: squeezelite can address the hardware itself. Best effort -- if the
    binary is missing the panel simply offers no ALSA choices.
    """
    # Cached: the device list changes only when hardware is plugged in, while
    # the panel polls its config every few seconds and this costs a subprocess
    # that can take seconds to answer.
    if max_age and time.time() - _alsa_cache["at"] < max_age:
        return _alsa_cache["devices"]

    if not shutil.which(SQUEEZELITE) and not os.path.exists(SQUEEZELITE):
        return []
    try:
        raw = subprocess.run(
            [SQUEEZELITE, "-l"], capture_output=True, text=True, timeout=15,
            env=_pw_env(),
        )
    except (OSError, subprocess.SubprocessError):
        _alsa_cache["at"] = time.time()
        _alsa_cache["devices"] = []
        return []

    devices = []
    for line in (raw.stdout or "").splitlines():
        if not line.startswith("  ") or line.strip().endswith(":"):
            continue
        parts = line.strip().split(" - ", 1)
        name = parts[0].strip()
        if not name or not ALSA_DEVICE_RE.match(name):
            continue
        devices.append(
            {
                "device": name,
                "description": parts[1].strip() if len(parts) > 1 else name,
                # Entries that address hardware rather than a conversion plugin.
                # Shown first because they are what somebody bypassing PipeWire
                # is looking for.
                "hardware": name.startswith(("hw:", "plughw:", "sysdefault", "front:"))
                or name == "default",
            }
        )
    devices.sort(key=lambda d: (not d["hardware"], d["device"]))
    _alsa_cache["at"] = time.time()
    _alsa_cache["devices"] = devices
    return devices


def sink_present(node):
    return any(s["node"] == node for s in list_sinks())


def set_sink_volume(node, volume):
    """Unmute and set a sink's volume, matching what entrypoint.sh does once."""
    if not shutil.which(WPCTL):
        return
    target = next((s for s in list_sinks() if s["node"] == node), None)
    if target is None or target.get("id") is None:
        return
    for args in (
        [WPCTL, "set-mute", str(target["id"]), "0"],
        [WPCTL, "set-volume", str(target["id"]), "%.2f" % volume],
    ):
        try:
            subprocess.run(args, capture_output=True, timeout=10, env=_pw_env())
        except (OSError, subprocess.SubprocessError):
            return


def _int_field(clean, key, label, low, high):
    try:
        clean[key] = int(clean[key])
    except (TypeError, ValueError):
        raise PlayerError("%s must be a whole number" % label) from None
    if not low <= clean[key] <= high:
        raise PlayerError("%s must be between %d and %d" % (label, low, high))


def validate(config, existing_names=(), existing_macs=()):
    """Normalise and check a player definition coming from the browser.

    Every value here ends up in a subprocess argument list or an environment
    variable. argv is built as a list so there is no shell to inject into, but
    the patterns still keep obvious nonsense out of the config file and give the
    user a real error instead of a squeezelite that dies on startup.
    """
    clean = dict(DEFAULTS)
    clean.update({k: v for k, v in (config or {}).items() if k in DEFAULTS})

    clean["name"] = str(clean["name"]).strip()
    if not NAME_RE.fullmatch(clean["name"]):
        raise PlayerError(
            "name must be 1-64 printable characters (no control characters)"
        )
    if clean["name"] in existing_names:
        raise PlayerError("a player named %r already exists" % clean["name"])

    clean["mac"] = str(clean["mac"]).strip().upper()
    if not clean["mac"]:
        taken = set(existing_macs)
        while True:
            candidate = random_mac()
            if candidate not in taken:
                clean["mac"] = candidate
                break
    elif not MAC_RE.match(clean["mac"]):
        raise PlayerError("invalid MAC address")
    elif clean["mac"] in existing_macs:
        raise PlayerError(
            "another player already uses %s -- the server tells players apart "
            "by MAC, so they must be unique" % clean["mac"]
        )

    clean["output_mode"] = str(clean["output_mode"]).strip() or "pipewire"
    if clean["output_mode"] not in ("pipewire", "alsa"):
        raise PlayerError("output mode must be 'pipewire' or 'alsa'")

    clean["node"] = str(clean["node"]).strip()
    if clean["node"] and not NODE_RE.match(clean["node"]):
        raise PlayerError("invalid PipeWire node name")

    clean["alsa_device"] = str(clean["alsa_device"]).strip()
    if clean["output_mode"] == "alsa":
        if not clean["alsa_device"]:
            raise PlayerError("an ALSA output needs a device name")
        if not ALSA_DEVICE_RE.match(clean["alsa_device"]):
            raise PlayerError("invalid ALSA device name")

    clean["server"] = str(clean["server"]).strip()
    if clean["server"] and not HOST_RE.match(clean["server"]):
        raise PlayerError("invalid server address")

    clean["mixer"] = str(clean["mixer"]).strip()
    if clean["mixer"] and not MIXER_RE.match(clean["mixer"]):
        raise PlayerError("invalid mixer control name")

    clean["alsa_format"] = str(clean["alsa_format"]).strip()
    if clean["alsa_format"] not in ALSA_FORMATS:
        raise PlayerError(
            "sample format must be one of %s"
            % ", ".join(f or "(device default)" for f in ALSA_FORMATS)
        )

    _int_field(clean, "port", "port", 1, 65535)
    _int_field(clean, "alsa_buffer", "ALSA buffer", 0, 1000000)
    _int_field(clean, "alsa_period", "ALSA period", 0, 1000000)
    _int_field(clean, "alsa_mmap", "mmap", 0, 1)
    _int_field(clean, "stream_buffer", "stream buffer", 0, 1000000)
    _int_field(clean, "output_buffer", "output buffer", 0, 1000000)
    _int_field(clean, "close_delay", "idle close delay", 0, 3600)

    try:
        clean["volume"] = float(clean["volume"])
    except (TypeError, ValueError):
        raise PlayerError("volume must be a number") from None
    if not 0.0 <= clean["volume"] <= 1.0:
        raise PlayerError("volume must be between 0.0 and 1.0")

    clean["pipewire_latency"] = str(clean["pipewire_latency"]).strip()
    if clean["pipewire_latency"] and not LATENCY_RE.match(clean["pipewire_latency"]):
        raise PlayerError("PipeWire latency must look like 1024/48000")

    clean["dsd"] = bool(clean["dsd"])
    clean["autostart"] = bool(clean["autostart"])

    # Extra arguments are split with shlex and appended to argv, never
    # shell-evaluated. Splitting here turns an unbalanced quote into a clear
    # error now instead of a player that refuses to start later.
    clean["extra"] = str(clean["extra"]).strip()
    if len(clean["extra"]) > 200:
        raise PlayerError("extra arguments are too long")
    try:
        shlex.split(clean["extra"])
    except ValueError as exc:
        raise PlayerError("extra arguments are not parseable: %s" % exc) from None

    return clean


def build_argv(cfg):
    """The squeezelite command line for a player config.

    A list, never a shell string -- which is why a name may contain spaces,
    quotes or semicolons without any of it meaning anything.
    """
    device = (
        cfg["alsa_device"] if cfg.get("output_mode") == "alsa" else "pipewire"
    )
    args = [SQUEEZELITE, "-o", device, "-n", cfg["name"]]

    if cfg.get("server"):
        args += ["-s", "%s:%d" % (cfg["server"], cfg["port"])]
    if cfg.get("mac"):
        args += ["-m", cfg["mac"]]
    if cfg.get("mixer"):
        args += ["-U", cfg["mixer"]]

    # -a <buffer>:<period>:<format>:<mmap>, each part optional. Emitted only if
    # the user actually set something, so the default stays squeezelite's own.
    # mmap is rendered whenever -a is emitted at all: 0 means "mmap off", which
    # is a real setting, not an empty field -- dropping it as falsy would turn
    # the documented "-a 16384:8:24:0" into "-a 16384:8:24:".
    alsa = [
        str(cfg["alsa_buffer"]) if cfg.get("alsa_buffer") else "",
        str(cfg["alsa_period"]) if cfg.get("alsa_period") else "",
        cfg.get("alsa_format") or "",
    ]
    if any(alsa) or cfg.get("alsa_mmap"):
        args += ["-a", ":".join(alsa + [str(int(cfg.get("alsa_mmap") or 0))])]

    if cfg.get("stream_buffer") or cfg.get("output_buffer"):
        args += [
            "-b",
            "%s:%s"
            % (
                cfg.get("stream_buffer") or "",
                cfg.get("output_buffer") or "",
            ),
        ]

    if cfg.get("close_delay"):
        args += ["-C", str(cfg["close_delay"])]
    if cfg.get("dsd"):
        args += ["-D"]

    args += ["-d", "all=info"]

    if cfg.get("extra"):
        args += shlex.split(cfg["extra"])
    return args


class Player:
    """One supervised squeezelite process."""

    def __init__(self, config, supervisor):
        self.config = config
        self.id = config["id"]
        self._supervisor = supervisor
        self._proc = None
        self._thread = None
        self._wake = threading.Event()   # interrupts the backoff sleep
        self._lock = threading.RLock()

        self.desired = False
        self.state = "stopped"
        self.detail = ""
        self.started_at = None
        self.restarts = 0
        self.last_exit = None
        self.logs = deque(maxlen=LOG_LINES)

    # ---- reporting ---------------------------------------------------------

    def status(self):
        with self._lock:
            return {
                **self.config,
                "state": self.state,
                "detail": self.detail,
                "running": self.state == "running",
                "uptime": (time.time() - self.started_at) if self.started_at else 0,
                "restarts": self.restarts,
                "last_exit": self.last_exit,
                "node_present": None,  # filled in by the supervisor, which batches
            }

    def log(self, line):
        self.logs.append("%s %s" % (time.strftime("%H:%M:%S"), line.rstrip()))

    # ---- lifecycle ---------------------------------------------------------

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                self.desired = True
                self._wake.set()  # cut short a backoff sleep
                return
            self.desired = True
            self.state = "starting"
            self.detail = ""
            self._wake.clear()
            self._thread = threading.Thread(
                target=self._supervise, name="player-%s" % self.id, daemon=True
            )
            self._thread.start()

    def stop(self, timeout=10.0):
        with self._lock:
            self.desired = False
            proc = self._proc
            thread = self._thread
        self._wake.set()
        if proc is not None:
            self._terminate(proc)
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                # Belt and braces: the lock in _supervise should make this
                # unreachable, but never leave a live squeezelite behind.
                with self._lock:
                    proc = self._proc
                if proc is not None:
                    self._terminate(proc)
                thread.join(timeout=timeout)
        with self._lock:
            self.state = "stopped"
            self.detail = ""
            self.started_at = None

    def _terminate(self, proc):
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        except OSError:
            pass

    # ---- the supervisor loop -----------------------------------------------

    def _supervise(self):
        delay = RETRY_START
        while self.desired:
            ready, why = self._prepare()
            if not ready:
                with self._lock:
                    self.state = "waiting"
                    self.detail = why
                self.log("not ready: %s" % why)
                if self._sleep(delay):
                    break
                delay = min(delay * 2, RETRY_MAX)
                continue

            started = time.time()
            proc = None
            # Deciding to launch and recording the child must be atomic against
            # stop(). Otherwise a stop that lands in between reads self._proc as
            # None, never terminates the process that is about to exist, and
            # leaves an orphaned squeezelite holding the sink for good.
            with self._lock:
                if not self.desired:
                    break
                try:
                    proc = self._spawn()
                except OSError as exc:
                    self.state = "failed"
                    self.detail = "cannot start squeezelite: %s" % exc
                else:
                    self._proc = proc
                    self.state = "running"
                    self.detail = ""
                    self.started_at = started

            if proc is None:
                self.log(self.detail)
                if self._sleep(delay):
                    break
                delay = min(delay * 2, RETRY_MAX)
                continue

            if (
                self.config.get("volume") is not None
                and self.config.get("node")
                and self._pipewire_output()
            ):
                set_sink_volume(self.config["node"], self.config["volume"])

            # The output pump has to run alongside the watchdog, not instead of
            # it: reading the child's stdout blocks until the child exits.
            reader = threading.Thread(
                target=self._pump, args=(proc,),
                name="player-%s-log" % self.id, daemon=True,
            )
            reader.start()
            self._watch(proc)
            code = proc.wait()
            reader.join(timeout=2)

            with self._lock:
                self._proc = None
                self.started_at = None
                self.last_exit = code
            self.log("squeezelite exited with code %s" % code)

            if not self.desired:
                break

            with self._lock:
                self.restarts += 1
            # A session that stayed up is not a failure streak.
            if time.time() - started >= HEALTHY_AFTER:
                delay = RETRY_START
            with self._lock:
                self.state = "backoff"
                self.detail = "restarting in %ds" % int(delay)
            if self._sleep(delay):
                break
            delay = min(delay * 2, RETRY_MAX)

        with self._lock:
            if not self.desired:
                self.state = "stopped"
                self.detail = ""

    def _watch(self, proc):
        """Wait for the child to exit, restarting it if its sink goes away.

        Only for PipeWire outputs: an ALSA device is not in the graph, so there
        is nothing to poll and squeezelite reports device loss itself.
        """
        node = self.config.get("node") if self._pipewire_output() else None
        absent_since = None
        while proc.poll() is None:
            if not self.desired:
                return
            if node and not sink_present(node):
                absent_since = absent_since or time.time()
                gone = time.time() - absent_since
                if gone >= SINK_GRACE:
                    self.log(
                        "sink %s has been gone %ds -- restarting the player"
                        % (node, int(gone))
                    )
                    with self._lock:
                        self.state = "waiting"
                        self.detail = "output sink disappeared"
                    self._terminate(proc)
                    return
                with self._lock:
                    self.detail = "sink missing for %ds" % int(gone)
            elif absent_since is not None:
                absent_since = None
                self.log("sink %s is back" % node)
                with self._lock:
                    self.detail = ""
            self._wake.wait(timeout=HEALTH_INTERVAL)
            if not self.desired:
                return
            self._wake.clear()

    def _sleep(self, seconds):
        """Interruptible backoff. Returns True if we were told to stop."""
        self._wake.wait(timeout=seconds)
        self._wake.clear()
        return not self.desired

    def _pipewire_output(self):
        return self.config.get("output_mode", "pipewire") != "alsa"

    def _prepare(self):
        """Wait for the bound sink, so we do not spawn into a missing device."""
        node = self.config.get("node") if self._pipewire_output() else None
        if not node:
            return True, ""

        if sink_present(node):
            return True, ""

        # Say so straight away. Waiting up to NODE_WAIT_SECONDS in silence looks
        # identical to a player that is simply broken.
        with self._lock:
            self.state = "waiting"
            self.detail = "waiting for sink %s" % node
        self.log("waiting for sink %s to appear" % node)

        deadline = time.time() + NODE_WAIT_SECONDS
        while time.time() < deadline:
            if sink_present(node):
                self.log("sink %s is present" % node)
                return True, ""
            if not self.desired:
                return False, "stopped"
            time.sleep(1.0)

        if not shutil.which(PW_DUMP):
            # No way to check; let squeezelite try and report for itself.
            return True, ""
        return False, "sink %s is not present" % node

    def _spawn(self):
        cfg = self.config
        env = _pw_env()
        # Only meaningful when playing through PipeWire; an ALSA output goes
        # straight to the device and must not be steered at a graph node.
        if cfg.get("output_mode") != "alsa":
            if cfg.get("node"):
                env["PIPEWIRE_NODE"] = cfg["node"]
            if cfg.get("pipewire_latency"):
                env["PIPEWIRE_LATENCY"] = cfg["pipewire_latency"]

        args = build_argv(cfg)
        self.log("launching: %s" % " ".join(args))
        return subprocess.Popen(
            args,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    def _pump(self, proc):
        """Drain the child's output into the ring buffer until it exits."""
        if proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                self.log(line)
        except (OSError, ValueError):
            pass


class Supervisor:
    def __init__(self, config_path=CONFIG_PATH):
        self._players = {}
        self._lock = threading.RLock()
        self.config_path = config_path
        # Surfaced by the panel as a warning. A config that cannot be written is
        # not a detail to swallow: every player the user creates would vanish on
        # the next container restart, with nothing anywhere saying why.
        self.save_error = ""
        self.load()

    # ---- persistence -------------------------------------------------------

    def load(self):
        try:
            with open(self.config_path) as handle:
                stored = json.load(handle)
        except (OSError, ValueError):
            return
        for entry in stored.get("players", []):
            try:
                config = validate(entry)
            except PlayerError:
                continue
            config["id"] = entry.get("id") or uuid.uuid4().hex[:8]
            with self._lock:
                self._players[config["id"]] = Player(config, self)

    def save(self):
        """Persist every configured player. Called on create, update and delete.

        Written to a temporary file and renamed, so a crash mid-write can never
        leave a half-written config that would drop players on the next boot.
        """
        with self._lock:
            payload = {"players": [p.config for p in self._players.values()]}
        tmp = self.config_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.config_path) or ".", exist_ok=True)
            with open(tmp, "w") as handle:
                json.dump(payload, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.config_path)  # atomic: readers see old or new
        except OSError as exc:
            self.save_error = "cannot write %s: %s" % (self.config_path, exc)
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return
        self.save_error = ""

    # ---- CRUD --------------------------------------------------------------

    def _taken(self, exclude=None):
        with self._lock:
            names = {
                p.config["name"] for p in self._players.values() if p.id != exclude
            }
            macs = {p.config["mac"] for p in self._players.values() if p.id != exclude}
        return names, macs

    def list(self):
        sinks = {s["node"] for s in list_sinks()}
        with self._lock:
            players = list(self._players.values())

        out = []
        for player in players:
            status = player.status()
            status["node_present"] = (
                status["node"] in sinks
                if status["node"] and status.get("output_mode") != "alsa"
                else None
            )
            out.append(status)
        out.sort(key=lambda p: p["name"].lower())
        return out

    def get(self, player_id):
        with self._lock:
            player = self._players.get(player_id)
        if player is None:
            raise PlayerError("no such player")
        return player

    def new_player_defaults(self):
        defaults = dict(DEFAULTS)
        defaults["mac"] = ""  # generated on create, shown as "auto"
        return defaults

    def create(self, config):
        names, macs = self._taken()
        clean = validate(config, existing_names=names, existing_macs=macs)
        clean["id"] = uuid.uuid4().hex[:8]
        player = Player(clean, self)
        with self._lock:
            self._players[clean["id"]] = player
        self.save()
        if clean["autostart"]:
            player.start()
        return player

    def update(self, player_id, config):
        player = self.get(player_id)
        names, macs = self._taken(exclude=player_id)
        merged = dict(player.config)
        merged.update(config or {})
        clean = validate(merged, existing_names=names, existing_macs=macs)
        clean["id"] = player_id

        was_running = player.desired
        if was_running:
            player.stop()
        player.config = clean
        self.save()
        if was_running:
            player.start()
        return player

    def delete(self, player_id):
        player = self.get(player_id)
        player.stop()
        with self._lock:
            self._players.pop(player_id, None)
        self.save()

    def autostart(self):
        with self._lock:
            players = list(self._players.values())
        for player in players:
            if player.config.get("autostart"):
                player.start()

    def stop_all(self):
        with self._lock:
            players = list(self._players.values())
        for player in players:
            player.stop()
