"""
ShopMate Pi Camera barcode scanner.

Uses the Raspberry Pi CSI camera explicitly as Picamera2 camera 0.

Behavior:
- A barcode fires once when it first appears.
- Holding the same barcode in front of the camera does not repeatedly add it.
- Remove it for GONE_FRAMES frames, then show it again to add another unit.
- Tries raw RGB, grayscale, and contrast-enhanced grayscale decoding.

The USB following webcam is NOT used by this scanner.
"""
import io
import threading
import time

import cv2
from PIL import Image
from pyzbar.pyzbar import decode as zbar_decode


GONE_FRAMES = 8
SCAN_FPS = 8
DEFAULT_CAMERA_SIZE = (1920, 1080)


class BarcodeScanner:
    def __init__(
        self,
        on_event,
        camera_size=DEFAULT_CAMERA_SIZE,
        fps=SCAN_FPS,
        camera_num=0,
    ):
        self.on_event = on_event
        self.camera_size = camera_size
        self.frame_interval = 1.0 / float(fps)
        self.camera_num = camera_num

        self._stop = threading.Event()
        self._thread = None
        self._visible_since_gone = {}
        self._currently_present = set()

        self._latest_jpeg = None
        self._latest_jpeg_lock = threading.Lock()

        self._cart = None

    def start(self, cart):
        self._cart = cart
        self._stop.clear()

        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

        if self._thread:
            self._thread.join(timeout=3)

    def _run(self):
        try:
            from picamera2 import Picamera2
        except ImportError:
            print(
                "[billing] ERROR: picamera2 is not installed. "
                "Billing scanner will not start.",
                flush=True,
            )
            return

        # Explicitly use CSI/Pi camera 0.
        picam2 = Picamera2(self.camera_num)

        config = picam2.create_video_configuration(
            main={
                "size": self.camera_size,
                "format": "RGB888",
            }
        )

        picam2.configure(config)
        picam2.start()

        print(
            f"[billing] Pi camera {self.camera_num} started "
            f"at {self.camera_size}",
            flush=True,
        )

        # Give auto-exposure/white-balance a moment to settle.
        time.sleep(1.5)

        try:
            while not self._stop.is_set():
                started = time.monotonic()

                frame = picam2.capture_array()
                self._process_frame(frame)

                elapsed = time.monotonic() - started
                sleep_for = self.frame_interval - elapsed

                if sleep_for > 0:
                    time.sleep(sleep_for)

        except Exception as exc:
            print(
                f"[billing] scanner error: {exc!r}",
                flush=True,
            )

        finally:
            try:
                picam2.stop()
            except Exception:
                pass

    def get_latest_jpeg(self):
        with self._latest_jpeg_lock:
            return self._latest_jpeg

    def _update_preview(self, frame):
        try:
            buf = io.BytesIO()

            Image.fromarray(frame).save(
                buf,
                format="JPEG",
                quality=75,
            )

            with self._latest_jpeg_lock:
                self._latest_jpeg = buf.getvalue()

        except Exception as exc:
            print(
                f"[billing] preview encode failed: {exc}",
                flush=True,
            )

    @staticmethod
    def _decode_frame(frame):
        """
        Try multiple simple representations. This helps the OV5647 when
        barcodes have moderate glare or low contrast.
        """
        attempts = [frame]

        try:
            gray = cv2.cvtColor(
                frame,
                cv2.COLOR_RGB2GRAY,
            )
            attempts.append(gray)

            equalized = cv2.equalizeHist(gray)
            attempts.append(equalized)

        except Exception:
            pass

        seen_payloads = set()
        decoded_unique = []

        for candidate in attempts:
            try:
                decoded = zbar_decode(candidate)
            except Exception:
                continue

            for item in decoded:
                payload = bytes(item.data)

                if payload in seen_payloads:
                    continue

                seen_payloads.add(payload)
                decoded_unique.append(item)

        return decoded_unique

    def _process_frame(self, frame):
        self._update_preview(frame)

        decoded = self._decode_frame(frame)
        seen_this_frame = set()

        for item in decoded:
            barcode = item.data.decode(
                "utf-8",
                errors="ignore",
            ).strip()

            if not barcode:
                continue

            seen_this_frame.add(barcode)
            self._visible_since_gone[barcode] = 0

            if barcode not in self._currently_present:
                self._currently_present.add(barcode)

                print(
                    f"[billing] barcode detected: {barcode}",
                    flush=True,
                )

                self._fire_add(barcode)

        for barcode in list(self._currently_present):
            if barcode in seen_this_frame:
                continue

            gone_count = (
                self._visible_since_gone.get(
                    barcode,
                    0,
                )
                + 1
            )

            self._visible_since_gone[barcode] = gone_count

            if gone_count >= GONE_FRAMES:
                self._currently_present.discard(barcode)
                self._visible_since_gone.pop(
                    barcode,
                    None,
                )

    def _fire_add(self, barcode):
        if self._cart is None:
            return

        status, payload = self._cart.add_barcode(
            barcode
        )

        self.on_event(
            status,
            payload,
        )
