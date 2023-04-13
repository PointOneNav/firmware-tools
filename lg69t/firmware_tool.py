#!/usr/bin/env python3

import argparse
import os
import struct
import sys
import time
import zlib
from zipfile import ZipFile
from enum import Enum, auto

from serial import Serial
from fusion_engine_client.parsers import FusionEngineEncoder, FusionEngineDecoder
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


def send_reboot(ser: Serial, timeout=10):
    start_time = time.time()
    reset_message = ResetRequest(ResetRequest.REBOOT_NAVIGATION_PROCESSOR)
    encoder = FusionEngineEncoder()
    data = encoder.encode_message(reset_message)
    ser.write(data)
    ser.flush()
    decoder = FusionEngineDecoder()
    while time.time() < start_time + timeout:
        messages = decoder.on_data(ser.read_all())
        for header, payload in messages:
            if header.message_type == CommandResponseMessage.MESSAGE_TYPE:
                if payload.response == Response.OK:
                    return True
                else:
                    print(f'Reboot Command Rejected: {payload.response}')
                    return False
    return False


def synchronize(ser: Serial, timeout=10):
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


def Upgrade(port_name: str, bin_path: str, upgrade_type: UpgradeType, should_send_reboot: bool):
    class_id = {
        UpgradeType.APP: CLASS_APP,
        UpgradeType.GNSS: CLASS_GNSS,
    }[upgrade_type]

    with Serial(port_name, baudrate=460800) as ser:
        if should_send_reboot:
            print('Sending Reboot Command')
            if not send_reboot(ser):
                print('Reboot Command Failed')
                return False
            else:
                print('Reboot Command Success')
        else:
            print('Please reboot the device...')

        # Note that the reboot command can take over 5 seconds to kick in.
        if not synchronize(ser):
            print('Sync Timed Out')
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
            if not should_send_reboot:
                print('Please reboot the device...')
            return True
        else:
            print('Update Failed')
            return False


def print_bytes(byte_data):
    print(", ".join(
        [f'0x{c:02X}' for c in byte_data]
    ))

def extract_fw_files(p1fw_path):
    fw_files = {}
    for filename in os.listdir(p1fw_path):
        dir = os.path.join(p1fw_path, filename)
        if filename.endswith('upg.bin'):
            fw_files['app'] = dir
        elif filename.endswith('sta.bin'):
            fw_files['gnss'] = dir
    if len(fw_files) == 0:
        print('GNSS and application firmware files not found in given p1fw path. Aborting.')
        sys.exit(1)
    elif 'app' not in fw_files:
        print('Application firmware file not found in given p1fw path. Aborting.')
        sys.exit(1)
    elif 'gnss' not in fw_files:
        print('GNSS firmware file not found in given p1fw path. Aborting.')
        sys.exit(1)

    print('GNSS and application firmware files found in given p1fw path. Will use these files to upgrade.')
    return fw_files

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--p1fw', type=str, metavar="FILE", default=None,
                        help="The path to the .p1fw file to be loaded.")
    parser.add_argument('--gnss', type=str, metavar="FILE", default=None,
                        help="The path to the GNSS (Teseo) firmware file to be loaded.")
    parser.add_argument('--app', type=str, metavar="FILE", default=None,
                        help="The path to the application firmware file to be loaded.")
    parser.add_argument('--port', type=str, default='/dev/ttyUSB0', help="The serial port of the device.")
    parser.add_argument('-m', '--manual-reboot', action='store_true',
                        help="Don't try to send a software reboot. User must manually reset the board.")

    args = parser.parse_args()

    port_name = args.port
    p1fw_path = args.p1fw
    gnss_bin_path = args.gnss
    app_bin_path = args.app
    should_send_reboot = not args.manual_reboot

    if p1fw_path is None and app_bin_path is None and gnss_bin_path is None:
        print('You must specify p1fw file, gnss file, or app file to upgrade.')
        sys.exit(1)

    print(f"Starting upgrade on device {port_name}.")

    fw_files = {}
    if p1fw_path is not None:
        try:
            with ZipFile(p1fw_path, 'r') as f:
                f.extractall('p1fw')
                p1fw = os.path.join(os.getcwd(), 'p1fw')
                fw_files = extract_fw_files(p1fw)
        except:
            if os.path.exists(p1fw_path):
                fw_files = extract_fw_files(p1fw_path)
            else:
                print("Directory %s not found." % p1fw_path)
                sys.exit(2)

    if gnss_bin_path is not None or 'gnss' in fw_files:
        if 'gnss' in fw_files:
            if gnss_bin_path is not None:
                print('Ignoring provided GNSS bin path, as p1fw path was provided.')
            gnss_bin_path = fw_files['gnss']
        print('Upgrading GNSS...')
        if not Upgrade(port_name, gnss_bin_path, UpgradeType.GNSS, should_send_reboot):
            sys.exit(2)
    if app_bin_path is not None or 'app' in fw_files:
        if 'app' in fw_files:
            if app_bin_path is not None:
                print('Ignoring provided application bin path, as p1fw path was provided.')
            app_bin_path = fw_files['app']
        print('Upgrading App...')
        if not Upgrade(port_name, app_bin_path, UpgradeType.APP, should_send_reboot):
            sys.exit(2)


if __name__ == '__main__':
    main()
