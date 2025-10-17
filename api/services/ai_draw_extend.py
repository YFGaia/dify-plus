import json
import threading
import time

from extensions.ext_database import db
from models.ai_draw_extnd import ForwardingExtend

# Create a shared dictionary
FORWARDING = {}
# Create a lock object
dict_lock = threading.Lock()


def thread_forwarding_write(key, value: ForwardingExtend):
    global dict_lock, FORWARDING
    with dict_lock:
        FORWARDING[key] = [
            json.dumps(
                {
                    "id": value.id,
                    "path": value.path,
                    "header": value.header,
                    "address": value.address,
                    "description": value.description,
                }
            ),
            int(time.time()),
        ]


def thread_forwarding_read(key) -> ForwardingExtend | None:
    global FORWARDING
    # prevent error: is not bound to a Session; attribute refresh operation cannot proceed
    info = FORWARDING.get(key)
    if info is not None and info[1] < int(time.time()) + 600:
        if info[0] is not None:
            try:
                forwarding_dict_back = json.loads(info[0])
                return ForwardingExtend(
                    id=forwarding_dict_back["id"],
                    path=forwarding_dict_back["path"],
                    header=forwarding_dict_back["header"],
                    address=forwarding_dict_back["address"],
                    description=forwarding_dict_back["description"],
                )
            except Exception as e:
                pass
        else:
            return None
    forwarding: ForwardingExtend = db.session.query(ForwardingExtend).filter(ForwardingExtend.path == key).first()
    # save
    if forwarding is not None:
        thread_forwarding_write(key, forwarding)
    else:
        FORWARDING[key] = [None, int(time.time())]
    return forwarding


class AiDrawForwarding:
    @classmethod
    def get_forwarding(cls, path: str) -> ForwardingExtend:
        """
        AI draws forwarding, obtains forwarding domain name
        :param path: str
        """
        info = thread_forwarding_read(path)
        if info is not None:
            return info
        info: ForwardingExtend = db.session.query(ForwardingExtend).filter(ForwardingExtend.path == path).first()
        # save
        thread_forwarding_write(path, info)
        return info

    @classmethod
    def get_all_forwarding(cls):
        address = {}
        for i in db.session.query(ForwardingExtend).all():
            # 1. 替换 https://  http:// :8000
            url = i.address.replace('https://', '', 1).replace('http://', '', 1).replace(':8000', '', 1)
            # 2. 移除末尾的/（如果有）
            url = url.rstrip('/')
            address[url] = i.path
        return address


