#!/usr/bin/env python3

import argparse
import json
import os
import struct
import sys
import time
import typing
import zlib
from enum import Enum, auto
from zipfile import ZipFile

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


def send_reboot(ser: Serial, timeout=10.0, reboot_flag=ResetRequest.REBOOT_NAVIGATION_PROCESSOR):
    start_time = time.time()
    last_send_time = 0
    reset_message = ResetRequest(reboot_flag)
    encoder = FusionEngineEncoder()
    data = encoder.encode_message(reset_message)
    decoder = FusionEngineDecoder()
    while time.time() < start_time + timeout:
        if time.time() > last_send_time + 0.5:
            ser.write(data)
            ser.flush()
            last_send_time = time.time()
        messages = decoder.on_data(ser.read_all())
        for header, payload in messages:
            if header.message_type == CommandResponseMessage.MESSAGE_TYPE:
                if payload.response == Response.OK:
                    return True
                else:
                    print(f'Reboot Command Rejected: {payload.response}')
                    return False
    return False


def synchronize(ser: Serial, timeout=10.0):
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


def Upgrade(port_name: str, bin_file: typing.BinaryIO, upgrade_type: UpgradeType, should_send_reboot: bool,
            wait_for_reboot: bool = False):
    class_id = {
        UpgradeType.APP: CLASS_APP,
        UpgradeType.GNSS: CLASS_GNSS,
    }[upgrade_type]

    with Serial(port_name, baudrate=460800) as ser:
        if should_send_reboot:
            print('Rebooting the device...')
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

        firmware_data = bin_file.read()

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
            if should_send_reboot:
                # Send a no-op reset request message and wait for a response. This won't actually restart the device,
                # it just waits for it to start on its own after the update completes.
                print('Waiting for software to start...')
                if send_reboot(ser, reboot_flag=0):
                    print('Device rebooted.')
                else:
                    print('Timed out waiting for device. Please reboot the device manually.')
                    if wait_for_reboot:
                        input('Press any key to continue...')
            else:
                print('Please reboot the device...')
                if wait_for_reboot:
                    input('Press any key to continue...')
            return True


def print_bytes(byte_data):
    print(", ".join(
        [f'0x{c:02X}' for c in byte_data]
    ))

def extract_fw_files(p1fw):
    app_bin_fd = None
    gnss_bin_fd = None
    if isinstance(p1fw, ZipFile):
        # Extract filenames from info.json file.
        if 'info.json' in p1fw.namelist():
            info_json = json.load(p1fw.open('info.json', 'r'))

            app_filename = info_json['fusion_engine']['filename']
            gnss_filename = info_json['gnss_receiver']['filename']

            if app_filename in p1fw.namelist():
                app_bin_fd = p1fw.open(app_filename, 'r')

            if gnss_filename in p1fw.namelist():
                gnss_bin_fd = p1fw.open(gnss_filename, 'r')
        else:
            print('No info.json file found. Aborting.')
            sys.exit(1)
    else:
        if os.path.exists(os.path.join(p1fw, 'info.json')):
            # Extract filenames from info.json file.
            info_json_path = os.path.join(p1fw, 'info.json')
            info_json = json.load(open(info_json_path))

            app_filename = info_json['fusion_engine']['filename']
            gnss_filename = info_json['gnss_receiver']['filename']
            app_path = os.path.join(p1fw, app_filename)
            gnss_path = os.path.join(p1fw, gnss_filename)

            if os.path.exists(app_path):
                app_bin_fd = open(os.path.join(p1fw, app_filename), 'rb')

            if os.path.exists(gnss_path):
                gnss_bin_fd = open(os.path.join(p1fw, gnss_filename), 'rb')
        else:
            print('No info.json file found. Aborting.')
            sys.exit(1)

    if app_bin_fd is None and gnss_bin_fd is None:
        print('GNSS and application firmware files not found in given p1fw path. Aborting.')
        sys.exit(1)
    elif app_bin_fd is None:
        print('Application firmware file not found in given p1fw path. Aborting.')
        sys.exit(1)
    elif gnss_bin_fd is None:
        print('GNSS firmware file not found in given p1fw path. Aborting.')
        sys.exit(1)

    print('GNSS and application firmware files found in given p1fw path. Will use these files to upgrade.')
    return app_bin_fd, gnss_bin_fd


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--p1fw', type=str, metavar="FILE", default=None,
                        help="The path to the .p1fw file to be loaded.")
    parser.add_argument('--p1fw-mode', type=str, metavar="MODE", action='append', choices=('gnss', 'app'),
                        help="The type of update to perform when using a .p1fw file: gnss, app. May be specified "
                             "multiple times. For example: --p1fw-mode=gnss --p1fw-mode=app (default)")
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

    p1fw = None
    app_bin_fd = None
    gnss_bin_fd = None
    if p1fw_path is not None:
        if os.path.exists(p1fw_path):
            # Check if a directory is what was provided. If not, then it is assumed that a compressed
            # file is what was provided (this is the expected use case).
            if os.path.isdir(p1fw_path):
                p1fw = p1fw_path
            else:
                try:
                    p1fw = ZipFile(p1fw_path, 'r')
                except:
                    print('Provided path does not lead to a zip file or a directory.')
                    sys.exit(2)
        else:
            print('Provided path %s not found.' % p1fw_path)
            sys.exit(2)

    if p1fw is not None:
        app_bin_fd, gnss_bin_fd = extract_fw_files(p1fw)

        if args.p1fw_mode is None:
            args.p1fw_mode = ('gnss', 'app')

        if 'app' not in args.p1fw_mode:
            app_bin_fd = None
        if 'gnss' not in args.p1fw_mode:
            gnss_bin_fd = None

    if gnss_bin_fd is not None:
        if gnss_bin_path is not None:
            print('Ignoring provided GNSS bin path, as p1fw path was provided.')
    elif gnss_bin_path is not None:
        gnss_bin_fd = open(gnss_bin_path, 'rb')

    if app_bin_fd is not None:
        if app_bin_path is not None:
            print('Ignoring provided application bin path, as p1fw path was provided.')
    elif app_bin_path is not None:
        app_bin_fd = open(app_bin_path, 'rb')

    if gnss_bin_fd is not None:
        print('Upgrading GNSS firmware...')
        if not Upgrade(port_name, gnss_bin_fd, UpgradeType.GNSS, should_send_reboot,
                       wait_for_reboot=app_bin_fd is not None):
            sys.exit(2)

    if app_bin_fd is not None:
        if gnss_bin_fd is not None:
            print('')

        print('Upgrading application firmware...')
        if not Upgrade(port_name, app_bin_fd, UpgradeType.APP, should_send_reboot):
            sys.exit(2)


if __name__ == '__main__':
    main()
