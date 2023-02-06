#!/usr/bin/env python3

import argparse
import struct
import time
import zlib
from enum import Enum, auto

from serial import Serial
from fusion_engine_client.parsers import FusionEngineEncoder
from fusion_engine_client.messages import *

SYNC_WORD1 = 0x514C1309
SYNC_WORD1_BYTES = struct.pack('<I', SYNC_WORD1)
RSP_WORD1 = 0xAAFC3A4D
RSP_WORD1_BYTES = struct.pack('<I', RSP_WORD1)
SYNC_WORD2 = 0x1203A504
SYNC_WORD2_BYTES = struct.pack('<I', SYNC_WORD2)
RSP_WORD2 = 0x55FD5BA0
RSP_WORD2_BYTES = struct.pack('<I', RSP_WORD2)

CLASS_GNSS = b'\x01'
CLASS_APP = b'\x02'

MSG_ID_FIRMWARE_ADDRESS = b'\x01'
MSG_ID_FIRMWARE_INFO = b'\x02'
MSG_ID_START_UPGRADE = b'\x03'
MSG_ID_SEND_FIRMWARE = b'\x04'

# The manual indicates this should be 0, but here I account for the bootloader.
APP_FLASH_OFFSET = 0x20000

PACKET_SIZE = 1024 * 5

RESPONSE_PAYLOAD_SIZE = 4
HEADER = b'\xAA'
TAIL = b'\x55'


def send_reboot(ser: Serial):
    reset_message = ResetRequest(ResetRequest.REBOOT_NAVIGATION_PROCESSOR)
    encoder = FusionEngineEncoder()
    data = encoder.encode_message(reset_message)
    ser.write(data)
    ser.flush()


def synchronize(ser: Serial, timeout=5):
    start_time = time.time()
    ser.timeout = 0.05
    resp_data = b'\x00\x00\x00\x00'
    while time.time() < start_time + timeout:
        ser.write(SYNC_WORD1_BYTES)
        c = ser.read()
        while len(c) > 0:
            resp_data = resp_data[1:] + c
            if resp_data == RSP_WORD1_BYTES:
                ser.write(SYNC_WORD2_BYTES)
                resp_data = ser.read(4)
                if len(resp_data) == 4 and resp_data == RSP_WORD2_BYTES:
                    return True
                else:
                    return False
            c = ser.read()
    return False


def get_response(class_id: bytes, msg_id: bytes, ser: Serial, timeout=60):
    response_fmt = '>BBBHBBHIB'
    response_size = struct.calcsize(response_fmt)

    ser.timeout = timeout
    data = ser.read(response_size)
    if len(data) < response_size:
        print('Timeout waiting for response')
        return False

    _, _, _, read_payload_size, read_class_id, read_msg_id, response, crc, _ = struct.unpack(
        response_fmt, data)

    calculated_crc = zlib.crc32(data[1:-5])

    if RESPONSE_PAYLOAD_SIZE != read_payload_size:
        print(
            f"Response had unexpected size field. [expected={RESPONSE_PAYLOAD_SIZE}, got={read_payload_size}]")
        return False

    if class_id[0] != read_class_id:
        print(
            f"Response had class id field. [expected={class_id[0]}, got={read_class_id}]")
        return False

    if msg_id[0] != read_msg_id:
        print(
            f"Response had unexpected message id field. [expected={msg_id[0]}, got={read_msg_id}]")
        return False

    if crc != calculated_crc:
        print(
            f"Response had bad CRC. [calculated={calculated_crc}, got={crc}]")
        return False

    if response != 0:
        print(f"Response indicates error occurred. [error={response}]")
        return False

    return True


def encode_message(class_id: bytes, msg_id: bytes, payload: bytes):
    data = class_id + msg_id + struct.pack('>H', len(payload)) + payload
    crc = struct.pack('>I', zlib.crc32(data))
    return HEADER + data + crc + TAIL


def encode_app_info(firmware_data):
    app_info_fmt = '>IIIB3x'
    fw_crc = zlib.crc32(struct.pack('<I', len(firmware_data)) + firmware_data)
    payload_data = struct.pack(app_info_fmt, len(
        firmware_data), fw_crc, APP_FLASH_OFFSET, 0x01)
    return encode_message(CLASS_APP, MSG_ID_FIRMWARE_INFO, payload_data)


