import base64
import io

from pyzkfp import ZKFP2
import time
import os
from PIL import Image
import numpy as np

from services.websocket import send_fingerprint


class FingerprintCapture:
    def __init__(self):
        self.zk = None

    def initialize_device(self):
        """Initialize the fingerprint device"""
        try:
            self.zk = ZKFP2()
            self.zk.Init()

            if self.zk.GetDeviceCount() < 1:
                raise Exception("No fingerprint device detected!")

            self.zk.OpenDevice(0)
            print(" Fingerprint device initialized successfully")
            return True

        except Exception as e:
            print(f" Error initializing device: {e}")
            return False

    def capture_single_template(self, attempt_num):
        """Capture a single fingerprint template and image"""
        print(f"\nScan {attempt_num}/3 ...")
        print("Place your finger on the scanner...")

        max_attempts = 30
        attempts = 0

        while attempts < max_attempts:
            try:
                capture = self.zk.AcquireFingerprint()
                if capture:
                    template, img = capture
                    print(f" Captured template {attempt_num}")
                    return template, img
            except Exception as e:
                print(f" Capture attempt failed: {e}")

            time.sleep(1)
            attempts += 1
            if attempts % 5 == 0:
                print(f"Still waiting... ({attempts}/{max_attempts})")

        raise Exception(f"Failed to capture template {attempt_num} after {max_attempts} attempts")

    def merge_templates(self, templates):
        """Merge three templates into a final template"""
        try:
            merge_result = self.zk.DBMerge(templates[0], templates[1], templates[2])
            if isinstance(merge_result, tuple):
                if len(merge_result) >= 2:
                    final_template, result_code  = merge_result[0], merge_result[1]
                    return final_template
            else:
                return merge_result
        except Exception as e:
            print(f" Error merging templates: {e}")
            print(" Using first template as fallback")
            return templates[0]

    def save_template(self, template, filename="fingerprint_final.tpl"):
        """Save template to file"""
        try:
            if isinstance(template, tuple):
                template = template[0] if len(template) > 0 else b''

            if not isinstance(template, bytes):
                if hasattr(template, 'encode'):
                    template = template.encode()
                else:
                    template = bytes(template)

            with open(filename, "wb") as f:
                f.write(template)

            file_size = os.path.getsize(filename)
            print(f" Final merged template saved as {filename} ({file_size} bytes)")
            return filename

        except Exception as e:
            print(f" Error saving template: {e}")
            return None

    def save_image(self, image_data, filename="fingerprint.bmp"):
        try:
            img_array = np.frombuffer(image_data, dtype=np.uint8)
            width, height = 300, 375
            if img_array.size != width * height:
                side = int(np.sqrt(img_array.size))
                width, height = side, side

            img_array = img_array.reshape((height, width))
            img = Image.fromarray(img_array)
            img.save(filename)
            print(f"Fingerprint image saved as {filename}")
            return filename
        except Exception as e:
            print(f" Error saving image: {e}")
            return None

    def capture_fingerprint(self, save_file=True, template_filename="fingerprint_final.tpl"):
        """
        Capture fingerprint 3 times, merge templates, save final template and images
        Returns:
            tuple: (final_template, image_filenames)
        """
        if not self.initialize_device():
            return None, None

        try:
            templates = []
            images = []
            image_files = []

            print("Please scan the same finger 3 times")
            print("Make sure to place your finger properly on the scanner each time")

            for i in range(3):
                template, img = self.capture_single_template(i + 1)
                templates.append(template)
                img_file = self.save_image(img, f"fingerprint_{i + 1}.bmp")
                image_files.append(img_file)
                pil_img = Image.frombytes('L', (300 , 375), img)  # 'L' = 8bit grayscale

                # Save PIL image to bytes buffer as BMP
                buffer = io.BytesIO()
                pil_img.save(buffer, format="BMP")
                img_bytes = buffer.getvalue()

                # Convert to base64
                img_b64 = base64.b64encode(img_bytes).decode('utf-8')

                # Send fingerprint step
                data = {"step": i + 1, "fingerprint": img_b64}
                send_fingerprint(data, "1003")

                if i < 2:
                    print("Please lift your finger and prepare for next scan...")
                    time.sleep(2)
            print("\n Merging templates...")
            final_template = self.merge_templates(templates)

            if final_template:
                print("Templates merged successfully!")

                if save_file:
                    self.save_template(final_template, template_filename)

                return final_template, image_files
            else:
                print("Failed to merge templates")
                return None, None

        except Exception as e:
            print(f" Error during fingerprint capture: {e}")
            return None, None

        finally:
            self.cleanup()

    def cleanup(self):
        """Clean up device resources"""
        try:
            if self.zk:
                self.zk.CloseDevice()
                self.zk.Terminate()
                print(" Device closed successfully")
        except Exception as e:
            print(f" Error during cleanup: {e}")
