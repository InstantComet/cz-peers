#!/usr/bin/python3
"""Peers: an unofficial Tailscale client for the CardputerZero.

Not affiliated with or endorsed by Tailscale Inc. Tailscale is a registered
trademark of Tailscale Inc.

A small text UI that runs inside the APPLaunch terminal (the .desktop entry has
Terminal=true), so "SSH" simply runs ssh in that same terminal and comes back
to the device list when the session ends.

Talks to tailscaled over its LocalAPI socket. Reading works for any user;
changing settings (connect/disconnect, exit node) needs this user to be the
tailscale operator: `sudo tailscale set --operator=$USER` (the package's
postinst does that for the desktop user).
"""
import base64
import http.client
import json
import os
from pathlib import Path
import pwd
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import termios
import threading
import time
import tty
import unicodedata
from datetime import datetime, timezone

import qr

VERSION = '1.2.1'
SOCK = os.environ.get('CZ_TS_SOCKET', '/var/run/tailscale/tailscaled.sock')
CONFIG_DIR = Path.home() / '.config/cz-peers'
HOSTS_FILE = CONFIG_DIR / 'hosts.json'
CLIP_FILE = Path.home() / '.cache/cz-peers/clipboard'
ASCII = os.environ.get('CZ_TS_ASCII') == '1'
# CardputerZero convention (as in the launcher's apps): F/X/Z/C are up/down/left/right
UP_KEYS = ('UP', 'f', 'F', ';')
DOWN_KEYS = ('DOWN', 'x', 'X', '.')
OK_KEYS = ('ENTER', 'RIGHT', 'c', 'C')
BACK_KEYS = ('ESC', 'LEFT', 'z', 'Z', 'BS', 'q')
ME = os.environ.get('USER') or pwd.getpwuid(os.getuid()).pw_name

# ---------------------------------------------------------------- styling --
WHITE, MUTED, DIM = (255, 255, 255), (167, 176, 187), (100, 108, 118)
BLUE, GREEN, YELLOW, RED = (79, 179, 255), (51, 224, 106), (255, 204, 26), (255, 90, 82)
BLACK, SEL_BG, BAR_BG = (0, 0, 0), (32, 58, 88), (28, 35, 43)
DOT_ON, DOT_OFF, CURSOR = ('*', '-', '>') if ASCII else ('●', '○', '▸')
TS_SSH_MARK = 'S'  # after a device name: it runs Tailscale SSH


def S(fg=WHITE, bg=None, bold=False):
    return (fg, bg, bold)


# The launcher's terminal has only a 16-colour palette, and it squeezes 24-bit
# colours into it in a way that never picks yellow (yellow came out salmon).
# So every colour is sent as a palette index instead (SGR 30-37/90-97 and
# 40-47/100-107), chosen from the launcher's palette:
#   0 #0D1117 background  1 #FF5F56 red   4 #2F81F7 blue    8 #6E7681 grey
#  10 #56D364 green      11 #E3B341 yellow 12 #79C0FF light blue 15 #FFFFFF white
# There is no dark grey, so the bars stay on the black background.
PALETTE_INDEX = {WHITE: 15, MUTED: 8, DIM: 8, BLUE: 12, GREEN: 10, YELLOW: 11, RED: 1,
                 BLACK: 0, SEL_BG: 4, BAR_BG: 0}


def sgr(style):
    fg, bg, bold = style
    parts = ['0']
    if bold:
        parts.append('1')
    if fg:
        i = PALETTE_INDEX[fg]
        if i == 8 and bg == SEL_BG:
            i = 7  # grey is unreadable on the selection blue; use the off-white
        parts.append(str(30 + i if i < 8 else 90 + i - 8))
    if bg:
        i = PALETTE_INDEX[bg]
        parts.append(str(40 + i if i < 8 else 100 + i - 8))
    return '\x1b[' + ';'.join(parts) + 'm'


def char_width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1


def text_width(text):
    return sum(char_width(c) for c in text)


def clip(text, width):
    """Cut text to at most width cells, with an ellipsis-ish '~' when cut."""
    if text_width(text) <= width:
        return text
    out, used = '', 0
    for ch in text:
        w = char_width(ch)
        if used + w > width - 1:
            break
        out += ch
        used += w
    return out + '~' if width > 0 else ''


def pad(text, width, right=False):
    text = clip(text, width)
    space = ' ' * (width - text_width(text))
    return space + text if right else text + space


class Row:
    """One screen line: a list of (text, style) segments and a fill colour."""

    def __init__(self, segs=None, bg=None):
        self.segs = list(segs or [])
        self.bg = bg

    def add(self, text, style=None):
        self.segs.append((text, style or S(bg=self.bg)))
        return self

    def render(self, width):
        out, used = [], 0
        for text, style in self.segs:
            if used >= width:
                break
            fg, bg, bold = style
            style = (fg, bg if bg is not None else self.bg, bold)
            text = clip(text, width - used)
            out.append(sgr(style) + text)
            used += text_width(text)
        out.append(sgr(S(bg=self.bg)) + ' ' * (width - used) + '\x1b[0m')
        return ''.join(out)


def text_row(text, fg=WHITE, bold=False, bg=None):
    return Row([(text, S(fg, bg, bold))], bg)


def wrap(text, width):
    lines = []
    for para in text.split('\n'):
        while text_width(para) > width:
            cut = width
            space = para.rfind(' ', 0, width + 1)
            if space > width // 2:
                cut = space
            lines.append(para[:cut])
            para = para[cut:].lstrip(' ')
        lines.append(para)
    return lines


# --------------------------------------------------------------- LocalAPI --
class APIError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__('local-tailscaled.sock', timeout=timeout)
        self._path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


