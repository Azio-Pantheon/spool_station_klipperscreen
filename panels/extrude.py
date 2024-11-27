import logging
import re
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango, Gdk  
from ks_includes.KlippyGcodes import KlippyGcodes
from ks_includes.screen_panel import ScreenPanel
from ks_includes.widgets.autogrid import AutoGrid


class Panel(ScreenPanel):

    def __init__(self, screen, title, shared_printer_config):
        super().__init__(screen, title)
        self.current_extruder = self._printer.get_stat("toolhead", "extruder")
        macros = self._printer.get_config_section_list("gcode_macro ")
        self.load_filament = any("LOAD_FILAMENT" in macro.upper() for macro in macros)
        self.unload_filament = any("UNLOAD_FILAMENT" in macro.upper() for macro in macros)

        self.shared_printer_config = shared_printer_config
        self.keyboard_visible = False
        self.wet_filament_purge_checked = False

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
        if i < (limit - 1) and self._printer.spoolman:
            xbox.add(self.buttons['spoolman'])

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
        
        # + 1 because we are adding wet_filament_purge here
        if (len(filament_sensors) + 1) > 0:
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

            # Add wet_filament_purge
            wet_filament_purge_key = "wet_filament_purge"
            wet_filament_purge_name = "Wet Filament Purge"
            self.labels[wet_filament_purge_key] = {
                'label': Gtk.Label(label=self.prettify(wet_filament_purge_name), hexpand=True, halign=Gtk.Align.CENTER,
                                ellipsize=Pango.EllipsizeMode.END),
                'switch': Gtk.Switch(width_request=round(self._gtk.font_size * 2),
                                    height_request=round(self._gtk.font_size)),
                'box': Gtk.Box()
            }
            self.labels[wet_filament_purge_key]['switch'].connect("notify::active", self.enable_disable_wet_filament_purge,
                                                                wet_filament_purge_name, wet_filament_purge_key)
            self.labels[wet_filament_purge_key]['box'].pack_start(self.labels[wet_filament_purge_key]['label'], True, True, 10)
            self.labels[wet_filament_purge_key]['box'].pack_start(self.labels[wet_filament_purge_key]['switch'], False, False, 0)
            self.labels[wet_filament_purge_key]['box'].get_style_context().add_class("filament_sensor")
            sensors.attach(self.labels[wet_filament_purge_key]['box'], len(filament_sensors), 0, 1, 1)

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
        self.enable_buttons(self._printer.state in ("ready", "paused"))

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

        if ("toolhead" in data and "wet_filament_purge" in data["toolhead"] and
            not self.wet_filament_purge_checked):
            self.labels["wet_filament_purge"]['switch'].set_active(True) 
            self.wet_filament_purge_checked = True  # Mark the check as completed
           

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
                self._screen._send_action(widget, "printer.gcode.script",
                                          {"script": f"LOAD_FILAMENT SPEED={self.speed * 60}"})
            self.open_filament_selection(widget)

    def enable_disable_wet_filament_purge(self, switch, gparams, name, x):
        # Determine the state of the switch (True for active, False for inactive)
        if switch.get_active():
            wet_filament_purge_enabled = 1
            self.labels["wet_filament_purge"]['box'].get_style_context().add_class("filament_sensor_detected")

        else:
            wet_filament_purge_enabled = 0
            self.labels["wet_filament_purge"]['box'].get_style_context().remove_class("filament_sensor_detected")


        # Define a callback function to handle the response
        def handle_response(response, method, params, *args):
            if response.get("error"):
                self._screen.show_popup_message(
                    f"Failed to set wet filament purge: {response['error']['message']}",
                    level=3
                )
            else:
                # Update shared_printer_config to reflect the new state
                self.shared_printer_config.wet_filament_purge = wet_filament_purge_enabled

                # Show a success message
                state_str = "enabled" if wet_filament_purge_enabled else "disabled"
                self._screen.show_popup_message(
                    f"Wet filament purge {state_str}.",
                    level=1
                )

        # Send Moonraker request to update the wet_filament_purge state
        self._screen._ws.send_method(
            "server.database.post_item",
            {
                "namespace": "HS3",
                "key": "wet_filament_purge",
                "value": wet_filament_purge_enabled
            },
            handle_response  # Pass the callback here
        )

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

    def open_filament_selection(self, widget):
        # List of filament types including a custom option
        filament_types = ["PETG-CF", "PA-CF", "PA-GF", "TPU", "Custom"]

        # Create the dialog for selecting filament types
        dialog = ClickOutsideDialog(title="Select Filament Type",
                                    transient_for=widget.get_toplevel(),
                                    flags=Gtk.DialogFlags.MODAL)
        dialog.set_default_size(600, 250)

        current_x, current_y = dialog.get_position()
        dialog.move(current_x, current_y - 80)  

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

        # Create buttons for each filament type and add them to the grid
        for i, filament in enumerate(filament_types):
            button = Gtk.Button(label=filament)
            button.get_style_context().add_class("color1")
            button.set_size_request(150, 200)
            if filament == "Custom":
                button.connect("clicked", self.open_custom_filament_dialog, dialog)
            else:
                button.connect("clicked", self.set_filament_type, filament, dialog)
            grid.attach(button, i % 3, i // 3, 1, 1)  # Arrange buttons in 3 columns

        # Add the grid to the dialog content area and show all
        content_area = dialog.get_content_area()
        content_area.add(grid)
        dialog.show_all()

    def set_filament_type(self, widget, filament_type, dialog):
        # Close the dialog when a filament type is selected
        dialog.destroy()

        # Define a callback function to handle the response
        def handle_response(response, method, params, *args):
            if response.get("error"):
                self._screen.show_popup_message(f"Failed to set filament type: {response['error']['message']}", level=3)
            else:
                self.shared_printer_config.filament = filament_type
                self._screen.show_popup_message(f"Filament type set to {filament_type}", level=1)
                self.update_button_labels()


        # Send Moonraker requests to set the filament type, passing the callback
        self._screen._ws.send_method(
            "server.database.post_item", 
            {
                "namespace": "HS3",
                "key": "filament_type",
                "value": filament_type
            },
            handle_response  # Pass the callback here
        )

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
            button.connect("clicked", self.set_nozzle_size, size, dialog)
            grid.attach(button, i % 2, i // 2, 1, 1)  # Arrange buttons in 3 columns

        # Add the grid to the dialog content area and show all
        content_area = dialog.get_content_area()
        content_area.add(grid)
        dialog.show_all()

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

    def open_custom_filament_dialog(self, widget, parent_dialog):
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
        entry.set_placeholder_text("Enter custom filament name")

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
        confirm_button.connect("clicked", self.confirm_custom_filament, entry, custom_dialog)
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

    def confirm_custom_filament(self, widget, entry, dialog):
        # Get the custom filament name from the entry
        custom_filament = entry.get_text()

        # Close the custom filament dialog
        dialog.destroy()

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
            handle_response  # Pass the callback here
        )

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
            nozzle_text = "No Nozzle"
        else:
            nozzle_text = self.shared_printer_config.nozzle

        # Create a vertical box to hold the labels
        nozzle_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        nozzle_vbox.set_vexpand(True)  # Ensure the vbox expands to the full height
        nozzle_vbox.set_valign(Gtk.Align.CENTER)  # Center the box vertically

        # Create the nozzle type label
        nozzle_label = Gtk.Label()
        nozzle_label.set_markup(f'<span font="18"><b>{nozzle_text}mm</b></span>')
        nozzle_label.set_justify(Gtk.Justification.CENTER)
        nozzle_label.set_valign(Gtk.Align.CENTER)  # Center the label vertically

        # Create the "Set Nozzle Size" label
        set_nozzle_label = Gtk.Label(label="Set Nozzle Size")
        set_nozzle_label.set_valign(Gtk.Align.CENTER)  # Center the label vertically

        # Pack the labels into the vbox
        nozzle_vbox.pack_start(nozzle_label, True, True, 0)
        nozzle_vbox.pack_start(set_nozzle_label, True, True, 0)

        # Check if the button already has a child widget
        if self.buttons['set_nozzle'].get_children():
            # Remove the existing child widget (icon or any existing content)
            self.buttons['set_nozzle'].get_children()[0].destroy()

        # Add the new vbox with labels
        self.buttons['set_nozzle'].add(nozzle_vbox)

        # Reapply the "color3" style class to the button
        self.buttons['set_nozzle'].get_style_context().add_class("color3")

        # Show the button with its new content
        self.buttons['set_nozzle'].show_all()

    def load_filament_nozzle(self):

        # Define a callback function to handle the response
        def handle_response(response, method, params, *args):
            # Extract the values from the response
            try:
                result = response.get("result", {})
                value = result.get("value", {})
                self.shared_printer_config.filament = value.get("filament_type", "")  # Set the filament type
                self.shared_printer_config.nozzle = value.get("nozzle_size", "")      # Set the nozzle size
                
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