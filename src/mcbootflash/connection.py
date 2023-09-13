# noqa: D100
import logging
from collections.abc import Callable
from struct import error as structerror
from typing import Any, Dict, Generator, Tuple, Type, Union

from intelhex import IntelHex  # type: ignore[import]
from serial import Serial  # type: ignore[import]

from mcbootflash.error import (
    BadAddress,
    BadLength,
    BootloaderError,
    UnsupportedCommand,
    VerifyFail,
)
from mcbootflash.protocol import (
    Checksum,
    Command,
    CommandCode,
    MemoryRange,
    Response,
    ResponseBase,
    ResponseCode,
    Version,
)

_logger = logging.getLogger(__name__)

_BOOTLOADER_EXCEPTIONS: Dict[ResponseCode, Type[BootloaderError]] = {
    ResponseCode.UNSUPPORTED_COMMAND: UnsupportedCommand,
    ResponseCode.BAD_ADDRESS: BadAddress,
    ResponseCode.BAD_LENGTH: BadLength,
    ResponseCode.VERIFY_FAIL: VerifyFail,
}

_RESPONSE_TYPE_MAP: Dict[CommandCode, Type[ResponseBase]] = {
    CommandCode.READ_VERSION: Version,
    CommandCode.READ_FLASH: Response,
    CommandCode.WRITE_FLASH: Response,
    CommandCode.ERASE_FLASH: Response,
    CommandCode.CALC_CHECKSUM: Checksum,
    CommandCode.RESET_DEVICE: Response,
    CommandCode.SELF_VERIFY: Response,
    CommandCode.GET_MEMORY_ADDRESS_RANGE: MemoryRange,
}

ProgressCallback = Callable[[int, int], None]


