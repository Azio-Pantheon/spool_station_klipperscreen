import logging
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk, Pango, GdkPixbuf
from panels.menu import Panel as MenuPanel
from ks_includes.widgets.heatergraph import HeaterGraph
from ks_includes.widgets.keypad import Keypad


class Panel(MenuPanel):
    def __init__(self, screen, title, items=None):
        super().__init__(screen, title, items)
        self.left_panel = None
        self.devices = {}
        self.active_heater = None
        self.h = self.f = 0
        self.main_menu = Gtk.Grid(row_homogeneous=True, column_homogeneous=True, hexpand=True, vexpand=True)
        scroll = self._gtk.ScrolledWindow()
        self.numpad_visible = False
        self.is_primed = True

        logging.info("### Making MainMenu")

        stats = self._printer.get_printer_status_data()["printer"]
        if stats["temperature_devices"]["count"] > 0 or stats["extruders"]["count"] > 0:
            self._gtk.reset_temp_color()
            self.main_menu.attach(self.create_left_panel(), 0, 0, 1, 1)
        if self._screen.vertical_mode:
            self.labels['menu'] = self.arrangeMenuItems(items, 3, True)
            scroll.add(self.labels['menu'])
            self.main_menu.attach(scroll, 0, 1, 1, 1)
        else:
            self.labels['menu'] = self.arrangeMenuItems(items, 2, True)
            scroll.add(self.labels['menu'])
            #self.main_menu.attach(scroll, 1, 0, 1, 1)
            self.prime_button = self._gtk.Button("complete","Prime Printer")
            style_context = self.prime_button.get_style_context()

            self.prime_button.get_style_context().add_class('print')

            self.prime_button.get_style_context().add_class('color1')

            self.prime_button.get_style_context().remove_class('text-button')
            self.prime_button.connect("clicked", self.prime_print)
            
            self.prime_button.set_size_request(470, 188)
            # Create an overlay widget
            self.overlay = Gtk.Overlay()

            # Add the scroll with the menu to the overlay as the base layer
            self.overlay.add(scroll)

            # Add the button to the overlay; this will be rendered on top
            self.overlay.add_overlay(self.prime_button)

            # Set button position relative to the overlay
            self.prime_button.set_halign(Gtk.Align.CENTER)  # Horizontal alignment (center, start, end)
            self.prime_button.set_valign(Gtk.Align.END)   # Vertical alignment (start, center, end)

            # Attach the overlay to the grid instead of the scroll directly
            self.main_menu.attach(self.overlay, 1, 0, 1, 1)

            self.prime_button.connect("realize", lambda widget: widget.hide())

        self.content.add(self.main_menu)

    def activate(self):
        self._async_update_filament_info()

    def deactivate(self):
        if self.active_heater is not None:
            self.hide_numpad()

    def add_device(self, device):
        logging.info(f"Adding device: {device}")

        temperature = self._printer.get_dev_stat(device, "temperature")
        if temperature is None:
            return False

        devname = device.split()[1] if len(device.split()) > 1 else device
        # Support for hiding devices by name
        if devname.startswith("_"):
            return False

        if device.startswith("extruder"):
            if self._printer.extrudercount > 1:
                image = f"extruder-{device[8:]}" if device[8:] else "extruder-0"
            else:
                image = "extruder"
            class_name = f"graph_label_{device}"
            dev_type = "extruder"
        elif device == "heater_bed":
            image = "bed"
            devname = "Heater Bed"
            class_name = "graph_label_heater_bed"
            dev_type = "bed"
        elif device.startswith("heater_generic"):
            self.h += 1
            image = "heater"
            class_name = f"graph_label_sensor_{self.h}"
            dev_type = "sensor"
        elif device.startswith("temperature_fan"):
            self.f += 1
            image = "fan"
            class_name = f"graph_label_fan_{self.f}"
            dev_type = "fan"
        elif self._config.get_main_config().getboolean("only_heaters", False):
            return False
        else:
            self.h += 1
            image = "heat-up"
            class_name = f"graph_label_sensor_{self.h}"
            dev_type = "sensor"

        can_target = self._printer.device_has_target(device)

        # Create device display (no graph functionality)
        hbox = Gtk.Box(spacing=10)
        image_widget = self._gtk.DeviceImage(image, self.bts)
        image_widget.set_margin_start(10)  
        label = Gtk.Label(label=self.prettify(devname))
        hbox.pack_start(image_widget, False, False, 0)
        hbox.pack_start(label, False, False, 0)
        hbox.get_style_context().add_class(class_name)
        hbox.set_border_width(3)
        hbox.set_size_request(300, -1)
        name = hbox

        temp = self._gtk.Button(label="", lines=1)
        temp.set_sensitive(False)
        
        if can_target:
            temp = self._gtk.Button(label="", lines=1, style=f"color{4}")
            temp.set_sensitive(True)
            temp.connect("clicked", self.show_numpad, device)

        self.devices[device] = {
            "class": class_name,
            "name": name,
            "temp": temp,
            "can_target": can_target
        }

        devices = sorted(self.devices)
        pos = devices.index(device) + 1

        self.labels['devices'].insert_row(pos)
        self.labels['devices'].attach(name, 0, pos, 1, 1)
        self.labels['devices'].attach(temp, 1, pos, 1, 1)
        self.labels['devices'].show_all()
        return True

    def change_target_temp(self, temp):
        name = self.active_heater.split()[1] if len(self.active_heater.split()) > 1 else self.active_heater
        temp = self.verify_max_temp(temp)
        if temp is False:
            return

        if self.active_heater.startswith('extruder'):
            self._screen._ws.klippy.set_tool_temp(self._printer.get_tool_number(self.active_heater), temp)
        elif self.active_heater == "heater_bed":
            self._screen._ws.klippy.set_bed_temp(temp)
        elif self.active_heater.startswith('heater_generic '):
            self._screen._ws.klippy.set_heater_temp(name, temp)
        elif self.active_heater.startswith('temperature_fan '):
            self._screen._ws.klippy.set_temp_fan_temp(name, temp)
        else:
            logging.info(f"Unknown heater: {self.active_heater}")
            self._screen.show_popup_message(_("Unknown Heater") + " " + self.active_heater)
        self._printer.set_dev_stat(self.active_heater, "target", temp)

    def verify_max_temp(self, temp):
        temp = int(temp)
        max_temp = int(float(self._printer.get_config_section(self.active_heater)['max_temp']))
        logging.debug(f"{temp}/{max_temp}")
        if temp > max_temp:
            self._screen.show_popup_message(_("Can't set above the maximum:") + f' {max_temp}')
            return False
        return max(temp, 0)

    def pid_calibrate(self, temp):
        if self.verify_max_temp(temp):
            script = {"script": f"PID_CALIBRATE HEATER={self.active_heater} TARGET={temp}"}
            self._screen._confirm_send_action(
                None,
                _("Initiate a PID calibration for:") + f" {self.active_heater} @ {temp} ÂºC"
                + "\n\n" + _("It may take more than 5 minutes depending on the heater power."),
                "printer.gcode.script",
                script
            )

    def create_left_panel(self):
        self.labels['devices'] = Gtk.Grid(vexpand=False)
        self.labels['devices'].get_style_context().add_class('heater-grid')

        name = Gtk.Label()
        temp = Gtk.Label(label=_("Temp (°C)"))
        temp.get_style_context().add_class("heater-grid-temp")

        self.labels['devices'].attach(name, 0, 0, 1, 1)
        self.labels['devices'].attach(temp, 1, 0, 1, 1)

        # REPLACE: Instead of HeaterGraph, create filament info panel
        # self.labels['da'] = HeaterGraph(self._screen, self._printer, self._gtk.font_size)
        self.labels['filament_info'] = self.create_filament_info_panel()

        scroll = self._gtk.ScrolledWindow(steppers=False)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.get_style_context().add_class('heater-list')
        scroll.add(self.labels['devices'])

        self.left_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.left_panel.add(scroll)
        
        # Add filament info panel instead of heater graph
        self.left_panel.add(self.labels['filament_info'])

        for d in self._printer.get_temp_devices():
            self.add_device(d)

        return self.left_panel

    def hide_numpad(self, widget=None):
        self.devices[self.active_heater]['name'].get_style_context().remove_class("button_active")
        self.active_heater = None

        if self._screen.vertical_mode:
            self.main_menu.remove_row(1)
            self.main_menu.attach(self.labels['menu'], 0, 1, 1, 1)
            self.main_menu.attach(self.overlay, 1, 0, 1, 1)

        else:
            self.main_menu.remove_column(1)
            self.main_menu.attach(self.labels['menu'], 1, 0, 1, 1)
            self.main_menu.attach(self.overlay, 1, 0, 1, 1)
        self.main_menu.show_all()
        self.numpad_visible = False
        self._screen.base_panel.set_control_sensitive(False, control='back')

    def process_update(self, action, data):
        if "print_stats" in data:
            if 'state' in data['print_stats']:
                if data["print_stats"]["state"] in ["cancelled", "error", "complete"]:
                    self.is_primed = False
                else:
                    self.is_primed = True

        # updating HS3 machine states
        if "machine_state" in data:
            if 'enable_prime' in data['machine_state']:
                    self._screen.shared_printer_config.enable_prime = data['machine_state']['enable_prime']
            if 'is_purging' in data['machine_state']:
                    self._screen.shared_printer_config.is_purging = data['machine_state']['is_purging']
        if self._screen.shared_printer_config.enable_prime == 1:
            if self.is_primed:
                self.hide_prime_button()
            else:
                self.show_prime_button()
        else:
            self.hide_prime_button()

        if action != "notify_status_update":
            return
        for x in self._printer.get_temp_devices():
            if x in data:
                self.update_temp(
                    x,
                    self._printer.get_dev_stat(x, "temperature"),
                    self._printer.get_dev_stat(x, "target"),
                    self._printer.get_dev_stat(x, "power"),
                )

    def show_numpad(self, widget, device):

        if self.active_heater is not None:
            self.devices[self.active_heater]['name'].get_style_context().remove_class("button_active")
        self.active_heater = device
        self.devices[self.active_heater]['name'].get_style_context().add_class("button_active")

        if "keypad" not in self.labels:
            self.labels["keypad"] = Keypad(self._screen, self.change_target_temp, self.pid_calibrate, self.hide_numpad)
        can_pid = self._printer.state not in ("printing", "paused") \
            and self._screen.printer.config[self.active_heater]['control'] == 'pid'
        self.labels["keypad"].clear()

        if self._screen.vertical_mode:
            self.main_menu.remove_row(1)
            self.main_menu.attach(self.labels["keypad"], 0, 1, 1, 1)
        else:
            self.main_menu.remove_column(1)
            self.main_menu.attach(self.labels["keypad"], 1, 0, 1, 1)
        self.main_menu.show_all()
        self.numpad_visible = True
        self._screen.base_panel.set_control_sensitive(True, control='back')

    def back(self):
        if self.numpad_visible:
            self.hide_numpad()
            return True
        return False

    def prime_print(self, widget):

        buttons = [
            {"name": _("Prime"), "response": Gtk.ResponseType.OK},
            {"name": _("Cancel"), "response": Gtk.ResponseType.CANCEL, "style": 'dialog-error'}
        ]

        label = Gtk.Label(hexpand=True, vexpand=True, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        label.set_markup(f"<b>{'Follow the instruction to prime the printer:'}</b>")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add(label)

        # Add another label with instructions
        instructions = """
        <b>1.</b> Clear bed of parts, prime line, and supports.\n
        <b>2.</b> Clean bed with alcohol and clean room wipe.\n
        <b>3.</b> Coat bed with adhesive.\n
        <b>4.</b> Inspect nozzle for goop, clean if goopy.
        """

        instructions_label = Gtk.Label(hexpand=True, vexpand=True, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        instructions_label.set_markup(instructions)

        # Adjust line spacing using Pango attributes
        attr_list = Pango.AttrList()
        attr_list.insert(Pango.attr_line_height_new(0.6))  # Adjust the value for tighter spacing (e.g., 0.8 is 80% of normal spacing)
        instructions_label.set_attributes(attr_list)
        
        # Add the instructions label to the box
        box.add(instructions_label)

        # Load the GIF
        gif_animation = GdkPixbuf.PixbufAnimation.new_from_file("/home/hs3/KlipperScreen/docs/img/SmallLandscape.gif")
        gif_image = Gtk.Image.new_from_animation(gif_animation)
        
        # Add the GIF to the box
        box.add(gif_image)

        self._gtk.Dialog(_("Prime Test"), buttons, box, self.prime_print_response)

    def prime_print_response(self, dialog, response_id):
        self._gtk.remove_dialog(dialog)
        if response_id == Gtk.ResponseType.OK:
            logging.info(f"Starting prime")
            self._screen._ws.klippy.gcode_script("SDCARD_RESET_FILE")
            self.is_primed = True

    def hide_prime_button(self):
        self.prime_button.hide()

    def show_prime_button(self):
        self.prime_button.show()

    def create_filament_info_panel(self):
        """Create panel showing filament type, nozzle size, and weight info as buttons"""
        
        # Get the filament and nozzle info
        filament_info = self.get_filament_nozzle_info()
        weight_info = self.get_spoolman_weight_info()
        
        # Determine how many buttons we need
        has_spoolman = weight_info is not None
        button_count = 3 if has_spoolman else 2
        
        # Create a grid for equally spaced buttons
        button_grid = Gtk.Grid(row_homogeneous=True, column_homogeneous=True, hexpand=True, vexpand=False)
        button_grid.set_column_spacing(1)
        button_grid.set_row_spacing(5)
        button_grid.set_margin_start(10)
        button_grid.set_margin_end(10)
        button_grid.set_margin_top(10)
        button_grid.set_margin_bottom(10)
        # Set a fixed height for the button panel to control the ratio
        button_grid.set_size_request(-1, 170)  # Fixed height
        
        # Filament Type Button
        filament_text = filament_info.get('filament', 'Not Set')
        filament_button = self._gtk.Button("filament", f"{filament_text}", "color1")
        filament_button.get_style_context().remove_class("color1")
        filament_button.get_style_context().add_class("button_fake")
        filament_button.get_style_context().add_class("large-text-button")

        filament_button.set_sensitive(False)
        # Nozzle Size Button  
        nozzle_text = f"{filament_info.get('nozzle', 'Not Set')}mm" if filament_info.get('nozzle') else 'Not Set'
        nozzle_button = self._gtk.Button("extruder", f"{nozzle_text}", "color1")
        nozzle_button.get_style_context().remove_class("color1")
        nozzle_button.get_style_context().add_class("button_fake")
        nozzle_button.set_sensitive(False) 
        
        if button_count == 2:
            # 2 buttons - each takes half the width
            button_grid.attach(filament_button, 0, 0, 1, 1)
            button_grid.attach(nozzle_button, 1, 0, 1, 1)
        else:
            # 3 buttons - each takes one third
            # Weight Button (only if spoolman enabled)
            weight_button = self._gtk.Button("spool", f"{weight_info}", "color1")
            weight_button.get_style_context().remove_class("color1")
            weight_button.get_style_context().add_class("button_fake")
            weight_button.set_sensitive(False)
            
            button_grid.attach(filament_button, 0, 0, 1, 1)
            button_grid.attach(nozzle_button, 1, 0, 1, 1)
            button_grid.attach(weight_button, 2, 0, 1, 1)
        
        # Store button references for later updates
        self.labels['filament_button'] = filament_button
        self.labels['nozzle_button'] = nozzle_button
        if has_spoolman:
            self.labels['weight_button'] = weight_button
        
        return button_grid

    def refresh_filament_info(self):
        """Refresh the filament info panel with current values - no async calls"""
        try:
            if ('filament_info' in self.labels and self.labels['filament_info'] and 
                hasattr(self, 'left_panel') and self.left_panel):
                
                # Remove old filament info panel
                if self.labels['filament_info'] in self.left_panel:
                    self.left_panel.remove(self.labels['filament_info'])
                
                # Create new one with updated info (this won't trigger async calls)
                self.labels['filament_info'] = self.create_filament_info_panel()
                self.left_panel.add(self.labels['filament_info'])
                self.left_panel.show_all()
                
        except Exception as e:
            logging.debug(f"Error refreshing filament info: {e}")

    def get_filament_nozzle_info(self):
        """Get filament and nozzle info from cached values only - no async calls"""
        try:
            config_info = {'filament': '', 'nozzle': ''}
            
            # Get current values from shared_printer_config
            if hasattr(self._screen, 'shared_printer_config'):
                if hasattr(self._screen.shared_printer_config, 'filament'):
                    config_info['filament'] = self._screen.shared_printer_config.filament or ''
                if hasattr(self._screen.shared_printer_config, 'nozzle'):
                    config_info['nozzle'] = self._screen.shared_printer_config.nozzle or ''
            
            return config_info
            
        except Exception as e:
            logging.debug(f"Error getting filament/nozzle info: {e}")
            return {'filament': '', 'nozzle': ''}
        
    def _async_update_filament_info(self):
        """Async update filament info using websocket (same method as base_panel)"""
        try:
            if not hasattr(self._screen, '_ws') or self._screen._ws is None:
                return
            
            def handle_config_response(response, method, params, *args):
                try:
                    result = response.get("result", {})
                    value = result.get("value", {})
                    filament = value.get("filament_type", "")
                    nozzle = value.get("nozzle_size", "")
                    
                    # Update shared_printer_config with fresh values
                    if hasattr(self._screen, 'shared_printer_config'):
                        if filament:
                            self._screen.shared_printer_config.filament = filament
                        if nozzle:
                            self._screen.shared_printer_config.nozzle = nozzle
                    
                    # Refresh the display with the updated values
                    self.refresh_filament_info()
                    
                except Exception as e:
                    logging.debug(f"Error processing async config response: {e}")
            
            # Use the same websocket method as base_panel.py
            self._screen._ws.send_method(
                "server.database.get_item",
                {"namespace": "HS3"},
                handle_config_response
            )
            
        except Exception as e:
            logging.debug(f"Error requesting async config update: {e}")

    def get_spoolman_weight_info(self):
        """Get spoolman weight info if spoolman is enabled"""
        try:
            # Check if spoolman is enabled first (same pattern as base_panel)
            if not hasattr(self._printer, 'spoolman') or not self._printer.spoolman:
                return None
                
            # Check if apiclient is available
            if not hasattr(self._screen, 'apiclient') or self._screen.apiclient is None:
                return None
                
            # Get active spool ID
            result = self._screen.apiclient.send_request("server/spoolman/spool_id")
            if not result:
                return None
            
            active_spool_id = result["result"]["spool_id"]
            if active_spool_id is None:
                return "Weight Untracked"
            
            # Get spool weight
            spool = self._screen.apiclient.post_request("server/spoolman/proxy", json={
                "request_method": "GET",
                "path": f"/v1/spool/{active_spool_id}",
            })
            
            if spool and "result" in spool:
                remaining_weight = spool["result"].get("remaining_weight")
                if remaining_weight is not None:
                    return f"{round(remaining_weight, 1)}g"
            
            return "Weight Untracked"
            
        except Exception as e:
            logging.debug(f"Error getting spoolman weight: {e}")
            return None