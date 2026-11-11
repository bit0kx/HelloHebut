"""Own a dedicated ephemeral Redis process; never connect to or flush a user's Redis."""
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import redis


class RedisProcess:
    def __init__(self):
        executable = shutil.which('redis-server')
        if not executable:
            raise unittest.SkipTest('redis-server executable unavailable')
        root = Path(__file__).resolve().parents[1] / 'tmp'
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()
        self.directory = tempfile.TemporaryDirectory(dir=root)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.url = f'redis://127.0.0.1:{self.port}/0'
        self.process = subprocess.Popen([executable, '--bind', '127.0.0.1', '--port', str(self.port),
                                         '--save', '', '--appendonly', 'no', '--dir', self.directory.name],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        client = redis.from_url(self.url, socket_connect_timeout=.2)
        try:
            for _ in range(100):
                try:
                    if client.ping():
                        return
                except redis.RedisError:
                    time.sleep(.05)
            self.close()
            raise RuntimeError('isolated redis failed to start')
        finally:
            client.close()

    def close(self):
        self.process.terminate()
        self.process.wait(timeout=10)
        if self.root not in Path(self.directory.name).resolve().parents:
            raise RuntimeError('test temp path escaped artifact directory')
        self.directory.cleanup()