class Bootloader:
    """Communication interface to device running MCC 16-bit bootloader.

    Parameters
    ----------
    port : str
       Serial port name. Typically /dev/ttyUSBx or /dev/ttyACMx on Posix, or COMx on
       Windows.
    **kwargs, optional
        Any additional arguments for the serial.Serial constructor.
    """

    # Is this key always the same? Perhaps generated by MCC during code generation?
    # If this key is incorrect, flash write operations will fail silently.
    _FLASH_UNLOCK_KEY = 0x00AA0055

    def __init__(self, port: str, **kwargs: Any):
        self.interface = Serial(port=port, **kwargs)
        _logger.info("Connecting to bootloader...")
        try:
            (
                _,  # version
                self._max_packet_length,
                _,  # device_id
                self._erase_size,
                self._write_size,
            ) = self._read_version()
            self._memory_range = range(*self._get_memory_address_range())
        except structerror as exc:
            raise BootloaderError("No response from bootloader") from exc
        _logger.info("Connected")

    def flash(
        self,
        hexfile: str,
        progress_callback: Union[None, ProgressCallback] = None,
    ) -> None:
        """Flash application firmware.

        Parameters
        ----------
        hexfile : str
            Path to a HEX-file containing application firmware.
        progress_callback : Callable[[int, int], None] (optional)
            A callable which takes the number of bytes written so far as its first
            argument, and the total number of bytes to write as its second argument.
            The callable returns None. If no callback is provided, progress is not
            reported.

        Raises
        ------
        BootloaderError
            If HEX-file cannot be flashed.
        """
        path = hexfile
        # Since the MCU uses 16-bit instructions, each "address" in the (8-bit) hex file
        # is actually only half an address. Therefore, we need to multiply addresses
        # received from MCC by two to get the corresponding address in the hex file.
        hexdata = IntelHex(path)[2 * self._memory_range[0] : 2 * self._memory_range[-1]]
        segments = [hexdata[u:l] for u, l in hexdata.segments()]

        if not segments:
            raise BootloaderError(
                "HEX file contains no data that fits entirely within program memory"
            )

        self.erase_flash(self._memory_range)
        _logger.info(f"Flashing {path}")
        chunk_size = self._max_packet_length - Command.get_size()
        chunk_size -= chunk_size % self._write_size
        total_bytes = sum(len(segment) for segment in segments)
        written_bytes = 0

        for segment in segments:
            chunks = self._chunk(segment, chunk_size)
            _logger.debug(f"Flashing segment {segments.index(segment)}")

            for chunk in chunks:
                self._write_flash(chunk, self._write_size)
                written_bytes += len(chunk)
                _logger.debug(
                    f"{written_bytes} bytes written of {total_bytes} "
                    f"({written_bytes / total_bytes * 100:.2f}%)"
                )
                self._checksum(chunk)

                if progress_callback:
                    progress_callback(written_bytes, total_bytes)

        self._self_verify()

    @staticmethod
    def _chunk(hexdata: IntelHex, size: int) -> Generator[IntelHex, None, None]:
        start = hexdata.minaddr()
        stop = hexdata.maxaddr()
        return (hexdata[i : i + size] for i in range(start, stop, size))

    def _send_and_receive(self, command: Command, data: bytes = b"") -> ResponseBase:
        self.interface.write(bytes(command) + data)
        response = _RESPONSE_TYPE_MAP[command.command].from_serial(self.interface)
        self._verify_good_response(command, response)
        return response

    @staticmethod
    def _verify_good_response(
        command_packet: Command,
        response_packet: ResponseBase,
    ) -> None:
        """Check that response is not an error."""
        if response_packet.command != command_packet.command:
            _logger.debug("Command code mismatch:")
            _logger.debug(f"Sent: {command_packet.command.name}")
            _logger.debug(f"{CommandCode(response_packet.command).name}")
            raise BootloaderError("Command code mismatch")

        if isinstance(response_packet, Version):
            return

        assert isinstance(response_packet, Response)

        if response_packet.success != ResponseCode.SUCCESS:
            _logger.debug("Command failed:")
            _logger.debug(f"Command:  {bytes(command_packet)!r}")
            _logger.debug(f"Response: {bytes(response_packet)!r}")
            raise _BOOTLOADER_EXCEPTIONS[response_packet.success]

    def _read_version(self) -> Tuple[int, int, int, int, int]:
        """Read bootloader version and some other useful information.

        Returns
        -------
        version : int
        max_packet_length : int
            The maximum size of a single packet sent to the bootloader,
            including both the command and associated data.
        device_id : int
        erase_size : int
            Flash page size. When erasing flash memory, the number of bytes to
            be erased must align with a flash page.
        write_size : int
            Write block size. When writing to flash, the number of bytes to be
            written must align with a write block.
        """
        read_version_response = self._send_and_receive(
            Command(CommandCode.READ_VERSION)
        )
        assert isinstance(read_version_response, Version)
        _logger.debug("Got bootloader attributes:")
        _logger.debug(f"Max packet length: {read_version_response.max_packet_length}")
        _logger.debug(f"Erase size:        {read_version_response.erase_size}")
        _logger.debug(f"Write size:        {read_version_response.write_size}")

        return (
            read_version_response.version,
            read_version_response.max_packet_length,
            read_version_response.device_id,
            read_version_response.erase_size,
            read_version_response.write_size,
        )

    def _get_memory_address_range(self) -> Tuple[int, int]:
        mem_range_response = self._send_and_receive(
            Command(CommandCode.GET_MEMORY_ADDRESS_RANGE)
        )
        assert isinstance(mem_range_response, MemoryRange)
        _logger.debug(
            "Got program memory range: "
            f"{mem_range_response.program_start:#08x}:"
            f"{mem_range_response.program_end:#08x}"
        )
        return mem_range_response.program_start, mem_range_response.program_end

    def erase_flash(
        self,
        erase_range: Union[None, range] = None,
        force: bool = False,
        verify: bool = True,
    ) -> None:
        """Erase program memory area.

        Parameters
        ----------
        erase_range: range, optional
            Address range to erase. By default the entire program memory is erased.
        force : bool, optional
            By default, flash erase will be skipped if no program is detected in the
            program memory area. Setting `force` to True skips program detection and
            erases regardless of whether a program is present or not.
        verify : bool, optional
            The ERASE_FLASH command may fail silently if the `unlock_sequence` field of
            the command packet is incorrect. By default, this method verifies that the
            erase was successful by checking that no application is detected after the
            erase. Set `verify` to False to skip this check.
        """
        start, *_, end = erase_range if erase_range else self._memory_range

        if force or self._detect_program():
            _logger.info("Erasing flash...")
            self._erase_flash(start, end)
        else:
            _logger.info("No application detected, skipping flash erase")
            return

        if verify:
            if self._detect_program():
                _logger.debug("An application was detected; flash erase failed")
                _logger.debug("unlock_sequence field may be incorrect")
                raise BootloaderError("Existing application could not be erased")
            _logger.info("No application detected; flash erase successful")

    def _erase_flash(self, start_address: int, end_address: int) -> None:
        _logger.debug(f"Erasing addresses {start_address:#08x}:{end_address:#08x}")
        normal_timeout = self.interface.timeout

        if self.interface.timeout is not None:
            self.interface.timeout *= 10  # Erase may take a while.

        self._send_and_receive(
            command=Command(
                command=CommandCode.ERASE_FLASH,
                data_length=(end_address - start_address) // self._erase_size,
                unlock_sequence=self._FLASH_UNLOCK_KEY,
                address=start_address,
            )
        )
        self.interface.timeout = normal_timeout

    def _detect_program(self) -> bool:
        try:
            # Program memory may be empty, which should not be logged as an error.
            _logger.disabled = True
            self._self_verify()
        except VerifyFail:
            return False
        finally:
            _logger.disabled = False
        return True

    def _write_flash(self, data: IntelHex, align: int) -> None:
        """Write data to bootloader.

        Parameters
        ----------
        data : intelhex.IntelHex
            An IntelHex instance of length no greater than the bootloader's
            max_packet_length attribute.
        """
        padding = bytes([data.padding] * ((align - (len(data) % align)) % align))
        _logger.debug(f"Writing {len(data)} bytes to {data.minaddr():#08x}")
        self._send_and_receive(
            Command(
                command=CommandCode.WRITE_FLASH,
                data_length=len(data) + len(padding),
                unlock_sequence=self._FLASH_UNLOCK_KEY,
                address=data.minaddr() >> 1,
            ),
            data.tobinstr() + padding,
        )

    def _self_verify(self) -> None:
        self._send_and_receive(Command(command=CommandCode.SELF_VERIFY))
        _logger.info("Self verify OK")

    def _get_remote_checksum(self, address: int, length: int) -> int:
        checksum_response = self._send_and_receive(
            Command(
                command=CommandCode.CALC_CHECKSUM,
                data_length=length,
                address=address,
            )
        )
        assert isinstance(checksum_response, Checksum)
        return checksum_response.checksum

    @staticmethod
    def _get_local_checksum(data: IntelHex) -> int:
        checksum = 0
        start = data.minaddr()
        stop = start + len(data)
        step = 4

        for i in range(start, stop, step):
            databytes = data[i : i + step].tobinstr()
            checksum += int.from_bytes(databytes, byteorder="little") & 0xFFFF
            checksum += (int.from_bytes(databytes, byteorder="little") >> 16) & 0xFF

        return checksum & 0xFFFF

    def _checksum(self, hexdata: IntelHex) -> None:
        """Compare checksums calculated locally and onboard device.

        Parameters
        ----------
        address : int
            Address from which to start checksum.
        length : int
            Number of bytes to checksum.
        """
        checksum1 = self._get_local_checksum(hexdata)
        checksum2 = self._get_remote_checksum(hexdata.minaddr() >> 1, len(hexdata))

        if checksum1 != checksum2:
            _logger.debug(f"Checksum mismatch: {checksum1} != {checksum2}")
            _logger.debug("unlock_sequence field may be incorrect")
            raise BootloaderError("Checksum mismatch while writing")

        _logger.debug(f"Checksum OK: {checksum1}")

    def reset(self) -> None:
        """Reset device."""
        self._send_and_receive(Command(command=CommandCode.RESET_DEVICE))
        _logger.info("Device reset")

    def _read_flash(self) -> None:
        raise NotImplementedError
