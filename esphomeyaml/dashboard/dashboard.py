# pylint: disable=wrong-import-position
from __future__ import print_function

import codecs
import collections
import hmac
import json
import logging
import multiprocessing
import os
import subprocess
import threading

import tornado
import tornado.concurrent
import tornado.httpserver
import tornado.netutil
import tornado.gen
import tornado.ioloop
import tornado.iostream
from tornado.log import access_log
import tornado.process
import tornado.web
import tornado.websocket

from esphomeyaml import const
from esphomeyaml.__main__ import get_serial_ports
from esphomeyaml.helpers import mkdir_p, run_system_command
from esphomeyaml.storage_json import EsphomeyamlStorageJSON, StorageJSON, \
    esphomeyaml_storage_path, ext_storage_path
from esphomeyaml.util import shlex_quote

# pylint: disable=unused-import, wrong-import-order
from typing import Optional  # noqa

_LOGGER = logging.getLogger(__name__)
CONFIG_DIR = ''
PASSWORD_DIGEST = ''
COOKIE_SECRET = None
USING_PASSWORD = False
ON_HASSIO = False
USING_HASSIO_AUTH = True
HASSIO_MQTT_CONFIG = None


# pylint: disable=abstract-method
class BaseHandler(tornado.web.RequestHandler):
    def is_authenticated(self):
        if USING_HASSIO_AUTH or USING_PASSWORD:
            return self.get_secure_cookie('authenticated') == 'yes'

        return True


# pylint: disable=abstract-method, arguments-differ
class EsphomeyamlCommandWebSocket(tornado.websocket.WebSocketHandler):
    def __init__(self, application, request, **kwargs):
        super(EsphomeyamlCommandWebSocket, self).__init__(application, request, **kwargs)
        self.proc = None
        self.closed = False

    def on_message(self, message):
        if USING_HASSIO_AUTH or USING_PASSWORD:
            if self.get_secure_cookie('authenticated') != 'yes':
                return
        if self.proc is not None:
            return
        command = self.build_command(message)
        _LOGGER.info(u"Running command '%s'", ' '.join(shlex_quote(x) for x in command))
        self.proc = tornado.process.Subprocess(command,
                                               stdout=tornado.process.Subprocess.STREAM,
                                               stderr=subprocess.STDOUT)
        self.proc.set_exit_callback(self.proc_on_exit)
        tornado.ioloop.IOLoop.current().spawn_callback(self.redirect_stream)

    @tornado.gen.coroutine
    def redirect_stream(self):
        while True:
            try:
                data = yield self.proc.stdout.read_until_regex('[\n\r]')
            except tornado.iostream.StreamClosedError:
                break
            try:
                self.write_message({'event': 'line', 'data': data})
            except UnicodeDecodeError:
                data = codecs.decode(data, 'utf8', 'replace')
                self.write_message({'event': 'line', 'data': data})

    def proc_on_exit(self, returncode):
        if not self.closed:
            _LOGGER.debug("Process exited with return code %s", returncode)
            self.write_message({'event': 'exit', 'code': returncode})

    def on_close(self):
        self.closed = True
        if self.proc is not None and self.proc.returncode is None:
            _LOGGER.debug("Terminating process")
            self.proc.proc.terminate()

    def build_command(self, message):
        raise NotImplementedError


class EsphomeyamlLogsHandler(EsphomeyamlCommandWebSocket):
    def build_command(self, message):
        js = json.loads(message)
        config_file = CONFIG_DIR + '/' + js['configuration']
        return ["esphomeyaml", "--dashboard", config_file, "logs", '--serial-port', js["port"]]


class EsphomeyamlRunHandler(EsphomeyamlCommandWebSocket):
    def build_command(self, message):
        js = json.loads(message)
        config_file = os.path.join(CONFIG_DIR, js['configuration'])
        return ["esphomeyaml", "--dashboard", config_file, "run", '--upload-port', js["port"]]


class EsphomeyamlCompileHandler(EsphomeyamlCommandWebSocket):
    def build_command(self, message):
        js = json.loads(message)
        config_file = os.path.join(CONFIG_DIR, js['configuration'])
        return ["esphomeyaml", "--dashboard", config_file, "compile"]


class EsphomeyamlValidateHandler(EsphomeyamlCommandWebSocket):
    def build_command(self, message):
        js = json.loads(message)
        config_file = os.path.join(CONFIG_DIR, js['configuration'])
        return ["esphomeyaml", "--dashboard", config_file, "config"]


