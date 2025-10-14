import os
import ctypes
from ctypes import *
import time
from dotenv import load_dotenv
load_dotenv()

PLCOMPRO_URL = os.getenv("PLCOMPRO_URL")

# Load the plcommpro.dll library
plcommpro = ctypes.CDLL(PLCOMPRO_URL)

# Define the argument and return types for the functions
plcommpro.Connect.argtypes = [c_char_p]
plcommpro.Connect.restype = c_void_p
plcommpro.Disconnect.argtypes = [c_void_p]
plcommpro.SetDeviceData.argtypes = [c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
plcommpro.SetDeviceData.restype = c_int
plcommpro.DeleteDeviceData.argtypes = [c_void_p, c_char_p, c_char_p, c_char_p]


def connect_to_device(ip_address, port, max_attempts=3):
    # Prepare the connection parameters
    params = f"protocol=TCP,ipaddress={ip_address},port={port},timeout=4000,passwd=".encode('utf-8')
    attempts = 0
    handle = None

    # Attempt to connect to the device with retries
    while attempts < max_attempts and not handle:
        print(f"Attempting to connect... ({attempts + 1}/{max_attempts})")
        handle = plcommpro.Connect(params)

        if handle:
            print(f"Connection established on {ip_address}")
        else:
            # Get the last error code when connection fails
            error_code = ctypes.get_last_error()
            print(f"Failed to connect to the device  {ip_address}. Error code: {error_code}. Retrying...")

        attempts += 1
        time.sleep(2)  # Wait for 2 seconds before retrying

    if not handle:
        print(f"Failed to connect after {max_attempts} attempts.")
    return handle


def disconnect_device(handle):
    if handle:
        plcommpro.Disconnect(handle)
        print("Disconnected from device.")
    else:
        print("No valid handle to disconnect.")


def add_user(handle, user_pin, card_no, start_time, end_time):
    print(start_time, end_time, user_pin, card_no)
    delete_user(user_pin , None)
    data = f"Pin={user_pin}\tCardNo={card_no}\tStartTime={start_time}\tEndTime={end_time}\tPassword=".encode('utf-8')
    result = plcommpro.SetDeviceData(handle, b"user", data, b"")
    print(card_no, user_pin)
    if result == 0:
        print("User added successfully.")
        authorize_user(handle, user_pin)
        return True
    else:
        print(f"Failed to add user. Error code: {result}")
        return False


def delete_user(handle, user_pin,fingerId):
    # Delete the user by PIN
    if fingerId==None:
       data = f"Pin={user_pin}".encode('utf-8')
       # result = plcommpro.DeleteDeviceData(handle, b"user", data,None)
       result3 = plcommpro.DeleteDeviceData(handle, b"userauthorize", data, None)
    else :
        data = f"Pin={user_pin}\tFingerID={fingerId}".encode('utf-8')
        result3 = plcommpro.DeleteDeviceData(handle, b"templatev10", data, None)

    if result3 == 0:
        print("User deleted successfully.")
        return True
    else:
        print(f"Failed to delete user. Error code: {result3}")
        return False




def authorize_user(handle, user_pin):
    # Authorize the user for access
    data = f"Pin={user_pin}\tAuthorizeTimezoneId=1\tAuthorizeDoorId=1\r\nPin={user_pin}\tAuthorizeTimezoneId=1\tAuthorizeDoorId=2"
    result = plcommpro.SetDeviceData(handle, b"userauthorize", data, b"")
    if result == 0:
        print("User authorized successfully.")
        return True
    else:
        print(f"Failed to authorize user. Error code: {result}")
        return False


def unauthorize_user(handle, user_pin):
    # Unauthorize the user, removing access
    table_name = "userauthorize".encode('utf-8')
    delete_condition = f"Pin={user_pin}".encode('utf-8')
    options = "".encode('utf-8')

    # Use DeleteDeviceData to unauthorize the user
    print(handle)
    result = plcommpro.DeleteDeviceData(handle, table_name, delete_condition, options)
    if result == 0:
        print(f"Authorizations for user PIN {user_pin} have been successfully deleted.")
    else:
        print(f"Failed to delete authorizations for user PIN {user_pin}. Error code: {result}")
