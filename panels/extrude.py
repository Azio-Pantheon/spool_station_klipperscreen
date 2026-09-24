import json
import logging
import re
import os
import socket
import threading
from datetime import datetime, timezone
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango, Gdk, GLib
import requests
from ks_includes.KlippyGcodes import KlippyGcodes
from ks_includes.screen_panel import ScreenPanel
from ks_includes.widgets.autogrid import AutoGrid

SPOOL_PENDING_FILE = "/home/hs3/printer_data/backup/klipperscreen_spool_pending.json"

# Materials already offered by the hardcoded preset buttons in the "Select
# Filament Type" dialog (upper-cased). Fleet catalog materials matching one of
# these are NOT added a second time; the two PA-GF presets collapse to PA-GF.
FLEET_PRESET_MATERIALS = {"PETG-CF", "PA-CF", "TPU", "PA-GF"}
# Hardcoded preset labels that Moonraker knows as the single type "PA-GF".
PA_GF_PRESET_LABELS = ("PA-GF (Natural)", "PA-GF (Dark Grey)")


class Panel(ScreenPanel):

    def __init__(self, screen, title, shared_printer_config):
        super().__init__(screen, title)
        self.current_extruder = self._printer.get_stat("toolhead", "extruder")
        macros = self._printer.get_config_section_list("gcode_macro ")
        self.load_filament = any("LOAD_FILAMENT" in macro.upper() for macro in macros)
        self.unload_filament = any("UNLOAD_FILAMENT" in macro.upper() for macro in macros)

        self.shared_printer_config = shared_printer_config
        self.keyboard_visible = False
        self.has_spool_tracker = self.check_spool_tracker_availability()
        # Spoolman filament mapping
        self.spoolman_filament_mapping = {
            "PETG-CF": {"id": 1, "default_weight": 3000},
            "PA-CF": {"id": 2, "default_weight": 3000},
            "TPU": {"id": 3, "default_weight": 2000},
            "PA-GF (Natural)": {"id": 4, "default_weight": 3000},
            "PA-GF (Dark Grey)": {"id": 5, "default_weight": 3000}
        }

        # Fleet daemon QR scan state
        self.fleet_daemon_url = (
            self.ks_printer_cfg.get("fleet_daemon_url", "").strip('" ')
            if self.ks_printer_cfg else ""
        )
        self.qr_scan_active = False
        self.qr_scan_buffer = ""
        self._active_run_load_macro = False
        # Change detection for status pushes: Moonraker injects the nozzle and
        # spool fields into every toolhead update (several per second), so the
        # labels and the title are only rebuilt when a value actually changes.
        self._nozzle_state = {}
        self._last_remaining_weight = None
        self._last_title = None

        # Fleet filament catalog (GET {fleet_daemon_url}/spool/filaments).
        # Extra materials from fleet are appended to the hardcoded presets in
        # the filament selection dialog when fleet is configured AND reachable.
        # Fetched in the background (never blocks the dialog); the dialog uses
        # whatever the last successful fetch returned.
        self.fleet_filaments = []          # deduped extras in display order
        self.fleet_filaments_ok = False    # True only when the latest fetch succeeded
        self.fleet_filament_specs = {}     # label -> {"density", "diameter", "default_weight"}; never cleared
        self._fleet_catalog_fetching = False
        if self.fleet_daemon_url:
            self._start_fleet_filament_refresh()

        self.speeds = ['1', '2', '5', '25']
        self.distances = ['5', '10', '15', '25']
        if self.ks_printer_cfg is not None:
            dis = self.ks_printer_cfg.get("extrude_distances", '')
            if re.match(r'^[0-9,\s]+$', dis):
                dis = [str(i.strip()) for i in dis.split(',')]
                if 1 < len(dis) < 5:
                    self.distances = dis
            vel = self.ks_printer_cfg.get("extrude_speeds", '')
            if re.match(r'^[0-9,\s]+$', vel):
                vel = [str(i.strip()) for i in vel.split(',')]
                if 1 < len(vel) < 5:
                    self.speeds = vel

        self.distance = int(self.distances[1])
        self.speed = int(self.speeds[1])
        self.buttons = {
            'extrude': self._gtk.Button("extrude", _("Extrude"), "color4"),
            'retract': self._gtk.Button("retract", _("Retract"), "color1"),
            'load': self._gtk.Button("arrow-down", _("Load"), "color3"),
            'unload': self._gtk.Button("arrow-up", _("Unload"), "color2"),
            'temperature': self._gtk.Button("heat-up", _("Temperature"), "color4"),
            'spoolman': self._gtk.Button("spoolman", "Spoolman", "color3"),
            'set_filament': self._gtk.Button(),
            'set_nozzle': self._gtk.Button(),
        }

        self.update_button_labels()

        self.buttons['extrude'].connect("clicked", self.extrude, "+")
        self.buttons['retract'].connect("clicked", self.extrude, "-")
        self.buttons['load'].connect("clicked", self.load_unload, "+")
        self.buttons['unload'].connect("clicked", self.load_unload, "-")
        self.buttons['temperature'].connect("clicked", self.menu_item_clicked, {
            "name": "Temperature",
            "panel": "temperature"
        })
        self.buttons['spoolman'].connect("clicked", self.menu_item_clicked, {
            "name": "Spoolman",
            "panel": "spoolman"
        })
        self.buttons['set_filament'].connect("clicked", self.open_filament_selection)
        self.buttons['set_nozzle'].connect("clicked", self.open_nozzle_selection)
        self.load_filament_nozzle()

        xbox = Gtk.Box(homogeneous=True)
        limit = 4
        i = 0
        extruder_buttons = []
        for extruder in self._printer.get_tools():
            if self._printer.extrudercount == 1:
                self.labels[extruder] = self._gtk.Button("extruder", "")
                self.labels[extruder].set_sensitive(False)
            else:
                n = self._printer.get_tool_number(extruder)
                self.labels[extruder] = self._gtk.Button(f"extruder-{n}", f"T{n}")
                self.labels[extruder].connect("clicked", self.change_extruder, extruder)
            if extruder == self.current_extruder:
                self.labels[extruder].get_style_context().add_class("button_fake")
            if self._printer.extrudercount <= limit:
                xbox.add(self.labels[extruder])
                i += 1
            else:
                extruder_buttons.append(self.labels[extruder])
        if extruder_buttons:
            self.labels['extruders'] = AutoGrid(extruder_buttons, vertical=self._screen.vertical_mode)
            self.labels['extruders_menu'] = self._gtk.ScrolledWindow()
            self.labels['extruders_menu'].add(self.labels['extruders'])
        if self._printer.extrudercount > limit:
            changer = self._gtk.Button("toolchanger")
            changer.connect("clicked", self.load_menu, 'extruders', _('Extruders'))
            xbox.add(changer)
            self.labels["current_extruder"] = self._gtk.Button("extruder", "")
            xbox.add(self.labels["current_extruder"])
            self.labels["current_extruder"].connect("clicked", self.load_menu, 'extruders', _('Extruders'))
        if i < limit:
            xbox.add(self.buttons['temperature'])

        xbox.add(self.buttons['set_filament'])
        xbox.add(self.buttons['set_nozzle'])

        distgrid = Gtk.Grid()
        for j, i in enumerate(self.distances):
            self.labels[f"dist{i}"] = self._gtk.Button(label=i)
            self.labels[f"dist{i}"].connect("clicked", self.change_distance, int(i))
            ctx = self.labels[f"dist{i}"].get_style_context()
            ctx.add_class("horizontal_togglebuttons")
            if int(i) == self.distance:
                ctx.add_class("horizontal_togglebuttons_active")
            distgrid.attach(self.labels[f"dist{i}"], j, 0, 1, 1)

        speedgrid = Gtk.Grid()
        for j, i in enumerate(self.speeds):
            self.labels[f"speed{i}"] = self._gtk.Button(label=i)
            self.labels[f"speed{i}"].connect("clicked", self.change_speed, int(i))
            ctx = self.labels[f"speed{i}"].get_style_context()
            ctx.add_class("horizontal_togglebuttons")
            if int(i) == self.speed:
                ctx.add_class("horizontal_togglebuttons_active")
            speedgrid.attach(self.labels[f"speed{i}"], j, 0, 1, 1)

        distbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.labels['extrude_dist'] = Gtk.Label(_("Distance (mm)"))
        distbox.pack_start(self.labels['extrude_dist'], True, True, 0)
        distbox.add(distgrid)
        speedbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.labels['extrude_speed'] = Gtk.Label(_("Speed (mm/s)"))
        speedbox.pack_start(self.labels['extrude_speed'], True, True, 0)
        speedbox.add(speedgrid)

        filament_sensors = self._printer.get_filament_sensors()
        sensors = Gtk.Grid(valign=Gtk.Align.CENTER, row_spacing=5, column_spacing=5)
        
        if len(filament_sensors) > 0:
            for s, x in enumerate(filament_sensors):
                if s > limit:
                    break
                name = x[23:].strip()
                self.labels[x] = {
                    'label': Gtk.Label(label=self.prettify(name), hexpand=True, halign=Gtk.Align.CENTER,
                                       ellipsize=Pango.EllipsizeMode.END),
                    'switch': Gtk.Switch(width_request=round(self._gtk.font_size * 2),
                                         height_request=round(self._gtk.font_size)),
                    'box': Gtk.Box()
                }
                self.labels[x]['switch'].connect("notify::active", self.enable_disable_fs, name, x)
                self.labels[x]['box'].pack_start(self.labels[x]['label'], True, True, 10)
                self.labels[x]['box'].pack_start(self.labels[x]['switch'], False, False, 0)
                self.labels[x]['box'].get_style_context().add_class("filament_sensor")
                sensors.attach(self.labels[x]['box'], s, 0, 1, 1)

        grid = Gtk.Grid(column_homogeneous=True)
        grid.attach(xbox, 0, 0, 4, 1)

        if self._screen.vertical_mode:
            grid.attach(self.buttons['extrude'], 0, 1, 2, 1)
            grid.attach(self.buttons['retract'], 2, 1, 2, 1)
            grid.attach(self.buttons['load'], 0, 2, 2, 1)
            grid.attach(self.buttons['unload'], 2, 2, 2, 1)
            grid.attach(distbox, 0, 3, 4, 1)
            grid.attach(speedbox, 0, 4, 4, 1)
            grid.attach(sensors, 0, 5, 4, 1)
        else:
            grid.attach(self.buttons['extrude'], 0, 2, 1, 1)
            grid.attach(self.buttons['load'], 2, 2, 1, 1)
            grid.attach(self.buttons['unload'], 3, 2, 1, 1)
            grid.attach(self.buttons['retract'], 1, 2, 1, 1)
            grid.attach(distbox, 0, 3, 2, 1)
            grid.attach(speedbox, 2, 3, 2, 1)
            grid.attach(sensors, 0, 4, 4, 1)

        self.menu = ['extrude_menu']
        self.labels['extrude_menu'] = grid
        self.content.add(self.labels['extrude_menu'])

    def back(self):
        if len(self.menu) > 1:
            self.unload_menu()
            return True
        return False

    def enable_buttons(self, enable):
        for button in self.buttons:
            if button in ("temperature", "spoolman"):
                continue
            self.buttons[button].set_sensitive(enable)

    def activate(self):
        # Another panel may have written the shared title label meanwhile, so
        # the next status push must be allowed to set it again.
        self._last_title = None
        self.enable_buttons(self._printer.state in ("ready", "paused"))
        if self.fleet_daemon_url:
            self._start_fleet_filament_refresh()

    def process_update(self, action, data):
        if action == "notify_gcode_response":
            if "action:cancel" in data or "action:paused" in data:
                self.enable_buttons(True)
            elif "action:resumed" in data:
                self.enable_buttons(False)
            return
        if action != "notify_status_update":
            return
        for x in self._printer.get_tools():
            if x in data:
                self.update_temp(
                    x,
                    self._printer.get_dev_stat(x, "temperature"),
                    self._printer.get_dev_stat(x, "target"),
                    self._printer.get_dev_stat(x, "power"),
                    lines=2,
                )
        if "current_extruder" in self.labels:
            self.labels["current_extruder"].set_label(self.labels[self.current_extruder].get_label())

        if ("toolhead" in data and "extruder" in data["toolhead"] and
                data["toolhead"]["extruder"] != self.current_extruder):
            for extruder in self._printer.get_tools():
                self.labels[extruder].get_style_context().remove_class("button_active")
            self.current_extruder = data["toolhead"]["extruder"]
            self.labels[self.current_extruder].get_style_context().add_class("button_active")
            if "current_extruder" in self.labels:
                n = self._printer.get_tool_number(self.current_extruder)
                self.labels["current_extruder"].set_image(self._gtk.Image(f"extruder-{n}"))

        th = data.get("toolhead") or {}
        nozzle_update = {k: th[k] for k in ("nozzle_type", "nozzle_size", "nozzle_life", "remaining_nozzle_life") if k in th}
        if nozzle_update and any(self._nozzle_state.get(k) != v for k, v in nozzle_update.items()):
            self._nozzle_state.update(nozzle_update)
            if "nozzle_type" in th:
                self.shared_printer_config.nozzle_type = th["nozzle_type"]
            if "nozzle_size" in th:
                self.shared_printer_config.nozzle = th["nozzle_size"]
            self.update_button_labels()  # also refreshes the title
        elif "remaining_weight" in th:
            try:
                remaining = round(float(th["remaining_weight"]), 1)
            except (TypeError, ValueError):
                remaining = None
            if remaining is not None and remaining != self._last_remaining_weight:
                self._last_remaining_weight = remaining
                self.refresh_title()
           
        for x in self._printer.get_filament_sensors():
            if x in data:
                if 'enabled' in data[x]:
                    self._printer.set_dev_stat(x, "enabled", data[x]['enabled'])
                    self.labels[x]['switch'].set_active(data[x]['enabled'])
                if 'filament_detected' in data[x]:
                    self._printer.set_dev_stat(x, "filament_detected", data[x]['filament_detected'])
                    if self._printer.get_stat(x, "enabled"):
                        if data[x]['filament_detected']:
                            self.labels[x]['box'].get_style_context().remove_class("filament_sensor_empty")
                            self.labels[x]['box'].get_style_context().add_class("filament_sensor_detected")
                        else:
                            self.labels[x]['box'].get_style_context().remove_class("filament_sensor_detected")
                            self.labels[x]['box'].get_style_context().add_class("filament_sensor_empty")
                logging.info(f"{x}: {self._printer.get_stat(x)}")

    def change_distance(self, widget, distance):
        logging.info(f"### Distance {distance}")
        self.labels[f"dist{self.distance}"].get_style_context().remove_class("horizontal_togglebuttons_active")
        self.labels[f"dist{distance}"].get_style_context().add_class("horizontal_togglebuttons_active")
        self.distance = distance

    def change_extruder(self, widget, extruder):
        logging.info(f"Changing extruder to {extruder}")
        for tool in self._printer.get_tools():
            self.labels[tool].get_style_context().remove_class("button_active")
        self.labels[extruder].get_style_context().add_class("button_active")
        self._screen._send_action(widget, "printer.gcode.script",
                                  {"script": f"T{self._printer.get_tool_number(extruder)}"})

    def change_speed(self, widget, speed):
        logging.info(f"### Speed {speed}")
        self.labels[f"speed{self.speed}"].get_style_context().remove_class("horizontal_togglebuttons_active")
        self.labels[f"speed{speed}"].get_style_context().add_class("horizontal_togglebuttons_active")
        self.speed = speed

    def extrude(self, widget, direction):
        self._screen._ws.klippy.gcode_script(KlippyGcodes.EXTRUDE_REL)
        self._screen._send_action(widget, "printer.gcode.script",
                                  {"script": f"G1 E{direction}{self.distance} F{self.speed * 60}"})

    def load_unload(self, widget, direction):
        if direction == "-":
            if not self.unload_filament:
                self._screen.show_popup_message("Macro UNLOAD_FILAMENT not found")
            else:
                self._screen._send_action(widget, "printer.gcode.script",
                                          {"script": f"UNLOAD_FILAMENT SPEED={self.speed * 60}"})
        if direction == "+":
            if not self.load_filament:
                self._screen.show_popup_message("Macro LOAD_FILAMENT not found")
            else:
                self.open_filament_selection(widget, run_load_macro=True)

    def enable_disable_fs(self, switch, gparams, name, x):
        if switch.get_active():
            self._printer.set_dev_stat(x, "enabled", True)
            self._screen._ws.klippy.gcode_script(f"SET_FILAMENT_SENSOR SENSOR={name} ENABLE=1")
            if self._printer.get_stat(x, "filament_detected"):
                self.labels[x]['box'].get_style_context().add_class("filament_sensor_detected")
            else:
                self.labels[x]['box'].get_style_context().add_class("filament_sensor_empty")
        else:
            self._printer.set_dev_stat(x, "enabled", False)
            self._screen._ws.klippy.gcode_script(f"SET_FILAMENT_SENSOR SENSOR={name} ENABLE=0")
            self.labels[x]['box'].get_style_context().remove_class("filament_sensor_empty")
            self.labels[x]['box'].get_style_context().remove_class("filament_sensor_detected")

    def open_filament_selection(self, widget, run_load_macro=False):
        # Hardcoded presets always come first.
        preset_types = ["PETG-CF", "PA-CF", "TPU", "PA-GF (Natural)", "PA-GF (Dark Grey)"]

        # Extra materials from the fleet filament catalog, only when fleet is
        # configured and the last fetch succeeded. Uses the cached result so
        # the dialog never waits on the network; kick a refresh for next time.
        fleet_extras = []
        if self.fleet_daemon_url:
            if self.fleet_filaments_ok:
                fleet_extras = list(self.fleet_filaments)   # snapshot
            self._start_fleet_filament_refresh()

        # (label, css class, kind) — presets, fleet extras, then Custom last so
        # "Custom" and the bottom-row "Scan Spool QR" stay the final two options.
        entries = [(t, "color1", "preset") for t in preset_types]
        entries += [(spec["label"], "color2", "fleet") for spec in fleet_extras]
        entries.append(("Custom", "color1", "custom"))

        # Create the dialog for selecting filament types
        dialog = ClickOutsideDialog(title="Select Filament Type",
                                    transient_for=widget.get_toplevel(),
                                    flags=Gtk.DialogFlags.MODAL)

        # Create a grid layout to place the buttons. Rows are NOT homogeneous:
        # each keeps its natural 150px(+margin) height so the grid can be
        # taller than the viewport and scroll, instead of stretching/collapsing.
        grid = Gtk.Grid()
        grid.set_column_homogeneous(True)
        grid.set_row_homogeneous(False)
        grid.set_valign(Gtk.Align.START)
        grid.set_column_spacing(10)
        grid.set_row_spacing(10)
        grid.set_margin_start(10)
        grid.set_margin_end(10)
        grid.set_margin_top(10)
        grid.set_margin_bottom(10)

        # Create buttons for each entry and add them to the grid
        for i, (label, css_class, kind) in enumerate(entries):
            button = Gtk.Button(label=label)
            button.get_style_context().add_class(css_class)
            button.set_size_request(150, 150)
            if kind == "fleet":
                # Fleet material names can be long: wrap to two lines, then ellipsize.
                lbl = button.get_child()
                if isinstance(lbl, Gtk.Label):
                    lbl.set_line_wrap(True)
                    lbl.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
                    lbl.set_justify(Gtk.Justification.CENTER)
                    lbl.set_lines(2)
                    lbl.set_ellipsize(Pango.EllipsizeMode.END)
                    lbl.set_max_width_chars(12)
            if kind == "custom":
                button.connect("clicked", self.open_custom_filament_dialog, dialog, run_load_macro)
            else:
                # Check if spool_tracker is enabled to decide workflow
                if self.has_spool_tracker:
                    button.connect("clicked", self.open_weight_entry_dialog, label, dialog, run_load_macro)
                else:
                    button.connect("clicked", self.set_filament_type_original, label, dialog, run_load_macro)
            grid.attach(button, i % 3, i // 3, 1, 1)  # Arrange buttons in 3 columns

        # Add bottom-row button (only if spool_tracker available)
        if self.has_spool_tracker:
            next_row = (len(entries) + 2) // 3  # Next row after filament buttons
            if self.fleet_daemon_url:
                # QR scan replaces "Update Weight Only" — QR lookup provides the weight
                qr_button = Gtk.Button(label="Scan Spool QR")
                qr_button.get_style_context().add_class("color4")
                qr_button.set_size_request(150, 150)
                qr_button.connect("clicked", self._on_qr_scan_clicked, dialog, run_load_macro)
                grid.attach(qr_button, 0, next_row, 3, 1)
            else:
                weight_button = Gtk.Button(label="Update Weight Only")
                weight_button.get_style_context().add_class("color3")
                weight_button.set_size_request(150, 150)
                weight_button.connect("clicked", self.open_weight_only_dialog, dialog)
                grid.attach(weight_button, 0, next_row, 3, 1)

        # Scrollable container. A ScrolledWindow (propagate_natural_height left
        # False) reports a minimum height independent of its child, so the
        # dialog's minimum no longer grows with the number of rows and the
        # explicit size below is honoured. Previously the bare grid's minimum
        # (3 rows ≈ 530px with button margins) overrode set_default_size and
        # the dialog ran off the screen.
        scroll = self._gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(grid)

        # Size the dialog to its content, capped to the screen.
        rows = (len(entries) + 2) // 3 + (1 if self.has_spool_tracker else 0)
        cell_h = 150 + 2 * int(round(0.3 * self._gtk.font_size))   # button CSS margin .3em top+bottom
        content_h = rows * cell_h + (rows - 1) * 10 + 20              # + row spacing + grid margins
        dialog_w = min(600, self._screen.width - 40)
        dialog_h = min(content_h, self._screen.height - 40)
        dialog.set_default_size(dialog_w, dialog_h)
        dialog.set_size_request(dialog_w, dialog_h)
        scroll.set_min_content_height(max(dialog_h - 20, 100))

        content_area = dialog.get_content_area()
        content_area.pack_start(scroll, True, True, 0)

        # Keep the upward nudge (the weight/custom dialogs position themselves
        # relative to this one) but clamp so the dialog always stays on screen.
        current_x, current_y = dialog.get_position()
        x = max(0, min(current_x, self._screen.width - dialog_w))
        y = max(0, min(current_y - 80, self._screen.height - dialog_h))
        dialog.move(x, y)

        dialog.show_all()

    def open_weight_entry_dialog(self, widget, filament_type, parent_dialog, run_load_macro=False):
        # Get the position of the parent dialog
        current_x, current_y = parent_dialog.get_position()
        
        # Close the parent dialog
        parent_dialog.destroy()

        # Get default weight for this filament type (preset, fleet material, or fallback)
        default_weight = self._default_weight_for(filament_type)

        # Get the toplevel window (parent window) for the dialog
        parent_window = widget.get_toplevel()
        if not isinstance(parent_window, Gtk.Window):
            parent_window = None

        # Create weight entry dialog
        weight_dialog = ClickOutsideDialog(
            title=f"Enter Weight for {filament_type}",
            transient_for=parent_window,
            flags=Gtk.DialogFlags.MODAL
        )
        weight_dialog.set_default_size(500, 500)
        weight_dialog.move(current_x + 100, current_y)

        # Store dialog reference and context for callbacks
        self.active_weight_dialog = weight_dialog
        self.active_filament_type = filament_type
        self.active_run_load_macro = run_load_macro

        # Create a vertical box layout
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        vbox.set_margin_start(20)
        vbox.set_margin_end(20)
        vbox.set_margin_top(20)
        vbox.set_margin_bottom(20)

        # Add instruction label
        instruction_label = Gtk.Label()
        instruction_label.set_markup(f'<span font="12">Enter weight for {filament_type} (grams)</span>')
        vbox.pack_start(instruction_label, False, False, 10)

        # Create the custom weight keypad
        from ks_includes.widgets.weight_keypad import WeightKeypad
        self.weight_keypad = WeightKeypad(
            self._screen, 
            self.process_weight_entry,   # Callback for when user confirms weight
            self.hide_weight_numpad      # Callback for when user cancels
        )
        
        # Set the initial value to the default weight
        self.weight_keypad.set_initial_value(default_weight)
        
        # Store preset info for replacing behavior
        self.preset_weight = str(default_weight)
        self.preset_active = True

        # Override the keypad's update_entry method to handle preset replacement
        original_update_entry = self.weight_keypad.update_entry
        
        def custom_update_entry(widget, action):
            if hasattr(self, 'preset_active') and self.preset_active:
                if action == 'B':
                    # Backspace on preset - clear the field
                    self.weight_keypad.labels['entry'].set_text("")
                    self.preset_active = False
                elif action not in ['E', 'C', 'CANCEL']:
                    # First digit/decimal pressed - replace preset with this input
                    if action == '.':
                        self.weight_keypad.labels['entry'].set_text("0.")
                    else:
                        self.weight_keypad.labels['entry'].set_text(action)
                    self.preset_active = False
                else:
                    # Enter, Clear, or Cancel with preset value - use original behavior
                    original_update_entry(widget, action)
            else:
                # Use original behavior for all subsequent inputs
                original_update_entry(widget, action)
        
        # Replace the method and reconnect all button signals
        self.weight_keypad.update_entry = custom_update_entry
        
        # Reconnect all the numpad buttons to use the new method
        keys = ['1', '2', '3', '4', '5', '6', '7', '8', '9', 'C', '0', '.']
        for key in keys:
            button_id = f'button_{key}'
            if button_id in self.weight_keypad.labels:
                # Disconnect existing handlers and connect to new method
                self.weight_keypad.labels[button_id].disconnect_by_func(original_update_entry)
                self.weight_keypad.labels[button_id].connect('clicked', custom_update_entry, key)
        
        # Also reconnect the entry field's activate signal
        self.weight_keypad.labels['entry'].disconnect_by_func(original_update_entry)
        self.weight_keypad.labels['entry'].connect("activate", custom_update_entry, "E")
        
        # Reconnect the bottom control buttons
        if 'backspace' in self.weight_keypad.labels:
            self.weight_keypad.labels['backspace'].disconnect_by_func(original_update_entry)
            self.weight_keypad.labels['backspace'].connect('clicked', custom_update_entry, 'B')
        
        vbox.pack_start(self.weight_keypad, True, True, 10)

        # Add the vertical box to the dialog content area
        content_area = weight_dialog.get_content_area()
        content_area.add(vbox)

        # Show all elements in the dialog
        weight_dialog.show_all()

    def process_weight_entry(self, weight):
        """Callback function called when user confirms weight entry"""
        
        # Get stored values
        filament_type = getattr(self, 'active_filament_type', None)
        run_load_macro = getattr(self, 'active_run_load_macro', False)
        
        if filament_type is None:
            self._screen.show_popup_message("Error: Filament type not found.", level=3)
            return

        # Close the weight dialog
        if hasattr(self, 'active_weight_dialog'):
            self.active_weight_dialog.destroy()
            self.cleanup_weight_dialog()

        # Process the filament selection with weight
        self.process_filament_selection(filament_type, weight, run_load_macro)

    def hide_weight_numpad(self, widget=None):
        """Callback function for when user cancels weight entry"""
        if hasattr(self, 'active_weight_dialog'):
            self.active_weight_dialog.destroy()
            self.cleanup_weight_dialog()

    def cleanup_weight_dialog(self):
        """Clean up dialog-related attributes"""
        for attr in ['active_weight_dialog', 'active_filament_type', 'active_run_load_macro', 'weight_keypad']:
            if hasattr(self, attr):
                delattr(self, attr)

    def open_weight_only_dialog(self, widget, parent_dialog):
        """Open a weight entry dialog that updates only the weight (keeps current filament type)"""
        
        # Get the position of the parent dialog
        current_x, current_y = parent_dialog.get_position()
        
        # Close the parent dialog
        parent_dialog.destroy()

        # Get the toplevel window for the dialog
        parent_window = widget.get_toplevel()
        if not isinstance(parent_window, Gtk.Window):
            parent_window = None

        # Create weight entry dialog
        weight_dialog = ClickOutsideDialog(
            title="Update Weight Only",
            transient_for=parent_window,
            flags=Gtk.DialogFlags.MODAL
        )
        weight_dialog.set_default_size(500, 500)
        weight_dialog.move(current_x + 100, current_y)

        # Store dialog reference for callbacks
        self.active_weight_only_dialog = weight_dialog

        # Create a vertical box layout
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        vbox.set_margin_start(20)
        vbox.set_margin_end(20)
        vbox.set_margin_top(20)
        vbox.set_margin_bottom(20)

        # Show current filament type so user knows what they're updating
        current_filament = getattr(self.shared_printer_config, 'filament', 'N/A') or 'N/A'
        instruction_label = Gtk.Label()
        instruction_label.set_markup(
            f'<span font="12">Enter new weight for <b>{current_filament}</b> (grams)</span>'
        )
        vbox.pack_start(instruction_label, False, False, 10)

        # Create the weight keypad
        from ks_includes.widgets.weight_keypad import WeightKeypad
        self.weight_only_keypad = WeightKeypad(
            self._screen,
            self.process_weight_only_entry,    # Callback for confirm
            self.hide_weight_only_numpad       # Callback for cancel
        )

        vbox.pack_start(self.weight_only_keypad, True, True, 10)

        # Add the vertical box to the dialog content area
        content_area = weight_dialog.get_content_area()
        content_area.add(vbox)

        # Show all elements in the dialog
        weight_dialog.show_all()

    def process_weight_only_entry(self, weight):
        """Callback when user confirms weight-only entry. Updates spool_tracker weight without changing filament type."""
        
        # Close the dialog
        if hasattr(self, 'active_weight_only_dialog'):
            self.active_weight_only_dialog.destroy()
            self.cleanup_weight_only_dialog()

        try:
            weight = float(weight)
            if weight <= 0:
                self._screen.show_popup_message("Weight must be positive", level=3)
                return
        except (ValueError, TypeError):
            self._screen.show_popup_message("Invalid weight value", level=3)
            return

        # POST only the weight to spool_tracker (no filament_type param)
        try:
            result = self._screen.apiclient.post_request(
                "server/spool_tracker/filament",
                json={"weight": weight}
            )

            if result and not result.get("error"):
                self._screen.show_popup_message(f"Weight updated to {weight}g", level=1)
                self.refresh_title()
            else:
                error_msg = (result.get("error", {}).get("message", "Unknown error")
                             if result else "No response")
                self._screen.show_popup_message(f"Weight update error: {error_msg}", level=3)

        except Exception as e:
            self._screen.show_popup_message(f"Weight update error: {str(e)}", level=3)

    def hide_weight_only_numpad(self, widget=None):
        """Callback for when user cancels weight-only entry"""
        if hasattr(self, 'active_weight_only_dialog'):
            self.active_weight_only_dialog.destroy()
            self.cleanup_weight_only_dialog()

    def cleanup_weight_only_dialog(self):
        """Clean up weight-only dialog attributes"""
        for attr in ['active_weight_only_dialog', 'weight_only_keypad']:
            if hasattr(self, attr):
                delattr(self, attr)

    def confirm_weight_entry(self, widget, entry, dialog, filament_type, run_load_macro=False):
        # Get the weight from the entry
        weight_text = entry.get_text().strip()
        
        # Validate weight
        try:
            weight = float(weight_text)
            if weight <= 0:
                raise ValueError("Weight must be positive")
        except (ValueError, TypeError):
            # Show popup warning and close dialog
            self._screen.show_popup_message("Invalid weight entry. Please enter a positive number.", level=3)
            dialog.destroy()
            return

        # Close the weight dialog
        dialog.destroy()

        # Process the filament selection with weight
        self.process_filament_selection(filament_type, weight, run_load_macro)

    def process_filament_selection(self, filament_type, weight, run_load_macro=False):
            # Determine moonraker filament name (the two PA-GF presets both become
            # PA-GF; any other name — including a fleet material such as
            # "PA-GF12" — is sent as-is so it keeps its own specs)
            if filament_type in PA_GF_PRESET_LABELS:
                moonraker_filament = "PA-GF"
            else:
                moonraker_filament = filament_type

            # Update Moonraker database first
            def handle_moonraker_response(response, method, params, *args):
                if response.get("error"):
                    self._screen.show_popup_message(f"Failed to set filament type: {response['error']['message']}", level=3)
                else:
                    self.shared_printer_config.filament = moonraker_filament
                    self._screen.show_popup_message(f"Filament type set to {moonraker_filament}", level=1)
                    self.update_button_labels()

            # Send Moonraker request
            self._screen._ws.send_method(
                "server.database.post_item", 
                {
                    "namespace": "HS3",
                    "key": "filament_type",
                    "value": moonraker_filament
                },
                handle_moonraker_response
            )

            # Handle spool_tracker workflow (much simpler than spoolman). A fleet
            # catalog material carries its density/diameter so Moonraker can
            # register it as a custom filament; presets are Moonraker built-ins.
            fleet_spec = self.fleet_filament_specs.get(filament_type)
            if fleet_spec:
                self.handle_spool_tracker_workflow(
                    moonraker_filament, weight, fleet_spec["density"], fleet_spec["diameter"]
                )
            else:
                self.handle_spool_tracker_workflow(moonraker_filament, weight)

            # Notify fleet_daemon to unload any spool on this printer (manual change = no QR)
            if self.fleet_daemon_url:
                threading.Thread(
                    target=self._notify_fleet_spool_unload,
                    daemon=True,
                ).start()

            # Run load macro if requested
            if run_load_macro:
                self._screen._send_action(None, "printer.gcode.script",
                                        {"script": f"LOAD_FILAMENT SPEED={self.speed * 60}"})

    # ── QR Spool Scan Methods ─────────────────────────────────────────

    def _on_qr_scan_clicked(self, widget, dialog, run_load_macro):
        """Start the QR scan workflow — close filament dialog and wait for barcode input."""
        dialog.destroy()
        self._active_run_load_macro = run_load_macro
        self.qr_scan_active = True
        self.qr_scan_buffer = ""
        self._screen.show_popup_message(
            '<span size="30000" weight="bold">Scan Spool QR Code</span>\n\n'
            '<span size="16000">Scan barcode or press Escape to cancel</span>',
            level=1,
        )

    def handle_key_press(self, event):
        """Handle keyboard input from barcode scanner. Returns True if consumed."""
        if not self.qr_scan_active:
            return False
        keyval = event.keyval
        keyval_name = Gdk.keyval_name(keyval)

        if keyval_name in ("Return", "KP_Enter"):
            if self.qr_scan_buffer:
                self._submit_spool_qr(self.qr_scan_buffer.strip())
                self.qr_scan_buffer = ""
            return True
        elif keyval_name == "BackSpace":
            self.qr_scan_buffer = self.qr_scan_buffer[:-1]
            return True
        elif keyval_name == "Escape":
            self.qr_scan_active = False
            self.qr_scan_buffer = ""
            return False  # Let Escape propagate to go home
        else:
            char = chr(keyval) if 32 <= keyval < 127 else Gdk.keyval_to_unicode(keyval)
            if isinstance(char, int):
                char = chr(char) if char > 0 else ""
            if char and char.isprintable():
                self.qr_scan_buffer += char
                return True
        return False

    def _submit_spool_qr(self, qr_code):
        """Disable scan mode and look up the QR code from fleet_daemon in a background thread."""
        self.qr_scan_active = False
        self._screen.show_popup_message(
            f'<span size="24000">Looking up: {GLib.markup_escape_text(qr_code)}</span>',
            level=1,
        )
        threading.Thread(
            target=self._lookup_spool_qr,
            args=(qr_code,),
            daemon=True,
        ).start()

    def _lookup_spool_qr(self, qr_code):
        """Background thread: GET fleet_daemon spool lookup and marshal result to GTK thread."""
        try:
            resp = requests.get(
                f"{self.fleet_daemon_url}/spool/lookup/{qr_code}",
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                GLib.idle_add(self._show_spool_confirmation, qr_code, data)
            elif resp.status_code == 404:
                GLib.idle_add(
                    self._screen.show_popup_message,
                    '<span size="24000" weight="bold">Spool Not Found</span>\n\n'
                    f'<span size="16000">QR: {GLib.markup_escape_text(qr_code)}</span>\n\n'
                    '<span size="16000">Use manual filament selection instead</span>',
                    2,
                )
            else:
                detail = resp.text[:200]
                GLib.idle_add(
                    self._screen.show_popup_message,
                    f'<span size="24000" weight="bold">Lookup Error ({resp.status_code})</span>\n\n'
                    f'<span size="16000">{GLib.markup_escape_text(detail)}</span>',
                    3,
                )
        except requests.exceptions.ConnectionError:
            hostname = self._get_printer_hostname()
            self._save_pending_spool("load", qr_code, hostname)
            GLib.idle_add(
                self._screen.show_popup_message,
                '<span size="24000" weight="bold">Fleet Offline – Spool Saved</span>\n\n'
                '<span size="16000">Spool load will be synced when fleet is back</span>',
                2,
            )
        except Exception as e:
            hostname = self._get_printer_hostname()
            self._save_pending_spool("load", qr_code, hostname)
            GLib.idle_add(
                self._screen.show_popup_message,
                f'<span size="24000" weight="bold">Fleet Offline – Spool Saved</span>\n\n'
                f'<span size="16000">{GLib.markup_escape_text(str(e))}</span>',
                2,
            )

    def _get_printer_hostname(self):
        """Get this printer's hostname with .local suffix."""
        hostname = socket.gethostname()
        if not hostname.endswith('.local'):
            hostname += '.local'
        return hostname

    def _show_spool_confirmation(self, qr_code, data):
        """Show a confirmation dialog with spool details from fleet_daemon lookup."""
        spool = data.get("spool", {})
        filament = spool.get("filament", {})
        vendor = filament.get("vendor") or {}

        # Check if spool is already loaded on another printer — warn but allow transfer
        loaded_on = spool.get("loaded_on_printer") or ""
        my_hostname = self._get_printer_hostname()
        transfer_warning = ""
        if loaded_on and loaded_on != my_hostname:
            transfer_warning = f"  (transferring from {loaded_on})"
            logging.warning(
                f"Spool {qr_code} is loaded on {loaded_on}, transferring to {my_hostname}"
            )

        vendor_name = vendor.get("name", "Unknown")
        filament_name = filament.get("name", "Unknown")
        material = filament.get("material", "Unknown")
        remaining_weight = spool.get("remaining_weight", 0)
        color_hex = filament.get("color_hex", "")
        density = filament.get("density", 0)
        diameter = filament.get("diameter", 1.75)

        dialog = ClickOutsideDialog(
            title="Confirm Spool",
            transient_for=self._screen,
            flags=Gtk.DialogFlags.MODAL,
        )
        dialog.set_default_size(500, 400)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        vbox.set_margin_start(20)
        vbox.set_margin_end(20)
        vbox.set_margin_top(20)
        vbox.set_margin_bottom(20)

        info_lines = [
            f"Vendor: {vendor_name}",
            f"Filament: {filament_name}",
            f"Material: {material}",
            f"Remaining: {remaining_weight:.0f}g",
        ]
        if color_hex:
            info_lines.append(f"Color: #{color_hex}")
        if transfer_warning:
            info_lines.append(f"⚠ Transferring from {loaded_on}")

        for text in info_lines:
            lbl = Gtk.Label(label=text)
            lbl.set_halign(Gtk.Align.START)
            lbl.modify_font(Pango.FontDescription("Sans 16"))
            vbox.pack_start(lbl, False, False, 0)

        btn_box = Gtk.Box(spacing=20)
        btn_box.set_margin_top(20)

        confirm_btn = Gtk.Button(label="Confirm")
        confirm_btn.get_style_context().add_class("color1")
        confirm_btn.set_size_request(200, 80)
        confirm_btn.connect(
            "clicked", self._confirm_spool_qr, dialog, qr_code,
            material, remaining_weight, density, diameter,
        )

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.get_style_context().add_class("color2")
        cancel_btn.set_size_request(200, 80)
        cancel_btn.connect("clicked", lambda w: dialog.destroy())

        btn_box.pack_start(confirm_btn, True, True, 0)
        btn_box.pack_start(cancel_btn, True, True, 0)
        vbox.pack_end(btn_box, False, False, 0)

        dialog.get_content_area().add(vbox)
        dialog.show_all()

    def _confirm_spool_qr(self, widget, dialog, qr_code, material, weight, density, diameter):
        """Register the QR-scanned spool to moonraker's spool_tracker."""
        dialog.destroy()

        # Map material to moonraker filament type
        # Known types map directly; PA-GF variants collapse to "PA-GF"
        known_types = {"PA-CF", "PETG-CF", "PA-GF", "TPU"}
        if material in known_types:
            moonraker_filament = material
        elif material and material.startswith("PA-GF"):
            moonraker_filament = "PA-GF"
        else:
            # Unknown material — will go through custom filament registration
            moonraker_filament = material

        # Use existing workflow (handles custom filament registration + spool_tracker POST)
        self.handle_spool_tracker_workflow(
            moonraker_filament, weight, density, diameter, qr_code=qr_code,
        )

        # Update Moonraker DB with filament type
        self._screen._ws.send_method(
            "server.database.post_item",
            {"namespace": "HS3", "key": "filament_type", "value": moonraker_filament},
            lambda *args: None,
        )
        self.shared_printer_config.filament = moonraker_filament
        self.update_button_labels()

        # Notify fleet_daemon directly so it updates loaded_on_printer immediately.
        # Capture the load time here so a queued/replayed notification keeps it.
        loaded_at = datetime.now(timezone.utc).isoformat()
        threading.Thread(
            target=self._notify_fleet_spool_load,
            args=(qr_code, loaded_at),
            daemon=True,
        ).start()

        # Run load macro if requested
        if self._active_run_load_macro and self.load_filament:
            self._screen._send_action(
                None, "printer.gcode.script",
                {"script": f"LOAD_FILAMENT SPEED={self.speed * 60}"},
            )

        self._screen.show_popup_message(
            f"Spool registered: {moonraker_filament}, {weight:.0f}g\nQR: {qr_code}",
            level=1,
        )

    def _notify_fleet_spool_load(self, qr_code, loaded_at=None):
        """Notify fleet_daemon that a spool has been loaded on this printer."""
        hostname = self._get_printer_hostname()
        if loaded_at is None:
            loaded_at = datetime.now(timezone.utc).isoformat()
        try:
            resp = requests.post(
                f"{self.fleet_daemon_url}/spool/load",
                json={"qr_code": qr_code, "printer_hostname": hostname, "loaded_at": loaded_at},
                timeout=5,
            )
            if resp.status_code == 200:
                logging.info(f"Fleet daemon notified: spool {qr_code} loaded on {hostname}")
            else:
                logging.warning(f"Fleet daemon spool load failed: {resp.status_code} {resp.text[:200]}")
                self._save_pending_spool("load", qr_code, hostname, loaded_at)
        except Exception as e:
            logging.warning(f"Fleet daemon spool load error: {e}")
            self._save_pending_spool("load", qr_code, hostname, loaded_at)

    def _notify_fleet_spool_unload(self):
        """Notify fleet_daemon to unload any spool on this printer (manual filament change)."""
        hostname = self._get_printer_hostname()
        try:
            resp = requests.post(
                f"{self.fleet_daemon_url}/spool/unload",
                json={"qr_code": "", "printer_hostname": hostname},
                timeout=5,
            )
            if resp.status_code == 200:
                logging.info(f"Fleet daemon notified: spool unloaded from {hostname}")
            else:
                logging.warning(f"Fleet daemon spool unload failed: {resp.status_code}")
                self._save_pending_spool("unload", "", hostname)
        except Exception as e:
            logging.warning(f"Fleet daemon spool unload error: {e}")
            self._save_pending_spool("unload", "", hostname)

    # ── Fleet filament catalog (extra presets in the filament dialog) ──

    def _start_fleet_filament_refresh(self):
        """Refresh the fleet filament catalog in the background (never blocks).
        No-op without a fleet URL or while a fetch is already in flight."""
        if not self.fleet_daemon_url or self._fleet_catalog_fetching:
            return
        self._fleet_catalog_fetching = True
        threading.Thread(target=self._fetch_fleet_filaments_bg, daemon=True).start()

    def _fetch_fleet_filaments_bg(self):
        """Worker thread: GET /spool/filaments, then hand the result to the GTK
        thread. Any failure (unreachable, timeout, non-200, bad JSON) marks the
        catalog unavailable so the dialog falls back to the hardcoded presets.
        Deliberately silent (no popup): this runs on panel init and on every
        dialog open, and the no-fleet experience must look exactly like before."""
        extras, ok = None, False
        try:
            resp = requests.get(f"{self.fleet_daemon_url}/spool/filaments", timeout=3)
            if resp.status_code == 200:
                rows = resp.json()
                if isinstance(rows, list):
                    extras = self._build_fleet_extras(rows)
                    ok = True
                    logging.info(f"[Fleet] Filament catalog: {len(rows)} rows, {len(extras)} extra materials")
                else:
                    logging.warning("[Fleet] Filament catalog: unexpected response shape")
            else:
                logging.warning(f"[Fleet] Filament catalog HTTP {resp.status_code}")
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            logging.warning(f"[Fleet] Cannot fetch filament catalog (fleet offline?): {e}")
        except Exception as e:
            logging.error(f"[Fleet] Filament catalog fetch error: {e}")
        finally:
            self._fleet_catalog_fetching = False
        GLib.idle_add(self._apply_fleet_filaments, extras, ok)

    def _apply_fleet_filaments(self, extras, ok):
        """GTK thread: publish a fetch result. On failure the extras list is
        left alone but gated off by fleet_filaments_ok; specs are never cleared
        so a weight keypad already open for a fleet material still resolves."""
        self.fleet_filaments_ok = ok
        if ok:
            self.fleet_filaments = extras
            for spec in extras:
                self.fleet_filament_specs[spec["label"]] = spec
        return False

    @staticmethod
    def _build_fleet_extras(rows):
        """Turn GET /spool/filaments rows into dialog entries: one per distinct
        material (case/whitespace-insensitive), first-registered wins, skipping
        materials already covered by the hardcoded presets. Pure function."""
        def _positive(value):
            try:
                v = float(value)
            except (TypeError, ValueError):
                return None
            return v if v > 0 else None

        extras = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            material = (row.get("material") or "").strip()
            if not material:
                continue
            key = material.upper()
            if key in FLEET_PRESET_MATERIALS:
                continue
            spec = extras.get(key)
            if spec is None:
                spec = {"label": material, "density": None, "diameter": None, "default_weight": None}
                extras[key] = spec
            for field, src in (("density", "density"), ("diameter", "diameter"), ("default_weight", "weight")):
                if spec[field] is None:
                    spec[field] = _positive(row.get(src))

        result = []
        for spec in extras.values():
            spec["density"] = spec["density"] if spec["density"] is not None else 0.0
            spec["diameter"] = spec["diameter"] if spec["diameter"] is not None else 1.75
            weight = spec["default_weight"] if spec["default_weight"] is not None else 3000
            spec["default_weight"] = int(weight) if float(weight).is_integer() else weight
            result.append(spec)
        return result

    def _default_weight_for(self, filament_type):
        """Keypad prefill: hardcoded preset weight, else the fleet catalog's
        weight for that material, else 3000 g."""
        mapping = self.spoolman_filament_mapping.get(filament_type)
        if mapping:
            return mapping["default_weight"]
        spec = self.fleet_filament_specs.get(filament_type)
        if spec and spec.get("default_weight"):
            return spec["default_weight"]
        return 3000

    @staticmethod
    def _save_pending_spool(action, qr_code, printer_hostname, loaded_at=None):
        """Save a spool load/unload action to the pending queue file."""
        pending = []
        if os.path.exists(SPOOL_PENDING_FILE):
            try:
                with open(SPOOL_PENDING_FILE, "r") as f:
                    pending = json.load(f)
            except Exception:
                pending = []
        entry = {
            "action": action,
            "qr_code": qr_code,
            "printer_hostname": printer_hostname,
        }
        if loaded_at:
            entry["loaded_at"] = loaded_at
        pending.append(entry)
        try:
            with open(SPOOL_PENDING_FILE, "w") as f:
                json.dump(pending, f)
            logging.info(f"[Spool] Saved pending {action}: qr={qr_code} ({len(pending)} pending)")
        except Exception as e:
            logging.error(f"[Spool] Failed to save pending spool action: {e}")

    @staticmethod
    def flush_pending_spool_actions(fleet_daemon_url):
        """Try to push all pending spool load/unload entries to fleet daemon.
        Call this on KlipperScreen startup."""
        if not os.path.exists(SPOOL_PENDING_FILE):
            return
        try:
            with open(SPOOL_PENDING_FILE, "r") as f:
                pending = json.load(f)
        except Exception:
            return
        if not pending:
            return

        logging.info(f"[Spool] Flushing {len(pending)} pending spool entries")
        remaining = []
        for entry in pending:
            action = entry.get("action", "load")
            url = f"{fleet_daemon_url}/spool/{action}"
            payload = {
                "qr_code": entry.get("qr_code", ""),
                "printer_hostname": entry.get("printer_hostname", ""),
            }
            if entry.get("loaded_at"):
                payload["loaded_at"] = entry["loaded_at"]
            try:
                resp = requests.post(url, json=payload, timeout=10)
                if resp.status_code == 200:
                    logging.info(f"[Spool] Flushed pending {action}: qr={entry.get('qr_code')}")
                else:
                    logging.warning(f"[Spool] Failed to flush {action}: {resp.status_code} {resp.text[:200]}")
                    remaining.append(entry)
            except Exception as e:
                logging.warning(f"[Spool] Failed to flush {action}: {e}")
                remaining.append(entry)

        if remaining:
            with open(SPOOL_PENDING_FILE, "w") as f:
                json.dump(remaining, f)
            logging.info(f"[Spool] {len(remaining)} pending spool entries remain")
        else:
            try:
                os.remove(SPOOL_PENDING_FILE)
            except OSError:
                pass
            logging.info("[Spool] All pending spool entries flushed")

    # ── End QR Spool Scan Methods ────────────────────────────────────

    def handle_spool_tracker_workflow(self, filament_type, weight, density=None, diameter=None, qr_code=None):
        """Set filament type and weight in spool_tracker, optionally register custom filament"""
        try:
            is_custom = filament_type not in self.spoolman_filament_mapping

            if is_custom:
                logging.info(
                    f"Filament '{filament_type}' not found in Spoolman mapping, "
                    f"registering as custom filament"
                )

                result = self._screen.apiclient.post_request(
                    "server/spool_tracker/custom_filament",
                    json={
                        "name": filament_type,
                        "density": density,
                        "diameter": diameter
                    }
                )

                if result and result.get("error"):
                    error_msg = result.get("error", {}).get("message", "Unknown error")
                    logging.error(f"Custom filament registration failed: {error_msg}")
                    self._screen.show_popup_message(
                        f"Custom filament error: {error_msg}",
                        level=3
                    )
                    return

                logging.info(f"Custom filament '{filament_type}' registered successfully")

            # Always set filament + weight (+ optional qr_code)
            payload = {
                "filament_type": filament_type,
                "weight": weight,
            }
            if qr_code:
                payload["qr_code"] = qr_code
            result = self._screen.apiclient.post_request(
                "server/spool_tracker/filament",
                json=payload,
            )

            if result and not result.get("error"):
                self._screen.show_popup_message(f"Spool tracker updated: {filament_type}, {weight}g", level=1)
            else:
                error_msg = result.get("error", {}).get("message", "Unknown error") if result else "No response"
                self._screen.show_popup_message(f"Spool tracker error: {error_msg}", level=3)
                
        except Exception as e:
            self._screen.show_popup_message(f"Spool tracker error: {str(e)}", level=3)

    def set_filament_type_original(self, widget, filament_type, dialog, run_load_macro=False):
        # Close the dialog when a filament type is selected
        dialog.destroy()

        # Determine moonraker filament name (the two PA-GF presets both become PA-GF)
        if filament_type in PA_GF_PRESET_LABELS:
            moonraker_filament = "PA-GF"
        else:
            moonraker_filament = filament_type

        # Define a callback function to handle the response
        def handle_response(response, method, params, *args):
            if response.get("error"):
                self._screen.show_popup_message(f"Failed to set filament type: {response['error']['message']}", level=3)
            else:
                self.shared_printer_config.filament = moonraker_filament
                self._screen.show_popup_message(f"Filament type set to {moonraker_filament}", level=1)
                self.update_button_labels()

        # Send Moonraker requests to set the filament type, passing the callback
        self._screen._ws.send_method(
            "server.database.post_item", 
            {
                "namespace": "HS3",
                "key": "filament_type",
                "value": moonraker_filament
            },
            handle_response  # Pass the callback here
        )

        # Run load macro if requested
        if run_load_macro:
            self._screen._send_action(None, "printer.gcode.script",
                                      {"script": f"LOAD_FILAMENT SPEED={self.speed * 60}"})

    def set_filament_type(self, widget, filament_type, dialog):
        # This method is kept for backward compatibility but now redirects to weight entry
        self.open_weight_entry_dialog(widget, filament_type, dialog)

    def open_nozzle_selection(self, widget):
        # List of nozzle sizes
        nozzle_sizes = ["0.4", "0.5", "0.6", "0.8"]

        # Create the dialog for selecting nozzle sizes
        dialog = ClickOutsideDialog(title="Select Nozzle Size",
                                    transient_for=widget.get_toplevel(),
                                    flags=Gtk.DialogFlags.MODAL)
        dialog.set_default_size(600, 250)

        current_x, current_y = dialog.get_position()
        dialog.move(current_x, current_y - 80)  # Moves dialog down by 100 pixels
        
        # Create a grid layout to place the buttons
        grid = Gtk.Grid()
        grid.set_column_homogeneous(True)
        grid.set_row_homogeneous(True)
        grid.set_column_spacing(10)
        grid.set_row_spacing(10)
        grid.set_margin_start(10)
        grid.set_margin_end(10)
        grid.set_margin_top(10)
        grid.set_margin_bottom(10)

        # Create buttons for each nozzle size and add them to the grid
        for i, size in enumerate(nozzle_sizes):
            button = Gtk.Button(label=f"{size}mm")
            button.get_style_context().add_class("color1")
            button.set_size_request(150, 200)
            button.connect("clicked", self.open_nozzle_type_selection, size, dialog)
            grid.attach(button, i % 2, i // 2, 1, 1)  # Arrange buttons in 3 columns

        # Add the grid to the dialog content area and show all
        content_area = dialog.get_content_area()
        content_area.add(grid)
        dialog.show_all()

    def open_nozzle_type_selection(self, widget, nozzle_size, parent_dialog):
        current_x, current_y = parent_dialog.get_position()
        parent_dialog.destroy()

        parent_window = widget.get_toplevel()
        if not isinstance(parent_window, Gtk.Window):
            parent_window = None

        nozzle_types = ["DLC Hardened Steel", "Nickel Plated Copper", "Custom"]
        prefills = {"DLC Hardened Steel": 12, "Nickel Plated Copper": 12}

        dialog = ClickOutsideDialog(title="Select Nozzle Type",
                                    transient_for=parent_window,
                                    flags=Gtk.DialogFlags.MODAL)
        dialog.set_default_size(600, 250)
        dialog.move(current_x, current_y)

        grid = Gtk.Grid()
        grid.set_column_homogeneous(True)
        grid.set_row_homogeneous(True)
        grid.set_column_spacing(10)
        grid.set_row_spacing(10)
        grid.set_margin_start(10)
        grid.set_margin_end(10)
        grid.set_margin_top(10)
        grid.set_margin_bottom(10)

        for i, nozzle_type in enumerate(nozzle_types):
            button = Gtk.Button(label=nozzle_type)
            button.get_style_context().add_class("color1")
            button.set_size_request(150, 220)
            if nozzle_type == "Custom":
                button.connect("clicked", self.open_custom_nozzle_type_dialog, nozzle_size, dialog)
            else:
                prefill = prefills[nozzle_type]
                button.connect("clicked", self.open_nozzle_life_dialog, nozzle_size, nozzle_type, prefill, dialog)
            grid.attach(button, i, 0, 1, 1)

        content_area = dialog.get_content_area()
        content_area.add(grid)
        dialog.show_all()

    def open_custom_nozzle_type_dialog(self, widget, nozzle_size, parent_dialog):
        current_x, current_y = parent_dialog.get_position()
        parent_dialog.destroy()

        parent_window = widget.get_toplevel()
        if not isinstance(parent_window, Gtk.Window):
            parent_window = None

        custom_dialog = ClickOutsideDialog(
            title="Enter Custom Nozzle Type",
            transient_for=parent_window,
            flags=Gtk.DialogFlags.MODAL
        )
        custom_dialog.set_default_size(400, 400)
        custom_dialog.move(current_x - 170, current_y - 70)
        custom_dialog.connect("destroy", self._screen.remove_custom_keyboard)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        vbox.set_margin_start(20)
        vbox.set_margin_end(20)
        vbox.set_margin_top(20)
        vbox.set_margin_bottom(20)

        entry = Gtk.Entry()
        entry.set_placeholder_text("Enter custom nozzle type (max 20 chars)")
        entry.set_max_length(20)
        entry.connect("focus-in-event", lambda w, e: self._screen.show_custom_keyboard(entry))
        entry.grab_focus()

        vbox.pack_start(entry, True, True, 0)

        keyboard = self._screen.show_custom_keyboard(entry)
        if keyboard:
            vbox.pack_start(keyboard, False, False, 10)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        confirm_button = Gtk.Button(label="Confirm")
        confirm_button.get_style_context().add_class("color1")
        confirm_button.set_size_request(150, 50)
        confirm_button.connect("clicked", self.confirm_custom_nozzle_type, entry, custom_dialog, nozzle_size)
        hbox.pack_start(confirm_button, True, True, 0)

        cancel_button = Gtk.Button(label="Cancel")
        cancel_button.get_style_context().add_class("color1")
        cancel_button.set_size_request(150, 50)
        cancel_button.connect("clicked", lambda w: custom_dialog.destroy())
        hbox.pack_start(cancel_button, True, True, 0)

        vbox.pack_start(hbox, False, False, 0)

        content_area = custom_dialog.get_content_area()
        content_area.add(vbox)
        custom_dialog.show_all()

    def confirm_custom_nozzle_type(self, widget, entry, dialog, nozzle_size):
        custom_type = entry.get_text().strip()

        if not custom_type:
            self._screen.show_popup_message("Please enter a nozzle type name", level=3)
            return

        if len(custom_type) > 20:
            self._screen.show_popup_message("Nozzle type name must be 20 characters or less", level=3)
            return

        dialog.destroy()
        self.open_nozzle_life_dialog(widget, nozzle_size, custom_type, 0, None)

    def open_nozzle_life_dialog(self, widget, nozzle_size, nozzle_type, prefill, parent_dialog):
        if parent_dialog is not None:
            current_x, current_y = parent_dialog.get_position()
            parent_dialog.destroy()
        else:
            current_x, current_y = 0, 0

        parent_window = widget.get_toplevel()
        if not isinstance(parent_window, Gtk.Window):
            parent_window = None

        life_dialog = ClickOutsideDialog(
            title=f"Enter Nozzle Life for {nozzle_type} in kg",
            transient_for=parent_window,
            flags=Gtk.DialogFlags.MODAL
        )
        life_dialog.set_default_size(500, 500)
        if parent_dialog is not None:
            life_dialog.move(current_x + 100, current_y)

        self.active_nozzle_life_dialog = life_dialog
        self.active_nozzle_size = nozzle_size
        self.active_nozzle_type = nozzle_type

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        vbox.set_margin_start(20)
        vbox.set_margin_end(20)
        vbox.set_margin_top(20)
        vbox.set_margin_bottom(20)

        instruction_label = Gtk.Label()
        instruction_label.set_markup(f'<span font="12">Enter nozzle life (in kg) for {nozzle_type}</span>')
        vbox.pack_start(instruction_label, False, False, 10)

        from ks_includes.widgets.weight_keypad import WeightKeypad
        self.nozzle_life_keypad = WeightKeypad(
            self._screen,
            self.process_nozzle_life_entry,
            self.hide_nozzle_life_dialog
        )

        if prefill:
            self.nozzle_life_keypad.set_initial_value(prefill)
            self.nozzle_life_preset_active = True
            original_update_entry = self.nozzle_life_keypad.update_entry

            def custom_life_update_entry(widget, action):
                if hasattr(self, 'nozzle_life_preset_active') and self.nozzle_life_preset_active:
                    if action == 'B':
                        self.nozzle_life_keypad.labels['entry'].set_text("")
                        self.nozzle_life_preset_active = False
                    elif action not in ['E', 'C', 'CANCEL']:
                        self.nozzle_life_keypad.labels['entry'].set_text("0." if action == '.' else action)
                        self.nozzle_life_preset_active = False
                    else:
                        original_update_entry(widget, action)
                else:
                    original_update_entry(widget, action)

            self.nozzle_life_keypad.update_entry = custom_life_update_entry

            keys = ['1', '2', '3', '4', '5', '6', '7', '8', '9', 'C', '0', '.']
            for key in keys:
                button_id = f'button_{key}'
                if button_id in self.nozzle_life_keypad.labels:
                    self.nozzle_life_keypad.labels[button_id].disconnect_by_func(original_update_entry)
                    self.nozzle_life_keypad.labels[button_id].connect('clicked', custom_life_update_entry, key)

            self.nozzle_life_keypad.labels['entry'].disconnect_by_func(original_update_entry)
            self.nozzle_life_keypad.labels['entry'].connect("activate", custom_life_update_entry, "E")

            if 'backspace' in self.nozzle_life_keypad.labels:
                self.nozzle_life_keypad.labels['backspace'].disconnect_by_func(original_update_entry)
                self.nozzle_life_keypad.labels['backspace'].connect('clicked', custom_life_update_entry, 'B')

        vbox.pack_start(self.nozzle_life_keypad, True, True, 10)

        content_area = life_dialog.get_content_area()
        content_area.add(vbox)
        life_dialog.show_all()

    def process_nozzle_life_entry(self, life_value):
        nozzle_size = getattr(self, 'active_nozzle_size', None)
        nozzle_type = getattr(self, 'active_nozzle_type', None)

        if nozzle_size is None or nozzle_type is None:
            self._screen.show_popup_message("Error: Nozzle configuration not found.", level=3)
            return

        try:
            nozzle_life = float(life_value)
            if nozzle_life <= 0:
                raise ValueError("Life must be positive")
        except (ValueError, TypeError):
            self._screen.show_popup_message("Invalid nozzle life value", level=3)
            return

        if hasattr(self, 'active_nozzle_life_dialog'):
            self.active_nozzle_life_dialog.destroy()
            self.cleanup_nozzle_life_dialog()

        self.finalize_nozzle_setup(nozzle_size, nozzle_type, nozzle_life)

    def hide_nozzle_life_dialog(self, widget=None):
        if hasattr(self, 'active_nozzle_life_dialog'):
            self.active_nozzle_life_dialog.destroy()
            self.cleanup_nozzle_life_dialog()

    def cleanup_nozzle_life_dialog(self):
        for attr in ['active_nozzle_life_dialog', 'active_nozzle_size', 'active_nozzle_type',
                     'nozzle_life_keypad', 'nozzle_life_preset_active']:
            if hasattr(self, attr):
                delattr(self, attr)

    def finalize_nozzle_setup(self, nozzle_size, nozzle_type, nozzle_life):
        def handle_size_response(response, method, params, *args):
            if response.get("error"):
                self._screen.show_popup_message(
                    f"Failed to set nozzle size: {response['error']['message']}", level=3)
            else:
                self.shared_printer_config.nozzle = nozzle_size
                self.update_button_labels()

        self._screen._ws.send_method(
            "server.database.post_item",
            {"namespace": "HS3", "key": "nozzle_size", "value": nozzle_size},
            handle_size_response
        )

        def handle_type_response(response, method, params, *args):
            if response.get("error"):
                self._screen.show_popup_message(
                    f"Failed to set nozzle type: {response['error']['message']}", level=3)
            else:
                self.shared_printer_config.nozzle_type = nozzle_type
                self.update_button_labels()

        self._screen._ws.send_method(
            "server.database.post_item",
            {"namespace": "HS3", "key": "nozzle_type", "value": nozzle_type},
            handle_type_response
        )

        def handle_life_response(response, method, params, *args):
            if response.get("error"):
                self._screen.show_popup_message(
                    f"Failed to set nozzle life: {response['error']['message']}", level=3)
            else:
                self._screen.show_popup_message(
                    f"Nozzle set: {nozzle_size}mm {nozzle_type}, {int(nozzle_life)}kg life", level=1)
                if hasattr(self._screen, 'base_panel'):
                    self._screen.base_panel._update_nozzle_life_label(nozzle_life, nozzle_life)

        self._screen._ws.send_method(
            "server.database.post_item",
            {"namespace": "HS3", "key": "nozzle_life", "value": nozzle_life},
            handle_life_response
        )

        self._screen._ws.send_method(
            "server.database.post_item",
            {"namespace": "HS3", "key": "remaining_nozzle_life", "value": nozzle_life},
            handle_life_response
        )

        try:
            self._screen.apiclient.post_request(
                "server/spool_tracker/status",
                json={"reset_tripmeter_e": True}
            )
        except Exception as e:
            logging.debug(f"Failed to reset tripmeter_e: {e}")

    def set_nozzle_size(self, widget, nozzle_size, dialog):
        # Close the dialog when a nozzle size is selected
        dialog.destroy()

        # Define a callback function to handle the response
        def handle_response(response, method, params, *args):
            if response.get("error"):
                self._screen.show_popup_message(f"Failed to set nozzle size: {response['error']['message']}", level=3)
            else:
                self.shared_printer_config.nozzle = nozzle_size
                self._screen.show_popup_message(f"Nozzle size set to {nozzle_size}mm", level=1)
                self.update_button_labels()

        # Send Moonraker requests to set the nozzle size, passing the callback
        self._screen._ws.send_method(
            "server.database.post_item",
            {
                "namespace": "HS3",
                "key": "nozzle_size",
                "value": nozzle_size
            },
            handle_response  # Pass the callback here
        )

    def open_custom_filament_dialog(self, widget, parent_dialog, run_load_macro=False):
        # Get the position of the parent dialog (Filament Selection Dialog)
        current_x, current_y = parent_dialog.get_position()

        # Close the original filament selection dialog
        parent_dialog.destroy()

        # Get the toplevel window (parent window) for the dialog
        parent_window = widget.get_toplevel()
        if not isinstance(parent_window, Gtk.Window):
            parent_window = None

        # Create a new dialog for custom filament input
        custom_dialog = ClickOutsideDialog(
            title="Enter Custom Filament Type",
            transient_for=parent_window,  # Pass the parent window here
            flags=Gtk.DialogFlags.MODAL
        )
        custom_dialog.set_default_size(400, 400)  # Adjust the height to accommodate the keyboard

        # Set the position of the custom dialog to match the original dialog
        custom_dialog.move(current_x - 170, current_y - 70)

        # Connect the destroy signal to remove the keyboard when the dialog is closed
        custom_dialog.connect("destroy", self._screen.remove_custom_keyboard)

        # Create a vertical box layout for the entry and buttons
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        vbox.set_margin_start(20)
        vbox.set_margin_end(20)
        vbox.set_margin_top(20)
        vbox.set_margin_bottom(20)

        # Create a text entry field for the custom filament name
        entry = Gtk.Entry()
        entry.set_placeholder_text("Enter custom filament name (max 20 chars)")
        entry.set_max_length(20)

        # Connect the entry to show the virtual keyboard when focused
        entry.connect("focus-in-event", lambda w, e: self._screen.show_custom_keyboard(entry))

        entry.grab_focus()

        vbox.pack_start(entry, True, True, 0)

        # Add the keyboard below the entry field
        keyboard = self._screen.show_custom_keyboard(entry)  # Get the keyboard widget
        if keyboard:
            vbox.pack_start(keyboard, False, False, 10)  # Add the keyboard to the layout

        # Create a horizontal box for the confirm and cancel buttons
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        # Create the Confirm button and set its size
        confirm_button = Gtk.Button(label="Confirm")
        confirm_button.get_style_context().add_class("color1")
        confirm_button.set_size_request(150, 50)  # Set width=150, height=50 to make it larger
        confirm_button.connect("clicked", self.confirm_custom_filament, entry, custom_dialog, run_load_macro)
        hbox.pack_start(confirm_button, True, True, 0)  # Set expand=True to let it take space

        # Create the Cancel button and set its size
        cancel_button = Gtk.Button(label="Cancel")
        cancel_button.get_style_context().add_class("color1")
        cancel_button.set_size_request(150, 50)  # Set width=150, height=50 to make it larger
        cancel_button.connect("clicked", lambda w: custom_dialog.destroy())
        hbox.pack_start(cancel_button, True, True, 0)  # Set expand=True to let it take space

        # Pack the buttons into the vertical box
        vbox.pack_start(hbox, False, False, 0)

        # Add the vertical box to the dialog content area
        content_area = custom_dialog.get_content_area()
        content_area.add(vbox)

        # Show all elements in the dialog
        custom_dialog.show_all()

    def confirm_custom_filament(self, widget, entry, dialog, run_load_macro=False):
        # Get the custom filament name from the entry
        custom_filament = entry.get_text().strip()

        if not custom_filament:
            self._screen.show_popup_message("Please enter a filament name", level=3)
            return

        if len(custom_filament) > 20:
            self._screen.show_popup_message("Filament name must be 20 characters or less", level=3)
            return

        # Close the custom filament dialog
        dialog.destroy()

        # Open the custom specs dialog
        self.open_custom_specs_dialog(custom_filament, run_load_macro)

    def open_custom_specs_dialog(self, custom_filament, run_load_macro=False):
        """Dialog to enter weight, density, and diameter for custom filament"""
        
        # Get the toplevel window (parent window) for the dialog
        parent_window = self._screen.get_toplevel()
        if not isinstance(parent_window, Gtk.Window):
            parent_window = None

        # Create specs entry dialog
        specs_dialog = ClickOutsideDialog(
            title=f"Enter Specs for {custom_filament}",
            transient_for=parent_window,
            flags=Gtk.DialogFlags.MODAL
        )
        specs_dialog.set_default_size(500, 550)

        # Store dialog reference and context for callbacks
        self.active_specs_dialog = specs_dialog
        self.active_custom_filament = custom_filament
        self.active_custom_run_load_macro = run_load_macro

        # Create a vertical box layout
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        vbox.set_margin_start(20)
        vbox.set_margin_end(20)
        vbox.set_margin_top(20)
        vbox.set_margin_bottom(20)

        # Add instruction label
        instruction_label = Gtk.Label()
        instruction_label.set_markup(f'<span font="12">Enter specifications for {custom_filament}. Leave it empty to disable tracking</span>')
        vbox.pack_start(instruction_label, False, False, 10)

        # Create the custom specs keypad
        from ks_includes.widgets.weight_keypad import WeightKeypad
        self.specs_keypad = WeightKeypad(
            self._screen, 
            self.process_custom_specs_entry,   # Callback for when user confirms
            self.hide_custom_specs_numpad      # Callback for when user cancels
        )
        
        # Store the current field being edited (weight, density, or diameter)
        self.current_spec_field = 'weight'
        self.custom_specs = {'weight': 0, 'density': 0, 'diameter': 0}
        
        # Set the initial field label
        self.specs_keypad.labels['entry'].set_placeholder_text("Enter weight (grams)")
        
        # Create a label to show which field is being edited
        self.field_label = Gtk.Label()
        self.field_label.set_markup('<span font="14"><b>Weight (grams)</b></span>')
        vbox.pack_start(self.field_label, False, False, 5)
        
        vbox.pack_start(self.specs_keypad, True, True, 10)

        # Add the vertical box to the dialog content area
        content_area = specs_dialog.get_content_area()
        content_area.add(vbox)

        # Show all elements in the dialog
        specs_dialog.show_all()

    def process_custom_specs_entry(self, value):
        """Callback function called when user confirms a spec entry"""
        
        try:
            float_value = float(value) if value else 0
        except ValueError:
            self._screen.show_popup_message("Invalid value entered", level=3)
            return
        
        # Store the current field value
        self.custom_specs[self.current_spec_field] = float_value
        
        # Move to next field or finish
        if self.current_spec_field == 'weight':
            self.current_spec_field = 'density'
            self.field_label.set_markup('<span font="14"><b>Density (g/cm³)</b></span>')
            self.specs_keypad.labels['entry'].set_text("")
            self.specs_keypad.labels['entry'].set_placeholder_text("Enter density (g/cm³)")
        elif self.current_spec_field == 'density':
            self.current_spec_field = 'diameter'
            self.field_label.set_markup('<span font="14"><b>Diameter (mm)</b></span>')
            # Pre-fill with 1.75mm default diameter
            self.specs_keypad.labels['entry'].set_text("1.75")
            self.specs_keypad.labels['entry'].set_placeholder_text("Enter diameter (mm)")
            
            # Enable preset replacement behavior for diameter
            self.preset_diameter = "1.75"
            self.diameter_preset_active = True
            
            # Store original update_entry method if not already stored
            if not hasattr(self, 'original_specs_update_entry'):
                self.original_specs_update_entry = self.specs_keypad.update_entry
            
            # Create custom update method for diameter preset replacement
            def custom_diameter_update_entry(widget, action):
                if hasattr(self, 'diameter_preset_active') and self.diameter_preset_active:
                    if action == 'B':
                        # Backspace on preset - clear the field
                        self.specs_keypad.labels['entry'].set_text("")
                        self.diameter_preset_active = False
                    elif action not in ['E', 'C', 'CANCEL']:
                        # First digit/decimal pressed - replace preset with this input
                        if action == '.':
                            self.specs_keypad.labels['entry'].set_text("0.")
                        else:
                            self.specs_keypad.labels['entry'].set_text(action)
                        self.diameter_preset_active = False
                    else:
                        # Enter, Clear, or Cancel with preset value - use original behavior
                        self.original_specs_update_entry(widget, action)
                else:
                    # Use original behavior for all subsequent inputs
                    self.original_specs_update_entry(widget, action)
            
            # Replace the method and reconnect all button signals
            self.specs_keypad.update_entry = custom_diameter_update_entry
            
            # Reconnect all the numpad buttons to use the new method
            keys = ['1', '2', '3', '4', '5', '6', '7', '8', '9', 'C', '0', '.']
            for key in keys:
                button_id = f'button_{key}'
                if button_id in self.specs_keypad.labels:
                    # Disconnect existing handlers and connect to new method
                    try:
                        self.specs_keypad.labels[button_id].disconnect_by_func(self.original_specs_update_entry)
                    except:
                        pass  # Handler may not be connected yet
                    self.specs_keypad.labels[button_id].connect('clicked', custom_diameter_update_entry, key)
            
            # Also reconnect the entry field's activate signal
            try:
                self.specs_keypad.labels['entry'].disconnect_by_func(self.original_specs_update_entry)
            except:
                pass
            self.specs_keypad.labels['entry'].connect("activate", custom_diameter_update_entry, "E")
            
            # Reconnect the bottom control buttons
            if 'backspace' in self.specs_keypad.labels:
                try:
                    self.specs_keypad.labels['backspace'].disconnect_by_func(self.original_specs_update_entry)
                except:
                    pass
                self.specs_keypad.labels['backspace'].connect('clicked', custom_diameter_update_entry, 'B')
        else:
            # All fields entered, process the custom filament
            self.finish_custom_filament_setup()

    def hide_custom_specs_numpad(self, widget=None):
        """Callback function for when user cancels custom specs entry"""
        if hasattr(self, 'active_specs_dialog'):
            self.active_specs_dialog.destroy()
            self.cleanup_custom_specs_dialog()

    def cleanup_custom_specs_dialog(self):
        """Clean up dialog-related attributes"""
        for attr in ['active_specs_dialog', 'active_custom_filament', 'active_custom_run_load_macro', 
                     'specs_keypad', 'current_spec_field', 'custom_specs', 'field_label',
                     'preset_diameter', 'diameter_preset_active', 'original_specs_update_entry']:
            if hasattr(self, attr):
                delattr(self, attr)

    def finish_custom_filament_setup(self):
        """Finalize custom filament setup after all specs are entered"""
        
        # Get stored values
        custom_filament = getattr(self, 'active_custom_filament', None)
        run_load_macro = getattr(self, 'active_custom_run_load_macro', False)
        
        if custom_filament is None:
            self._screen.show_popup_message("Error: Filament name not found.", level=3)
            return

        # Close the specs dialog
        if hasattr(self, 'active_specs_dialog'):
            self.active_specs_dialog.destroy()
            
        weight = self.custom_specs.get('weight', 0)
        density = self.custom_specs.get('density', 0)
        diameter = self.custom_specs.get('diameter', 0)
        
        # Cleanup before processing
        self.cleanup_custom_specs_dialog()

        # Process based on whether all specs are provided
        if weight == 0 or density == 0 or diameter == 0:
            # If any value is 0, only update filament_type (current behavior)
            logging.info(f"Custom filament {custom_filament}: incomplete specs, only updating filament_type")
            self.process_custom_filament_type_only(custom_filament, run_load_macro)
        else:
            # If all values are provided, update both filament_type and filament_specs
            logging.info(f"Custom filament {custom_filament}: weight={weight}, density={density}, diameter={diameter}")
            self.process_custom_filament_with_specs(custom_filament, weight, density, diameter, run_load_macro)

    def process_custom_filament_type_only(self, custom_filament, run_load_macro=False):
        """Update only filament_type for custom filament (original behavior)"""
        
        # Define a callback function to handle the response
        def handle_response(response, method, params, *args):
            if response.get("error"):
                self._screen.show_popup_message(f"Failed to set custom filament type: {response['error']['message']}", level=3)
            else:
                self.shared_printer_config.filament = custom_filament
                self._screen.show_popup_message(f"Custom filament type set to {custom_filament}", level=1)
                self.update_button_labels()

        # Send Moonraker requests to set the custom filament type
        self._screen._ws.send_method(
            "server.database.post_item", 
            {
                "namespace": "HS3",
                "key": "filament_type",
                "value": custom_filament
            },
            handle_response
        )

        # Handle spool_tracker workflow with weight, density, and diameter
        if self.has_spool_tracker:
            self.handle_spool_tracker_workflow(custom_filament, 0, 0, 0)

        # Run load macro if requested
        if run_load_macro:
            self._screen._send_action(None, "printer.gcode.script",
                                      {"script": f"LOAD_FILAMENT SPEED={self.speed * 60}"})

    def process_custom_filament_with_specs(self, custom_filament, weight, density, diameter, run_load_macro=False):
        """Update both filament_type and filament_specs for custom filament"""
        
        # Define callback for filament_type update
        def handle_type_response(response, method, params, *args):
            if response.get("error"):
                self._screen.show_popup_message(f"Failed to set custom filament type: {response['error']['message']}", level=3)
            else:
                self.shared_printer_config.filament = custom_filament
                self.update_button_labels()

        # Define callback for filament_specs update
        def handle_specs_response(response, method, params, *args):
            if response.get("error"):
                self._screen.show_popup_message(f"Failed to set filament specs: {response['error']['message']}", level=3)
            else:
                self._screen.show_popup_message(f"Custom filament set: {custom_filament} ({weight}g, {density}g/cm³, {diameter}mm)", level=1)

        # Send Moonraker request to set the custom filament type
        self._screen._ws.send_method(
            "server.database.post_item", 
            {
                "namespace": "HS3",
                "key": "filament_type",
                "value": custom_filament
            },
            handle_type_response
        )

        # Send Moonraker request to set the filament specs
        self._screen._ws.send_method(
            "server.database.post_item", 
            {
                "namespace": "HS3",
                "key": "filament_specs",
                "value": {
                    "density": density,
                    "diameter": diameter
                }
            },
            handle_specs_response
        )

        # Handle spool_tracker workflow with weight, density, and diameter
        if self.has_spool_tracker:
            self.handle_spool_tracker_workflow(custom_filament, weight, density, diameter)

        # Run load macro if requested
        if run_load_macro:
            self._screen._send_action(None, "printer.gcode.script",
                                      {"script": f"LOAD_FILAMENT SPEED={self.speed * 60}"})

    def update_button_labels(self):
        # Create the filament label and replace the icon
        if self.shared_printer_config.filament == '':
            filament_text = "No Filament"
        else:
            filament_text = self.shared_printer_config.filament

        # Create a vertical box to hold the labels
        filament_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        filament_vbox.set_vexpand(True)  # Ensure the vbox expands to the full height
        filament_vbox.set_valign(Gtk.Align.CENTER)  # Center the box vertically

        # Create the filament type label
        filament_label = Gtk.Label()
        filament_label.set_markup(f'<span font="18"><b>{filament_text}</b></span>')
        filament_label.set_justify(Gtk.Justification.CENTER)
        filament_label.set_valign(Gtk.Align.CENTER)  # Center the label vertically

        # Create the "Set Filament" label
        set_filament_label = Gtk.Label(label="Set Filament")
        set_filament_label.set_valign(Gtk.Align.CENTER)  # Center the label vertically

        # Pack the labels into the vbox
        filament_vbox.pack_start(filament_label, True, True, 0)
        filament_vbox.pack_start(set_filament_label, True, True, 0)

        # Check if the button already has a child widget
        if self.buttons['set_filament'].get_children():
            # Remove the existing child widget (icon or any existing content)
            self.buttons['set_filament'].get_children()[0].destroy()

        # Add the new vbox with labels
        self.buttons['set_filament'].add(filament_vbox)

        # Reapply the "color3" style class to the button
        self.buttons['set_filament'].get_style_context().add_class("color3")

        # Show the button with its new content
        self.buttons['set_filament'].show_all()

        # Create the nozzle label and replace the icon
        if self.shared_printer_config.nozzle == '':
            nozzle_size_text = "No Nozzle"
        else:
            nozzle_size_text = f"{self.shared_printer_config.nozzle}mm"

        nozzle_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        nozzle_vbox.set_vexpand(True)
        nozzle_vbox.set_valign(Gtk.Align.CENTER)

        nozzle_label = Gtk.Label()
        nozzle_label.set_markup(f'<span font="18"><b>{nozzle_size_text}</b></span>')
        nozzle_label.set_justify(Gtk.Justification.CENTER)
        nozzle_label.set_valign(Gtk.Align.CENTER)

        set_nozzle_label = Gtk.Label(label="Set Nozzle")
        set_nozzle_label.set_valign(Gtk.Align.CENTER)

        nozzle_vbox.pack_start(nozzle_label, True, True, 0)
        nozzle_vbox.pack_start(set_nozzle_label, True, True, 0)

        if self.buttons['set_nozzle'].get_children():
            self.buttons['set_nozzle'].get_children()[0].destroy()

        self.buttons['set_nozzle'].add(nozzle_vbox)
        self.buttons['set_nozzle'].get_style_context().add_class("color3")
        self.buttons['set_nozzle'].show_all()

        # Overlay nozzle_type text on top of the extruder button icon
        nozzle_type_text = getattr(self.shared_printer_config, 'nozzle_type', '')
        icon_size = self._gtk.img_scale * self._gtk.button_image_scale

        for extruder in self._printer.get_tools():
            if extruder not in self.labels:
                continue
            if self._printer.extrudercount == 1:
                image_name = "extruder"
            else:
                n = self._printer.get_tool_number(extruder)
                image_name = f"extruder-{n}"

            extruder_image = self._gtk.Image(image_name, icon_size, icon_size)

            if nozzle_type_text:
                icon_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                icon_vbox.set_halign(Gtk.Align.CENTER)
                type_label = Gtk.Label()
                type_label.set_markup(f'<span font="9"><b>{nozzle_type_text}</b></span>')
                type_label.set_halign(Gtk.Align.CENTER)
                icon_vbox.pack_start(type_label, False, False, 0)
                icon_vbox.pack_start(extruder_image, False, False, 0)
                icon_vbox.show_all()
                self.labels[extruder].set_image(icon_vbox)
            else:
                self.labels[extruder].set_image(extruder_image)

        self.refresh_title()


    def load_filament_nozzle(self):

        # Define a callback function to handle the response
        def handle_response(response, method, params, *args):
            # Extract the values from the response
            try:
                result = response.get("result", {})
                value = result.get("value", {})
                self.shared_printer_config.filament = value.get("filament_type", "")  # Set the filament type
                self.shared_printer_config.nozzle = value.get("nozzle_size", "")      # Set the nozzle size
                self.shared_printer_config.nozzle_type = value.get("nozzle_type", "")  # Set the nozzle type
                
                # Update the icons based on the extracted values
                self.update_button_labels()

            except KeyError as e:
                print(f"Error processing response: {e}")


        # Send Moonraker requests to set the filament type, passing the callback
        self._screen._ws.send_method(
            "server.database.get_item", 
            {
                "namespace": "HS3",
            },
            handle_response  # Pass the callback here
        )

    def refresh_title(self):
        try:
            import os
            
            # Build the title string
            hostname = os.uname().nodename
            base_title = f"{hostname}.local"
            
            # Add filament and nozzle info if available
            filament = ""
            nozzle = ""
            
            if (hasattr(self.shared_printer_config, 'filament') and 
                self.shared_printer_config.filament):
                filament = self.shared_printer_config.filament
                base_title += f" | {filament}"
                
            if (hasattr(self.shared_printer_config, 'nozzle') and 
                self.shared_printer_config.nozzle):
                nozzle = self.shared_printer_config.nozzle
                base_title += f" {nozzle}mm"
            
            # Get spoolman weight and update title
            self._get_spoolman_weight_for_title(base_title, " | Extrude")
            
            return True
            
        except Exception as e:
            logging.error(f"Error updating title: {e}")
            return False

    def _get_spoolman_weight_for_title(self, base_title, suffix):
        """Get spool_tracker weight and update the title"""
        weight_text = ""
        try:
            result = None
            if getattr(self._screen, 'apiclient', None) is not None:
                result = self._screen.apiclient.send_request("server/spool_tracker/status")
            if result:
                tracker_data = result.get("result", {})
                if not tracker_data.get("can_track", False):
                    weight_text = " weight untracked"
                else:
                    remaining_weight = tracker_data.get("weights", {}).get("remaining_weight", 0)
                    if remaining_weight > 0:
                        weight_text = f" {round(remaining_weight, 1)}g"
                    else:
                        weight_text = " weight untracked"
        except Exception as e:
            logging.debug(f"Error requesting spool_tracker data: {e}")
        self._set_title(base_title + weight_text + suffix)

    def _set_title(self, title):
        # The title is rebuilt on every status push; only touch the widget and
        # the log when it actually changes.
        if title == self._last_title:
            return
        self._last_title = title
        self._screen.base_panel.titlelbl.set_label(title)
        logging.info(f"Title updated to: {title}")


    def check_spool_tracker_availability(self):
        """Check if spool_tracker is available in moonraker"""
        try:
            if not hasattr(self._screen, 'apiclient') or self._screen.apiclient is None:
                return False
            
            result = self._screen.apiclient.send_request("server/spool_tracker/status")
            return result is not None and "result" in result
            
        except Exception as e:
            logging.debug(f"Spool tracker not available: {e}")
            return False

class ClickOutsideDialog(Gtk.Dialog):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Set dialog to modal and always above other windows (optional)
        self.set_modal(True)
        self.set_keep_above(True)

        # Connect event to detect clicks outside the dialog
        self.get_toplevel().connect("button-press-event", self.on_button_press)

        self.add_background()

    def on_button_press(self, widget, event):
        # Get the dialog's allocation (size) and position
        allocation = self.get_allocation()
        x, y = self.get_position()
        # Check if the click is outside the dialog
        if not (x <= event.x_root <= x + allocation.width and
                y <= event.y_root <= y + allocation.height):
            self.destroy()
            return True  # Event handled
        return False  # Let other handlers process the event
    
    def add_background(self):
        # Create a CSS provider
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(b"""
            .pantheon-background {
                background-color: #1a191a;
            }
        """)

        # Get the content area (or main container) of the dialog
        content_area = self.get_content_area()

        # Apply the red-background class to the content area
        content_area.get_style_context().add_class('pantheon-background')

        # Add the CSS provider to the screen's default display
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )