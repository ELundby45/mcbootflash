import enum
import struct
from dataclasses import asdict, dataclass
from typing import ClassVar, Type, TypeVar

from serial import Serial


# Is this key always the same? Perhaps generated by MCC during code generation?
# If this key is incorrect, flash write operations will fail silently.
FLASH_UNLOCK_KEY = 0x00AA0055


class BootCommand(enum.IntEnum):
    """The MCC 16-bit bootloader supports these commands."""

    READ_VERSION = 0x00
    READ_FLASH = 0x01
    WRITE_FLASH = 0x02
    ERASE_FLASH = 0x03
    CALC_CHECKSUM = 0x08
    RESET_DEVICE = 0x09
    SELF_VERIFY = 0x0A
    GET_MEMORY_ADDRESS_RANGE = 0x0B


class BootResponseCode(enum.IntEnum):
    """Sent by the bootloader in response to a command."""

    UNDEFINED = 0x00
    SUCCESS = 0x01
    UNSUPPORTED_COMMAND = 0xFF
    BAD_ADDRESS = 0xFE
    BAD_LENGTH = 0xFD
    VERIFY_FAIL = 0xFC


_P = TypeVar("_P", bound="Packet")


@dataclass
class Packet:
    """Base class for communication packets to and from the bootloader."""

    command: BootCommand
    data_length: int = 0
    unlock_sequence: int = 0
    address: int = 0
    format: ClassVar = "=BH2I"

    def __bytes__(self) -> bytes:
        return struct.pack(self.format, *list(asdict(self).values()))

    @classmethod
    def from_bytes(cls: Type[_P], data: bytes) -> _P:
        """Create a Packet instance from a bytes-like object."""
        return cls(*struct.unpack(cls.format, data))

    @classmethod
    def from_serial(cls: Type[_P], interface: Serial) -> _P:
        """Create a Packet instance by reading from a serial interface."""
        return cls.from_bytes(interface.read(cls.size))

    @classmethod
    @property
    def size(cls: Type[_P]) -> int:
        """Get the size of Packet in bytes."""
        return struct.calcsize(cls.format)


@dataclass
class CommandPacket(Packet):
    """Base class for packets sent to the bootloader."""

    pass


@dataclass
class VersionResponsePacket(Packet):
    """Packet received in response to a READ_VERSION command."""

    version: int = 0
    max_packet_length: int = 0
    device_id: int = 0
    erase_size: int = 0
    write_size: int = 0
    format: ClassVar = Packet.format + "2H2xH2x2H12x"


@dataclass
class ResponsePacket(Packet):
    """Base class for most packets received from the bootloader.

    The exception is READ_VERSION, in response to which a VersionResponsePacket
    is received instead.
    """

    success: BootResponseCode = BootResponseCode.UNDEFINED
    format: ClassVar = Packet.format + "B"


@dataclass
class MemoryRangePacket(ResponsePacket):
    """Packet received in response to a GET_MEMORY_RANGE command."""

    program_start: int = 0
    program_end: int = 0
    format: ClassVar = ResponsePacket.format + "2I"


@dataclass
class ChecksumPacket(ResponsePacket):
    """Response to CALCULATE_CHECKSUM."""

    checksum: int = 0
    format: ClassVar = ResponsePacket.format + "H"
