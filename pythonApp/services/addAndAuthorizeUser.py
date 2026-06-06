import os
import ctypes
from ctypes import *
import time
import logging
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()

PLCOMPRO_URL = os.getenv("PLCOMPRO_URL")

plcommpro = ctypes.CDLL(PLCOMPRO_URL)

plcommpro.Connect.argtypes = [c_char_p]
plcommpro.Connect.restype = c_void_p
plcommpro.Disconnect.argtypes = [c_void_p]
plcommpro.SetDeviceData.argtypes = [c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
plcommpro.SetDeviceData.restype = c_int
plcommpro.DeleteDeviceData.argtypes = [c_void_p, c_char_p, c_char_p, c_char_p]


def connect_to_device(ip_address, port, max_attempts=3):
    params = f"protocol=TCP,ipaddress={ip_address},port={port},timeout=4000,passwd=".encode('utf-8')
    attempts = 0
    handle = None

    while attempts < max_attempts and not handle:
        logger.info("Attempting to connect... (%s/%s)", attempts + 1, max_attempts)
        handle = plcommpro.Connect(params)

        if handle:
            logger.info("Connection established on %s", ip_address)
        else:
            error_code = ctypes.get_last_error()
            logger.warning("Failed to connect to device %s. Error code: %s. Retrying...", ip_address, error_code)

        attempts += 1
        time.sleep(2)

    if not handle:
        logger.error("Failed to connect after %s attempts.", max_attempts)
    return handle


def disconnect_device(handle):
    if handle:
        plcommpro.Disconnect(handle)
        logger.info("Disconnected from device.")
    else:
        logger.warning("No valid handle to disconnect.")


def add_user(handle, user_pin, card_no, start_time, end_time):
    logger.info("add_user: %s %s %s %s", start_time, end_time, user_pin, card_no)
    delete_user(user_pin, None)
    data = f"Pin={user_pin}\tCardNo={card_no}\tStartTime={start_time}\tEndTime={end_time}\tPassword=".encode('utf-8')
    result = plcommpro.SetDeviceData(handle, b"user", data, b"")
    logger.info("card_no=%s, user_pin=%s", card_no, user_pin)
    if result == 0:
        logger.info("User added successfully.")
        authorize_user(handle, user_pin)
        return True
    else:
        logger.error("Failed to add user. Error code: %s", result)
        return False


def delete_user(handle, user_pin, fingerId):
    if fingerId is None:
        data = f"Pin={user_pin}".encode('utf-8')
        result3 = plcommpro.DeleteDeviceData(handle, b"userauthorize", data, None)
    else:
        data = f"Pin={user_pin}\tFingerID={fingerId}".encode('utf-8')
        result3 = plcommpro.DeleteDeviceData(handle, b"templatev10", data, None)

    if result3 == 0:
        logger.info("User deleted successfully.")
        return True
    else:
        logger.error("Failed to delete user. Error code: %s", result3)
        return False


def authorize_user(handle, user_pin):
    data = f"Pin={user_pin}\tAuthorizeTimezoneId=1\tAuthorizeDoorId=1\r\nPin={user_pin}\tAuthorizeTimezoneId=1\tAuthorizeDoorId=2"
    result = plcommpro.SetDeviceData(handle, b"userauthorize", data, b"")
    if result == 0:
        logger.info("User authorized successfully.")
        return True
    else:
        logger.error("Failed to authorize user. Error code: %s", result)
        return False


def unauthorize_user(handle, user_pin):
    table_name = "userauthorize".encode('utf-8')
    delete_condition = f"Pin={user_pin}".encode('utf-8')
    options = "".encode('utf-8')

    logger.debug("Unauthorizing user PIN %s", user_pin)
    result = plcommpro.DeleteDeviceData(handle, table_name, delete_condition, options)
    if result == 0:
        logger.info("Authorizations for user PIN %s have been successfully deleted.", user_pin)
    else:
        logger.error("Failed to delete authorizations for user PIN %s. Error code: %s", user_pin, result)
