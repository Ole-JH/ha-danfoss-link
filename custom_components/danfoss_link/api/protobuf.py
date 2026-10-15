"""Just enough protobuf wire format to speak the MDG control and routing protocols.

The .proto files are not public, so messages are built and parsed by field number.
"""

from __future__ import annotations

import struct

WT_VARINT = 0
WT_FIXED64 = 1
WT_BYTES = 2
WT_FIXED32 = 5


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("negative varint")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    shift = 0
    result = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def varint_field(field: int, value: int) -> bytes:
    return encode_varint(field << 3 | WT_VARINT) + encode_varint(value)


def fixed32_field(field: int, value: int) -> bytes:
    return encode_varint(field << 3 | WT_FIXED32) + struct.pack("<I", value)


def bytes_field(field: int, value: bytes | str) -> bytes:
    if isinstance(value, str):
        value = value.encode()
    return encode_varint(field << 3 | WT_BYTES) + encode_varint(len(value)) + value


class Message:
    """A decoded protobuf message: field number -> list of raw values."""

    def __init__(self, data: bytes) -> None:
        self.fields: dict[int, list[int | bytes]] = {}
        pos = 0
        while pos < len(data):
            key, pos = _decode_varint(data, pos)
            field, wire_type = key >> 3, key & 7
            value: int | bytes
            if wire_type == WT_VARINT:
                value, pos = _decode_varint(data, pos)
            elif wire_type == WT_FIXED64:
                value = struct.unpack_from("<Q", data, pos)[0]
                pos += 8
            elif wire_type == WT_BYTES:
                length, pos = _decode_varint(data, pos)
                if pos + length > len(data):
                    raise ValueError("truncated length-delimited field")
                value = data[pos : pos + length]
                pos += length
            elif wire_type == WT_FIXED32:
                value = struct.unpack_from("<I", data, pos)[0]
                pos += 4
            else:
                raise ValueError(f"unsupported wire type {wire_type}")
            self.fields.setdefault(field, []).append(value)

    def int(self, field: int, default: int | None = None) -> int | None:
        values = self.fields.get(field)
        if not values or not isinstance(values[-1], int):
            return default
        return values[-1]

    def bytes(self, field: int) -> bytes | None:
        values = self.fields.get(field)
        if not values or not isinstance(values[-1], bytes):
            return None
        return values[-1]

    def str(self, field: int) -> str | None:
        value = self.bytes(field)
        return None if value is None else value.decode("utf-8", "replace")

    def message(self, field: int) -> Message | None:
        value = self.bytes(field)
        return None if value is None else Message(value)

    def messages(self, field: int) -> list[Message]:
        return [Message(v) for v in self.fields.get(field, []) if isinstance(v, bytes)]