def localapi(method, path, body=None, timeout=8):
    conn = _UnixHTTP(SOCK, timeout)
    try:
        headers = {'Sec-Tailscale': 'localapi',
                   'Authorization': 'Basic ' + base64.b64encode(b':').decode()}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        try:
            conn.request(method, '/localapi/v0/' + path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except OSError as e:
            raise APIError(0, 'tailscaled not reachable (%s)' % e.__class__.__name__) from e
        if resp.status >= 400:
            raise APIError(resp.status, raw.decode(errors='replace').strip() or resp.reason)
        return json.loads(raw) if raw.strip() else None
    finally:
        conn.close()


def tailscale_installed():
    return os.path.exists(SOCK) or shutil.which('tailscale') is not None


# ------------------------------------------------------------ framebuffer --
class Overlay:
    """Paints a picture straight onto the screen, over the launcher's terminal.

    The terminal's cells are 7x15 px, far too coarse for a QR code, so the
    login view fills part of the terminal with white cells and this draws the
    code on top of them in /dev/fb0 (the launcher's own framebuffer). The
    launcher only repaints cells whose content changes, so the picture stays
    while the text under it is unchanged; paint() is called again now and then
    and puts it back if the launcher drew white cells over it, but leaves the
    screen alone if anything else is showing there (screen saver, another page).

    Off when run over SSH (the device's screen is not what you are looking
    at); CZ_TS_FB=1 forces it on, CZ_TS_FB=0 off. CZ_TS_FB=test also paints
    over whatever is on screen (for testing outside the launcher's terminal).
    """
    TERM_X, TERM_Y = 0, 20  # the terminal page sits under the launcher's 20 px top bar
    CELL_W, CELL_H = 7, 15

    def __init__(self):
        self.fd = None
        mode = os.environ.get('CZ_TS_FB')
        self.force = mode == 'test'
        if mode == '0' or (mode not in ('1', 'test') and (os.environ.get('SSH_CONNECTION') or os.environ.get('SSH_TTY'))):
            return
        try:
            base = '/sys/class/graphics/fb0/'
            with open(base + 'virtual_size') as f:
                self.width, self.height = (int(v) for v in f.read().split(','))
            with open(base + 'bits_per_pixel') as f:
                bpp = int(f.read())
            with open(base + 'stride') as f:
                self.stride = int(f.read())
            if bpp == 16:
                self.fd = os.open('/dev/fb0', os.O_RDWR | os.O_CLOEXEC)
        except (OSError, ValueError):
            self.fd = None

    @property
    def available(self):
        return self.fd is not None

    @staticmethod
    def _whitish(pixel):
        return pixel >> 11 >= 28 and (pixel >> 5) & 63 >= 56 and pixel & 31 >= 28

    def paint(self, image):
        """image = (col, row, [RGB565 bytes per pixel row]). Returns True if it is on screen."""
        if self.fd is None or not image:
            return False
        col, row, rows = image
        x, y = self.TERM_X + col * self.CELL_W, self.TERM_Y + row * self.CELL_H
        if y + len(rows) > self.height or x + len(rows[0]) // 2 > self.width:
            return False
        try:
            stale = []
            for i, want in enumerate(rows):
                offset = (y + i) * self.stride + x * 2
                have = os.pread(self.fd, len(want), offset)
                if have == want:
                    continue
                want_px, have_px = memoryview(want).cast('H'), memoryview(have).cast('H')
                if not self.force and any(h != w and not self._whitish(h) for h, w in zip(have_px, want_px)):
                    return False  # not our white cells: something else is on screen
                stale.append((offset, want))
            for offset, want in stale:
                os.pwrite(self.fd, want, offset)
            return True
        except OSError:
            return False


def qr_image(text, width, height):
    """Rows of RGB565 pixels with the QR code for text centred on white."""
    try:
        code = qr.encode(text)
    except ValueError:
        return None
    n = code.size
    scale = min(width, height) // (n + 4)  # at least a 2-module quiet zone
    if scale < 2:
        return None
    side = n * scale
    ox, oy = (width - side) // 2, (height - side) // 2
    white, black = b'\xff\xff', b'\x00\x00'
    blank = white * width
    rows = []
    for y in range(height):
        my = (y - oy) // scale
        if y < oy or my >= n:
            rows.append(blank)
            continue
        line = bytearray(blank)
        for mx, dark in enumerate(code.modules[my]):
            if dark:
                start = (ox + mx * scale) * 2
                line[start:start + 2 * scale] = black * scale
        rows.append(bytes(line))
    return rows


# ------------------------------------------------------------------ model --
def parse_time(value):
    if not value or value.startswith('0001-'):
        return None
    try:
        return datetime.strptime(value[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def span(seconds):
    seconds = abs(int(seconds))
    for unit, size in (('y', 31536000), ('d', 86400), ('h', 3600), ('m', 60)):
        if seconds >= size:
            return '%d%s' % (seconds // size, unit)
    return '%ds' % seconds


def ago(value):
    t = parse_time(value)
    if not t:
        return 'never'
    s = (datetime.now(timezone.utc) - t).total_seconds()
    return 'just now' if s < 60 else span(s) + ' ago'


def until(value):
    t = parse_time(value)
    if not t:
        return None
    s = (t - datetime.now(timezone.utc)).total_seconds()
    return ('expired %s ago' % span(s)) if s < 0 else ('in ' + span(s))


def human_bytes(n):
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return ('%d %s' if unit == 'B' else '%.1f %s') % (n, unit)
        n /= 1024


def ssh_alias(node):
    """The name ssh is given for a device: its short tailnet name, so that a
    `Host <name>` block in ~/.ssh/config (IdentityFile, ProxyJump, ...) applies.
    Falls back to the IP when the name is not a plain host name."""
    name = node.name or ''
    ok = name and name[0].isalnum() and all(c.isalnum() or c in '-.' for c in name)
    return name if ok else node.ip


def ssh_argv(node, user, port):
    """ssh by device name, but always to its Tailscale IP: -o HostName on the
    command line beats any HostName in ~/.ssh/config and needs no MagicDNS,
    while Host/Match blocks for the name still supply keys and other options.

    New host keys are accepted without the yes/no question: the connection
    goes over Tailscale, which has already checked who the device is. A host
    key that changed is still refused (the app then offers to forget it)."""
    argv = ['ssh', '-o', 'ConnectTimeout=15', '-o', 'StrictHostKeyChecking=accept-new']
    alias = ssh_alias(node)
    if alias != node.ip:
        argv += ['-o', 'HostName=' + node.ip]
    if port != '22':
        argv += ['-p', port]
    return argv + ['%s@%s' % (user, alias)]


def ssh_shown(node, user, port):
    return 'ssh %s%s@%s' % ('-p %s ' % port if port != '22' else '', user, ssh_alias(node))


# ---------------------------------------------------------------- SSH keys --
SSH_DIR = Path(os.environ.get('CZ_TS_SSH_DIR') or Path.home() / '.ssh')
KEY_NAMES = ('id_ed25519', 'id_ecdsa', 'id_rsa')  # the ones ssh offers by default
CONFIG_BEGIN = '# >>> Peers (cz-peers): keys it installed on these devices >>>'
CONFIG_END = '# <<< Peers (cz-peers) <<<'


def find_key():
    """This user's default SSH key as (private key path, public key text), or None."""
    for name in KEY_NAMES:
        priv, pub = SSH_DIR / name, SSH_DIR / (name + '.pub')
        if not priv.exists():
            continue
        try:
            text = pub.read_text().strip()
        except OSError:
            # a private key without its .pub: derive the public half
            try:
                text = subprocess.run(['ssh-keygen', '-y', '-f', str(priv)], capture_output=True, text=True,
                                      stdin=subprocess.DEVNULL, timeout=10).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                text = ''
        if text.startswith(('ssh-', 'ecdsa-', 'sk-')):
            return priv, text
    return None


def make_key():
    """Create ~/.ssh/id_ed25519 (no passphrase). Returns (path, public key) or raises OSError."""
    SSH_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    priv = SSH_DIR / 'id_ed25519'
    comment = '%s@%s' % (ME, socket.gethostname())
    try:
        r = subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', comment, '-f', str(priv)],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
    except subprocess.SubprocessError as e:
        raise OSError(str(e)) from e
    if r.returncode != 0:
        raise OSError((r.stderr or r.stdout).strip() or 'ssh-keygen failed (%d)' % r.returncode)
    found = find_key()
    if not found:
        raise OSError('ssh-keygen made no key')
    return found


def key_fingerprint(pub):
    """('ED25519', 'SHA256:...') for a public key line."""
    try:
        r = subprocess.run(['ssh-keygen', '-l', '-f', '-'], input=pub + '\n', capture_output=True,
                           text=True, timeout=10)
        parts = r.stdout.split()
        if r.returncode == 0 and len(parts) >= 2:
            return parts[-1].strip('()'), parts[1]
    except (OSError, subprocess.SubprocessError):
        pass
    return pub.split()[0], ''


def config_path_for(priv):
    home = str(Path.home())
    path = str(priv)
    return '~' + path[len(home):] if path.startswith(home + '/') else path


def remember_key_in_config(alias, priv):
    """Add `Host <alias> / IdentityFile <key>` to a section of ~/.ssh/config
    that Peers owns, so plain ssh to the device (e.g. from "Shell here") uses
    the key too, even under an earlier `IdentitiesOnly yes`. Leaves the rest
    of the file as it is."""
    path = SSH_DIR / 'config'
    try:
        text = path.read_text()
    except FileNotFoundError:
        text = ''
    lines = text.split('\n')
    entries, outside, inside = {}, [], False
    host = None
    for line in lines:
        s = line.strip()
        if s == CONFIG_BEGIN:
            inside = True
        elif s == CONFIG_END:
            inside = False
        elif inside:
            if s.lower().startswith('host '):
                host = s[5:].strip()
            elif s.lower().startswith('identityfile ') and host:
                entries[host] = s[13:].strip()
        else:
            outside.append(line)
    want = config_path_for(priv)
    if entries.get(alias) == want:
        return
    entries[alias] = want
    while outside and outside[-1] == '':
        outside.pop()
    section = [CONFIG_BEGIN]
    for h in sorted(entries):
        section += ['Host ' + h, '    IdentityFile ' + entries[h]]
    section.append(CONFIG_END)
    body = '\n'.join(outside + ([''] if outside else []) + section) + '\n'
    SSH_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name('config.cz-peers.tmp')
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(body)
    tmp.replace(path)


def posix_install_command(pub):
    """Remote command that appends pub to ~/.ssh/authorized_keys once (any
    POSIX-ish login shell; the script itself runs under sh)."""
    k = shlex.quote(pub)
    script = ('umask 077; mkdir -p ~/.ssh && chmod 700 ~/.ssh && f=~/.ssh/authorized_keys && touch "$f" && '
              'chmod 600 "$f" && if grep -qxF %s "$f"; then echo "Key already in $f"; else '
              '{ [ -s "$f" ] && [ -n "$(tail -c1 "$f")" ] && echo >> "$f"; '
              'echo %s >> "$f" && echo "Added the key to $f"; }; fi' % (k, k))
    return 'sh -c ' + shlex.quote(script)


WINDOWS_INSTALL_PS = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$k = '%s'
try {
  $admin = [bool](whoami /groups | Select-String 'S-1-5-32-544')
  $cfg = Join-Path $env:ProgramData 'ssh\sshd_config'
  $match = (Test-Path $cfg) -and (Select-String -Path $cfg -Pattern '^\s*Match\s+Group\s+administrators' -Quiet)
  if ($admin -and $match) {
    $f = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
  } else {
    $d = Join-Path $HOME '.ssh'
    New-Item -ItemType Directory -Force -Path $d | Out-Null
    $f = Join-Path $d 'authorized_keys'
  }
  $old = ''
  if (Test-Path $f) { $old = [IO.File]::ReadAllText($f) }
  if (($old -split "`r?`n") -contains $k) {
    Write-Output "Key already in $f"
  } else {
    if ($old.Length -gt 0 -and -not $old.EndsWith("`n")) { $old += "`r`n" }
    [IO.File]::WriteAllText($f, $old + $k + "`r`n", (New-Object Text.UTF8Encoding $false))
    Write-Output "Added the key to $f"
  }
  if ($f -like '*administrators_authorized_keys') {
    icacls $f /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F' | Out-Null
    if ($LASTEXITCODE) { throw "icacls could not set the permissions sshd needs on $f" }
  }
  exit 0
} catch {
  Write-Output "Failed: $_"
  exit 1
}
'''


def windows_install_command(pub):
    """Remote command for Windows OpenSSH (works whether its shell is cmd or
    PowerShell). Admins' keys go to administrators_authorized_keys when
    sshd_config has the stock `Match Group administrators` block."""
    script = WINDOWS_INSTALL_PS % pub.replace("'", "''")
    encoded = base64.b64encode(script.encode('utf-16-le')).decode()
    return 'powershell -NoProfile -NonInteractive -EncodedCommand ' + encoded


def install_command(node, pub):
    return windows_install_command(pub) if node.os.lower() == 'windows' else posix_install_command(pub)


def takes_password(node, methods):
    """Whether a refusing server would take a password. Windows OpenSSH lists
    keyboard-interactive even with `PasswordAuthentication no` and then never
    asks, so there only `password` counts; elsewhere keyboard-interactive is
    normally PAM asking for the password."""
    if 'password' in methods:
        return True
    return 'keyboard-interactive' in methods and node.os.lower() != 'windows'


def ssh_failure(text):
    """Why an ssh run that exited 255 failed, from its stderr:
    ('hostkey' | 'denied' | 'refused' | 'timeout' | 'dns' | None, auth methods offered)."""
    if 'REMOTE HOST IDENTIFICATION HAS CHANGED' in text or 'Host key verification failed' in text:
        return 'hostkey', []
    denied = [line for line in text.splitlines() if 'Permission denied (' in line]
    if denied:
        methods = denied[-1].rsplit('(', 1)[1].split(')')[0].split(',')
        return 'denied', [m.strip() for m in methods if m.strip()]
    if 'Connection refused' in text:
        return 'refused', []
    if 'timed out' in text:
        return 'timeout', []
    if 'Could not resolve hostname' in text:
        return 'dns', []
    return None, []


class Node:
    def __init__(self, raw, suffix, users):
        self.raw = raw
        dns = (raw.get('DNSName') or '').rstrip('.')
        self.dns = dns
        if suffix and dns.endswith('.' + suffix):
            self.name = dns[:-len(suffix) - 1]
        else:
            # shared-in devices: keep the list short, details show the full name
            self.name = dns.split('.')[0] if dns else (raw.get('HostName') or '?')
        ips = raw.get('TailscaleIPs') or []
        self.ipv4 = next((ip for ip in ips if '.' in ip), '')
        self.ipv6 = next((ip for ip in ips if ':' in ip), '')
        self.ip = self.ipv4 or self.ipv6
        self.os = raw.get('OS') or ''
        self.online = bool(raw.get('Online'))
        self.id = raw.get('ID') or ''
        self.exit_node = bool(raw.get('ExitNode'))
        self.exit_option = bool(raw.get('ExitNodeOption'))
        # the device runs Tailscale SSH: tailnet identity, no key needed
        self.tailscale_ssh = bool(raw.get('sshHostKeys'))
        user =users.get(str(raw.get('UserID')), {})
        self.owner = user.get('LoginName') or user.get('DisplayName') or ''

    @property
    def link(self):
        """Short connection state for the list column."""
        if not self.online:
            return ago(self.raw.get('LastSeen'))
        if self.raw.get('CurAddr'):
            return 'direct'
        if self.raw.get('Relay') and self.raw.get('Active'):
            return 'relay ' + self.raw['Relay']
        return 'online'

    def matches(self, text):
        text = text.lower()
        return any(text in s.lower() for s in (self.name, self.ip, self.os, self.owner))


class Model:
    def __init__(self):
        self.status = None
        self.error = None
        self.self_node = None
        self.peers = []
        self.lock = threading.Lock()

    def refresh(self):
        try:
            status = localapi('GET', 'status')
            error = None
        except (APIError, ValueError) as e:
            status, error = None, str(e)
        with self.lock:
            self.error = error
            if status is None:
                return
            self.status = status
            suffix = status.get('MagicDNSSuffix') or ''
            users = status.get('User') or {}
            self.self_node = Node(status['Self'], suffix, users) if status.get('Self') else None
            peers = [Node(p, suffix, users) for p in (status.get('Peer') or {}).values()]
            peers.sort(key=lambda n: (not n.online, n.name.lower()))
            self.peers = peers

    @property
    def state(self):
        return (self.status or {}).get('BackendState') or ('Unavailable' if self.error else '...')


def load_hosts():
    try:
        return json.loads(HOSTS_FILE.read_text())
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        return {}
    try:  # saved by the package's earlier name, cz-tailscale (1.1.0 and older)
        return json.loads((Path.home() / '.config/cz-tailscale/hosts.json').read_text())
    except (OSError, ValueError):
        return {}


def save_hosts(hosts):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = HOSTS_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(hosts, indent=1, sort_keys=True))
        tmp.replace(HOSTS_FILE)
    except OSError:
        pass


# --------------------------------------------------------------- terminal --
KEYS = {
    b'\x1b[A': 'UP', b'\x1bOA': 'UP', b'\x1b[B': 'DOWN', b'\x1bOB': 'DOWN',
    b'\x1b[C': 'RIGHT', b'\x1bOC': 'RIGHT', b'\x1b[D': 'LEFT', b'\x1bOD': 'LEFT',
    b'\x1b[5~': 'PGUP', b'\x1b[6~': 'PGDN', b'\x1b[H': 'HOME', b'\x1b[F': 'END',
    b'\x1b[3~': 'DEL',
}


def parse_keys(data):
    keys, i = [], 0
    while i < len(data):
        if data[i] == 0x1b:
            for seq, name in KEYS.items():
                if data.startswith(seq, i):
                    keys.append(name)
                    i += len(seq)
                    break
            else:
                if i + 1 < len(data) and data[i + 1] in b'[O':
                    j = i + 2  # unknown CSI/SS3: skip to its final byte
                    while j < len(data) and not 0x40 <= data[j] <= 0x7e:
                        j += 1
                    i = j + 1
                else:
                    keys.append('ESC')
                    i += 1
            continue
        b = data[i]
        if b in (0x0d, 0x0a):
            keys.append('ENTER')
        elif b in (0x7f, 0x08):
            keys.append('BS')
        elif b == 0x09:
            keys.append('TAB')
        elif b == 0x03:
            keys.append('CTRL_C')
        elif b >= 0x20:
            n = 1 if b < 0x80 else 2 if b < 0xe0 else 3 if b < 0xf0 else 4
            keys.append(data[i:i + n].decode(errors='replace'))
            i += n
            continue
        i += 1
    return keys


class Terminal:
    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)
        self.fresh = True  # screen was cleared: the next frame must be drawn in full

    def enter(self):
        tty.setraw(self.fd)
        self.write('\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[2J')
        self.fresh = True

    def leave(self):
        self.write('\x1b[0m\x1b[?7h\x1b[?25h\x1b[?1049l')
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    @staticmethod
    def write(text):
        data = text.encode()
        while data:
            n = os.write(sys.stdout.fileno(), data)
            data = data[n:]

    @staticmethod
    def size():
        try:
            s = os.get_terminal_size(sys.stdout.fileno())
            return max(s.columns, 20), max(s.lines, 6)
        except OSError:
            return 45, 10

    def read_keys(self):
        data = os.read(self.fd, 256)
        if data.endswith(b'\x1b'):  # maybe the start of an escape sequence
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if r:
                data += os.read(self.fd, 256)
        return parse_keys(data)

    def wait_key(self):
        while True:
            keys = self.read_keys()
            if keys:
                return keys[0]

    def run_interactive(self, argv, banner, env=None, capture=False):
        """Run a program in the plain terminal, then come back. Returns the
        exit code, or (code, stderr text) with capture=True: stderr is then
        still shown as it arrives, and its last 8 KB are kept. Password
        prompts go to /dev/tty, not stderr, so they are not affected."""
        self.leave()
        sys.stdout.write('\x1b[2J\x1b[H' + banner + '\r\n')
        sys.stdout.flush()
        old = signal.signal(signal.SIGINT, signal.SIG_IGN)
        err = bytearray()
        try:
            proc = subprocess.Popen(argv, env=env, stderr=subprocess.PIPE if capture else None,
                                    preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
            if capture:
                out = sys.stdout.fileno()
                pipe = proc.stderr.fileno()
                # read until EOF, or until the process has gone and nothing more
                # is waiting (a ControlPersist master may keep the pipe open)
                while True:
                    r, _, _ = select.select([pipe], [], [], 0.2)
                    if r:
                        chunk = os.read(pipe, 4096)
                        if not chunk:
                            break
                        os.write(out, chunk)
                        err += chunk
                        del err[:-8192]
                    elif proc.poll() is not None:
                        break
                proc.stderr.close()
            code = proc.wait()
        except OSError as e:
            sys.stdout.write('%s\r\n' % e)
            code = -1
        finally:
            signal.signal(signal.SIGINT, old)
        tty.setraw(self.fd)
        self.write('\r\n\x1b[0m\x1b[7m %s exited (%d). Press any key \x1b[0m' % (argv[0], code))
        termios.tcflush(self.fd, termios.TCIFLUSH)
        self.wait_key()
        self.enter()
        return (code, err.decode(errors='replace')) if capture else code


# ------------------------------------------------------------------ views --
def header(title, right='', width=45):
    row = Row(bg=BAR_BG)
    row.add(' ' + title, S(WHITE, BAR_BG, True))
    if right:
        room = width - text_width(' ' + title) - 1
        row.add(pad(right, room, right=True) + ' ', S(MUTED, BAR_BG))
    return row


def footer(hints):
    row = Row(bg=BAR_BG)
    for key, label in hints:
        row.add(' ' + key, S(YELLOW, BAR_BG, True))
        row.add(' ' + label, S(MUTED, BAR_BG))
    return row


class View:
    def __init__(self, app):
        self.app = app

    def render(self, w, h):
        return []

    def key(self, k):
        pass


class ScrollText(View):
    """A titled, scrollable list of (label, value[, colour]) lines."""
    title = ''

    def __init__(self, app):
        super().__init__(app)
        self.top = 0

    def lines(self, w):
        return []

    def hints(self):
        return [('Z', 'back')]

    def render(self, w, h):
        body_h = h - 2
        rows = []
        for item in self.lines(w):
            if isinstance(item, Row):
                rows.append(item)
                continue
            label, value = item[0], item[1]
            colour = item[2] if len(item) > 2 else WHITE
            if not label:
                rows += [text_row(' ' + line, colour) for line in wrap(value, w - 2)]
                continue
            lw = 11
            parts = wrap(value, w - lw - 2) or ['']
            rows.append(Row([(' ' + pad(label, lw), S(MUTED)), (parts[0], S(colour))]))
            rows += [Row([(' ' * (lw + 1), S()), (p, S(colour))]) for p in parts[1:]]
        self.top = max(0, min(self.top, len(rows) - body_h))
        rows += [Row()] * max(0, body_h - len(rows))
        more = '%d/%d' % (min(self.top + body_h, len(rows)), len(rows)) if len(rows) > body_h else ''
        return [header(self.title, more, w)] + rows[self.top:self.top + body_h] + [footer(self.hints())]

    def key(self, k):
        if k in UP_KEYS:
            self.top -= 1
        elif k in DOWN_KEYS:
            self.top += 1
        elif k == 'PGUP':
            self.top -= 5
        elif k == 'PGDN':
            self.top += 5
        elif k in BACK_KEYS:
            self.app.pop()
        else:
            return False
        return True


class HelpView(ScrollText):
    title = 'Help'

    def lines(self, w):
        return [
            ('F / X', 'up / down (arrow keys work too)'),
            ('C / Enter', 'actions for the device'),
            ('Z / Esc', 'back (Esc in the list quits)'),
            ('s', 'SSH to the device'),
            ('k', 'install my SSH key on the device'),
            ('K', "this Cardputer's SSH key (QR code)"),
            ('y', 'copy its IP (see below)'),
            ('p', 'tailscale ping'),
            ('i', 'device details'),
            ('/', 'filter by name, IP or OS'),
            ('m', 'this Cardputer: IPs, account, key expiry'),
            ('e', 'choose an exit node'),
            ('t', 'connect / disconnect'),
            ('l', 'log in (shows a QR code to scan)'),
            ('o', 'log out (in the m page)'),
            ('', ''),
            ('', 'There is no system clipboard. "Copy" saves the IP to ~/.cache/cz-peers/clipboard; '
                 'in the CLI app type cz-peers-clip to print it, e.g. scp f pi@$(cz-peers-clip):'),
            ('', '"Shell here" opens bash with $IP and $HOST set to the device.'),
            ('', ''),
            ('', 'SSH keys: K shows (or makes) this Cardputer\'s key; scan its QR code to get the '
                 'public key. k on a device logs in once with its password and adds the key there '
                 '(Linux, macOS and Windows), so ssh needs no password after that. If ssh is refused, '
                 'the app offers this by itself.'),
            ('', 'A blue %s after a name: the device runs Tailscale SSH, so no key is needed.' % TS_SSH_MARK),
            ('', ''),
            ('', 'Changing settings needs: sudo tailscale set --operator=$USER '
                 '(the app offers to run it when a change is refused).'),
            ('', 'Peers (cz-peers) %s' % VERSION, DIM),
            ('', 'Unofficial app, not affiliated with or endorsed by Tailscale Inc. Tailscale is a registered trademark of Tailscale Inc.', DIM),
        ]


class DetailsView(ScrollText):
    def __init__(self, app, node):
        super().__init__(app)
        self.node = node
        self.title = node.name

    def lines(self, w):
        n = self.node
        # pick up fresh data for the same node
        for p in self.app.model.peers:
            if p.id == n.id:
                n = self.node = p
        r = n.raw
        out = [('IPv4', n.ipv4 or '-'), ('IPv6', n.ipv6 or '-'), ('DNS', n.dns or '-'),
               ('OS', n.os or '-'), ('Owner', n.owner or '-')]
        if n.online:
            out.append(('Status', 'online', GREEN))
        else:
            out.append(('Status', 'offline, seen ' + ago(r.get('LastSeen')), MUTED))
        if r.get('CurAddr'):
            out.append(('Path', 'direct ' + r['CurAddr'], GREEN))
        elif r.get('Relay'):
            out.append(('Path', 'DERP relay ' + r['Relay'], YELLOW if r.get('Active') else MUTED))
        if r.get('RxBytes') or r.get('TxBytes'):
            out.append(('Traffic', 'rx %s  tx %s' % (human_bytes(r.get('RxBytes')), human_bytes(r.get('TxBytes')))))
        if r.get('LastHandshake') and parse_time(r['LastHandshake']):
            out.append(('Handshake', ago(r['LastHandshake'])))
        exp = until(r.get('KeyExpiry'))
        if exp:
            out.append(('Key', 'expires ' + exp if not exp.startswith('expired') else exp,
                        RED if exp.startswith('expired') else WHITE))
        if r.get('Tags'):
            out.append(('Tags', ' '.join(r['Tags'])))
        if n.exit_node:
            out.append(('Exit node', 'in use', YELLOW))
        elif n.exit_option:
            out.append(('Exit node', 'offered'))
        if n.tailscale_ssh:
            out.append(('SSH', 'Tailscale SSH (no key needed)', BLUE))
        saved = self.app.hosts.get(n.name, {})
        if saved.get('user'):
            out.append(('SSH as', '%s (port %s)' % (saved['user'], saved.get('port', '22'))))
        if saved.get('key'):
            out.append(('SSH key', 'installed for %s' % saved['key'], GREEN))
        return out

    def hints(self):
        return [('Z', 'back'), ('s', 'ssh'), ('k', 'add key'), ('p', 'ping')]

    def key(self, k):
        if k in ('s', 'p', 'y', 'h', 'k'):
            return self.app.node_action(k, self.node)
        return super().key(k)


class SelfView(ScrollText):
    title = 'This device'

    def lines(self, w):
        m = self.app.model
        st = m.status or {}
        out = []
        if m.error:
            out.append(('Error', m.error, RED))
        out.append(('State', m.state, GREEN if m.state == 'Running' else YELLOW))
        n = m.self_node
        if n:
            out += [('Name', n.name), ('IPv4', n.ipv4 or '-'), ('IPv6', n.ipv6 or '-'), ('DNS', n.dns or '-')]
            if n.owner:
                out.append(('Account', n.owner))
        tailnet = (st.get('CurrentTailnet') or {}).get('Name')
        if tailnet and not (n and tailnet == n.owner):
            out.append(('Tailnet', tailnet))
        if n:
            exp = until(n.raw.get('KeyExpiry'))
            if exp is None:
                out.append(('Key', 'no expiry'))
            else:
                days_left = (parse_time(n.raw['KeyExpiry']) - datetime.now(timezone.utc)).days
                out.append(('Key', exp if exp.startswith('expired') else 'expires ' + exp,
                            RED if days_left < 0 else YELLOW if days_left < 14 else WHITE))
        exit_node = next((p for p in m.peers if p.exit_node), None)
        out.append(('Exit node', exit_node.name if exit_node else 'none', YELLOW if exit_node else WHITE))
        prefs = self.app.prefs
        if prefs:
            out.append(('Accept DNS', 'yes' if prefs.get('CorpDNS') else 'no'))
            out.append(('Routes', 'accepted' if prefs.get('RouteAll') else 'not accepted'))
            op = prefs.get('OperatorUser') or ''
            me = ME
            if os.geteuid() != 0 and op != me:
                out.append(('Operator', '%s (changes need: sudo tailscale set --operator=%s)' % (op or 'none', me), YELLOW))
        if st.get('AuthURL'):
            out.append(('Log in', st['AuthURL'], BLUE))
        for h in st.get('Health') or []:
            out.append(('Warning', h, YELLOW))
        if st.get('Version'):
            out.append(('Version', st['Version'].split('-')[0]))
        found = find_key()
        out.append(('SSH key', '~/.ssh/%s (k)' % found[0].name if found
                    else 'none yet (k to make one)', WHITE if found else MUTED))
        return out

    def hints(self):
        if self.app.model.state in ('NeedsLogin', 'NoState'):
            return [('Z', 'back'), ('l', 'log in'), ('k', 'ssh key')]
        return [('Z', 'back'), ('t', 'on/off'), ('e', 'exit'), ('o', 'logout'), ('k', 'key')]

    def key(self, k):
        if k in ('k', 'K'):
            self.app.push(KeyView(self.app))
            return True
        if k in ('t', 'e', 'l'):
            return self.app.list_view.key(k)
        if k == 'o':
            self.app.logout()
            return True
        return super().key(k)


class LoginView(View):
    """Log in by scanning a QR code (or opening the link on another device)."""
    QR_COLS = 22  # 154 x 150 px of white cells for the code on the left

    def __init__(self, app):
        super().__init__(app)
        self.url = None
        self.image = None
        self.error = None
        self.done = False
        self.request()

    def request(self):
        self.error = None

        def work():
            try:
                localapi('POST', 'login-interactive', timeout=20)
            except (APIError, ValueError) as e:
                self.app.call_soon(lambda e=e: self.failed(e))
            self.app.refresh_now()

        threading.Thread(target=work, daemon=True).start()

    def failed(self, err):
        if isinstance(err, APIError) and err.status == 403:
            self.app.pop()
            self.app.offer_operator_fix()
        else:
            self.error = str(err)

    def tick(self):
        m = self.app.model
        state = m.state
        if state in ('Running', 'Starting', 'Stopped') and not (m.status or {}).get('AuthURL'):
            if self.done:
                return
            self.done = True
            self.app.pop()
            if state == 'Stopped':  # logged in but not connected yet
                self.app.edit_prefs({'WantRunningSet': True, 'WantRunning': True}, 'Logged in, connecting...')
            else:
                me = m.self_node
                self.app.flash('Logged in' + (' as ' + me.owner if me and me.owner else ''))
            return
        url = (m.status or {}).get('AuthURL') or None
        if url != self.url:
            self.url = url
            self.image = None
            if url and self.app.overlay.available:
                self.image = qr_image(url, self.QR_COLS * Overlay.CELL_W, 10 * Overlay.CELL_H)

    def overlay_image(self):
        return (0, 0, self.image) if self.image else None

    def render(self, w, h):
        state = self.app.model.state
        if state == 'NeedsMachineAuth':
            body = ('Logged in. An admin must approve this device in the Tailscale admin console '
                    '(Machines page) before it can connect.')
        elif self.error:
            body = 'Could not start the login: ' + self.error
        elif not self.url:
            body = 'Getting a login link...'
        else:
            body = None
        show_qr = self.image is not None and w >= self.QR_COLS + 20 and h >= 10
        hints = [('Z', 'back'), ('r', 'new link')]
        if show_qr:
            rw = w - self.QR_COLS - 1
            lines = [('Log in to Tailscale', WHITE, True), ('', WHITE, False),
                     ('Scan with your phone,', MUTED, False), ('or open this link:', MUTED, False)]
            lines += [(part, BLUE, False) for part in wrap(self.url, rw)]
            lines += [('', WHITE, False), ('Waiting for login...', YELLOW, False)]
            lines = lines[:h - 1]
            lines += [('', WHITE, False)] * (h - 1 - len(lines))
            rows = []
            for text, colour, bold in lines:
                row = Row([(' ' * self.QR_COLS, S(WHITE, WHITE)), (' ', S())])
                row.add(text, S(colour, None, bold))
                rows.append(row)
            foot = footer(hints)
            foot.segs.insert(0, (' ' * self.QR_COLS, S(WHITE, WHITE)))
            return rows + [foot]
        rows = [header('Log in', '', w), Row()]
        if body:
            rows += [text_row(' ' + line, YELLOW if state == 'NeedsMachineAuth' else WHITE)
                     for line in wrap(body, w - 2)]
        else:
            rows += [text_row(' Open this link on your phone or computer:', MUTED)]
            rows += [text_row(' ' + line, BLUE) for line in wrap(self.url, w - 2)]
            if not self.app.overlay.available:
                rows += [Row()] + [text_row(' ' + line, DIM) for line in
                                   wrap('Open this app on the Cardputer itself to see a QR code.', w - 2)]
        rows = rows[:h - 1]
        rows += [Row()] * (h - 1 - len(rows))
        return rows + [footer(hints)]

    def key(self, k):
        if k in BACK_KEYS:
            self.app.pop()
        elif k in ('r', 'R'):
            self.url = self.image = None
            self.request()
        return True


class KeyView(View):
    """This Cardputer's SSH key: makes one if there is none, shows the public
    key as a QR code (scan it with a phone and paste it into authorized_keys),
    copies it, or lists it as text."""
    QR_COLS = 22

    def __init__(self, app):
        super().__init__(app)
        self.as_text = False
        self.top = 0
        self.load()

    def load(self):
        self.found = find_key()
        self.image = None
        self.kind = self.fingerprint = ''
        if self.found:
            self.kind, self.fingerprint = key_fingerprint(self.found[1])
            if self.app.overlay.available:
                self.image = qr_image(self.found[1], self.QR_COLS * Overlay.CELL_W, 10 * Overlay.CELL_H)

    def generate(self):
        try:
            make_key()
            self.app.flash('Made ~/.ssh/id_ed25519')
        except OSError as e:
            self.app.flash('Could not make a key: %s' % e, RED, 6)
        self.load()

    def overlay_image(self):
        return (0, 0, self.image) if self.image and not self.as_text else None

    def render(self, w, h):
        if not self.found:
            rows = [header('SSH key', '', w), Row()]
            rows += [text_row(' ' + line) for line in wrap(
                'This Cardputer has no SSH key yet, so ssh can only log in with passwords.', w - 2)]
            rows.append(Row())
            rows += [text_row(' ' + line, MUTED) for line in wrap(
                'Press g to make one (ed25519, saved as ~/.ssh/id_ed25519).', w - 2)]
            rows = rows[:h - 1] + [Row()] * max(0, h - 1 - len(rows))
            return rows + [footer([('Z', 'back'), ('g', 'make key')])]
        hints = [('Z', 'back'), ('y', 'copy'), ('t', 'text' if not self.as_text else 'QR')]
        pub = self.found[1]
        if self.image and not self.as_text and w >= self.QR_COLS + 20 and h >= 10:
            rw = w - self.QR_COLS - 1
            lines = [('SSH key', WHITE, True), (self.kind + ' ' + self.fingerprint[7:15], MUTED, False),
                     ('', WHITE, False)]
            lines += [(part, MUTED, False) for part in wrap(
                'Scan for the public key and add it to authorized_keys there. '
                'Or press k on a device.', rw)]
            lines = lines[:h - 1] + [('', WHITE, False)] * max(0, h - 1 - len(lines))
            rows = []
            for text, colour, bold in lines:
                row = Row([(' ' * self.QR_COLS, S(WHITE, WHITE)), (' ', S())])
                row.add(text, S(colour, None, bold))
                rows.append(row)
            foot = footer(hints)
            foot.segs.insert(0, (' ' * self.QR_COLS, S(WHITE, WHITE)))
            return rows + [foot]
        body = [text_row(' %s ~/.ssh/%s' % (self.kind, self.found[0].name), MUTED)]
        if self.fingerprint:
            body.append(text_row(' ' + self.fingerprint, MUTED))
        body += [text_row(' ' + line) for line in wrap(pub, w - 2)]
        if not self.app.overlay.available:
            body += [Row()] + [text_row(' ' + line, DIM) for line in
                               wrap('Open this app on the Cardputer itself to see a QR code.', w - 2)]
        body_h = h - 2
        self.top = max(0, min(self.top, len(body) - body_h))
        more = '%d/%d' % (min(self.top + body_h, len(body)), len(body)) if len(body) > body_h else ''
        rows = body[self.top:self.top + body_h]
        rows += [Row()] * (body_h - len(rows))
        if not self.image:
            hints = hints[:2]
        return [header('SSH key', more, w)] + rows + [footer(hints)]

    def key(self, k):
        if k in BACK_KEYS:
            self.app.pop()
        elif k in ('g', 'G') and not self.found:
            self.generate()
        elif not self.found:
            return False
        elif k in ('y', 'Y'):
            self.app.copy(self.found[1], 'Copied the public key  (cz-peers-clip)')
        elif k in ('t', 'T') and self.image:
            self.as_text = not self.as_text
            self.top = 0
        elif k in UP_KEYS:
            self.top -= 1
        elif k in DOWN_KEYS:
            self.top += 1
        else:
            return False
        return True


class MenuView(View):
    """A vertical list of choices; each item is (label, callback, hint)."""

    def __init__(self, app, title, items, right=''):
        super().__init__(app)
        self.title, self.items, self.right = title, items, right
        self.sel = 0

    def render(self, w, h):
        body_h = h - 2
        top = max(0, min(self.sel - body_h + 1, len(self.items) - body_h))
        rows = []
        for i, item in enumerate(self.items[top:top + body_h], top):
            label, _, hint = item
            bg = SEL_BG if i == self.sel else None
            row = Row(bg=bg)
            row.add((CURSOR if i == self.sel else ' ') + ' ', S(BLUE, bg, True))
            row.add(pad(label, w - 3 - text_width(hint) - 1), S(WHITE, bg, i == self.sel))
            row.add(hint, S(MUTED, bg))
            rows.append(row)
        rows += [Row()] * (body_h - len(rows))
        return [header(self.title, self.right, w)] + rows + [footer([('C', 'choose'), ('Z', 'back')])]

    def key(self, k):
        if k in UP_KEYS:
            self.sel = (self.sel - 1) % len(self.items)
        elif k in DOWN_KEYS:
            self.sel = (self.sel + 1) % len(self.items)
        elif k in OK_KEYS:
            self.items[self.sel][1]()
        elif k in BACK_KEYS:
            self.app.pop()
        else:
            for label, callback, hint in self.items:
                if hint == k:
                    callback()
                    return True
            return False
        return True


class ConfirmView(View):
    def __init__(self, app, title, message, on_yes):
        super().__init__(app)
        self.title, self.message, self.on_yes = title, message, on_yes

    def render(self, w, h):
        rows = [header(self.title, '', w), Row()]
        rows += [text_row(' ' + line) for line in wrap(self.message, w - 2)]
        rows += [Row()] * (h - 1 - len(rows))
        return rows + [footer([('y', 'yes'), ('n', 'no')])]

    def key(self, k):
        if k in ('y', 'Y', 'ENTER'):
            self.app.pop()
            self.on_yes()
        elif k in ('n', 'N', 'ESC', 'q'):
            self.app.pop()
        return True


class SSHPrompt(View):
    """User and port for ssh; install=True asks them for installing the key."""

    def __init__(self, app, node, install=False):
        super().__init__(app)
        self.node = node
        self.install = install
        saved = app.hosts.get(node.name, {})
        default_user = saved.get('user') or app.hosts.get('_default', {}).get('user') or ME
        self.fields = [['User', default_user], ['Port', str(saved.get('port', '22'))]]
        self.focus = 0
        self.fresh = True  # first keystroke replaces the suggested value

    def render(self, w, h):
        title = ('Add key to ' if self.install else 'SSH ') + self.node.name
        rows = [header(title, self.node.ip, w)]
        if not self.install:
            rows.append(Row())
        for i, (label, value) in enumerate(self.fields):
            focused = i == self.focus
            row = Row()
            row.add('  ' + pad(label, 6), S(MUTED))
            bg = SEL_BG if focused else BAR_BG
            shown = value + ('_' if focused else '')
            row.add(' ' + pad(shown, min(24, w - 10)) + ' ', S(WHITE, bg, focused))
            rows.append(row)
            rows.append(Row())
        user, port = self.fields[0][1], self.fields[1][1]
        if self.install:
            os_name = 'Windows' if self.node.os.lower() == 'windows' else 'it'
            rows += [text_row(' ' + line, MUTED) for line in wrap(
                'Logs in once with the password of %s on %s and adds this Cardputer\'s '
                'key there.' % (user or 'the user', os_name), w - 2)]
        else:
            rows.append(text_row('  ' + ssh_shown(self.node, user, port), DIM))
            if self.node.tailscale_ssh:
                rows.append(text_row('  Tailscale SSH: no key needed', BLUE))
        rows = rows[:h - 1] + [Row()] * max(0, h - 1 - len(rows))
        return rows + [footer([('Enter', 'add key' if self.install else 'connect'), ('Tab', 'field'), ('Esc', 'back')])]

    def key(self, k):
        field = self.fields[self.focus]
        if k == 'ESC':
            self.app.pop()
        elif k in ('TAB', 'DOWN', 'UP'):
            self.focus = 1 - self.focus
            self.fresh = True
        elif k == 'ENTER':
            user, port = self.fields[0][1].strip(), self.fields[1][1].strip() or '22'
            if not user:
                self.app.flash('User is empty', RED)
            elif not port.isdigit():
                self.app.flash('Port must be a number', RED)
            else:
                self.app.pop()
                if self.install:
                    self.app.install_key(self.node, user, port)
                else:
                    self.app.ssh(self.node, user, port)
        elif k == 'BS':
            field[1] = '' if self.fresh else field[1][:-1]
            self.fresh = False
        elif len(k) == 1 and k.isprintable() and k not in ' @':
            if self.focus == 1 and not k.isdigit():
                return True
            field[1] = k if self.fresh else field[1] + k
            self.fresh = False
        return True


class PingView(View):
    def __init__(self, app, node):
        super().__init__(app)
        self.node = node
        self.lines = []
        self.proc = None
        self.start()

    def start(self):
        self.stop()
        self.lines = ['tailscale ping %s' % self.node.ip]
        try:
            self.proc = subprocess.Popen(
                ['tailscale', 'ping', '-c', '5', '--timeout', '3s', self.node.ip],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        except OSError as e:
            self.lines.append(str(e))
            return
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        for raw in proc.stdout:
            line = raw.decode(errors='replace').rstrip()
            if line:
                self.lines.append(line)
                self.app.wake()
        code = proc.wait()
        if proc is self.proc:
            self.lines.append('-- done --' if code == 0 else '-- exit %d --' % code)
            self.app.wake()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def render(self, w, h):
        body_h = h - 2
        rows = []
        for line in self.lines:
            colour = GREEN if 'via' in line and 'DERP' not in line else YELLOW if 'DERP' in line else \
                RED if ('timeout' in line or 'exit' in line or 'no reply' in line) else WHITE
            rows += [text_row(' ' + part, colour) for part in wrap(line, w - 2)]
        running = self.proc and self.proc.poll() is None
        return ([header('Ping ' + self.node.name, 'running' if running else '', w)] + rows[-body_h:]
                + [Row()] * max(0, body_h - len(rows)) + [footer([('Z', 'back'), ('r', 'again')])])

    def key(self, k):
        if k in BACK_KEYS:
            self.stop()
            self.app.pop()
        elif k == 'r':
            self.start()
        return True


class FilterBar:
    def __init__(self):
        self.active = False
        self.text = ''


class ListView(View):
    def __init__(self, app):
        super().__init__(app)
        self.sel = 0
        self.sel_id = None
        self.top = 0
        self.filter = FilterBar()

    def visible(self):
        peers = self.app.model.peers
        if self.filter.text:
            peers = [p for p in peers if p.matches(self.filter.text)]
        return peers

    def current(self):
        peers = self.visible()
        if not peers:
            return None
        # keep the selection on the same device when the list re-sorts
        if self.sel_id:
            for i, p in enumerate(peers):
                if p.id == self.sel_id:
                    self.sel = i
                    break
        self.sel = max(0, min(self.sel, len(peers) - 1))
        self.sel_id = peers[self.sel].id
        return peers[self.sel]

    def move(self, delta):
        peers = self.visible()
        if peers:
            self.current()
            self.sel = (self.sel + delta) % len(peers) if abs(delta) == 1 else max(0, min(len(peers) - 1, self.sel + delta))
            self.sel_id = peers[self.sel].id

    def render(self, w, h):
        m = self.app.model
        state = m.state
        colour = GREEN if state == 'Running' else YELLOW if state in ('Starting', '...') else RED
        me = m.self_node
        top = Row(bg=BAR_BG)
        top.add(' Peers ', S(WHITE, BAR_BG, True))
        top.add(DOT_ON + ' ', S(colour, BAR_BG))
        label = {'Running': 'On', 'Stopped': 'Off', 'NeedsLogin': 'Log in', 'NeedsMachineAuth': 'Needs approval'}.get(state, state)
        top.add(label, S(colour, BAR_BG))
        right = (me.ip if me else '')
        room = w - text_width(' Peers ' + DOT_ON + ' ' + label) - 1
        top.add(pad(right, room, right=True) + ' ', S(MUTED, BAR_BG))
        rows = [top]

        body_h = h - 2
        peers = self.visible()
        cur = self.current()
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + body_h:
            self.top = self.sel - body_h + 1
        self.top = max(0, min(self.top, max(0, len(peers) - body_h)))

        if not peers:
            if m.error and not tailscale_installed():
                msg = 'Tailscale is not installed. Press Enter to install it (needs internet, takes a few minutes).'
            else:
                msg = m.error or ('No devices match "%s"' % self.filter.text if self.filter.text else
                                  {'Stopped': 'Tailscale is off. Press t to connect.',
                                   'NeedsLogin': 'Not logged in. Press Enter to log in with a QR code.',
                                   'NeedsMachineAuth': 'Waiting for an admin to approve this device.',
                                   }.get(state, 'No other devices yet.'))
            rows.append(Row())
            rows += [text_row(' ' + line, MUTED) for line in wrap(msg, w - 2)]
        ip_w = 15
        link_w = 10 if w < 60 else 16
        name_w = max(8, w - 3 - ip_w - 1 - link_w - 1)
        for i, p in enumerate(peers[self.top:self.top + body_h], self.top):
            selected = p is cur
            bg = SEL_BG if selected else None
            row = Row(bg=bg)
            row.add(CURSOR if selected else ' ', S(BLUE, bg, True))
            row.add(DOT_ON if p.online else DOT_OFF, S(GREEN if p.online else DIM, bg))
            if p.tailscale_ssh:
                row.add(' ' + pad(p.name, name_w - 2), S(WHITE if p.online else MUTED, bg, selected))
                row.add(' ' + TS_SSH_MARK, S(BLUE, bg, True))
            else:
                row.add(' ' + pad(p.name, name_w), S(WHITE if p.online else MUTED, bg, selected))
            row.add(' ' + pad(p.ip, ip_w), S(MUTED if p.online else DIM, bg))
            link = 'EXIT' if p.exit_node else p.link
            lcol = YELLOW if p.exit_node or link.startswith('relay') else GREEN if link == 'direct' else MUTED if p.online else DIM
            row.add(' ' + pad(link, link_w, right=True), S(lcol, bg))
            rows.append(row)
        rows += [Row()] * (h - 1 - len(rows))

        if self.filter.active or self.filter.text:
            bar = Row(bg=BAR_BG)
            bar.add(' / ', S(YELLOW, BAR_BG, True))
            bar.add(self.filter.text + ('_' if self.filter.active else ''), S(WHITE, BAR_BG))
            bar.add('  %d match' % len(peers), S(MUTED, BAR_BG))
            rows.append(bar)
        else:
            rows.append(footer([('C', 'menu'), ('s', 'ssh'), ('y', 'copy'), ('m', 'me'), ('?', 'help')]))
        return rows

    def key(self, k):
        f = self.filter
        if f.active:
            if k in ('ESC',):
                f.active, f.text = False, ''
            elif k == 'ENTER':
                f.active = False
            elif k == 'BS':
                f.text = f.text[:-1]
            elif k in ('UP', 'DOWN'):
                self.move(-1 if k == 'UP' else 1)
            elif len(k) == 1 and k.isprintable():
                f.text += k
                self.sel, self.sel_id = 0, None
            return True
        if k in UP_KEYS:
            self.move(-1)
        elif k in DOWN_KEYS:
            self.move(1)
        elif k == 'PGUP':
            self.move(-5)
        elif k == 'PGDN':
            self.move(5)
        elif k == '/':
            f.active = True
        elif k in ('ESC', 'z', 'LEFT'):
            if f.text:
                f.text = ''
            elif k == 'ESC':
                self.app.quit()
        elif k == 'q':
            self.app.quit()
        elif k in OK_KEYS:
            node = self.current()
            if node:
                self.app.node_menu(node)
            elif self.app.model.error and not tailscale_installed():
                self.app.install_tailscale()
            elif self.app.model.state == 'NeedsLogin':
                self.app.login()
            elif self.app.model.state == 'Stopped':
                self.app.toggle_connection()
        elif k == 'l':
            self.app.login()
        elif k in ('s', 'y', 'p', 'i', 'h', 'k'):
            node = self.current()
            if node:
                self.app.node_action(k, node)
        elif k == 'K':
            self.app.push(KeyView(self.app))
        elif k == 'm':
            self.app.push(SelfView(self.app))
        elif k == '?':
            self.app.push(HelpView(self.app))
        elif k == 'e':
            self.app.exit_node_menu()
        elif k == 't':
            self.app.toggle_connection()
        elif k == 'r':
            self.app.refresh_now()
        else:
            return False
        return True


# -------------------------------------------------------------------- app --
class App:
    def __init__(self):
        self.term = Terminal()
        self.model = Model()
        self.hosts = load_hosts()
        self.prefs = None
        self.list_view = ListView(self)
        self.stack = [self.list_view]
        self.message = None
        self.running = True
        self.wake_r, self.wake_w = os.pipe()
        self.refresh_event = threading.Event()
        self.overlay = Overlay()
        self.overlay_at = None
        self.last_frame = None
        self.pending = []  # callables from worker threads, run on the main loop

    # -- plumbing
    def wake(self):
        try:
            os.write(self.wake_w, b'x')
        except OSError:
            pass

    def call_soon(self, fn):
        self.pending.append(fn)
        self.wake()

    def push(self, view):
        self.stack.append(view)

    def pop(self):
        if len(self.stack) > 1:
            self.stack.pop()

    def quit(self):
        self.running = False

    def flash(self, text, colour=GREEN, seconds=3):
        self.message = (text, colour, time.monotonic() + seconds)

    def refresh_now(self):
        self.refresh_event.set()

    def _poller(self):
        while self.running:
            self.model.refresh()
            try:
                self.prefs = localapi('GET', 'prefs')
            except (APIError, ValueError):
                pass
            self.wake()
            self.refresh_event.wait(4)
            self.refresh_event.clear()

    # -- actions
    def node_menu(self, node):
        items = [('SSH', lambda: self.node_action('s', node), 's'),
                 ('Install my SSH key', lambda: self.node_action('k', node), 'k'),
                 ('Copy IP  ' + node.ip, lambda: self.node_action('y', node), 'y'),
                 ('Ping', lambda: self.node_action('p', node), 'p'),
                 ('Details', lambda: self.node_action('i', node), 'i'),
                 ('Shell here ($IP set)', lambda: self.node_action('h', node), 'h')]
        if node.ipv6:
            items.insert(2, ('Copy IPv6', lambda: self.copy(node.ipv6), ''))
        if node.dns:
            items.insert(2, ('Copy DNS name', lambda: self.copy(node.dns), ''))
        if node.exit_option:
            if node.exit_node:
                items.append(('Stop using as exit node', lambda: self.set_exit_node(None), ''))
            else:
                items.append(('Use as exit node', lambda: self.set_exit_node(node), ''))
        self.push(MenuView(self, node.name, items, 'online' if node.online else 'offline'))

    def node_action(self, k, node):
        while len(self.stack) > 1 and isinstance(self.stack[-1], MenuView):
            self.stack.pop()
        if k == 's':
            self.push(SSHPrompt(self, node))
        elif k == 'k':
            self.push(SSHPrompt(self, node, install=True))
        elif k == 'y':
            self.copy(node.ip)
        elif k == 'p':
            self.push(PingView(self, node))
        elif k == 'i':
            self.push(DetailsView(self, node))
        elif k == 'h':
            env = dict(os.environ, IP=node.ip, HOST=node.name)
            self.term.run_interactive(['bash', '-i'], '\x1b[33m$IP=%s  $HOST=%s\x1b[0m  (exit to go back)' % (node.ip, node.name), env)
        return True

    def copy(self, text, message=None):
        while len(self.stack) > 1 and isinstance(self.stack[-1], MenuView):
            self.stack.pop()
        try:
            CLIP_FILE.parent.mkdir(parents=True, exist_ok=True)
            CLIP_FILE.write_text(text + '\n')
            self.flash(message or 'Copied %s  (cz-peers-clip)' % text)
        except OSError as e:
            self.flash('Copy failed: %s' % e, RED)

    def remember_host(self, node, user, port, **extra):
        saved = self.hosts.get(node.name, {})
        entry = {'user': user, 'port': port}
        if saved.get('key') == user:  # the key was installed for this user
            entry['key'] = user
        entry.update(extra)
        self.hosts[node.name] = entry
        self.hosts['_default'] = {'user': user}
        save_hosts(self.hosts)

    def ssh(self, node, user, port):
        self.remember_host(node, user, port)
        argv = ssh_argv(node, user, port)
        code, err = self.term.run_interactive(argv, '\x1b[33m%s\x1b[0m  %s' % (ssh_shown(node, user, port), node.ip),
                                              capture=True)
        if code == 255:
            self.ssh_failed(node, user, port, err)
        self.refresh_now()

    def ssh_failed(self, node, user, port, err, installing=False):
        """Say what to do next after ssh exited with 255."""
        why, methods = ssh_failure(err)
        where = '%s@%s' % (user, node.name)
        if why == 'hostkey':
            self.push(ConfirmView(
                self, 'Host key changed', '%s now shows a different host key than the one saved in '
                '~/.ssh/known_hosts (reinstalled, or a different machine on this IP). Forget the old '
                'key and try again?' % node.name,
                lambda: self.forget_host_key(node) and (self.install_key(node, user, port) if installing
                                                         else self.ssh(node, user, port))))
        elif why == 'denied' and node.tailscale_ssh:
            self.flash('Tailscale SSH refused %s: check the tailnet SSH policy' % where, RED, 8)
        elif why == 'denied' and installing:
            self.flash('Wrong password for %s?' % where if takes_password(node, methods)
                       else '%s takes no passwords: add the key by hand (K)' % node.name, RED, 8)
        elif why == 'denied' and takes_password(node, methods):
            self.push(ConfirmView(
                self, 'Key not accepted', '%s did not accept a key from this Cardputer. Add your SSH key '
                'there now? You type the password once; ssh then logs in without it.' % where,
                lambda: self.install_key(node, user, port)))
        elif why == 'denied':
            self.push(ConfirmView(
                self, 'Key not accepted', '%s only takes keys, and none of this Cardputer\'s is in its '
                'authorized_keys. Show your public key as a QR code to add it there by hand?' % where,
                lambda: self.push(KeyView(self))))
        elif why == 'refused':
            hint = ' (Windows: turn on OpenSSH Server)' if node.os.lower() == 'windows' else ''
            self.flash('No SSH server on %s port %s%s' % (node.name, port, hint), RED, 8)
        elif why == 'timeout':
            self.flash('%s did not answer: is it online?' % node.name, RED, 6)

    def forget_host_key(self, node):
        known = SSH_DIR / 'known_hosts'
        names = {ssh_alias(node), node.ip, node.dns}
        for name in filter(None, names):
            try:
                subprocess.run(['ssh-keygen', '-R', name, '-f', str(known)], capture_output=True,
                               stdin=subprocess.DEVNULL, timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
        self.flash('Forgot the old host key of %s' % node.name, YELLOW)
        return True

    def install_key(self, node, user, port):
        """Log in once (with the device's password) and add this Cardputer's
        public key to the device's authorized_keys."""
        found = find_key()
        if not found:
            try:
                found = make_key()
            except OSError as e:
                self.flash('Could not make an SSH key: %s' % e, RED, 8)
                return
        priv, pub = found
        self.remember_host(node, user, port)
        argv = ssh_argv(node, user, port) + [install_command(node, pub)]
        banner = ('\x1b[33mAdding this Cardputer\'s SSH key to %s@%s\x1b[0m\r\n'
                  'Type the password of %s if asked.' % (user, node.name, user))
        code, err = self.term.run_interactive(argv, banner, capture=True)
        if code == 0:
            try:
                remember_key_in_config(ssh_alias(node), priv)
            except OSError:
                pass
            self.remember_host(node, user, port, key=user)
            self.push(ConfirmView(self, 'Key added', 'ssh to %s@%s now logs in with the key, without a '
                                  'password. Connect now?' % (user, node.name),
                                  lambda: self.ssh(node, user, port)))
        elif code == 255:
            self.ssh_failed(node, user, port, err, installing=True)
            if not self.message and not isinstance(self.stack[-1], ConfirmView):
                self.flash('Could not reach %s@%s' % (user, node.name), RED, 6)
        else:
            self.flash('Adding the key failed on %s (exit %d)' % (node.name, code), RED, 8)
        self.refresh_now()

    def api_failed(self, err):
        self.message = None  # e.g. a "Logging out..." still showing
        if isinstance(err, APIError) and err.status == 403:
            self.offer_operator_fix()
        else:
            self.flash(str(err), RED, 6)

    def offer_operator_fix(self):
        self.push(ConfirmView(
            self, 'Permission', 'Tailscale only lets its operator change settings, and %s is not it yet. '
            'Make %s the operator now? Runs: sudo tailscale set --operator=%s' % (ME, ME, ME),
            self.make_operator))

    def make_operator(self):
        argv = ['sudo', 'tailscale', 'set', '--operator=' + ME]
        code = self.term.run_interactive(argv, '\x1b[33m%s\x1b[0m' % ' '.join(argv))
        self.flash('Done: try again' if code == 0 else 'sudo failed (%d)' % code, GREEN if code == 0 else RED, 5)
        self.refresh_now()

    def install_tailscale(self):
        self.push(ConfirmView(
            self, 'Install Tailscale', 'Download and install Tailscale from tailscale.com (official '
            'install script, needs sudo and internet)? Then you can log in with a QR code.',
            self._run_install))

    def _run_install(self):
        script = ('curl -fsSL https://tailscale.com/install.sh | sh && '
                  'tailscale set --operator=%s' % shlex.quote(ME))
        code = self.term.run_interactive(['sudo', 'sh', '-c', script], '\x1b[33mInstalling Tailscale...\x1b[0m')
        if code == 0:
            self.flash('Installed. Press Enter to log in', GREEN, 6)
        else:
            self.flash('Install failed (%d)' % code, RED, 6)
        self.refresh_now()

    def login(self):
        state = self.model.state
        if state == 'NeedsLogin' or (self.model.status or {}).get('AuthURL'):
            self.push(LoginView(self))
        elif state == 'NeedsMachineAuth':
            self.flash('Waiting for an admin to approve this device', YELLOW, 5)
        elif self.model.error:
            self.flash(self.model.error, RED, 5)
        else:
            self.flash('Already logged in (to log out: m, then o)', YELLOW, 5)

    def logout(self):
        me = self.model.self_node
        who = me.owner if me and me.owner else 'this account'

        def work():
            try:
                localapi('POST', 'logout', timeout=30)
                self.call_soon(lambda: self.flash('Logged out. Press Enter to log in again', GREEN, 6))
            except (APIError, ValueError) as e:
                self.call_soon(lambda e=e: self.api_failed(e))
            self.refresh_now()

        def go():
            while len(self.stack) > 1:
                self.stack.pop()
            self.flash('Logging out...', YELLOW, 30)
            threading.Thread(target=work, daemon=True).start()

        self.push(ConfirmView(self, 'Log out', 'Log %s out of Tailscale on this Cardputer? It leaves the '
                              'tailnet until you log in again (with a QR code). SSH sessions into it '
                              'over Tailscale will drop.' % who, go))

    def edit_prefs(self, prefs, ok_message):
        try:
            localapi('PATCH', 'prefs', prefs)
            self.flash(ok_message)
        except (APIError, ValueError) as e:
            self.api_failed(e)
        self.refresh_now()

    def set_exit_node(self, node):
        while len(self.stack) > 1 and isinstance(self.stack[-1], MenuView):
            self.stack.pop()
        self.edit_prefs({'ExitNodeIDSet': True, 'ExitNodeID': node.id if node else '',
                         'ExitNodeIPSet': True, 'ExitNodeIP': ''},
                        'Exit node: %s' % (node.name if node else 'none'))

    def exit_node_menu(self):
        options = [p for p in self.model.peers if p.exit_option]
        items = [('None (direct internet)', lambda: self.set_exit_node(None), '')]
        for p in options:
            mark = ' (in use)' if p.exit_node else '' if p.online else ' (offline)'
            items.append((p.name + mark, lambda p=p: self.set_exit_node(p), ''))
        lan = bool((self.prefs or {}).get('ExitNodeAllowLANAccess'))
        items.append(('Allow LAN access: %s' % ('on' if lan else 'off'), lambda: self.edit_prefs(
            {'ExitNodeAllowLANAccessSet': True, 'ExitNodeAllowLANAccess': not lan},
            'LAN access %s' % ('off' if lan else 'on')) or self.pop(), ''))
        menu = MenuView(self, 'Exit node', items, '%d offered' % len(options))
        menu.sel = next((i for i, p in enumerate(options, 1) if p.exit_node), 0)
        self.push(menu)

    def toggle_connection(self):
        state = self.model.state
        if state == 'Running':
            self.push(ConfirmView(self, 'Disconnect', 'Turn Tailscale off on this Cardputer? '
                                  'SSH sessions into it over Tailscale will drop.',
                                  lambda: self.edit_prefs({'WantRunningSet': True, 'WantRunning': False}, 'Tailscale off')))
        elif state == 'NeedsLogin':
            self.login()
        else:
            self.edit_prefs({'WantRunningSet': True, 'WantRunning': True}, 'Connecting...')

    # -- main loop
    def draw(self):
        """Draw the top view; returns True if anything was sent to the terminal."""
        w, h = self.term.size()
        view = self.stack[-1]
        rows = view.render(w, h)
        rows = (rows + [Row()] * h)[:h]
        if self.message:
            text, colour, until_t = self.message
            if time.monotonic() < until_t:
                rows[-1] = Row([(' ' + text, S(BLACK, colour, True))], colour)
                if hasattr(view, 'overlay_image') and view.overlay_image():  # keep the QR's white cells
                    rows[-1].segs.insert(0, (' ' * view.QR_COLS, S(WHITE, WHITE)))
            else:
                self.message = None
        out = ['\x1b[?25l']
        for y, row in enumerate(rows):
            # never write the bottom-right cell: some terminals scroll on it
            width = w - 1 if y == h - 1 else w
            out.append('\x1b[%d;1H' % (y + 1) + row.render(width))
        frame = ''.join(out)
        # Unchanged frames are not resent, so the launcher does not repaint (and
        # wipe the QR overlay); it also saves work on this small CPU.
        if frame == self.last_frame and not self.term.fresh:
            return False
        self.term.write(frame)
        self.term.fresh = False
        self.last_frame = frame
        return True

    def run(self):
        self.term.enter()
        signal.signal(signal.SIGWINCH, lambda *_: (setattr(self.term, 'fresh', True), self.wake()))
        signal.signal(signal.SIGTERM, lambda *_: self.quit() or self.wake())
        threading.Thread(target=self._poller, daemon=True).start()
        try:
            while self.running:
                while self.pending:
                    self.pending.pop(0)()
                view = self.stack[-1]
                if hasattr(view, 'tick'):
                    view.tick()
                    view = self.stack[-1]
                wrote = self.draw()
                image = view.overlay_image() if hasattr(view, 'overlay_image') else None
                now = time.monotonic()
                if image is None:
                    self.overlay_at = None
                elif wrote or self.overlay_at is None:
                    self.overlay_at = now + 0.3  # let the launcher paint the white cells first
                timeout = 1.0 if self.overlay_at is None else max(0.02, min(1.0, self.overlay_at - now))
                try:
                    r, _, _ = select.select([self.term.fd, self.wake_r], [], [], timeout)
                except InterruptedError:
                    continue
                if self.overlay_at is not None and time.monotonic() >= self.overlay_at:
                    self.overlay.paint(image)
                    self.overlay_at = time.monotonic() + 1.0
                if self.wake_r in r:
                    os.read(self.wake_r, 4096)
                if self.term.fd in r:
                    for k in self.term.read_keys():
                        if k == 'CTRL_C':
                            self.quit()
                            break
                        self.stack[-1].key(k)
                        if not self.running:
                            break
        finally:
            self.running = False
            for view in self.stack:
                if isinstance(view, PingView):
                    view.stop()
            self.term.leave()


def main():
    if not sys.stdin.isatty():
        print('cz-peers needs a terminal (the launcher entry runs it in one).', file=sys.stderr)
        return 1
    # ssh and bash run in this terminal; the launcher's own shell uses st-256color
    if os.environ.get('TERM', 'dumb') in ('dumb', 'linux', ''):
        os.environ['TERM'] = 'st-256color'
    os.environ.setdefault('COLORTERM', 'truecolor')
    App().run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