class EsphomeyamlCleanMqttHandler(EsphomeyamlCommandWebSocket):
    def build_command(self, message):
        js = json.loads(message)
        config_file = os.path.join(CONFIG_DIR, js['configuration'])
        return ["esphomeyaml", "--dashboard", config_file, "clean-mqtt"]


class EsphomeyamlCleanHandler(EsphomeyamlCommandWebSocket):
    def build_command(self, message):
        js = json.loads(message)
        config_file = os.path.join(CONFIG_DIR, js['configuration'])
        return ["esphomeyaml", "--dashboard", config_file, "clean"]


class EsphomeyamlHassConfigHandler(EsphomeyamlCommandWebSocket):
    def build_command(self, message):
        js = json.loads(message)
        config_file = os.path.join(CONFIG_DIR, js['configuration'])
        return ["esphomeyaml", "--dashboard", config_file, "hass-config"]


class SerialPortRequestHandler(BaseHandler):
    def get(self):
        if not self.is_authenticated():
            self.redirect('/login')
            return
        ports = get_serial_ports()
        data = []
        for port, desc in ports:
            if port == '/dev/ttyAMA0':
                desc = 'UART pins on GPIO header'
            split_desc = desc.split(' - ')
            if len(split_desc) == 2 and split_desc[0] == split_desc[1]:
                # Some serial ports repeat their values
                desc = split_desc[0]
            data.append({'port': port, 'desc': desc})
        data.append({'port': 'OTA', 'desc': 'Over-The-Air'})
        self.write(json.dumps(sorted(data, reverse=True)))


class WizardRequestHandler(BaseHandler):
    def post(self):
        from esphomeyaml import wizard

        if not self.is_authenticated():
            self.redirect('/login')
            return
        kwargs = {k: ''.join(v) for k, v in self.request.arguments.items()}
        destination = os.path.join(CONFIG_DIR, kwargs['name'] + '.yaml')
        wizard.wizard_write(path=destination, **kwargs)
        self.redirect('/?begin=True')


class DownloadBinaryRequestHandler(BaseHandler):
    def get(self):
        if not self.is_authenticated():
            self.redirect('/login')
            return

        configuration = self.get_argument('configuration')
        storage_path = ext_storage_path(CONFIG_DIR, configuration)
        storage_json = StorageJSON.load(storage_path)
        if storage_json is None:
            self.send_error()
            return

        path = storage_json.firmware_bin_path
        self.set_header('Content-Type', 'application/octet-stream')
        filename = '{}.bin'.format(storage_json.name)
        self.set_header("Content-Disposition", 'attachment; filename="{}"'.format(filename))
        with open(path, 'rb') as f:
            while 1:
                data = f.read(16384)  # or some other nice-sized chunk
                if not data:
                    break
                self.write(data)
        self.finish()


def _list_yaml_files():
    files = []
    for file in os.listdir(CONFIG_DIR):
        if not file.endswith('.yaml'):
            continue
        if file.startswith('.'):
            continue
        if file == 'secrets.yaml':
            continue
        files.append(file)
    files.sort()
    return files


def _list_dashboard_entries():
    files = _list_yaml_files()
    return [DashboardEntry(file) for file in files]


class DashboardEntry(object):
    def __init__(self, filename):
        self.filename = filename
        self._storage = None
        self._loaded_storage = False

    @property
    def full_path(self):  # type: () -> str
        return os.path.join(CONFIG_DIR, self.filename)

    @property
    def storage(self):  # type: () -> Optional[StorageJSON]
        if not self._loaded_storage:
            self._storage = StorageJSON.load(ext_storage_path(CONFIG_DIR, self.filename))
            self._loaded_storage = True
        return self._storage

    @property
    def address(self):
        if self.storage is None:
            return None
        return self.storage.address

    @property
    def name(self):
        if self.storage is None:
            return self.filename[:-len('.yaml')]
        return self.storage.name

    @property
    def esp_platform(self):
        if self.storage is None:
            return None
        return self.storage.esp_platform

    @property
    def board(self):
        if self.storage is None:
            return None
        return self.storage.board

    @property
    def update_available(self):
        if self.storage is None:
            return True
        return self.update_old != self.update_new

    @property
    def update_old(self):
        if self.storage is None:
            return ''
        return self.storage.esphomeyaml_version or ''

    @property
    def update_new(self):
        return const.__version__


