"""Tests for preview-link. No root, no network, no change outside a temp directory.

StubTests run the CLI against fake nginx, package-manager and systemctl programs.
RealNginxTests start a real, unprivileged nginx in a private prefix. They share one nginx and
run in name order (test_01, test_02, ...), because each step builds on the one before.
"""

import atexit
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import runpy
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / 'preview-link'
REAL_NGINX = '/usr/sbin/nginx'
MARKER = '# managed by preview-link'


def clean_env(tmp, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(('PREVIEW_LINK_', 'XDG_'))}
    env.update(HOME=str(tmp / 'home'), PREVIEW_LINK_LOG=str(tmp / 'log.jsonl'),
               PREVIEW_LINK_CONFIG=str(tmp / 'config.json'))
    env.update(extra)
    return env


def cli(args, env, **kwargs):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], env=env,
                          capture_output=True, text=True, timeout=60, **kwargs)


def tree(path):
    path = Path(path)
    if not path.exists():
        return None
    out = {}
    for item in sorted(path.rglob('*')):
        info = item.lstat()
        content = os.readlink(item) if item.is_symlink() else (item.read_bytes() if item.is_file() else None)
        out[str(item.relative_to(path))] = (info.st_mode, content)
    return out


def mode_of(path):
    return Path(path).stat().st_mode & 0o777


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait_for(check, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if check():
                return True
        except OSError:
            pass
        time.sleep(0.1)
    return False


def http_request(port, host, path, method='GET'):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    try:
        conn.request(method, path, headers={'Host': host})
        response = conn.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        conn.close()


class ProbeSafetyTests(unittest.TestCase):
    def test_existing_probe_directory_is_not_deleted(self):
        module = runpy.run_path(str(SCRIPT))
        token = 'a' * 20
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / token
            folder.mkdir()
            existing = folder / 'existing.html'
            existing.write_text('keep')
            results = []
            with patch.object(module['secrets'], 'token_hex', return_value=token):
                module['round_trip'](Path(tmp), 'preview.test', '127.0.0.1', 80,
                                     lambda *args: results.append(args))
            self.assertEqual(existing.read_text(), 'keep')
            self.assertFalse(results[0][0])
            self.assertIn('cannot create probe directory', results[0][2])

# ------------------------------------------------------------------ stubs

PY = f'#!{sys.executable}\n'

NGINX_STUB = PY + '''
import os, sys
args = sys.argv[1:]
if os.environ.get('FAKE_LOG'):
    with open(os.environ['FAKE_LOG'], 'a') as out:
        out.write(' '.join(['nginx'] + args) + '\\n')
if args[:1] == ['-v']:
    sys.stderr.write('nginx version: nginx/1.24.0 (Fake)\\n')
elif args[:1] == ['-V']:
    sys.stderr.write('nginx version: nginx/1.24.0 (Fake)\\nconfigure arguments: --conf-path=%s\\n' % os.environ.get('FAKE_CONF', ''))
elif args[:1] == ['-T']:
    if os.environ.get('FAKE_T_DUMP_FAIL'):
        sys.stderr.write('nginx: [emerg] cannot open the pid file\\n')
        sys.exit(1)
    sys.stderr.write('nginx: the configuration file ok\\n')
    sys.stdout.write(open(os.environ['FAKE_DUMP']).read())
elif args[:1] == ['-t']:
    code = int(os.environ.get('FAKE_T_RC', '0'))
    sys.stderr.write('nginx: [emerg] fake failure\\n' if code else 'nginx: configuration file test is successful\\n')
    sys.exit(code)
'''

LOGGING_STUB = PY + '''
import os, sys
if os.environ.get('FAKE_LOG'):
    with open(os.environ['FAKE_LOG'], 'a') as out:
        out.write(' '.join([os.path.basename(sys.argv[0])] + sys.argv[1:]) + '\\n')
if os.path.basename(sys.argv[0]) == 'systemctl' and sys.argv[1:2] == ['is-active']:
    sys.exit(int(os.environ.get('FAKE_SYSTEMD_ACTIVE', '0')))
'''


class StubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='pl-stub-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()
        (self.tmp / 'home').mkdir()
        self.state = self.tmp / 'state'
        self.state.mkdir()
        self.calls = self.state / 'calls.log'
        self.release = self.state / 'os-release'
        self.set_release('ubuntu', 'Ubuntu 24.04 LTS')

    def set_release(self, ident, pretty, like=''):
        lines = [f'ID={ident}', f'PRETTY_NAME="{pretty}"'] + ([f'ID_LIKE="{like}"'] if like else [])
        self.release.write_text('\n'.join(lines) + '\n')

    def stub(self, name, body=LOGGING_STUB):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def env(self, **extra):
        values = dict(PATH=str(self.bin), PREVIEW_LINK_EXTRA_PATH='', PREVIEW_LINK_OS_RELEASE=str(self.release),
                      FAKE_LOG=str(self.calls))
        values.update(extra)
        return clean_env(self.tmp, **values)

    def logged(self):
        return self.calls.read_text() if self.calls.exists() else ''

    def build_nginx(self, kind, extra=None):
        etc = self.tmp / 'etc' / 'nginx'
        etc.mkdir(parents=True)
        includes = {
            'debian': [f'include {etc}/conf.d/*.conf;', f'include {etc}/sites-enabled/*;'],
            'confd': [f'include {etc}/conf.d/*.conf;'],
            'servers': ['include servers/*;'],
            'relative': ['include conf.d/*.conf;'],
            'none': [],
            'outside': [],
        }[kind]
        top = {'debian': 'user www-data;', 'confd': 'user nginx nginx;'}.get(kind, '')
        if kind == 'outside':
            top = f'include {etc}/conf.d/*.conf;'
        main = f'{top}\nevents {{}}\nhttp {{\n' + ''.join(f'    {line}\n' for line in includes) + '}\n'
        for sub in {'debian': ['sites-available', 'sites-enabled', 'conf.d'], 'servers': ['servers']}.get(kind, ['conf.d']):
            (etc / sub).mkdir()
        (etc / 'nginx.conf').write_text(main)
        dump = f'# configuration file {etc}/nginx.conf:\n{main}\n'
        for path, text in (extra or {}).items():
            Path(path).write_text(text)
            dump += f'# configuration file {path}:\n{text}\n'
        (self.state / 'dump.txt').write_text(dump)
        self.stub('nginx', NGINX_STUB)
        self.etc = etc
        return etc

    def nginx_env(self, **extra):
        return self.env(FAKE_DUMP=str(self.state / 'dump.txt'), FAKE_CONF=str(self.etc / 'nginx.conf'), **extra)

    def snapshot(self):
        return (tree(self.tmp / 'etc'), tree(self.tmp / 'www'), tree(self.tmp / 'home'),
                (self.tmp / 'config.json').exists(), (self.tmp / 'log.jsonl').exists())

    # -- package managers and missing nginx

    def missing_nginx(self, ident, pretty, like, manager, expect):
        self.set_release(ident, pretty, like)
        self.stub(manager)
        done = cli(['init', '--host', 'preview.test'], self.env())
        self.assertNotEqual(done.returncode, 0)
        for text in expect:
            self.assertIn(text, done.stdout)
        self.assertEqual(self.logged(), '', 'a dry run must not run the package manager')

    def test_apt_plan(self):
        self.missing_nginx('ubuntu', 'Ubuntu 24.04 LTS', 'debian', 'apt-get',
                           ['package manager: apt-get', 'apt-get update', 'apt-get install -y nginx'])

    def test_dnf_plan(self):
        self.missing_nginx('fedora', 'Fedora Linux 40', '', 'dnf', ['package manager: dnf', 'dnf install -y nginx'])

    def test_yum_through_id_like(self):
        self.missing_nginx('ol', 'Oracle Linux 7', 'fedora', 'yum', ['package manager: yum', 'yum install -y nginx'])

    def test_apk_plan(self):
        self.missing_nginx('alpine', 'Alpine Linux v3.20', '', 'apk', ['package manager: apk', 'apk add nginx'])

    def test_unknown_manager(self):
        self.set_release('plan9', 'Plan 9')
        done = cli(['init', '--host', 'preview.test'], self.env())
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('no supported package manager', done.stdout)
        self.assertIn('package manager: none found', done.stdout)

    def test_install_dry_run_changes_nothing(self):
        self.stub('apt-get')
        done = cli(['init', '--host', 'preview.test', '--install-nginx'], self.env())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('Plan: install nginx', done.stdout)
        self.assertEqual(self.logged(), '')

    def test_install_apply_needs_root(self):
        self.stub('apt-get')
        done = cli(['init', '--host', 'preview.test', '--install-nginx', '--apply'], self.env())
        if os.geteuid() == 0:
            # As root the stub package manager runs. It only logs, so nothing real is installed.
            self.assertIn('apt-get install -y nginx', self.logged())
        else:
            self.assertNotEqual(done.returncode, 0)
            self.assertIn('needs root', done.stderr)
            self.assertEqual(self.logged(), '')

    # -- vhost loading styles

    def dry_run(self, *extra, **env):
        before = self.snapshot()
        done = cli(['init', '--host', 'preview.test', *extra], self.nginx_env(**env))
        self.assertEqual(self.snapshot(), before, 'a dry run must change nothing')
        return done

    def test_debian_layout(self):
        self.build_nginx('debian')
        self.stub('apt-get')
        done = self.dry_run('--listen', '8080', '--root', self.tmp / 'www')
        self.assertEqual(done.returncode, 0, done.stderr)
        out = done.stdout
        for text in ('DRY RUN', 'package manager: apt-get', 'version 1.24.0', 'worker user:     www-data',
                     'sites-available + sites-enabled symlink', f'{self.etc}/sites-available/preview-link-preview.test.conf',
                     f'ln -sf {self.etc}/sites-available/preview-link-preview.test.conf {self.etc}/sites-enabled/',
                     '# managed by preview-link', 'listen 8080;', 'server_name preview.test;',
                     'link prefix: http://preview.test:8080/<token>/<file>', 'nginx -t', 'server_tokens off;'):
            self.assertIn(text, out)

    def test_confd_layout(self):
        self.build_nginx('confd')
        done = self.dry_run('--root', self.tmp / 'www')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('conf.d include', done.stdout)
        self.assertIn('worker user:     nginx', done.stdout)
        self.assertIn('link prefix: http://preview.test/<token>/<file>', done.stdout)

    def test_servers_layout_and_relative_include(self):
        self.build_nginx('servers')
        done = self.dry_run('--root', self.tmp / 'www')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('servers include (Homebrew style)', done.stdout)
        self.assertIn('not set in the config', done.stdout)

    def test_relative_include(self):
        self.build_nginx('relative')
        done = self.dry_run('--root', self.tmp / 'www')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('conf.d include', done.stdout)

    def test_no_include_stops_with_the_line_to_add(self):
        self.build_nginx('none')
        done = self.dry_run('--root', self.tmp / 'www')
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(f'include {self.etc}/conf.d/*.conf;', done.stderr)
        self.assertIn('never edits nginx.conf', done.stderr)

    def test_include_outside_http_is_no_proof(self):
        self.build_nginx('outside')
        done = self.dry_run('--root', self.tmp / 'www')
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('cannot prove', done.stderr)

    def test_conf_dir_override(self):
        self.build_nginx('debian')
        done = self.dry_run('--root', self.tmp / 'www', '--nginx-conf-dir', self.etc / 'conf.d')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('conf.d include', done.stdout)
        done = self.dry_run('--root', self.tmp / 'www', '--nginx-conf-dir', self.etc / 'sites-available')
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('cannot prove', done.stderr)

    def test_config_read_from_disk_when_dump_fails(self):
        self.build_nginx('confd')
        done = self.dry_run('--root', self.tmp / 'www', FAKE_T_DUMP_FAIL='1')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('read from disk', done.stdout)
        self.assertIn('conf.d include', done.stdout)

    def test_duplicate_server_name_warns(self):
        other = self.tmp / 'etc' / 'nginx' / 'conf.d' / 'other.conf'
        self.build_nginx('confd', {str(other): 'server { listen 80; server_name preview.test; }\n'})
        done = self.dry_run('--root', self.tmp / 'www')
        self.assertIn('another server block names preview.test on port 80', done.stdout)

    # -- TLS modes and base URL

    def test_tls_modes(self):
        self.build_nginx('confd')
        base = ['--root', self.tmp / 'www']
        out = self.dry_run(*base).stdout
        self.assertIn('TLS mode: none', out)
        self.assertIn('http://preview.test/<token>', out)
        out = self.dry_run(*base, '--tls', 'external').stdout
        self.assertIn('https://preview.test/<token>', out)
        out = self.dry_run(*base, '--tls', 'certbot').stdout
        self.assertIn('certbot --nginx -d preview.test', out)
        self.assertIn('https://preview.test/<token>', out)
        out = self.dry_run(*base, '--tls', 'external', '--base-url', 'https://links.example/').stdout
        self.assertIn('link prefix: https://links.example/<token>', out)
        self.assertNotIn('certbot', self.logged())

    def test_bad_arguments(self):
        self.build_nginx('confd')
        for args in (['--host', 'bad host'], ['--host', 'a..b'], ['--root', 'has space'], ['--root', '/'],
                     ['--listen', '99999'], ['--listen', 'x'], ['--base-url', 'ftp://x']):
            full = ['init', '--host', 'preview.test'] + args if args[0] != '--host' else ['init'] + args
            done = cli(full, self.nginx_env())
            self.assertNotEqual(done.returncode, 0, args)
        done = cli(['init'], self.nginx_env())
        self.assertNotEqual(done.returncode, 0)

    # -- apply with a fake nginx

    def apply(self, *extra, **env):
        return cli(['init', '--host', 'preview.test', '--apply', '--root', self.tmp / 'www', *extra], self.nginx_env(**env))

    def test_apply_confd_and_reload_through_nginx(self):
        self.build_nginx('confd')
        done = self.apply('--nginx-bin', self.bin / 'nginx')
        self.assertEqual(done.returncode, 0, done.stderr)
        vhost = self.etc / 'conf.d' / 'preview-link-preview.test.conf'
        self.assertTrue(vhost.read_text().startswith(MARKER))
        self.assertEqual(mode_of(vhost), 0o644)
        self.assertEqual(mode_of(self.tmp / 'www'), 0o755)
        config = json.loads((self.tmp / 'config.json').read_text())
        self.assertEqual(config['host'], 'preview.test')
        self.assertEqual(config['base_url'], 'http://preview.test')
        self.assertEqual(config['vhost'], str(vhost))
        self.assertIn('nginx -t', self.logged())
        self.assertIn('nginx -s reload', self.logged())

    def test_systemd_reload_is_chosen_when_the_unit_is_active(self):
        self.build_nginx('confd')
        self.stub('systemctl')
        done = self.apply()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('systemctl is-active --quiet nginx', self.logged())
        self.assertIn('systemctl reload nginx', self.logged())
        self.assertNotIn('nginx -s reload', self.logged())

    def test_inactive_systemd_unit_falls_back_to_nginx_signal(self):
        self.build_nginx('confd')
        self.stub('systemctl')
        done = self.apply(FAKE_SYSTEMD_ACTIVE='3')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn('systemctl reload', self.logged())
        self.assertIn('nginx -s reload', self.logged())

    def test_no_reload_flag(self):
        self.build_nginx('confd')
        self.stub('systemctl')
        done = self.apply('--no-reload')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn('reload', self.logged())

    def test_debian_apply_writes_file_and_symlink(self):
        self.build_nginx('debian')
        done = self.apply('--no-reload')
        self.assertEqual(done.returncode, 0, done.stderr)
        name = 'preview-link-preview.test.conf'
        available, enabled = self.etc / 'sites-available' / name, self.etc / 'sites-enabled' / name
        self.assertTrue(available.is_file())
        self.assertTrue(enabled.is_symlink())
        self.assertEqual(os.readlink(enabled), str(available))

    def test_failed_nginx_test_removes_new_file_and_symlink(self):
        self.build_nginx('debian')
        done = self.apply('--no-reload', FAKE_T_RC='1')
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('fake failure', done.stderr)
        self.assertEqual(list((self.etc / 'sites-available').iterdir()), [])
        self.assertEqual(list((self.etc / 'sites-enabled').iterdir()), [])
        self.assertFalse((self.tmp / 'www').exists(), 'a root created by the failed run is removed')
        self.assertFalse((self.tmp / 'config.json').exists())

    def test_failed_nginx_test_restores_previous_content(self):
        self.build_nginx('confd')
        self.assertEqual(self.apply('--no-reload').returncode, 0)
        vhost = self.etc / 'conf.d' / 'preview-link-preview.test.conf'
        before = vhost.read_bytes()
        done = cli(['init', '--host', 'preview.test', '--apply', '--no-reload', '--root', self.tmp / 'www2'],
                   self.nginx_env(FAKE_T_RC='1'))
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(vhost.read_bytes(), before)
        self.assertFalse((self.tmp / 'www2').exists())
        self.assertEqual(json.loads((self.tmp / 'config.json').read_text())['root'], str(self.tmp / 'www'))

    def test_unwritable_conf_dir_needs_root_and_changes_nothing(self):
        if os.geteuid() == 0:
            self.skipTest('root can write to any directory')
        self.build_nginx('confd')
        (self.etc / 'conf.d').chmod(0o555)
        self.addCleanup((self.etc / 'conf.d').chmod, 0o755)
        done = self.apply('--no-reload')
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('needs root', done.stderr)
        self.assertIn('never calls sudo', done.stderr)
        self.assertEqual(list((self.etc / 'conf.d').iterdir()), [])
        self.assertFalse((self.tmp / 'config.json').exists())

    def test_foreign_file_is_not_overwritten(self):
        self.build_nginx('confd')
        foreign = self.etc / 'conf.d' / 'preview-link-preview.test.conf'
        foreign.write_text('server { listen 80; }\n')
        done = self.apply('--no-reload')
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('--force', done.stderr)
        self.assertEqual(foreign.read_text(), 'server { listen 80; }\n')
        done = self.apply('--no-reload', '--force')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(foreign.read_text().startswith(MARKER))

    # -- put without configuration

    def test_put_without_config_says_to_run_init(self):
        source = self.tmp / 'a.html'
        source.write_text('x')
        done = cli(['put', source], clean_env(self.tmp))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('preview-link init', done.stderr)
        self.assertFalse((self.tmp / 'log.jsonl').exists())


# ------------------------------------------------------------------ real nginx

_PID_FILES = []


def _kill_leftover_nginx():
    for pid_file in _PID_FILES:
        try:
            os.kill(int(Path(pid_file).read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass


atexit.register(_kill_leftover_nginx)


class RealNginxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.access(REAL_NGINX, os.X_OK):
            raise unittest.SkipTest(f'{REAL_NGINX} is not available')
        cls.tmp = Path(tempfile.mkdtemp(prefix='pl-real-'))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.addClassCleanup(cls.stop_nginx)
        prefix = cls.prefix = cls.tmp / 'prefix'
        for sub in ('logs', 'tmp', 'conf.d'):
            (prefix / sub).mkdir(parents=True)
        (cls.tmp / 'home').mkdir()
        cls.port = free_port()
        cls.confd, cls.www = prefix / 'conf.d', prefix / 'www'
        cls.pid_file = prefix / 'nginx.pid'
        (prefix / 'nginx.conf').write_text(f'''
worker_processes 1;
pid {cls.pid_file};
error_log {prefix}/logs/error.log;
events {{ worker_connections 64; }}
http {{
    access_log {prefix}/logs/access.log;
    client_body_temp_path {prefix}/tmp/client;
    proxy_temp_path {prefix}/tmp/proxy;
    fastcgi_temp_path {prefix}/tmp/fastcgi;
    uwsgi_temp_path {prefix}/tmp/uwsgi;
    scgi_temp_path {prefix}/tmp/scgi;
    types {{ text/html html; text/plain txt; }}
    default_type application/octet-stream;
    server_names_hash_bucket_size 32;
    server {{
        listen 127.0.0.1:{cls.port} default_server;
        return 404 "default server\\n";
    }}
    include conf.d/*.conf;
}}
''')
        cls.wrapper = cls.tmp / 'nginx-private'
        cls.wrapper.write_text(f'#!/bin/sh\nexec {REAL_NGINX} -p {prefix}/ -e {prefix}/logs/error.log '
                               f'-c {prefix}/nginx.conf "$@"\n')
        cls.wrapper.chmod(0o755)
        _PID_FILES.append(cls.pid_file)
        start_log = cls.tmp / 'start.log'
        with start_log.open('w') as out:
            started = subprocess.run([str(cls.wrapper)], stdout=out, stderr=subprocess.STDOUT, timeout=20)
        ready = wait_for(lambda: http_request(cls.port, 'x', '/')[0] == 404)
        if started.returncode != 0 or not ready:
            log = (prefix / 'logs' / 'error.log')
            raise RuntimeError(f'test nginx did not start: {start_log.read_text()}\n{log.read_text() if log.exists() else ""}')

    @classmethod
    def stop_nginx(cls):
        subprocess.run([str(cls.wrapper), '-s', 'stop'], capture_output=True, timeout=20)
        if not wait_for(lambda: not cls.pid_file.exists(), 5):
            _kill_leftover_nginx()

    @classmethod
    def reload_nginx(cls):
        done = subprocess.run([str(cls.wrapper), '-s', 'reload'], capture_output=True, text=True, timeout=20)
        assert done.returncode == 0, done.stderr
        time.sleep(0.5)

    def env(self, config=None, **extra):
        return clean_env(self.tmp, PREVIEW_LINK_CONFIG=str(config or self.tmp / 'config.json'),
                         PREVIEW_LINK_CONNECT=f'127.0.0.1:{self.port}', **extra)

    def init_args(self, host='preview.test', root=None):
        return ['init', '--host', host, '--listen', f'127.0.0.1:{self.port}', '--root', root or self.www,
                '--nginx-conf-dir', self.confd, '--nginx-bin', self.wrapper]

    def vhost(self, host='preview.test'):
        return self.confd / f'preview-link-{host}.conf'

    def get(self, path, host='preview.test', method='GET'):
        return http_request(self.port, host, path, method)

    def serving(self):
        return self.get('/robots.txt')[0] == 200

    def test_01_dry_run_changes_nothing(self):
        before = (tree(self.prefix), (self.tmp / 'config.json').exists(), (self.tmp / 'log.jsonl').exists())
        done = cli(self.init_args(), self.env())
        self.assertEqual(done.returncode, 0, done.stderr)
        for text in ('DRY RUN', 'config source:   nginx -T', 'conf.d include', 'server_name preview.test;',
                     'try_files $uri =404;', 'location = /robots.txt', f'{self.wrapper} -t'):
            self.assertIn(text, done.stdout)
        after = (tree(self.prefix), (self.tmp / 'config.json').exists(), (self.tmp / 'log.jsonl').exists())
        self.assertEqual(before[0].keys() - {'nginx.pid'}, after[0].keys() - {'nginx.pid'})
        self.assertEqual(before[1:], after[1:])
        self.assertFalse(self.www.exists())
        self.assertEqual(list(self.confd.iterdir()), [])

    def test_02_apply_writes_vhost_and_nginx_serves_it_after_reload(self):
        done = cli(self.init_args() + ['--apply', '--no-reload'], self.env())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('nginx not reloaded', done.stdout)
        vhost = self.vhost()
        self.assertEqual(vhost.read_text().splitlines()[0], MARKER)
        self.assertEqual(mode_of(vhost), 0o644)
        self.assertEqual(mode_of(self.www), 0o755)
        config = json.loads((self.tmp / 'config.json').read_text())
        self.assertEqual(config['base_url'], f'http://preview.test:{self.port}')
        self.assertEqual(config['root'], str(self.www))
        self.assertFalse(self.serving(), 'nginx has not reloaded yet')
        self.reload_nginx()
        self.assertTrue(wait_for(self.serving))
        self.assertEqual(self.get('/robots.txt', host='other.test')[0], 404, 'another host gets the default server')

    def test_03_doctor_passes(self):
        done = cli(['doctor'], self.env())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertNotIn('FAIL', done.stdout)
        for text in ('ok    nginx binary', 'ok    vhost file', 'ok    nginx -t', 'ok    root writable',
                     'ok    GET probe file', 'ok    header Cache-Control', 'ok    header X-Robots-Tag',
                     'ok    header X-Content-Type-Options', 'ok    token directory not listable',
                     'ok    root not listable', 'ok    dotfile denied', 'ok    robots.txt disallows all',
                     'ok    POST refused', 'ok    deleted file returns 404', 'All checks passed.'):
            self.assertIn(text, done.stdout)
        self.assertEqual([p.name for p in self.www.iterdir()], [], 'doctor deletes its probe')

    def test_04_put_check_over_ls_rm(self):
        source = self.tmp / 'page.html'
        source.write_text('<p>first</p>\n')
        done = cli(['put', source], self.env(), preexec_fn=lambda: os.umask(0o077))
        self.assertEqual(done.returncode, 0, done.stderr)
        link = done.stdout.strip()
        self.assertRegex(link, rf'^http://preview\.test:{self.port}/[0-9a-f]{{20}}/page\.html$')
        token = link.split('/')[-2]
        self.assertEqual(mode_of(self.www / token), 0o755, 'a strict umask must not hide the folder')
        self.assertEqual(mode_of(self.www / token / 'page.html'), 0o644)
        (self.www / 'unrelated.txt').write_text('private')
        self.assertEqual(self.get('/unrelated.txt')[0], 404)
        (self.www / token / 'linked.txt').symlink_to(self.www / 'unrelated.txt')
        self.assertNotEqual(self.get(f'/{token}/linked.txt')[0], 200)
        (self.www / token / 'linked.txt').unlink()

        foreign = f'http://foreign.test/{token}/page.html'
        for target in (foreign, f'http://preview.test:{self.port}/{token}/missing.html'):
            denied = cli(['rm', target, '--no-check'], self.env())
            self.assertNotEqual(denied.returncode, 0)
            self.assertTrue((self.www / token / 'page.html').is_file())
        denied = cli(['put', source, '--over', foreign], self.env())
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual((self.www / token / 'page.html').read_text(), '<p>first</p>\n')
        (self.www / 'unrelated.txt').unlink()

        done = cli(['check', link], self.env())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(done.stdout.strip(), '200\ttext/html')
        self.assertEqual(done.stderr, '')

        status, headers, body = self.get(f'/{token}/page.html')
        self.assertEqual((status, body), (200, b'<p>first</p>\n'))
        self.assertEqual(headers['cache-control'], 'no-store, no-transform')
        self.assertEqual(headers['x-robots-tag'], 'noindex')
        self.assertEqual(headers['x-content-type-options'], 'nosniff')
        self.assertNotIn('nginx/', headers.get('server', ''), 'server_tokens off')
        self.assertEqual(self.get(f'/{token}/page.html', method='HEAD')[0], 200)
        self.assertEqual(self.get(f'/{token}/page.html', method='POST')[0], 405)
        self.assertIn(self.get(f'/{token}/')[0], (403, 404), 'no listing of the token folder')
        self.assertIn(self.get('/')[0], (403, 404), 'no listing of the root')
        (self.www / token / '.secret').write_text('hidden')
        self.assertIn(self.get(f'/{token}/.secret')[0], (403, 404), 'dotfile denied')
        self.assertEqual(self.get(f'/{token}/page.html', host='other.test')[0], 404)

        source.write_text('<p>second</p>\n')
        done = cli(['put', source, '--over', link], self.env())
        self.assertEqual((done.returncode, done.stdout.strip()), (0, link), done.stderr)
        self.assertEqual(self.get(f'/{token}/page.html')[2], b'<p>second</p>\n')

        done = cli(['ls'], self.env())
        self.assertIn(link, done.stdout)

        bad = cli(['put', source, '--name', 'bad name'], self.env())
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn('letters, digits', bad.stderr)

        (self.www / token / '.secret').unlink()
        done = cli(['rm', link], self.env())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('deleted, now 404', done.stdout)
        self.assertEqual(self.get(f'/{token}/page.html')[0], 404)
        self.assertFalse((self.www / token).exists())
        done = cli(['check', link], self.env())
        self.assertNotEqual(done.returncode, 0)
        self.assertTrue(done.stdout.startswith('404'))

        actions = [json.loads(line)['action'] for line in (self.tmp / 'log.jsonl').read_text().splitlines()]
        self.assertEqual(actions, ['init', 'put', 'replace', 'rm'])

    def test_05_failed_nginx_test_rolls_back(self):
        host = 'a' * 50 + '.test'
        other_config = self.tmp / 'config-fail.json'
        before = tree(self.confd)
        done = cli(self.init_args(host, self.tmp / 'www-fail') + ['--apply'], self.env(config=other_config))
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn('emerg', done.stderr)
        self.assertIn('removed or restored', done.stderr)
        self.assertEqual(tree(self.confd), before)
        self.assertFalse((self.tmp / 'www-fail').exists())
        self.assertFalse(other_config.exists())
        self.assertTrue(self.serving(), 'the running nginx is untouched')

    def test_06_refuse_overwrite_then_force(self):
        other_config = self.tmp / 'config-other.json'
        foreign = self.vhost('other.test')
        foreign.write_text(f'server {{ listen 127.0.0.1:{self.port}; server_name other.test; return 200 "mine\\n"; }}\n')
        original = foreign.read_text()
        args = self.init_args('other.test', self.tmp / 'www-other') + ['--apply', '--no-reload']
        done = cli(args, self.env(config=other_config))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('--force', done.stderr)
        self.assertEqual(foreign.read_text(), original)
        self.assertFalse((self.tmp / 'www-other').exists())
        done = cli(args + ['--force'], self.env(config=other_config))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(foreign.read_text().splitlines()[0], MARKER)
        done = cli(['init', '--undo', 'other.test', '--apply', '--no-reload', '--root', self.tmp / 'www-other',
                    '--nginx-conf-dir', self.confd, '--nginx-bin', self.wrapper], self.env(config=other_config))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(foreign.exists())

    def test_07_init_again_reloads_nginx(self):
        before = self.vhost().read_bytes()
        done = cli(self.init_args() + ['--apply'], self.env())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('-s reload: ok', done.stdout)
        self.assertEqual(self.vhost().read_bytes(), before, 'a second run writes the same vhost')
        self.assertTrue(wait_for(self.serving))
        self.assertEqual(cli(['doctor'], self.env()).returncode, 0)

    def test_08_undo_keeps_files_then_purge_deletes_them(self):
        source = self.tmp / 'keep.txt'
        source.write_text('keep me\n')
        link = cli(['put', source], self.env()).stdout.strip()
        token = link.split('/')[-2]

        before = (tree(self.confd), tree(self.www), (self.tmp / 'config.json').read_bytes())
        done = cli(['init', '--undo', 'preview.test', '--nginx-bin', self.wrapper, '--nginx-conf-dir', self.confd], self.env())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('DRY RUN', done.stdout)
        self.assertEqual(before, (tree(self.confd), tree(self.www), (self.tmp / 'config.json').read_bytes()))

        done = cli(['init', '--undo', 'preview.test', '--apply', '--nginx-bin', self.wrapper], self.env())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse(self.vhost().exists())
        self.assertTrue((self.www / token / 'keep.txt').is_file(), 'published files stay without --purge')
        self.assertFalse((self.tmp / 'config.json').exists())
        self.assertTrue(wait_for(lambda: self.get('/robots.txt')[2] == b'default server\n'))

        done = cli(self.init_args() + ['--apply'], self.env())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(wait_for(self.serving))
        done = cli(['init', '--undo', 'preview.test', '--apply', '--purge', '--nginx-bin', self.wrapper], self.env())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse(self.www.exists(), 'purge removes the token folders and the empty root')
        self.assertFalse(self.vhost().exists())

    def test_09_doctor_fails_without_the_vhost(self):
        done = cli(['doctor'], self.env(PATH='/nonexistent', PREVIEW_LINK_EXTRA_PATH=''))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('FAIL  vhost file', done.stdout)
        self.assertIn('check(s) failed', done.stdout)

    def test_10_undo_refuses_a_file_without_the_marker(self):
        foreign = self.vhost('keepme.test')
        foreign.write_text('server { listen 80; }\n')
        done = cli(['init', '--undo', 'keepme.test', '--apply', '--no-reload', '--nginx-conf-dir', self.confd,
                    '--nginx-bin', self.wrapper], self.env())
        self.assertNotEqual(done.returncode, 0)
        self.assertTrue(foreign.exists())
        foreign.unlink()


if __name__ == '__main__':
    unittest.main(verbosity=2)
