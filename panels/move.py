import re
import logging
import gi
import cairo

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango, Gdk, GdkPixbuf
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
            'home': self._gtk.Button("home", _("Home All"), "color1"),
            'precise_move': self._gtk.Button("move","Precise Move","color1") 
        }

        self.buttons['home'].connect("clicked", self.home_all)
        
        adjust = self._gtk.Button("settings", None, "color2", 1, Gtk.PositionType.LEFT, 1)
        adjust.connect("clicked", self.load_menu, 'options', _('Settings'))
        adjust.set_hexpand(False)

        self.buttons['precise_move'].connect("clicked", self.menu_item_clicked, {
            "panel": "precise_move", "name": _("Precise Move")})

        grid = Gtk.Grid(row_homogeneous=True, column_homogeneous=True)
        grid.attach(self.buttons['home'], 0, 0, 1, 1)
        grid.attach(self.buttons['precise_move'], 0, 1, 1, 1)

        ###### Create the gantry drawing area
        self.gantry_image = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            "/home/hs3/KlipperScreen/styles/Pantheon/images/gantry_image.png",  # Replace with your actual file path
            540, 540,  # Set background size
            False  # Preserve aspect ratio
        )

        #self.toolhead_image = GdkPixbuf.Pixbuf.new_from_file_at_scale(
        #    "/path/to/toolhead.png",  # Replace with your actual file path
        #    40, 40,  # Scale toolhead size (adjust as needed)
        #    True  # Preserve aspect ratio
        #)
        self.gantry_drawing_area = Gtk.DrawingArea()
        self.gantry_drawing_area.set_size_request(540, 540)
        self.gantry_drawing_area.connect("draw", self.on_draw)
        # Initialize toolhead position
        self.toolhead_position = {'x': 0, 'y': 0}
        self.targeted_toolhead_position= {'x': None, 'y': None}
        self.axis_maximum = None
        self.axis_minimum = None

        self.scale_factor_x = None
        self.scale_factor_y = None
        self.scale_factor_z = None

        self.target_xy = None
        self.target_z = None
        self.dragging_toolhead = False

        gantry_box = Gtk.Box()
        gantry_box.pack_start(self.gantry_drawing_area, False, False, 0)
        #Enable touch events
        self.gantry_drawing_area.set_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK | Gdk.EventMask.POINTER_MOTION_MASK)        
        self.gantry_drawing_area.connect("button-press-event", self.on_toolhead_press)
        self.gantry_drawing_area.connect("motion-notify-event", self.on_toolhead_drag)
        self.gantry_drawing_area.connect("button-release-event", self.on_toolhead_release)
        
        ###### Create the build tray drawing area
        self.tray_image = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            "/home/hs3/KlipperScreen/styles/Pantheon/images/build_tray_image.png",  # Replace with your actual file path
            180, 540,  # Set background size
            False  # Preserve aspect ratio
        )
        self.tray_drawing_area = Gtk.DrawingArea()
        self.tray_drawing_area.set_size_request(180, 540)
        self.tray_drawing_area.connect("draw", self.tray_on_draw)
        # Initialize tray position
        self.tray_position = 0
        self.targeted_tray_position= None
        self.dragging_tray = False

        tray_box = Gtk.Box()
        tray_box.pack_start(self.tray_drawing_area, False, False, 0)
        #Enable touch events
        self.tray_drawing_area.set_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK | Gdk.EventMask.POINTER_MOTION_MASK)        
        self.tray_drawing_area.connect("button-press-event", self.on_tray_press)
        self.tray_drawing_area.connect("motion-notify-event", self.on_tray_drag)
        self.tray_drawing_area.connect("button-release-event", self.on_tray_release)


        for p in ('pos_x', 'pos_y', 'pos_z'):
            self.labels[p] = Gtk.Label()

        bottomgrid = Gtk.Grid(row_homogeneous=True, column_homogeneous=True)
        bottomgrid.set_direction(Gtk.TextDirection.LTR)
        bottomgrid.attach(self.labels['pos_x'], 0, 0, 1, 1)
        bottomgrid.attach(self.labels['pos_y'], 1, 0, 1, 1)
        bottomgrid.attach(self.labels['pos_z'], 2, 0, 1, 1)
        #bottomgrid.attach(self.labels['move_dist'], 0, 1, 3, 1)

        self.labels['move_menu'] = Gtk.Grid(row_homogeneous=False, column_homogeneous=False)
        self.labels['move_menu'].set_column_spacing(20)  # Adds 10px padding between columns

        self.labels['move_menu'].attach(grid, 4, 0, 2, 3)
        self.labels['move_menu'].attach(bottomgrid, 0, 4, 4, 1)
        
        self.labels['move_menu'].attach(gantry_box, 0, 0, 3, 3)
        self.labels['move_menu'].attach(tray_box, 3, 0, 1, 3)
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
                self.update_toolhead_tray_position(data['gcode_move']['gcode_position'][1],data['gcode_move']['gcode_position'][0],data['gcode_move']['gcode_position'][2])
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

        if self._printer.get_stat("toolhead", "axis_maximum") is not None and self.axis_maximum is None:
            self.axis_maximum = self._printer.get_stat("toolhead", "axis_maximum")

        if self._printer.get_stat("toolhead", "axis_minimum") is not None and self.axis_minimum is None:
            self.axis_minimum = self._printer.get_stat("toolhead", "axis_minimum")
            # Calculate total range for X and Y and Z
            x_range = self.axis_maximum[0] - self.axis_minimum[0]  # X max - X min
            y_range = self.axis_maximum[1] - self.axis_minimum[1]  # Y max - Y min
            z_range = self.axis_maximum[2] - self.axis_minimum[2]  # Z max - Z min
            # Choose the scaling factor based on the larger range (maintaining aspect ratio)
            self.scale_factor_x = 540 / x_range
            self.scale_factor_y = 540 / y_range
            self.scale_factor_z = 540 / z_range


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

    def home_all(self):
        self._screen._ws.klippy.gcode_script("G28")

    def on_draw(self, widget, cr):
        # Get the dimensions of the drawing area
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()
        #width = 540
        #height = 540
        # Draw the grid
        self.draw_background(cr, width, height)

        # Draw the toolhead position
        self.draw_toolhead(cr, width, height)
        # Draw the targeted toolhead position
        self.draw_targeted_toolhead(cr, width, height)
        

    def draw_background(self, cr, width, height):
        """Draws the background image instead of a grid."""

        # Convert Pixbuf to Cairo Image
        image_surface = Gdk.cairo_surface_create_from_pixbuf(self.gantry_image, 1)

        # Draw the background image
        cr.set_source_surface(image_surface, 0, 0)
        cr.paint()

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

    def update_toolhead_tray_position(self, x, y, z):
        mapped_x, mapped_y, mapped_z = self.actual_to_grid(x, y, z)
        self.toolhead_position['x'] = mapped_x
        self.toolhead_position['y'] = mapped_y
        self.tray_position = mapped_z
        # If toolhead reaches the target, clear the target
        if (self.targeted_toolhead_position['x'] is not None and self.targeted_toolhead_position['y'] is not None and
            int(x) == int(self.targeted_toolhead_position['x']) and
            int(y) == int(self.targeted_toolhead_position['y'])):
            self.targeted_toolhead_position = {'x': None, 'y': None}  # Clear the target

        # If tray reaches the target, clear the target
        if (self.targeted_tray_position is not None and self.targeted_tray_position is not None and
            int(z) == int(self.targeted_tray_position)):
            self.targeted_tray_position = None  # Clear the target

        # Redraw the drawing area
        self.gantry_drawing_area.queue_draw()

    def confirm_move(self, widget):
        """Sends the move command and clears the selection."""
        if self.target_xy is None and self.target_z is None:
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
        self.targeted_toolhead_position = {'x': None, 'y': None}
        self.targeted_tray_position = None

        if not self._printer.get_stat("gcode_move", "absolute_coordinates"):
            self._screen._ws.klippy.gcode_script("G90")

    def actual_to_grid(self, x, y, z=None):
        """Maps a (x, y) toolhead position to the 540x540 grid."""
        # Map the original coordinates. axis_minimum is flipped btw
        grid_x = (x - self.axis_minimum[1]) * self.scale_factor_x
        grid_y = (y - self.axis_minimum[0]) * self.scale_factor_y

        grid_z = None
        if z is not None and self.axis_minimum[2] is not None:
            grid_z = (z - self.axis_minimum[2]) * self.scale_factor_z

        return (grid_x, grid_y, grid_z) if z is not None else (grid_x, grid_y)
    
    def grid_to_actual(self, grid_x, grid_y, grid_z=None):
        # Convert grid coordinates back to real-world printer coordinates. axis_minimum is flipped btw
        actual_x = (grid_x / self.scale_factor_x) + self.axis_minimum[1]
        actual_y = (grid_y / self.scale_factor_y) + self.axis_minimum[0]

        actual_z = None
        if grid_z is not None and self.axis_minimum[2] is not None:
            actual_z = (grid_z / self.scale_factor_z) + self.axis_minimum[2]

        return (actual_x, actual_y, actual_z) if grid_z is not None else (actual_x, actual_y)
    
    def on_toolhead_press(self, widget, event):
        """Handles user press on the toolhead to start dragging."""
        if event.button == 1:  # Left mouse button or touch
            x = event.x
            y = event.y

            # Check if the press is inside the toolhead rectangle
            tool_x = self.toolhead_position['x']
            tool_y = self.toolhead_position['y']
            marker_size = 80  # triple the selection box so its easier to click

            if tool_x - marker_size / 2 <= x <= tool_x + marker_size / 2 and \
            tool_y - marker_size / 2 <= y <= tool_y + marker_size / 2:
                self.dragging_toolhead = True  # Enable dragging mode

    def on_toolhead_drag(self, widget, event):
        """Handles dragging movement while the user moves the toolhead."""
        if self.dragging_toolhead:  # Only move if dragging is active
            x = max(0, min(int(event.x), 540))
            y = max(0, min(int(event.y), 540))

            # Update the toolhead position while dragging
            #self.toolhead_position['x'] = x
            #self.toolhead_position['y'] = y
            self.targeted_toolhead_position['x'] = x
            self.targeted_toolhead_position['y'] = y
            # Redraw the drawing area to reflect new position
            self.gantry_drawing_area.queue_draw()

    def on_toolhead_release(self, widget, event):
        """Handles user releasing the toolhead to set the final position."""
        if self.dragging_toolhead:
            self.dragging_toolhead = False  # Disable dragging mode

            x = max(0, min(int(event.x), 540))
            y = max(0, min(int(event.y), 540))

            # Set the target position based on the final dragged position
            self.targeted_toolhead_position['x'] = x
            self.targeted_toolhead_position['y'] = y

            # Convert to actual coordinates
            mapped_x, mapped_y = self.grid_to_actual(x, y)
            self.target_xy = [int(mapped_y), int(mapped_x)]  # Flip coordinates

            # Redraw the drawing area
            self.gantry_drawing_area.queue_draw()

            self.confirm_move(widget)

    def tray_on_draw(self, widget, cr):
        # Get the dimensions of the drawing area
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()
        #width = 540
        #height = 540
        # Draw the grid
        self.draw_tray_background(cr, width, height)

        # Draw the tray position
        self.draw_tray(cr, width, height)
        # Draw the targeted tray position
        self.draw_targeted_tray(cr, width, height)
        

    def draw_tray_background(self, cr, width, height):
        """Draws the background image instead of a grid."""

        # Convert Pixbuf to Cairo Image
        image_surface = Gdk.cairo_surface_create_from_pixbuf(self.tray_image, 1)

        # Draw the background image
        cr.set_source_surface(image_surface, 0, 0)
        cr.paint()

    def draw_tray(self, cr, width, height):

        # Map tray position to drawing area coordinates
        z = self.tray_position
 
        # Set the tray color
        cr.set_source_rgb(1.0, 0, 0)  # Red

        # Define tray marker size
        marker_size = 20  # pixels

        # Draw the tray as a rectangle
        cr.rectangle((width / 2) - (marker_size / 2), z - marker_size / 2, marker_size, marker_size)
        cr.fill()

    def draw_targeted_tray(self, cr, width, height):
        if self.targeted_tray_position is None:
            return  # No target set
        # Map tray position to drawing area coordinates
        z = self.targeted_tray_position

        # Set the tray color
        cr.set_source_rgb(0, 1.0, 0)  # Red

        # Define tray marker size
        marker_size = 20  # pixels

        # Draw the tray as a rectangle
        cr.rectangle((width / 2) - (marker_size / 2), z - marker_size / 2, marker_size, marker_size)
        cr.fill()

    def on_tray_press(self, widget, event):
        """Handles user press on the tray to start dragging."""
        if event.button == 1:  # Left mouse button or touch
            x = event.x
            y = event.y

            # Check if the press is inside the tray rectangle
            marker_size = 100  # triple the selection box so its easier to click

            if 45 - marker_size / 2 <= x <= 45 + marker_size / 2 and \
            self.tray_position - marker_size / 2 <= y <= self.tray_position + marker_size / 2:
                self.dragging_tray = True  # Enable dragging mode

    def on_tray_drag(self, widget, event):
        """Handles dragging movement while the user moves the tray."""
        if self.dragging_tray:  # Only move if dragging is active
            z = max(0, min(int(event.y), 540))


            # Update the tray position while dragging

            self.targeted_tray_position = z

            # Redraw the drawing area to reflect new position
            self.tray_drawing_area.queue_draw()

    def on_tray_release(self, widget, event):
        """Handles user releasing the toolhead to set the final position."""
        if self.dragging_tray:
            self.dragging_tray = False  # Disable dragging mode

            z = max(0, min(int(event.y), 540))

            # Set the target position based on the final dragged position
            self.targeted_tray_position = z

            # Convert to actual coordinates
            mapped_x, mapped_y, mapped_z = self.grid_to_actual(0, 0, z)
            self.target_z =int(mapped_z)

            # Redraw the drawing area
            self.tray_drawing_area.queue_draw()

            self.confirm_move(widget)