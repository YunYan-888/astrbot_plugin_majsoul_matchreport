import struct

import betterproto

from .lib import lq as liblq
from .model import InflightRequest, MajsoulDecodedMessage, MajsoulLiqiProto


def _apply_renamed_fields(message, payload: dict) -> None:
    """补写 betterproto 无法从 dict 里识别的“被改名”字段。

    生成的 liqi.py 会把 `type` 这类与内置名冲突的字段存成 `type_`，而
    betterproto 的 from_dict 会先把键名归一化成 snake_case（`type_` -> `type`），
    随后在 metadata 里查不到 `type`，该字段就被静默丢弃、回落到默认值 0。
    雀魂新网关握手必须带 `type=1`，这里对被改名的标量字段做一次补写。
    """
    fields = getattr(message, "__dataclass_fields__", {})
    for key, value in payload.items():
        alias = f"{key}_"
        if key in fields or alias not in fields:
            continue
        if isinstance(value, (dict, list)):
            continue
        setattr(message, alias, value)


class MajsoulProtoCodec:
    NOTIFY = 1
    REQUEST = 2
    RESPONSE = 3

    def __init__(self, pb_def: MajsoulLiqiProto, version: str):
        self._pb = pb_def
        self.index = 1
        self._inflight_requests: dict[int, InflightRequest] = {}
        self.version = version

    def unwrap(self, wrapped: bytes):
        return liblq.Wrapper().parse(wrapped)

    def wrap(self, name: str, data: bytes):
        wrapper = liblq.Wrapper()
        wrapper.name = name
        wrapper.data = data
        return bytes(wrapper)

    @staticmethod
    def lookup_method(path: str) -> type[betterproto.Message]:
        return getattr(liblq, path)

    def decode_message(self, buf: bytes):
        type_byte = buf[0]

        if type_byte == self.NOTIFY:
            req_index = self.index
            msg = self.unwrap(buf[1:])
            method_name = msg.name
            _, _, notify = method_name.split(".")
            msg_obj = self.lookup_method(notify)
        elif type_byte == self.REQUEST:
            req_index = buf[1] | (buf[2] << 8)
            msg = self.unwrap(buf[3:])
            method_name = msg.name
            _, lq, service, rpc = method_name.split(".")
            proto_method = self._pb.nested[lq].nested[service].methods
            if proto_method is None or rpc not in proto_method:
                raise ValueError(f"Unknown method {rpc}")
            proto_domain = proto_method[rpc]
            msg_obj = self.lookup_method(proto_domain.requestType)
        elif type_byte == self.RESPONSE:
            req_index = buf[1] | (buf[2] << 8)
            msg = self.unwrap(buf[3:])
            inflight_req = self._inflight_requests.pop(req_index, None)
            if not inflight_req:
                raise ValueError(f"Unknown request {req_index}")
            msg_obj = inflight_req.msg_obj
            method_name = inflight_req.method_name
        else:
            raise ValueError(f"Invalid message type: {type_byte}")

        return MajsoulDecodedMessage(
            msg_type=type_byte,
            req_index=req_index,
            method_name=method_name,
            payload=msg_obj().parse(msg.data),
        )

    def encode_request(self, method_name: str, payload: dict):
        current_index = self.index
        self.index += 1

        _, lq, service, rpc = method_name.split(".")
        proto_method = self._pb.nested[lq].nested[service].methods
        if proto_method is None or rpc not in proto_method:
            raise ValueError(f"Unknown method {rpc}")
        proto_domain = proto_method[rpc]

        request_type = self.lookup_method(proto_domain.requestType)
        response_type = self.lookup_method(proto_domain.responseType)

        msg = request_type().from_dict(payload)
        _apply_renamed_fields(msg, payload)
        msg = self.wrap(method_name, msg.SerializeToString())

        self._inflight_requests[current_index] = InflightRequest(
            method_name=method_name,
            msg_obj=response_type,
        )

        return (
            struct.pack(
                "<BBB",
                self.REQUEST,
                current_index & 0xFF,
                current_index >> 8,
            )
            + msg
        )
