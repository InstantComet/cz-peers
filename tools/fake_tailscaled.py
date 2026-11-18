#!/usr/bin/python3
"""Test helper: a pretend tailscaled LocalAPI on a unix socket, so logout and
login can be tried without logging the real device out (which would also cut
an SSH session that runs over Tailscale).

usage: fake_tailscaled.py SOCKET [STATE]
  STATE: Running (default) or NeedsLogin. Then run the app with
  CZ_TS_SOCKET=SOCKET. Login completes LOGIN_AFTER seconds (default 6) after
  the app asks for it; FORBID=1 answers every write with 403.
"""
import http.server
import json
import os
import socketserver
import sys
import threading
import time

SOCK = sys.argv[1]
LOGIN_AFTER = float(os.environ.get('LOGIN_AFTER', 6))
FORBID = os.environ.get('FORBID') == '1'
URL = 'https://login.tailscale.com/a/1a2b3c4d5e6f7'
state = {'backend': sys.argv[2] if len(sys.argv) > 2 else 'Running', 'auth_url': '',
         'prefs': {'WantRunning': True, 'CorpDNS': True, 'RouteAll': False, 'OperatorUser': 'pi',
                   'ExitNodeAllowLANAccess': False}}
lock = threading.Lock()
def _ago(seconds):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - seconds))


# Placeholder devices only (also used for the store screenshots).
SELF = {'ID': 'self', 'DNSName': 'cardzero.example.ts.net.', 'TailscaleIPs': ['100.101.102.103', 'fd7a:115c:a1e0::6501:6667'],
        'OS': 'linux', 'Online': True, 'UserID': 1, 'KeyExpiry': '2027-05-01T00:00:00Z'}


def _peer(i, name, ip, os_, online=True, **extra):
    p = {'ID': 'p%d' % i, 'DNSName': name + '.example.ts.net.', 'TailscaleIPs': [ip, 'fd7a:115c:a1e0::%x' % (0x1000 + i)],
         'OS': os_, 'Online': online, 'UserID': 1, 'RxBytes': 1234567 * i, 'TxBytes': 765432 * i,
         'KeyExpiry': '2027-03-01T00:00:00Z'}
    p.update(extra)
    return p


PEERS = {'k%d' % i: p for i, p in enumerate([
    _peer(1, 'laptop', '100.64.12.7', 'macOS', CurAddr='192.168.1.20:41641', LastHandshake=_ago(30)),
    _peer(2, 'homeserver', '100.88.3.21', 'linux', CurAddr='192.168.1.10:41641', ExitNodeOption=True),
    _peer(3, 'vps-tokyo', '100.71.40.9', 'linux', Relay='tok', Active=True, ExitNodeOption=True, ExitNode=True),
    _peer(4, 'raspberrypi', '100.77.19.2', 'linux', CurAddr='192.168.1.31:41641'),
    _peer(5, 'pixel-8', '100.92.5.66', 'android'),
    _peer(6, 'work-desktop', '100.83.27.140', 'windows', Relay='fra', Active=True),
    _peer(7, 'nas', '100.66.8.14', 'linux', online=False, LastSeen=_ago(2 * 3600)),
    _peer(8, 'old-thinkpad', '100.99.61.5', 'linux', online=False, LastSeen=_ago(9 * 86400)),
], 1)}


def status():
    with lock:
        s = {'BackendState': state['backend'], 'AuthURL': state['auth_url'], 'Version': '1.104.7-fake',
             'MagicDNSSuffix': 'example.ts.net', 'Health': []}
        if state['backend'] in ('Running', 'Starting', 'Stopped'):
            s.update(Self=SELF, Peer=PEERS, User={'1': {'LoginName': 'alex@example.com'}},
                     CurrentTailnet={'Name': 'alex@example.com'})
        else:
            s.update(Self={'ID': '', 'DNSName': '', 'TailscaleIPs': [], 'Online': False}, Peer={}, User={})
        return s


def finish_login():
    time.sleep(LOGIN_AFTER)
    with lock:
        if state['auth_url']:
            state.update(backend='Running', auth_url='')
            state['prefs']['WantRunning'] = True


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        sys.stderr.write('fake: %s %s\n' % (self.command, self.path))

    def reply(self, code, body):
        data = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.send_response(code)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.endswith('/status'):
            self.reply(200, status())
        elif self.path.endswith('/prefs'):
            self.reply(200, state['prefs'])
        else:
            self.reply(404, b'not found')

    def write_op(self):
        n = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(n) or b'null') if n else None
        if FORBID:
            return self.reply(403, b'access denied')
        p = self.path
        with lock:
            if p.endswith('/logout'):
                time.sleep(1)
                state.update(backend='NeedsLogin', auth_url='')
                state['prefs']['WantRunning'] = False
                return self.reply(204, b'')
            if p.endswith('/login-interactive'):
                state['auth_url'] = URL
                threading.Thread(target=finish_login, daemon=True).start()
                return self.reply(204, b'')
            if p.endswith('/prefs'):
                for k, v in (body or {}).items():
                    if not k.endswith('Set'):
                        state['prefs'][k] = v
                if 'WantRunning' in (body or {}) and state['backend'] in ('Running', 'Stopped'):
                    state['backend'] = 'Running' if body['WantRunning'] else 'Stopped'
                return self.reply(200, state['prefs'])
        self.reply(404, b'not found')

    do_POST = do_PATCH = write_op


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class UnixHandler(Handler):
    def setup(self):
        super().setup()
        self.client_address = ('fake', 0)


if os.path.exists(SOCK):
    os.unlink(SOCK)
Server(SOCK, UnixHandler).serve_forever()
