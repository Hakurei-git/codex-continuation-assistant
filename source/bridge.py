import ctypes
from ctypes import wintypes
import json
import msvcrt
import os
import pathlib
import struct
import time
import uuid



class Disconnected(Exception):
    pass


class Bridge:
    def __init__(self, preferred=''):
        self.pipe = preferred or os.environ.get('CODEX_APP_TOOLS_PIPE_PATH', '')
        self.caller = None

    def request(self, path, method, params, timeout=15):
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.PeekNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k.PeekNamedPipe.restype = wintypes.BOOL
        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}, ensure_ascii=False).encode()
        with open(path, 'r+b', buffering=0) as pipe:
            pipe.write(struct.pack('<I', len(body)) + body)
            data = bytearray()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                available = wintypes.DWORD()
                if not k.PeekNamedPipe(msvcrt.get_osfhandle(pipe.fileno()), None, 0, None, ctypes.byref(available), None):
                    raise Disconnected('Codex 本地连接已断开')
                if available.value:
                    data.extend(pipe.read(min(available.value, 65536)))
                    if len(data) >= 4:
                        size = struct.unpack('<I', data[:4])[0]
                        if size > 8 * 1024 * 1024:
                            raise Disconnected('接口返回超出允许大小')
                        if len(data) >= 4 + size:
                            value = json.loads(data[4:4+size])
                            if 'error' in value:
                                raise Disconnected(value['error'].get('message', '接口错误'))
                            return value['result']
                time.sleep(.04)
            raise Disconnected('本地接口超时')

    def connect(self):
        candidates = [self.pipe] if self.pipe else []
        try:
            candidates += ['\\\\.\\pipe\\' + n for n in os.listdir('\\\\.\\pipe\\')
                           if n.startswith('codex-browser-use-')]
        except OSError:
            pass
        valid = []
        for path in dict.fromkeys(candidates):
            try:
                result = self.request(path, 'tools/list', {'threadStartKind': 'all'}, timeout=2)
                names = {t['name'] for t in result.get('tools', [])}
                if {'read_thread', 'wait_threads', 'send_message_to_thread', 'list_threads'} <= names:
                    valid.append(path)
                    if path == self.pipe:
                        return
            except (OSError, Disconnected, ValueError):
                continue
        if len(valid) != 1:
            raise Disconnected('未找到唯一可用的 Codex 连接，请打开 Codex；不会自动启动或连接其他主机')
        self.pipe = valid[0]

    def call(self, tool, arguments):
        params = {'arguments': arguments, 'callerSource': 'codex',
            'callId': 'continuation-' + uuid.uuid4().hex, 'namespace': 'codex_app',
            'tool': tool, 'turnId': 'continuation-monitor'}
        caller = self.caller or arguments.get('threadId')
        if caller:
            params['threadId'] = caller
        response = self.request(self.pipe, 'tools/call', params)
        if not response.get('success'):
            raise Disconnected('Codex 未接受此次操作；不会绕过应用审批或权限')
        texts = [i['text'] for i in response.get('contentItems', []) if i.get('type') == 'inputText']
        if not texts:
            raise Disconnected('接口没有返回可读结果')
        return json.loads(texts[0])
