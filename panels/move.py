import re
import logging
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango, Gdk
from ks_includes.KlippyGcodes import KlippyGcodes
from ks_includes.screen_panel import ScreenPanel


class Panel(ScreenPanel):
    distances = ['.1', '.5', '1', '5', '10', '25', '50']
    distance = distances[-2]

    def __init__(self, screen, title):
        super().__init__(screen, title)

        if self.ks_printer_cfg is not None:
            dis = self.ks_printer_cfg.get("move_distances", '')
            if re.match(r'^[0-9,\.\s]+$', dis):
                dis = [str(i.strip()) for i in dis.split(',')]
                if 1 < len(dis) <= 7:
                    self.distances = dis
                    self.distance = self.distances[-2]

        self.settings = {}
        self.menu = ['move_menu']
        self.buttons = {
            'home': self._gtk.Button("home", _("Home"), "color1"),
            'bed_to_top': self._gtk.Button("z-farther", "Bed to Top", "color1"),
            'bed_to_middle': self._gtk.Button("z-farther", "Bed to Middle", "color1"),
            'bed_to_bottom': self._gtk.Button("z-closer", "Bed to Bottom", "color1"),
            'confirm': self._gtk.Button("complete","Confirm Move","color1"),
            'precise_move': self._gtk.Button("move","Precise Move","color1") 
        }

        self.buttons['home'].connect("clicked", self.home)
        self.buttons['bed_to_top'].connect("clicked", self.toggle_bed_selection, "bed_to_top")
        self.buttons['bed_to_middle'].connect("clicked", self.toggle_bed_selection, "bed_to_middle")
        self.buttons['bed_to_bottom'].connect("clicked", self.toggle_bed_selection, "bed_to_bottom")
        self.buttons['confirm'].connect("clicked", self.confirm_move)
        
        adjust = self._gtk.Button("settings", None, "color2", 1, Gtk.PositionType.LEFT, 1)
        adjust.connect("clicked", self.load_menu, 'options', _('Settings'))
        adjust.set_hexpand(False)

        self.buttons['precise_move'].connect("clicked", self.menu_item_clicked, {
            "panel": "precise_move", "name": _("Precise Move")})

        grid = Gtk.Grid(row_homogeneous=True, column_homogeneous=True)
        grid.attach(self.buttons['bed_to_top'], 0, 0, 1, 1)
        grid.attach(self.buttons['bed_to_middle'], 0, 1, 1, 1)
        grid.attach(self.buttons['bed_to_bottom'], 0, 2, 1, 1)
        grid.attach(self.buttons['confirm'], 1, 2, 1, 1)
        grid.attach(self.buttons['home'], 1, 0, 1, 1)
        grid.attach(self.buttons['precise_move'], 1, 1, 1, 1)

        # Create the drawing area
        self.drawing_area = Gtk.DrawingArea()
        self.drawing_area.set_size_request(540, 540)
        self.drawing_area.connect("draw", self.on_draw)
        # Initialize toolhead position
        self.toolhead_position = {'x': None, 'y': None}
        self.targeted_toolhead_position= {'x': None, 'y': None}
        self.selected_bed_button = None
        self.axes_maximum = None
        self.z_maximum = None
        self.scale_factor = None
        self.target_xy = None
        self.target_z = None

        box = Gtk.Box()
        box.pack_start(self.drawing_area, False, False, 0)
        #Enable touch events
        self.drawing_area.set_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK)
        self.drawing_area.connect("button-press-event", self.on_grid_press)
        #self.drawing_area.connect("button-release-event", self.on_grid_release)

        
        for p in ('pos_x', 'pos_y', 'pos_z'):
            self.labels[p] = Gtk.Label()

        bottomgrid = Gtk.Grid(row_homogeneous=True, column_homogeneous=True)
        bottomgrid.set_direction(Gtk.TextDirection.LTR)
        bottomgrid.attach(self.labels['pos_x'], 0, 0, 1, 1)
        bottomgrid.attach(self.labels['pos_y'], 1, 0, 1, 1)
        bottomgrid.attach(self.labels['pos_z'], 2, 0, 1, 1)
        #bottomgrid.attach(self.labels['move_dist'], 0, 1, 3, 1)

        self.labels['move_menu'] = Gtk.Grid(row_homogeneous=False, column_homogeneous=False)
        self.labels['move_menu'].attach(grid, 3, 0, 2, 3)
        self.labels['move_menu'].attach(bottomgrid, 0, 4, 3, 1)
        
        self.labels['move_menu'].attach(box, 0, 0, 3, 3)
        self.content.add(self.labels['move_menu'])
        printer_cfg = self._printer.get_config_section("printer")
        # The max_velocity parameter is not optional in klipper config.
        max_velocity = int(float(printer_cfg["max_velocity"]))
        if max_velocity <= 1:
            logging.error(f"Error getting max_velocity\n{printer_cfg}")
            max_velocity = 50
        if "max_z_velocity" in printer_cfg:
            max_z_velocity = max(int(float(printer_cfg["max_z_velocity"])), 10)
        else:
            max_z_velocity = max_velocity

        configurable_options = [
            {"invert_x": {"section": "main", "name": _("Invert X"), "type": "binary", "value": "False"}},
            {"invert_y": {"section": "main", "name": _("Invert Y"), "type": "binary", "value": "False"}},
            {"invert_z": {"section": "main", "name": _("Invert Z"), "type": "binary", "value": "False"}},
            {"move_speed_xy": {
                "section": "main", "name": _("XY Speed (mm/s)"), "type": "scale", "value": "50",
                "range": [1, max_velocity], "step": 1}},
            {"move_speed_z": {
                "section": "main", "name": _("Z Speed (mm/s)"), "type": "scale", "value": "10",
                "range": [1, max_z_velocity], "step": 1}}
        ]
        self.labels['options_menu'] = self._gtk.ScrolledWindow()
        self.labels['options'] = Gtk.Grid()
        self.labels['options_menu'].add(self.labels['options'])
        for option in configurable_options:
            name = list(option)[0]
            self.add_option('options', self.settings, name, option[name])

    def process_update(self, action, data):
        if action != "notify_status_update":
            return
        homed_axes = self._printer.get_stat("toolhead", "homed_axes")
        if homed_axes == "xyz":
            if "gcode_move" in data and "gcode_position" in data["gcode_move"]:
                self.labels['pos_x'].set_text(f"X: {data['gcode_move']['gcode_position'][0]:.2f}")
                self.labels['pos_y'].set_text(f"Y: {data['gcode_move']['gcode_position'][1]:.2f}")
                self.labels['pos_z'].set_text(f"Z: {data['gcode_move']['gcode_position'][2]:.2f}")
        else:
            if "x" in homed_axes:
                if "gcode_move" in data and "gcode_position" in data["gcode_move"]:
                    self.labels['pos_x'].set_text(f"X: {data['gcode_move']['gcode_position'][0]:.2f}")
            else:
                self.labels['pos_x'].set_text("X: ?")
            if "y" in homed_axes:
                if "gcode_move" in data and "gcode_position" in data["gcode_move"]:
                    self.labels['pos_y'].set_text(f"Y: {data['gcode_move']['gcode_position'][1]:.2f}")
            else:
                self.labels['pos_y'].set_text("Y: ?")
            if "z" in homed_axes:
                if "gcode_move" in data and "gcode_position" in data["gcode_move"]:
                    self.labels['pos_z'].set_text(f"Z: {data['gcode_move']['gcode_position'][2]:.2f}")
            else:
                self.labels['pos_z'].set_text("Z: ?")

        if self._printer.get_stat("toolhead", "axis_maximum") is not None and self.axes_maximum is None:
            self.axes_maximum = self._printer.get_stat("toolhead", "axis_maximum")
            min_axis_max = min(self.axes_maximum[0], self.axes_maximum[1])
            self.z_maximum = self.axes_maximum[2]
            self.scale_factor = 540 / min_axis_max

        gcode_position = data.get("gcode_move", {}).get("gcode_position", [None, None, None])
        if gcode_position[0] is not None and gcode_position[1] is not None:
            # Flip x and y because of printers coordianates are different
            self.update_toolhead_position(gcode_position[1],gcode_position[0])

    def move(self, widget, axis, direction):
        if self._config.get_config()['main'].getboolean(f"invert_{axis.lower()}", False):
            direction = "-" if direction == "+" else "+"

        dist = f"{direction}{self.distance}"
        config_key = "move_speed_z" if axis == "Z" else "move_speed_xy"
        speed = None if self.ks_printer_cfg is None else self.ks_printer_cfg.getint(config_key, None)
        if speed is None:
            speed = self._config.get_config()['main'].getint(config_key, 20)
        speed = 60 * max(1, speed)
        script = f"{KlippyGcodes.MOVE_RELATIVE}\nG0 {axis}{dist} F{speed}"
        self._screen._send_action(widget, "printer.gcode.script", {"script": script})
        if self._printer.get_stat("gcode_move", "absolute_coordinates"):
            self._screen._ws.klippy.gcode_script("G90")

    def add_option(self, boxname, opt_array, opt_name, option):
        name = Gtk.Label(hexpand=True, vexpand=True, halign=Gtk.Align.START, valign=Gtk.Align.CENTER, wrap=True)
        name.set_markup(f"<big><b>{option['name']}</b></big>")
        name.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)

        dev = Gtk.Box(spacing=5,
                      hexpand=True, vexpand=False, valign=Gtk.Align.CENTER)
        dev.get_style_context().add_class("frame-item")
        dev.add(name)

        if option['type'] == "binary":
            box = Gtk.Box(hexpand=False)
            switch = Gtk.Switch(hexpand=False, vexpand=False,
                                width_request=round(self._gtk.font_size * 7),
                                height_request=round(self._gtk.font_size * 3.5),
                                active=self._config.get_config().getboolean(option['section'], opt_name))
            switch.connect("notify::active", self.switch_config_option, option['section'], opt_name)
            box.add(switch)
            dev.add(box)
        elif option['type'] == "scale":
            dev.set_orientation(Gtk.Orientation.VERTICAL)
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL,
                                             min=option['range'][0], max=option['range'][1], step=option['step'])
            scale.set_hexpand(True)
            scale.set_value(int(self._config.get_config().get(option['section'], opt_name, fallback=option['value'])))
            scale.set_digits(0)
            scale.connect("button-release-event", self.scale_moved, option['section'], opt_name)
            dev.add(scale)

        opt_array[opt_name] = {
            "name": option['name'],
            "row": dev
        }

        opts = sorted(list(opt_array), key=lambda x: opt_array[x]['name'])
        pos = opts.index(opt_name)

        self.labels[boxname].insert_row(pos)
        self.labels[boxname].attach(opt_array[opt_name]['row'], 0, pos, 1, 1)
        self.labels[boxname].show_all()

    def back(self):
        if len(self.menu) > 1:
            self.unload_menu()
            return True
        return False

    def home(self, widget):
        if "delta" in self._printer.get_config_section("printer")['kinematics']:
            self._screen._send_action(widget, "printer.gcode.script", {"script": 'G28'})
            return
        name = "homing"
        disname = self._screen._config.get_menu_name("move", name)
        menuitems = self._screen._config.get_menu_items("move", name)
        self._screen.show_panel("menu", disname, items=menuitems)

    def on_draw(self, widget, cr):
        # Get the dimensions of the drawing area
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()
        #width = 540
        #height = 540
        # Draw the grid
        self.draw_grid(cr, width, height)

        # Draw the toolhead position
        self.draw_toolhead(cr, width, height)
        # Draw the targeted toolhead position
        self.draw_targeted_toolhead(cr, width, height)
        

    def draw_grid(self, cr, width, height):
        # Set the grid color
        cr.set_source_rgb(0.9, 0.9, 0.9)  # Light gray

        # Define grid spacing
        grid_spacing = 30  # pixels

        # Draw vertical lines
        for x in range(0, width, grid_spacing):
            cr.move_to(x, 0)
            cr.line_to(x, height)
            cr.stroke()

        # Draw horizontal lines
        for y in range(0, height, grid_spacing):
            cr.move_to(0, y)
            cr.line_to(width, y)
            cr.stroke()

    def draw_toolhead(self, cr, width, height):
        # Map toolhead position to drawing area coordinates
        x = self.toolhead_position['x']
        y = self.toolhead_position['y']

        # Set the toolhead color
        cr.set_source_rgb(1.0, 0, 0)  # Red

        # Define toolhead marker size
        marker_size = 20  # pixels

        # Draw the toolhead as a rectangle
        cr.rectangle(x - marker_size / 2, y - marker_size / 2, marker_size, marker_size)
        cr.fill()

    def draw_targeted_toolhead(self, cr, width, height):
        if self.targeted_toolhead_position['x'] is None or self.targeted_toolhead_position['y'] is None:
            return  # No target set
        # Map toolhead position to drawing area coordinates
        x = self.targeted_toolhead_position['x']
        y = self.targeted_toolhead_position['y']
        # Set the toolhead color
        cr.set_source_rgb(0, 1.0, 0)  # Red

        # Define toolhead marker size
        marker_size = 20  # pixels

        # Draw the toolhead as a rectangle
        cr.rectangle(x - marker_size / 2, y - marker_size / 2, marker_size, marker_size)
        cr.fill()

    def update_toolhead_position(self, x, y):
        mapped_x, mapped_y = self.actual_to_grid(x, y)
        self.toolhead_position['x'] = mapped_x
        self.toolhead_position['y'] = mapped_y

        # If toolhead reaches the target, clear the target
        if (self.targeted_toolhead_position['x'] is not None and self.targeted_toolhead_position['y'] is not None and
            int(x) == int(self.targeted_toolhead_position['x']) and
            int(y) == int(self.targeted_toolhead_position['y'])):
            self.targeted_toolhead_position = {'x': None, 'y': None}  # Clear the target

        # Redraw the drawing area
        self.drawing_area.queue_draw()

    def issue_move_command(self, x, y):
        self._screen.show_popup_message((f"Move to {x},{y}"), level=2)

    def on_grid_press(self, widget, event):
        """Handles user press on the drawing area."""
        if event.button == 1:  # Left mouse button or touch
            x = int(event.x)
            y = int(event.y)
            self.targeted_toolhead_position['x'] = x
            self.targeted_toolhead_position['y'] = y
            mapped_x, mapped_y = self.grid_to_actual(x, y)
            #flipping coordinates here because our printer axises
            self.target_xy = [int(mapped_y), int(mapped_x)]
            #self.issue_move_command(int(mapped_y), int(mapped_x))
        # Redraw the drawing area
        self.drawing_area.queue_draw()

    def toggle_bed_selection(self, widget, position):    
        # Deselect previous button if a different one is clicked
        if self.selected_bed_button:
            self.buttons[self.selected_bed_button].get_style_context().remove_class("selected")

        # Toggle selection state
        if self.selected_bed_button == position:
            self.selected_bed_button = None  # Unselect if clicked again
            self.target_z = None
        else:
            self.selected_bed_button = position
            if self.selected_bed_button == "bed_to_top":
                self.target_z = 20
            elif self.selected_bed_button == "bed_to_middle":
                self.target_z = self.z_maximum / 2
            elif self.selected_bed_button == "bed_to_bottom":
                self.target_z = self.z_maximum - 5
            widget.get_style_context().add_class("selected")  # Highlight the button

    def confirm_move(self, widget):
        """Sends the move command and clears the selection."""
        if self.target_xy is None and self.selected_bed_button is None:
            self._screen.show_popup_message("No position is selected!", level=2)
            return  # No action if nothing is selected

        move_command = "G90\nG0"  # Absolute positioning move
        # Handle XY movement
        if self.target_xy is not None:
            x, y = self.target_xy
            move_command += f" X{x} Y{y}"
            speed = self.ks_printer_cfg.getint("move_speed_xy", None) if self.ks_printer_cfg else None
            if speed is None:
                speed = self._config.get_config()['main'].getint("move_speed_xy", 20)
            speed = 60 * max(1, speed)  # Convert to mm/min

        # Handle Z movement
        if self.target_z is not None:
            move_command += f" Z{self.target_z}"
            speed = self.ks_printer_cfg.getint("move_speed_z", None) if self.ks_printer_cfg else None
            if speed is None:
                speed = self._config.get_config()['main'].getint("move_speed_z", 20)
            speed = 60 * max(1, speed)  # Convert to mm/min

        # Add speed to the move command
        move_command += f" F{speed}"

        # Send the command to Klipper
        self._screen._send_action(widget, "printer.gcode.script", {"script": move_command})

        # Reset target positions after move
        self.target_xy = None
        self.target_z = None
        self.targeted_toolhead_position= {'x': None, 'y': None}

        if not self._printer.get_stat("gcode_move", "absolute_coordinates"):
            self._screen._ws.klippy.gcode_script("G90")

        # Clear selection
        self.selected_bed_button = None
        for btn in ['bed_to_top', 'bed_to_middle', 'bed_to_bottom']:
            self.buttons[btn].get_style_context().remove_class("selected")

    def actual_to_grid(self, x, y):
        """Maps a (x, y) toolhead position to the 540x540 grid."""
        # Map the original coordinates
        mapped_x = x * self.scale_factor
        mapped_y = y * self.scale_factor

        return mapped_x, mapped_y
    
    def grid_to_actual(self, grid_x, grid_y):
        # Convert grid coordinates back to real-world printer coordinates
        actual_x = grid_x / self.scale_factor
        actual_y = grid_y / self.scale_factor

        return actual_x, actual_y
    