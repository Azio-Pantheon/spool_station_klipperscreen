"""Spool registration station home panel (station mode).

Shown as the root panel when the connected ``[printer]`` section has
``device_type: spool_station``.  It listens permanently for an HID
(keyboard-wedge) barcode scanner, forwards every code to Moonraker's
``server.spool_station.scan`` RPC and mirrors the station status pushed as
``notify_spool_station_status``.  Moonraker owns the state machine (idle ->
awaiting_batch -> registering) and talks to fleet_daemon; this panel only
displays it.

Labels only, no dialogs: a separate toplevel would steal the scanner's key
events from the window handler that feeds ``handle_key_press``.
"""
import logging
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango
from ks_includes.screen_panel import ScreenPanel

# Same-code debounce: only meant to swallow a scanner double-trigger (the same
# barcode decoded twice within a few hundred ms).
SCAN_DEBOUNCE_SECONDS = 0.5
SCAN_BUFFER_MAX = 256

esc = GLib.markup_escape_text


class Panel(ScreenPanel):

    def __init__(self, screen, title):
        super().__init__(screen, title)
        # show_panel(remove_all=True) re-runs __init__ on a cached panel after
        # deactivate(); make sure nothing from a previous life keeps running.
        if getattr(self, "_tick_id", None) is not None:
            GLib.source_remove(self._tick_id)
        if getattr(self, "_serial", None) is not None:
            self._serial.stop()
        self.status = {}
        self.scan_buffer = ""
        self._last_scan = (None, 0.0)
        self._tick_id = None
        self._serial = None
        self._qr_deadline = None   # monotonic time at which the armed QR expires
        self._component_error_shown = False
        self.buttons = {}

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5, hexpand=True, vexpand=True)
        main.pack_start(self._build_header(), False, False, 0)
        main.pack_start(self._build_centre(), True, True, 0)
        main.pack_end(self._build_buttons(), False, False, 0)
        self.content.add(main)

        self._paint_header()
        self._paint_state()
        self._paint_hint()
        self._paint_result(None)
        self.buttons["cancel"].set_sensitive(False)

    # ── Layout ───────────────────────────────────────────────────────────

    def _build_header(self):
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5, hexpand=True, vexpand=False)
        self.labels["fleet"] = Gtk.Label(valign=Gtk.Align.CENTER)
        self.labels["fleet"].get_style_context().add_class("station-badge")
        self.labels["host"] = Gtk.Label(halign=Gtk.Align.START, xalign=0, valign=Gtk.Align.CENTER,
                                        ellipsize=Pango.EllipsizeMode.END)
        self.labels["filament"] = Gtk.Label(halign=Gtk.Align.END, xalign=1, hexpand=True, valign=Gtk.Align.CENTER,
                                            ellipsize=Pango.EllipsizeMode.END)
        self.labels["filament"].get_style_context().add_class("station-sub")
        header.pack_start(self.labels["fleet"], False, False, 0)
        header.pack_start(self.labels["host"], False, False, 0)
        header.pack_end(self.labels["filament"], True, True, 0)
        return header

    def _build_centre(self):
        centre = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True, vexpand=True,
                         valign=Gtk.Align.CENTER)
        for key, style in (("state", "station-state"), ("hint", "station-sub"), ("result", None)):
            label = Gtk.Label(hexpand=True, halign=Gtk.Align.CENTER, justify=Gtk.Justification.CENTER)
            label.set_line_wrap(True)
            label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            if style:
                label.get_style_context().add_class(style)
            self.labels[key] = label
            centre.pack_start(label, False, False, 0)
        return centre

    def _build_buttons(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5, hexpand=True, vexpand=False,
                      homogeneous=True)
        specs = (
            ("cancel", "cancel", _("Cancel"), self._on_cancel),
            ("refresh", "refresh", _("Refresh"), self._on_refresh),
            ("network", "network", _("Network"), self._on_network),
            ("system", "settings", _("System"), self._on_system),
        )
        for key, icon, label, handler in specs:
            button = self._gtk.Button(icon, label, "color1", scale=self.bts)
            button.set_vexpand(False)
            button.connect("clicked", handler)
            self.buttons[key] = button
            bar.pack_start(button, True, True, 0)
        return bar

    # ── Lifecycle ────────────────────────────────────────────────────────

    def activate(self):
        self.request_status()
        if self._tick_id is None:
            self._tick_id = GLib.timeout_add_seconds(1, self._tick)
        self._start_serial()

    def deactivate(self):
        if self._tick_id is not None:
            GLib.source_remove(self._tick_id)
            self._tick_id = None
        self._stop_serial()

    def _tick(self):
        if self._qr_deadline is not None:
            self._paint_state()
        return True

    # ── Status from Moonraker ────────────────────────────────────────────

    def _ws_ready(self):
        ws = self._screen._ws
        return ws is not None and ws.connected

    def request_status(self):
        if self._ws_ready():
            self._screen._ws.klippy.spool_station_status(self._on_status)
        return False

    def _on_status(self, response, method, params, *args):
        error = self._error_message(response)
        if error:
            logging.warning(f"{method} failed: {error}")
            if not self._component_error_shown:
                self._component_error_shown = True
                self._show_error(f"{_('Spool station component unavailable')}: {error}")
            return
        result = response.get("result")
        if isinstance(result, dict):
            self._apply_status(result)

    def process_update(self, action, data):
        if action == "notify_spool_station_status" and isinstance(data, dict):
            self._apply_status(data)

    def _apply_status(self, result):
        if not isinstance(result, dict):
            return
        self.status = result
        state = self._state()
        if state == "awaiting_batch":
            try:
                remaining = int(result.get("qr_remaining") or 0)
            except (TypeError, ValueError):
                remaining = 0
            self._qr_deadline = time.monotonic() + max(remaining, 0)
        else:
            self._qr_deadline = None
        self._paint_header()
        self._paint_state()
        self._paint_hint()
        self._paint_result(result.get("last_result"))
        self.buttons["cancel"].set_sensitive(state != "idle")

    def _state(self):
        return str(self.status.get("state") or "idle")

    def _remaining(self):
        if self._qr_deadline is None:
            return 0
        return max(0, int(round(self._qr_deadline - time.monotonic())))

    # ── Painting ─────────────────────────────────────────────────────────

    def _paint_header(self):
        fleet_on = bool(self.status.get("fleet_connected"))
        fleet = self.labels["fleet"]
        fleet.set_text(_("Fleet ON") if fleet_on else _("Fleet OFF"))
        ctx = fleet.get_style_context()
        ctx.remove_class("station-fleet-off" if fleet_on else "station-fleet-on")
        ctx.add_class("station-fleet-on" if fleet_on else "station-fleet-off")

        self.labels["host"].set_text(str(self.status.get("station_hostname") or ""))

        filament = self.status.get("filament")
        if isinstance(filament, dict) and filament.get("name"):
            parts = [str(filament.get("vendor_name") or "").strip(), str(filament.get("name") or "").strip()]
            text = " ".join(p for p in parts if p)
            material = str(filament.get("material") or "").strip()
            if material:
                text = f"{text} ({material})"
            self.labels["filament"].set_markup(f"{esc(_('Filament'))}: <b>{esc(text)}</b>")
        else:
            self.labels["filament"].set_text(_("No filament selected — choose one on the station page"))

    def _paint_state(self):
        state = self._state()
        label = self.labels["state"]
        if state == "awaiting_batch":
            qr = esc(str(self.status.get("pending_qr") or ""))
            second = esc(_("Scan the batch number on the phone (expires in %ds)") % self._remaining())
            label.set_markup(f"{esc(_('QR'))} <b>{qr}</b> {esc(_('armed'))}\n{second}")
        elif state == "registering":
            label.set_text(_("Registering…"))
        else:
            label.set_text(_("Scan a spool QR"))

    def _paint_hint(self):
        label = self.labels["hint"]
        if self.scan_buffer:
            label.set_markup(f"{esc(_('Input'))}: <tt>{esc(self.scan_buffer)}</tt>")
            return
        state = self._state()
        if state == "awaiting_batch":
            label.set_text(_("Scanning another spool QR here replaces the pending one"))
        elif state == "registering":
            label.set_text("")
        else:
            label.set_text(_("USB scanner ready"))

    def _paint_result(self, last):
        label = self.labels["result"]
        ctx = label.get_style_context()
        ctx.remove_class("station-result-ok")
        ctx.remove_class("station-result-err")
        if not isinstance(last, dict):
            label.set_text("")
            return
        ok = bool(last.get("ok"))
        spool = last.get("spool") if isinstance(last.get("spool"), dict) else None
        if ok and spool:
            filament = self.status.get("filament")
            name = spool.get("filament_name") or (filament.get("name") if isinstance(filament, dict) else None)
            text = f"{_('Spool')} #{spool.get('id')} · {name or ''} · {_('lot')} {spool.get('lot_nr') or ''}"
        else:
            text = str(last.get("message") or ("OK" if ok else _("Error")))
        ctx.add_class("station-result-ok" if ok else "station-result-err")
        label.set_text(text)

    def _show_error(self, text):
        self._paint_result({"ok": False, "message": text})

    # ── Scanner input (HID keyboard wedge, always listening) ─────────────

    def handle_key_press(self, event):
        """Consume barcode-scanner keystrokes. Returns True when the event was consumed."""
        keyval = event.keyval
        keyval_name = Gdk.keyval_name(keyval)
        if keyval_name in ("Return", "KP_Enter", "Tab"):
            code = self.scan_buffer.strip()
            self.scan_buffer = ""
            self._paint_hint()
            if code:
                self.submit_scan(code, "scanner")
            return True
        if keyval_name == "BackSpace":
            self.scan_buffer = self.scan_buffer[:-1]
            self._paint_hint()
            return True
        if keyval_name == "Escape":
            self.scan_buffer = ""
            self._paint_hint()
            return False  # Let Escape propagate (go home)
        char = chr(keyval) if 32 <= keyval < 127 else Gdk.keyval_to_unicode(keyval)
        if isinstance(char, int):
            char = chr(char) if char > 0 else ""
        if char and char.isprintable():
            self.scan_buffer = (self.scan_buffer + char)[-SCAN_BUFFER_MAX:]
            self._paint_hint()
            return True
        return False

    def submit_scan_threadsafe(self, code):
        """Entry point for non-GTK threads (serial scanner)."""
        GLib.idle_add(self.submit_scan, code, "serial")

    def submit_scan(self, code, source="scanner"):
        code = (code or "").strip()
        if not code:
            return False
        now = time.monotonic()
        last_code, last_time = self._last_scan
        if code == last_code and now - last_time < SCAN_DEBOUNCE_SECONDS:
            logging.debug(f"Spool station scan ignored (debounce): {code}")
            return False
        self._last_scan = (code, now)
        logging.info(f"Spool station scan ({source}): {code}")
        self._screen.close_screensaver()
        if hasattr(self._screen, "reset_screensaver_timeout"):
            self._screen.reset_screensaver_timeout()
        # Every code from this screen is a spool QR for Moonraker (source
        # "scanner"); batch numbers come from the phone page (source "phone").
        sent = False
        if self._ws_ready():
            sent = self._screen._ws.klippy.spool_station_scan(code, "scanner", self._on_scan_response, code)
        if sent is False:
            self._show_error(_("Not connected to Moonraker"))
        return False

    def _on_scan_response(self, response, method, params, code, *args):
        error = self._error_message(response)
        if error:
            self._show_error(f"{code}: {error}")
            self.request_status()
            return
        result = response.get("result")
        if isinstance(result, dict):
            self._apply_status(result)
        else:
            self.request_status()

    @staticmethod
    def _error_message(response):
        if not isinstance(response, dict):
            return _("No response")
        error = response.get("error")
        if error is None:
            return None
        if isinstance(error, dict):
            return str(error.get("message") or error)
        return str(error)

    # ── Buttons ──────────────────────────────────────────────────────────

    def _on_cancel(self, widget):
        if not self._ws_ready():
            self._show_error(_("Not connected to Moonraker"))
            return
        if self._screen._ws.klippy.spool_station_cancel(self._on_status) is False:
            self._show_error(_("Not connected to Moonraker"))

    def _on_refresh(self, widget):
        self._component_error_shown = False
        self.request_status()

    def _on_network(self, widget):
        self._screen.show_panel("network", _("Network"))

    def _on_system(self, widget):
        self._screen.show_panel("system", _("System"))

    # ── Optional serial scanner ──────────────────────────────────────────

    def _start_serial(self):
        if self._serial is not None or not self._screen.connected_printer:
            return
        cfg = self._config.get_printer_config(self._screen.connected_printer)
        if not cfg:
            return
        port = (cfg.get("scanner_serial_port", "") or "").strip('" ')
        if not port:
            return
        try:
            baud = int(float(cfg.get("scanner_serial_baud", "9600") or 9600))
        except ValueError:
            baud = 9600
        try:
            from ks_includes.scanner_serial import SerialScannerThread
            self._serial = SerialScannerThread(port, baud, self.submit_scan_threadsafe)
            self._serial.start()
        except Exception as e:
            logging.error(f"Unable to start serial scanner on {port}: {e}")
            self._serial = None

    def _stop_serial(self):
        if self._serial is not None:
            self._serial.stop()
            self._serial = None
