import json
import logging
import os
import re
import subprocess
import threading
import time
from threading import Thread

from flask import Flask, render_template, request
from flask_socketio import SocketIO
import eventlet

eventlet.monkey_patch()

# 从环境变量中读取日志级别
log_level = os.environ.get('NTWA_LOG_LEVEL', 'INFO').upper()

# 验证获取的日志级别是否有效
valid_log_levels = ['CRITICAL', 'ERROR', 'WARNING', 'INFO', 'DEBUG', 'NOTSET']
if log_level not in valid_log_levels:
    log_level = 'INFO'  # 设置默认值为 INFO 如果环境变量中的值无效

# 使用获取到的日志级别配置日志
logging.basicConfig(level=log_level, format='%(asctime)s %(levelname)s %(message)s')

app = Flask(__name__, static_folder='assets')
app.config['SECRET_KEY'] = 'secret'
socketio = SocketIO(app)
nexttrace_path = '/usr/local/bin/nexttrace'
time_limit = 10

# 存储每个客户端的进程
clients = {}
client_last_active = {}


def check_timeouts():
    while True:
        for sid, last_active in list(client_last_active.items()):
            if time.time() - last_active > time_limit:
                logging.debug(f"Client {sid} timed out")
                stop_nexttrace_for_sid(sid)
                del client_last_active[sid]
        time.sleep(1)


def stop_nexttrace_for_sid(sid):
    task = clients.get(sid)
    if task and task.process:
        task.process.terminate()
        socketio.emit('nexttrace_complete', room=sid)
        if sid in clients:
            del clients[sid]
            logging.debug(f"Client {sid} removed from clients dictionary after process termination")


Thread(target=check_timeouts, daemon=True).start()


class OutputMonitor:
    def __init__(self, process, socketio, sid, options):
        self.process = process
        self.last_output_time = time.time()
        self.lock = threading.Lock()
        self.socketio = socketio
        self.sid = sid
        self.options = options

    def monitor(self, line):
        with self.lock:
            self.last_output_time = time.time()

    def start_newline_inserter(self, timeout):
        def insert_newline():
            while True:
                time.sleep(1)
                with self.lock:
                    if time.time() - self.last_output_time > timeout:
                        if len(self.options) > 0:
                            logging.debug(f"in start_newline_inserter: {self.options}")
                            self.socketio.emit('nexttrace_options', self.options, room=self.sid)
                            self.options = []
                            break

        t = Thread(target=insert_newline, daemon=True)
        t.start()


class NextTraceTask:
    def __init__(self, sid, _socketio, params, _nexttrace_path):
        self.sid = sid
        self.socketio = _socketio
        self.params = params
        self.nexttrace_path = _nexttrace_path
        self.process = None

    def run(self):
        fixParam = '--map --raw -q 1 --send-time 1'  # -d disable-geoip
        process_env = os.environ.copy()
        process_env['NEXTTRACE_UNINTERRUPTED'] = '1'

        # DNS options
        options = []

        pattern = re.compile(r'[&;<>\"\'()|\[\]{}$#!%*+=]')
        if pattern.search(self.params):
            self.socketio.emit('nexttrace_output', 'Invalid params', room=self.sid)
            self.socketio.emit('nexttrace_complete', room=self.sid)
            raise ValueError('Invalid params')
        logging.debug(f"cmd: {[self.nexttrace_path] + self.params.split() + fixParam.split()}")
        self.process = subprocess.Popen(
            [self.nexttrace_path] + self.params.split() + fixParam.split(),
            stdout=subprocess.PIPE, stdin=subprocess.PIPE, universal_newlines=True, env=process_env, bufsize=1
        )
        output_monitor = OutputMonitor(self.process, self.socketio, self.sid, options)
        output_monitor_flag = True

        for line in iter(self.process.stdout.readline, ''):
            logging.debug(f"line: {line}")
            if re.match(r'^\d+\..*$', line):
                options.append(line.split()[1])
                if output_monitor_flag:
                    output_monitor.start_newline_inserter(timeout=0.1)  # 0.1 seconds
                    output_monitor_flag = False
            elif re.match(r'^\d+\|', line):
                line_split = line.split('|')
                res = line_split[0:5] + [''.join(line_split[5:9])] + line_split[9:10]
                if '||||||' in line:
                    res = line_split[0:1] + ['', '', '', '', '', '']
                logging.debug(f"{res}")
                res_str = json.dumps(obj=res, ensure_ascii=False)
                logging.debug(f"nexttrace_output: {res_str}")
                self.socketio.emit('nexttrace_output', res_str, room=self.sid)
                client_last_active[self.sid] = time.time()  # 更新客户端的最后活跃时间

            if self.process.poll() is not None:
                self.socketio.emit('nexttrace_complete', room=self.sid)
                break

    def process_input(self, data):
        if self.process:
            self.process.stdin.write(data)
            self.process.stdin.flush()
        else:
            logging.warning('want to input but Process not started')


@socketio.on('connect')
def handle_connect():
    logging.info(f'Client {request.sid} connected')
    client_last_active[request.sid] = time.time()