def encode_gnss_info(firmware_data):
    gnss_info_fmt = '>IIIIIIBBB5x'
    fw_crc = zlib.crc32(struct.pack('<I', len(firmware_data)) + firmware_data)
    payload_data = struct.pack(gnss_info_fmt, len(
        firmware_data), fw_crc, 0x10000000, 0x00000400, 0x00180000, 0x00080000, 0x01, 0x00, 0x00)
    return encode_message(CLASS_GNSS, MSG_ID_FIRMWARE_INFO, payload_data)


def send_firmware(ser: Serial, class_id: bytes, firmware_data):
    sequence_num = 0
    total_len = len(firmware_data)
    while len(firmware_data) > 0:
        data = encode_message(class_id, MSG_ID_SEND_FIRMWARE, struct.pack(
            '>I', sequence_num) + firmware_data[:PACKET_SIZE])
        ser.write(data)
        if not get_response(class_id, MSG_ID_SEND_FIRMWARE, ser):
            print()
            return False
        firmware_data = firmware_data[PACKET_SIZE:]
        sequence_num += 1
        print(
            f'\r{int((total_len - len(firmware_data))/total_len * 100.):02d}%', end='')
    print()
    return True


class UpgradeType(Enum):
    APP = auto()
    GNSS = auto()


def Upgrade(port_name: str, bin_path: str, upgrade_type: UpgradeType):
    class_id = {
        UpgradeType.APP: CLASS_APP,
        UpgradeType.GNSS: CLASS_GNSS,
    }[upgrade_type]

    with Serial(port_name, baudrate=460800) as ser:
        print('Rebooting (or user press reboot)')
        send_reboot(ser)
        send_reboot(ser)
        if not synchronize(ser):
            print('Sync Failed')
            return False
        else:
            print('Sync Success')

        print('Sending Firmware Address')
        ser.write(encode_message(
            class_id, MSG_ID_FIRMWARE_ADDRESS, b'\x00' * 4))
        if not get_response(class_id, MSG_ID_FIRMWARE_ADDRESS, ser):
            return False

        with open(bin_path, 'rb') as fd:
            firmware_data = fd.read()

        print('Sending Firmware Info')
        if upgrade_type == UpgradeType.GNSS:
            ser.write(encode_gnss_info(firmware_data))
        else:
            ser.write(encode_app_info(firmware_data))
        if not get_response(class_id, MSG_ID_FIRMWARE_INFO, ser):
            return False

        print('Sending Upgrade Start and Flash Erase (takes 30 seconds)')
        ser.write(encode_message(
            class_id, MSG_ID_START_UPGRADE, b''))
        if not get_response(class_id, MSG_ID_START_UPGRADE, ser):
            return False

        print('Sending Data')
        if send_firmware(ser, class_id, firmware_data) is True:
            print('Update Success')
            return True
        else:
            print('Update Failed')
            return False


def print_bytes(byte_data):
    print(", ".join(
        [f'0x{c:02X}' for c in byte_data]
    ))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--gnss', type=str, metavar="FILE", default=None, help="The path to the GNSS (Teseo) firmware file to be loaded.")
    parser.add_argument('--app', type=str, metavar="FILE", default=None, help="The path to the application firmware file to be loaded.")
    parser.add_argument('--port', type=str, default='/dev/ttyUSB0', help="The serial port of the device.")
    
    args = parser.parse_args()

    port_name = args.port
    gnss_bin_path = args.gnss
    app_bin_path = args.app
     
    if app_bin_path is None and gnss_bin_path is None:
        print('You must specify gnss or app files to upgrade.')
        sys.exit(1)

    print(f"Starting upgrade on device {port_name}.")

    if gnss_bin_path is not None:
        print('Upgrading GNSS...')
        if not Upgrade(port_name, gnss_bin_path, UpgradeType.GNSS):
            sys.exit(2)
    if app_bin_path is not None:
        print('Upgrading App...')
        if not Upgrade(port_name, app_bin_path, UpgradeType.APP):
            sys.exit(2)

    
if __name__ == '__main__':
    main()
