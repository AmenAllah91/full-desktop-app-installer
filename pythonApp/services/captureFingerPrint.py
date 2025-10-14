# import json
# import os
# import threading
# import time
#
# import jpype
# import jpype.imports
# import ctypes
# import numpy as np
# import base64
# from kafka_service.kafkaservice import KafkaService
# from PIL import Image
# # from ctypes import c_char_p, c_void_p, c_int
# from jpype.types import *
# from io import BytesIO
# from flask import jsonify
# from dotenv import load_dotenv
#
# load_dotenv()
# DEVICE_IP = os.getenv("DEVICE_IP")
# DEVICE_PORT = os.getenv("DEVICE_PORT")
# PLCOMPRO_URL = os.getenv("PLCOMPRO_URL")
# KafkaBroker = os.getenv("KAFKA_BROKER")
#
# # Load the required DLL
# plcommpro = ctypes.CDLL(PLCOMPRO_URL)
#
# # Define argument and return types for the DLL functions
# plcommpro.Connect.argtypes = [c_char_p]
# plcommpro.Connect.restype = c_void_p
# plcommpro.Disconnect.argtypes = [c_void_p]
# plcommpro.SetDeviceData.argtypes = [c_void_p, c_char_p, c_char_p, c_char_p]
# plcommpro.SetDeviceData.restype = c_int
# plcommpro.DeleteDeviceData.argtypes = [c_void_p, c_char_p, c_char_p, c_char_p]
# plcommpro.GetDeviceData.argtypes = [c_void_p, c_char_p, c_int, c_char_p, c_char_p, c_char_p, c_char_p]
# plcommpro.GetDeviceData.restype = c_int
# # Set the path to the JDK 17 JVM DLL
# jvm_path = r"C:\\Program Files\\Java\\jdk-17\\bin\\server\\jvm.dll"
# jpype.startJVM(jvmpath=jvm_path, classpath=["ZKFingerReader.jar"])
#
# from com.zkteco.biometric import FingerprintSensorEx
#
#
# class FingerprintService:
#     def __init__(self):
#         self.userId = None
#         self.fingerId=None
#         self.gymbranchId = None
#         self.step = 0
#         self.sensor = FingerprintSensorEx()
#         self.device_handle = None
#         self.db_handle = 0
#         self.fpWidth = 0
#         self.fpHeight = 0
#         self.access_handle = None
#         self.device_ip = DEVICE_IP
#         self.device_port = DEVICE_PORT
#         self.templates = []
#         self.kafka_service = KafkaService(kafka_broker=KafkaBroker, group_id="fingerprint_group")
#     def log_message(self, message):
#         print(message)
#
#     def connect_to_access_device(self):
#         params = f"protocol=TCP,ipaddress={self.device_ip},port={self.device_port},timeout=4000,passwd=".encode('utf-8')
#         self.access_handle = plcommpro.Connect(params)
#         if not self.access_handle:
#             error_code = ctypes.get_last_error()
#             self.log_message(f"Failed to connect to access control device. Error code: {error_code}")
#         else:
#             self.log_message("Connected to access control device.")
#
#     def open_device(self):
#         self.log_message("Initializing SDK and opening the device...")
#         if self.sensor.Init() == 0:
#             self.device_handle = self.sensor.OpenDevice(0)
#             if self.device_handle != 0:
#                 self.log_message("Device opened successfully.")
#                 self.db_handle = self.sensor.DBInit()
#                 if self.db_handle != 0:
#                     self.log_message("Database initialized successfully.")
#                     self.fpWidth = self.get_device_param(1)
#                     self.fpHeight = self.get_device_param(2)
#                 else:
#                     self.log_message("Failed to initialize database.")
#             else:
#                 self.log_message("Failed to open device.")
#         else:
#             self.log_message("SDK initialization failed.")
#
#     def get_device_param(self, param_code):
#         param_value = jpype.JArray(JByte)(4)
#         size = jpype.JArray(JInt)([4])
#         self.sensor.GetParameters(self.device_handle, param_code, param_value, size)
#         return int.from_bytes(param_value[:4], byteorder='little')
#
#
#     def capture_fingerprint(self, success_callback, failure_callback, timeout=60):
#         self.reinitialize_sensor()  # Reinitialize before starting capture
#
#         """Continuously check for fingerprint input until successful or timeout."""
#         self.log_message("Waiting for the user to place their finger on the sensor...")
#         start_time = time.time()  # Record the start time
#         img_buffer = jpype.JArray(JByte)(self.fpWidth * self.fpHeight)
#         template_buffer = jpype.JArray(JByte)(2048)
#         template_size = jpype.JArray(JInt)([2048])
#
#         while time.time() - start_time < timeout:  # Loop until timeout
#             if self.sensor.AcquireFingerprint(self.device_handle, img_buffer, template_buffer, template_size) == 0:
#                 self.log_message("Fingerprint captured successfully. Please remove your finger.")
#                 self.step += 1
#                 self.show_fingerprint_image(img_buffer)  # Show the captured fingerprint image
#                 success_callback(template_buffer[:template_size[0]])
#                 return  # Exit the function after successful capture
#             else:
#                 # Continue waiting for the user to place their finger
#                 time.sleep(0.5)  # Check again after a short delay
#
#         # If timeout is reached, trigger failure callback
#         self.log_message("Fingerprint capture timed out. Please try again.")
#         failure_callback()
#
#     def send_fingerprint_to_kafka(self, image):
#         # Convert the image to a base64-encoded string
#         buffered = BytesIO()
#         image.save(buffered, format="JPEG")
#         image_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
#         message = f"{image_base64} {self.gymbranchId} {self.userId} {self.step}"
#
#         # Send the message to Kafka
#         self.kafka_service.produce(topic="rt_fingerprint_capture", message=message)
#         self.log_message("Fingerprint image sent to Kafka topic.")
#
#     def show_fingerprint_image(self, img_buffer):
#         image_array = np.array(img_buffer).reshape((self.fpHeight, self.fpWidth))
#         image = Image.fromarray(image_array).convert("L")
#         image = image.resize((256, 288), Image.LANCZOS)
#         self.send_fingerprint_to_kafka(image)
#
#
#
#     def enroll_capture_step(self, step):
#         if step <= 3:
#             self.log_message(f"Capture {step}: Place your finger on the sensor.")
#             self.capture_fingerprint(
#                 success_callback=lambda template: self.enroll_capture_success(step, template),
#                 failure_callback=lambda: self.retry_enroll(step)
#             )
#         else:
#             self.merge_templates(self.fingerId)
#
#     def enroll_capture_success(self, step, template):
#         self.templates.append(template)
#         self.log_message(f"Capture {step} successful. Remove your finger.")
#         threading.Timer(2.0, lambda: self.enroll_capture_step(step + 1)).start()
#
#     def retry_enroll(self, step):
#         threading.Timer(1.0, lambda: self.enroll_capture_step(step)).start()
#
#     def merge_templates(self,finger_id):
#         if len(self.templates) == 3:
#             self.log_message("Merging captured fingerprints for enrollment.")
#             merged_template = jpype.JArray(JByte)(2048)
#             template_size = jpype.JArray(JInt)([2048])
#
#             if self.sensor.DBMerge(self.db_handle, self.templates[0], self.templates[1], self.templates[2],
#                                    merged_template, template_size) == 0:
#                 if self.sensor.DBAdd(self.db_handle, self.userId, merged_template) == 0:
#                     self.log_message("Fingerprint enrolled successfully.")
#                     self.add_and_authorize_user(self.userId, merged_template[:template_size[0]], "20241101", "20241231",finger_id)
#                     self.step = 0  # Reset step after successful enrollment
#
#                 else:
#                     self.log_message("Failed to enroll fingerprint in the database.")
#             else:
#                 self.log_message("Failed to merge fingerprint templates.")
#         else:
#             self.log_message("Enrollment failed due to insufficient captures.")
#
#     def free_sensor(self):
#         """Ensure proper cleanup of sensor resources."""
#         self.log_message("Releasing sensor resources...")
#
#         # Join any active threads
#         if hasattr(self, 'capture_thread') and self.capture_thread.is_alive():
#             self.log_message("Waiting for capture thread to terminate...")
#             self.capture_thread.join(timeout=1)
#
#         # Free database handle
#         if self.db_handle:
#             self.sensor.DBFree(self.db_handle)
#             self.db_handle = None
#             self.log_message("Database handle released.")
#
#         # Close device handle
#         if self.device_handle:
#             self.sensor.CloseDevice(self.device_handle)
#             self.device_handle = None
#             self.log_message("Device handle closed.")
#
#         # Terminate the SDK
#         self.sensor.Terminate()
#         self.log_message("Sensor SDK terminated.")
#
#     def reinitialize_sensor(self):
#         """Reinitialize the sensor for subsequent operations."""
#         self.log_message("Reinitializing sensor...")
#         self.free_sensor()  # Ensure resources from previous session are released
#         self.open_device()  # Reopen the device
#
#     def add_fingerprint(self, user_id, fingerprint_template, finger_id,main):
#         if not self.access_handle:
#             self.access_handle = self.connect_to_access_device()
#
#         buffer_size = 10 * 1024 * 1024  # Set a large buffer size
#         buffer = ctypes.create_string_buffer(buffer_size)
#         table_name = "user".encode('utf-8')  # Encode the table name
#         filter = f"Pin={user_id}\t".encode('utf-8')  # Set the filter condition by pin
#         options = "".encode('utf-8')  # No special options
#         field_names = "*".encode('utf-8')  # Récupérer tous les champs de la table
#         result = plcommpro.GetDeviceData(self.access_handle, buffer, buffer_size, table_name, field_names, filter, options)
#         if main == False:
#             fingerprint_template_base64 = fingerprint_template
#         else:
#             fingerprint_template_base64 = base64.b64encode(fingerprint_template).decode('utf-8')
#         if (result <= 0):
#             self.add_and_authorize_user(user_id, fingerprint_template, 20001021, 20021021,finger_id)
#         result = plcommpro.GetDeviceData(self.access_handle, buffer, buffer_size, b"templatev10", field_names, f"Pin={user_id}\tFingerID={finger_id}\t".encode('utf-8'), options)
#         print(result)
#         if result > 0:
#             print("already saved")
#         else:
#             template_data = (
#                 f"Size={len(fingerprint_template_base64)}\t"
#                 f"UID={user_id}\t"
#                 f"Pin={user_id}\t"
#                 f"FingerID={self.fingerId}\t"
#                 f"Valid=1\t"
#                 f"Template={fingerprint_template_base64}\t"
#                 f"Resverd=\t"
#                 f"EndTag="
#             ).encode("utf-8")
#
#             result_template = plcommpro.SetDeviceData(self.access_handle, b"templatev10", template_data, None)
#             if result_template == 0:
#                 message = f"{fingerprint_template_base64} {user_id}"
#                 if main==True:
#                     self.kafka_service.produce("rt_fingerprint_capture", message)
#                     self.log_message(f"Fingerprint template for FingerID {finger_id} saved successfully.")
#                     message2 = f"{self.fingerId} {self.gymbranchId} {self.userId}"
#                     self.kafka_service.produce(topic="rt_fingerprint_capture", message=message2)
#                     self.free_sensor()
#             else:
#                 self.log_message(
#                     f"Failed to save fingerprint template for FingerID {finger_id}. Error code: {result_template}")
#                 self.free_sensor()
#
#
#
#
#
#     def add_and_authorize_user(self, user_id, fingerprint_template, start_time, end_time, finger_id=1):
#         if not self.access_handle:
#             self.log_message("Cannot add user. Device is not connected.")
#             return
#
#         # Step 1: Check if the user record exists
#         user_data_check = f"Pin={user_id}".encode('utf-8')
#         result_user_exists = plcommpro.SetDeviceData(self.access_handle, b"user", user_data_check, None)
#
#         if result_user_exists == 0:  # User exists
#             self.log_message(f"User {user_id} exists. Checking for FingerID {finger_id}...")
#             self.db_handle = jpype.JLong(self.db_handle)
#
#             # Step 2: Check if fingerprint with specified FingerID is associated with this user
#             fid = jpype.JArray(JInt)([0])  # Array to store ID if fingerprint exists
#             score = jpype.JArray(JInt)([0])  # Array to store match score
#             result_fingerprint = self.sensor.DBIdentify(self.db_handle, fingerprint_template, fid, score)
#
#             if result_fingerprint == 0 and fid[0] == finger_id:
#                 self.log_message(f"User {user_id} already has a fingerprint with FingerID {finger_id}.")
#                 # Decide if you want to update the fingerprint or skip.
#             else:
#                 # Step 3: Add new fingerprint template for this FingerID if not present
#                 fingerprint_template_base64 = base64.b64encode(fingerprint_template).decode('utf-8')
#                 template_data = (
#                     f"Size={len(fingerprint_template)}\t"
#                     f"UID={user_id}\t"
#                     f"Pin={user_id}\t"
#                     f"FingerID={self.fingerId}\t"
#                     f"Valid=1\t"
#                     f"Template={fingerprint_template_base64}\t"
#                     f"Resverd=\t"
#                     f"EndTag="
#                 ).encode("utf-8")
#
#                 result_template = plcommpro.SetDeviceData(self.access_handle, b"templatev10", template_data, None)
#                 if (result_template == 0):
#                     print("added")
#
#         else:
#             # Step 5: If user doesn't exist, add user record
#             self.log_message(f"User {user_id} does not exist. Adding new user.")
#             user_data = f"Pin={user_id}\tStartTime={start_time}\tEndTime={end_time}\t".encode('utf-8')
#             result_add_user = plcommpro.SetDeviceData(self.access_handle, b"user", user_data, None)
#
#             if result_add_user == 0:
#                 self.log_message(f"User {user_id} added successfully.")
#                 self.add_and_authorize_user(user_id, fingerprint_template, start_time, end_time, finger_id)
#             else:
#                 self.log_message(f"Failed to add user. Error code: {result_add_user}")
#
#     def api_capture_fingerprint(self):
#         try:
#             self.log_message(f"API request received to capture fingerprint for user {self.userId}.")
#             self.open_device()
#             self.api_templates = []  # Reset templates for each API call
#             self.api_enroll_capture_step(step=1)  # Start capturing
#             return jsonify({"status": "Fingerprint capturing started for user.", "user_id": self.userId})
#         except Exception as e:
#             self.log_message(f"Error capturing fingerprint for user {self.userId}: {str(e)}")
#             return jsonify({"status": "error", "message": str(e)}), 500  # Return error response
#
#     def api_enroll_capture_step(self, step):
#         if step <= 3:
#             self.log_message(f"API Capture {step}: Place your finger on the sensor for user {self.userId}.")
#             self.capture_fingerprint(
#                 success_callback=lambda template: self.api_enroll_capture_success(step, template),
#                 failure_callback=lambda: self.log_message(f"API capture step {step} failed for user {self.userId}.")
#             )
#         else:
#
#             self.api_merge_templates(self.userId)  # Merge after 3 captures
#
#     def api_enroll_capture_success(self, step, template):
#         self.api_templates.append(template)
#         self.log_message(f"API Capture {step} successful for user {self.userId}. Remove your finger.")
#         if step < 3:
#             threading.Timer(2.0, lambda: self.api_enroll_capture_step(step + 1)).start()
#         else:
#             self.api_merge_templates(self.userId)
#
#     def api_merge_templates(self, user_id):
#         if len(self.api_templates) == 3:
#             self.log_message("Merging API captured fingerprints for enrollment.")
#             merged_template = jpype.JArray(JByte)(2048)
#             template_size = jpype.JArray(JInt)([2048])
#
#             if self.sensor.DBMerge(self.db_handle, self.api_templates[0], self.api_templates[1], self.api_templates[2],
#                                    merged_template, template_size) == 0:
#                 if self.sensor.DBAdd(self.db_handle, user_id, merged_template) == 0:
#                     self.log_message("Fingerprint enrolled successfully for user through API.")
#                     self.add_fingerprint(user_id, merged_template[:template_size[0]], self.fingerId,True)
#                     self.step = 0
#
#                 else:
#                     self.log_message("Failed to enroll fingerprint in the database.")
#                     self.free_sensor()
#             else:
#                 self.log_message("Failed to merge fingerprint templates.")
#                 self.free_sensor()
#         else:
#             self.log_message("API enrollment failed due to insufficient captures.")
#             self.free_sensor()
#
#
#
#
