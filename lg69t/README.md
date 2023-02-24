# LG69T Firmware Tools
These tools are used to manage the firmware on the Quectel LG69T family of products.

The firmware consists of three files:
- Bootloader (optional)
- Application
- GNSS

The tools require Python 3. They work in Linux, Windows and Mac.

Before using the tools, install the Python requirements:

```
python3 -m pip install -r requirements.txt
```

And download the latest firmware package from [Point One's Developer Portal](https://pointonenav.com/docs/).

## Application and GNSS

To update the Application and GNSS firmware, you must use UART1 (`Standard COM Port` for Windows, typically `/dev/ttyUSB1` in Linux for P1SDK):

```
python3 firmware_tool.py --port=/dev/ttyUSB1 --app=/path/to/quectel-lg69t-am-0.XX.0_upg.bin
python3 firmware_tool.py --port=/dev/ttyUSB1 --gnss=/path/to/lg69t_teseo_A.B.CC.D_sta.bin
```

Replace `/dev/ttyUSB1` with the serial port connected to the device via UART1 (use the appropriate COM port number in Windows, e.g., `Standard COM Port`).

Replace `/path/to/quectel-lg69t-am-0.XX.0_upg.bin` and `/path/to/lg69t_teseo_A.B.CC.D_sta.bin` with the path to the application image and GNSS image files respectively. The device must be rebooted after each command is executed successfully.

## Considerations

If the reset command is not working, the `firmware_tool.py` should be run with the `--manual-reboot` flag. When prompted, manually power cycle the board after the script exits.

## Bootloader

Note: In general, you should never need to reprogram the bootloader. Doing so will completely erase the chip, including any saved configuration, calibration, and the application firmware.

To program the bootloader:
- Run `pip install stm32loader`
- Press and HOLD the BOOT button while powering on the module.
- Release the BOOT button after the device is powered up.
- Run `stm32loader -p /dev/ttyUSB0 -e -w -v -a 0x08000000 quectel-bootloader-A.B.C.bin`
- Press the RESET button to complete the process.