class MainRequestHandler(BaseHandler):
    def get(self):
        if not self.is_authenticated():
            self.redirect('/login')
            return

        begin = bool(self.get_argument('begin', False))
        entries = _list_dashboard_entries()
        version = const.__version__
        docs_link = 'https://beta.esphomelib.com/esphomeyaml/' if 'b' in version else \
            'https://esphomelib.com/esphomeyaml/'

        self.render("templates/index.html", entries=entries,
                    version=version, begin=begin, docs_link=docs_link,
                    get_static_file_url=get_static_file_url)


def _ping_func(filename, address):
    if os.name == 'nt':
        command = ['ping', '-n', '1', address]
    else:
        command = ['ping', '-c', '1', address]
    rc, _, _ = run_system_command(*command)
    return filename, rc == 0


class PingThread(threading.Thread):
    def run(self):
        pool = multiprocessing.Pool(processes=8)

        while not STOP_EVENT.is_set():
            # Only do pings if somebody has the dashboard open
            PING_REQUEST.wait()
            PING_REQUEST.clear()

            def callback(ret):
                PING_RESULT[ret[0]] = ret[1]

            entries = _list_dashboard_entries()
            queue = collections.deque()
            for entry in entries:
                if entry.address is None:
                    PING_RESULT[entry.filename] = None
                    continue

                result = pool.apply_async(_ping_func, (entry.filename, entry.address),
                                          callback=callback)
                queue.append(result)

            while queue:
                item = queue[0]
                if item.ready():
                    queue.popleft()
                    continue

                try:
                    item.get(0.1)
                except OSError:
                    # ping not installed
                    pass
                except multiprocessing.TimeoutError:
                    pass

                if STOP_EVENT.is_set():
                    pool.terminate()
                    return


class PingRequestHandler(BaseHandler):
    def get(self):
        if not self.is_authenticated():
            self.redirect('/login')
            return

        PING_REQUEST.set()
        self.write(json.dumps(PING_RESULT))


def is_allowed(configuration):
    return os.path.sep not in configuration


class EditRequestHandler(BaseHandler):
    def get(self):
        if not self.is_authenticated():
            self.redirect('/login')
            return
        configuration = self.get_argument('configuration')
        if not is_allowed(configuration):
            self.set_status(401)
            return

        with open(os.path.join(CONFIG_DIR, configuration), 'r') as f:
            content = f.read()
        self.write(content)

    def post(self):
        if not self.is_authenticated():
            self.redirect('/login')
            return
        configuration = self.get_argument('configuration')
        if not is_allowed(configuration):
            self.set_status(401)
            return

        with open(os.path.join(CONFIG_DIR, configuration), 'w') as f:
            f.write(self.request.body)
        self.set_status(200)
        return


PING_RESULT = {}  # type: dict
STOP_EVENT = threading.Event()
PING_REQUEST = threading.Event()


class LoginHandler(BaseHandler):
    def get(self):
        if USING_HASSIO_AUTH:
            self.render_hassio_login()
            return
        self.write('<html><body><form action="/login" method="post">'
                   'Password: <input type="password" name="password">'
                   '<input type="submit" value="Sign in">'
                   '</form></body></html>')

    def render_hassio_login(self, error=None):
        version = const.__version__
        docs_link = 'https://beta.esphomelib.com/esphomeyaml/' if 'b' in version else \
            'https://esphomelib.com/esphomeyaml/'

        self.render("templates/login.html", version=version, docs_link=docs_link, error=error,
                    get_static_file_url=get_static_file_url)

    def post_hassio_login(self):
        import requests

        headers = {
            'X-HASSIO-KEY': os.getenv('HASSIO_TOKEN'),
        }
        data = {
            'username': str(self.get_argument('username', '')),
            'password': str(self.get_argument('password', ''))
        }
        try:
            req = requests.post('http://hassio/auth', headers=headers, data=data)
            if req.status_code == 200:
                self.set_secure_cookie("authenticated", "yes")
                self.redirect('/')
                return
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("Error during Hass.io auth request: %s", err)
            self.set_status(500)
            self.render_hassio_login(error="Internal server error")
            return
        self.set_status(401)
        self.render_hassio_login(error="Invalid username or password")

    def post(self):
        if USING_HASSIO_AUTH:
            self.post_hassio_login()
            return

        password = str(self.get_argument("password", ''))
        password = hmac.new(password).digest()
        if hmac.compare_digest(PASSWORD_DIGEST, password):
            self.set_secure_cookie("authenticated", "yes")
        self.redirect("/")


_STATIC_FILE_HASHES = {}