@socketio.on('disconnect')
def handle_disconnect():
    logging.info(f'Client {request.sid} disconnected')
    stop_nexttrace_for_sid(request.sid)


@socketio.on('start_nexttrace')
def start_nexttrace(data):
    try:
        # 尝试将数据解析为JSON
        if isinstance(data, str):
            data = json.loads(data)

        # 确保数据是一个字典且包含 'ip' 键
        if isinstance(data, dict) and 'ip' in data:
            logging.info(f"Client {request.sid} start nexttrace, params: {data}")
            params = data['ip']
            if params:
                dst = params.strip()
                pattern0 = re.compile(r'^[a-fA-F0-9:]+$')
                pattern1 = re.compile(r'^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$')
                pattern2 = re.compile(
                    r'^(?=^.{3,255}$)[a-zA-Z0-9][-a-zA-Z0-9]{0,62}(\.[a-zA-Z0-9][-a-zA-Z0-9]{0,62})+$')
                if not (pattern0.match(dst) or pattern1.match(dst) or pattern2.match(dst)) or len(dst) > 127:
                    logging.warning(f"Invalid dst: {params}")
                    return
            data = data.get('extra')
            if isinstance(data, str):
                data = json.loads(data)
            # 从 JSON 中提取其他参数
            ipVersion = data.get('ipVersion')
            if ipVersion == 'ipv4':
                params += ' --ipv4'
            elif ipVersion == 'ipv6':
                params += ' --ipv6'
            protocol = data.get('protocol')
            if protocol == 'tcp':
                params += ' --tcp'
            elif protocol == 'udp':
                params += ' --udp'
            language = data.get('language')
            if language == 'en':
                params += ' --language en'
            intervalSeconds = data.get('intervalSeconds')
            if intervalSeconds:
                params += f' --ttl-time {int(float(intervalSeconds) * 1000)}'
            packetSize = data.get('packetSize')
            if packetSize:
                params += f' --psize {int(packetSize)}'
            maxHop = data.get('maxHop')
            if maxHop:
                params += f' --max-hops {int(maxHop)}'
            minHop = data.get('minHop')
            if minHop:
                params += f' --first {int(minHop)}'
            port = data.get('port')
            if port:
                params += f' --port {int(port)}'
            device = data.get('device')
            if device:
                device = device.strip()
                pattern = re.compile(r'^[a-zA-Z]*\d*$')
                if pattern.match(device) and len(device) < 128:
                    params += f' --dev {device}'
            dataProvider = data.get('dataProvider')
            if dataProvider and len(dataProvider) < 16:
                dataProvider = dataProvider.strip()
                allowedList = [
                    "Ip2region", "ip2region", "IP.SB", "ip.sb", "IPInfo", "ipinfo",
                    "IPInsight", "ipinsight", "IPAPI.com", "ip-api.com", "IPInfoLocal",
                    "ipinfolocal", "chunzhen", "LeoMoeAPI", "leomoeapi", "disable-geoip"
                ]
                if dataProvider in allowedList:
                    params += f' --data-provider {dataProvider}'

            # 创建任务
            task = NextTraceTask(request.sid, socketio, params, nexttrace_path)
            clients[request.sid] = task
            # 更新客户端的最后活跃时间
            client_last_active[request.sid] = time.time()
            # 启动线程
            thread = Thread(target=task.run)
            try:
                thread.start()
            except ValueError:
                logging.warning(f"Invalid params: {params}")
        else:
            logging.warning(f"Invalid data format received: {data}")

    except json.JSONDecodeError:
        logging.warning(f"Received data is not valid JSON: {data}")


@socketio.on('stop_nexttrace')
def stop_nexttrace():
    stop_nexttrace_for_sid(request.sid)


@socketio.on('nexttrace_options_choice')
def nexttrace_options_choice(data):
    try:
        if isinstance(data, str):
            data = json.loads(data)
        if isinstance(data, dict) and 'choice' in data:
            choice = data['choice']
            if isinstance(choice, int):
                logging.info(f"Client {request.sid} choose option {choice}")
                choice_str = f"{choice}\n"  # Convert choice to string and append newline character
                task = clients.get(request.sid)
                if task:
                    logging.debug(f"Client {request.sid} send choice {choice_str}")
                    task.process_input(choice_str)
                else:
                    logging.debug(f"Client want to send choice {choice_str}, but {request.sid} not found")
            else:
                logging.warning(f"Invalid choice format: {choice}")
        else:
            logging.warning(f"Invalid data format received: {data}")
    except json.JSONDecodeError:
        logging.warning(f"Received data is not valid JSON: {data}")


@app.route('/')
def index():
    return render_template('index.html'), 200


if __name__ == '__main__':
    # 从环境变量中读取主机和端口，如果环境变量不存在，使用默认值'127.0.0.1'和35000
    host = os.environ.get('TEST_HOST', '127.0.0.1')
    _port = int(os.environ.get('TEST_PORT', 35000))

    # 使用从环境变量中读取的主机和端口运行应用
    socketio.run(app, host, _port)
