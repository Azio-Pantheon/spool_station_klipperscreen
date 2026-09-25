"""Optional serial barcode-scanner reader for oven screens.

HID (keyboard-wedge) scanners need nothing here: they type into the window and
``panels/oven.py`` consumes the key events.  This thread is only started when a
``[printer]`` section sets ``scanner_serial_port`` and covers scanners that
present as a serial device instead.  ``pyserial`` is imported lazily so screens
without a serial scanner do not need it installed (see the commented line in
``scripts/KlipperScreen-requirements.txt``).
"""
import logging
import threading


class SerialScannerThread(threading.Thread):
    """Read newline-terminated codes from ``port`` and pass each one to ``on_code``.

    ``on_code(code)`` is invoked from this background thread; GTK consumers must
    hop to the main loop themselves (``GLib.idle_add``).  The port is reopened
    automatically after errors (unplugged scanner) until ``stop()`` is called.
    """

    def __init__(self, port, baud=9600, on_code=None, retry_seconds=5.0):
        super().__init__(name=f"SerialScanner({port})", daemon=True)
        self.port = port
        self.baud = int(baud)
        self.on_code = on_code
        self.retry_seconds = float(retry_seconds)
        self._stop_event = threading.Event()
        self._conn = None

    def stop(self):
        self._stop_event.set()
        conn = self._conn
        if conn is not None:
            try:
                conn.close()
            except Exception as e:
                logging.debug(f"Serial scanner close: {e}")

    def run(self):
        try:
            import serial
        except ImportError:
            logging.error(
                f"scanner_serial_port is set to {self.port} but pyserial is not installed "
                "(pip install pyserial); serial scanner disabled"
            )
            return
        while not self._stop_event.is_set():
            try:
                with serial.Serial(self.port, self.baud, timeout=1) as conn:
                    self._conn = conn
                    logging.info(f"Serial scanner listening on {self.port} @ {self.baud}")
                    while not self._stop_event.is_set():
                        raw = conn.readline()
                        if not raw:
                            continue
                        code = raw.decode("utf-8", errors="ignore").strip()
                        if code and self.on_code is not None:
                            self.on_code(code)
            except Exception as e:
                if self._stop_event.is_set():
                    break
                logging.warning(f"Serial scanner {self.port}: {e}; retrying in {self.retry_seconds:.0f}s")
                self._stop_event.wait(self.retry_seconds)
            finally:
                self._conn = None
        logging.info(f"Serial scanner on {self.port} stopped")