def get_static_file_url(name):
    static_path = os.path.join(os.path.dirname(__file__), 'static')
    if name in _STATIC_FILE_HASHES:
        hash_ = _STATIC_FILE_HASHES[name]
    else:
        path = os.path.join(static_path, name)
        with open(path, 'rb') as f_handle:
            hash_ = hash(f_handle.read())
        _STATIC_FILE_HASHES[name] = hash_
    return u'/static/{}?hash={}'.format(name, hash_)


def make_app(debug=False):
    def log_function(handler):
        if handler.get_status() < 400:
            log_method = access_log.info

            if isinstance(handler, SerialPortRequestHandler) and not debug:
                return
            if isinstance(handler, PingRequestHandler) and not debug:
                return
        elif handler.get_status() < 500:
            log_method = access_log.warning
        else:
            log_method = access_log.error

        request_time = 1000.0 * handler.request.request_time()
        # pylint: disable=protected-access
        log_method("%d %s %.2fms", handler.get_status(),
                   handler._request_summary(), request_time)

    class StaticFileHandler(tornado.web.StaticFileHandler):
        def set_extra_headers(self, path):
            if debug:
                self.set_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')

    static_path = os.path.join(os.path.dirname(__file__), 'static')
    app = tornado.web.Application([
        (r"/", MainRequestHandler),
        (r"/login", LoginHandler),
        (r"/logs", EsphomeyamlLogsHandler),
        (r"/run", EsphomeyamlRunHandler),
        (r"/compile", EsphomeyamlCompileHandler),
        (r"/validate", EsphomeyamlValidateHandler),
        (r"/clean-mqtt", EsphomeyamlCleanMqttHandler),
        (r"/clean", EsphomeyamlCleanHandler),
        (r"/hass-config", EsphomeyamlHassConfigHandler),
        (r"/edit", EditRequestHandler),
        (r"/download.bin", DownloadBinaryRequestHandler),
        (r"/serial-ports", SerialPortRequestHandler),
        (r"/ping", PingRequestHandler),
        (r"/wizard.html", WizardRequestHandler),
        (r'/static/(.*)', StaticFileHandler, {'path': static_path}),
    ], debug=debug, cookie_secret=COOKIE_SECRET, log_function=log_function)

    if debug:
        _STATIC_FILE_HASHES.clear()

    return app


def start_web_server(args):
    global CONFIG_DIR
    global PASSWORD_DIGEST
    global USING_PASSWORD
    global ON_HASSIO
    global USING_HASSIO_AUTH
    global COOKIE_SECRET

    CONFIG_DIR = args.configuration
    mkdir_p(CONFIG_DIR)
    mkdir_p(os.path.join(CONFIG_DIR, ".esphomeyaml"))

    ON_HASSIO = args.hassio
    if ON_HASSIO:
        USING_HASSIO_AUTH = not bool(os.getenv('DISABLE_HA_AUTHENTICATION'))
        USING_PASSWORD = False
    else:
        USING_HASSIO_AUTH = False
        USING_PASSWORD = args.password

    if USING_PASSWORD:
        PASSWORD_DIGEST = hmac.new(args.password).digest()

    if USING_HASSIO_AUTH or USING_PASSWORD:
        path = esphomeyaml_storage_path(CONFIG_DIR)
        storage = EsphomeyamlStorageJSON.load(path)
        if storage is None:
            storage = EsphomeyamlStorageJSON.get_default()
            storage.save(path)
        COOKIE_SECRET = storage.cookie_secret

    app = make_app(args.verbose)
    if args.socket is not None:
        _LOGGER.info("Starting dashboard web server on unix socket %s and configuration dir %s...",
                     args.socket, CONFIG_DIR)
        server = tornado.httpserver.HTTPServer(app)
        socket = tornado.netutil.bind_unix_socket(args.socket, mode=0o666)
        server.add_socket(socket)
    else:
        _LOGGER.info("Starting dashboard web server on port %s and configuration dir %s...",
                     args.port, CONFIG_DIR)
        app.listen(args.port)

        if args.open_ui:
            import webbrowser

            webbrowser.open('localhost:{}'.format(args.port))

    ping_thread = PingThread()
    ping_thread.start()
    try:
        tornado.ioloop.IOLoop.current().start()
    except KeyboardInterrupt:
        _LOGGER.info("Shutting down...")
        STOP_EVENT.set()
        PING_REQUEST.set()
        ping_thread.join()
        if args.socket is not None:
            os.remove(args.socket)
