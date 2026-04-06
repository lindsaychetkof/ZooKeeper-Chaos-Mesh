import time
from datetime import datetime
from kazoo.client import KazooClient
from kazoo.exceptions import KazooException

ZK_HOST = "127.0.0.1:2181"
INTERVAL = 0.5  # seconds between operations

zk = KazooClient(hosts=ZK_HOST)
zk.start()
zk.ensure_path("/test")

counter = 0
while True:
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    try:
        zk.set("/test", str(counter).encode())
        data, _ = zk.get("/test")
        print(f"[{timestamp}] OK  - wrote {counter}, read back {data.decode()}")
        counter += 1
    except KazooException as e:
        print(f"[{timestamp}] ERROR - {e}")
    except Exception as e:
        print(f"[{timestamp}] ERROR - {e}")
    time.sleep(INTERVAL)