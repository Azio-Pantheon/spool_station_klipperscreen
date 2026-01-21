import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk


class WeightKeypad(Gtk.Box):
    def __init__(self, screen, confirm_callback, cancel_callback):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        self.labels = {}
        self.confirm_callback = confirm_callback
        self.cancel_callback = cancel_callback
        self.screen = screen
        self._gtk = screen.gtk

        # Create the main numpad grid
        numpad = Gtk.Grid(row_homogeneous=True, column_homogeneous=True)
        numpad.set_direction(Gtk.TextDirection.LTR)
        numpad.get_style_context().add_class('numpad')

        # Define the keypad layout
        keys = [
            ['1', 'numpad_tleft'],
            ['2', 'numpad_top'],
            ['3', 'numpad_tright'],
            ['4', 'numpad_left'],
            ['5', 'numpad_button'],
            ['6', 'numpad_right'],
            ['7', 'numpad_left'],
            ['8', 'numpad_button'],
            ['9', 'numpad_right'],
            ['C', 'numpad_bleft'],      # Clear button
            ['0', 'numpad_bottom'],
            ['.', 'numpad_bright']      # Decimal point
        ]

        # Create buttons for the keypad
        for i in range(len(keys)):
            k_id = f'button_{str(keys[i][0])}'
            
            if keys[i][0] == "C":
                self.labels[k_id] = self._gtk.Button("cancel", scale=1)
            else:
                self.labels[k_id] = Gtk.Button(label=keys[i][0])
                
            self.labels[k_id].connect('clicked', self.update_entry, keys[i][0])
            self.labels[k_id].get_style_context().add_class(keys[i][1])
            self.labels[k_id].get_style_context().add_class("numpad_key")
            numpad.attach(self.labels[k_id], i % 3, i // 3, 1, 1)

        # Create the entry field
        self.labels['entry'] = Gtk.Entry()
        self.labels['entry'].props.xalign = 0.5
        self.labels['entry'].set_placeholder_text("Enter weight (grams)")
        self.labels['entry'].connect("activate", self.update_entry, "E")

        # Create control buttons
        self.labels['backspace'] = self._gtk.Button('backspace', _('Backspace'), None, .66, Gtk.PositionType.LEFT, 1)
        self.labels['backspace'].connect("clicked", self.update_entry, "B")
        
        confirm_btn = self._gtk.Button('complete', _('Confirm'), None, .66, Gtk.PositionType.LEFT, 1)
        confirm_btn.connect("clicked", self.update_entry, "E")

        # Layout the components
        self.add(self.labels['entry'])
        self.add(numpad)
        
        # Bottom row with control buttons
        bottom_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, homogeneous=True)
        bottom_row.add(self.labels['backspace'])
        bottom_row.add(confirm_btn)

        self.add(bottom_row)

    def set_initial_value(self, value):
        """Set an initial value in the entry field"""
        self.labels['entry'].set_text(str(value))

    def clear(self):
        """Clear the entry field"""
        self.labels['entry'].set_text("")
        self.preset_active = False

    def update_entry(self, widget, action):
        """Handle keypad button presses"""
        text = self.labels['entry'].get_text()
        
        if action == 'B':  # Backspace
            if len(text) > 0:
                self.labels['entry'].set_text(text[:-1])
                
        elif action == 'C':  # Clear
            self.labels['entry'].set_text("")
            
        elif action == 'E':  # Enter/Confirm
            weight = self.validate_weight(text)
            if weight is not None:
                self.confirm_callback(weight)
            else:
                # Invalid weight - could show error message or just ignore
                pass
                
        elif action == 'CANCEL':  # Cancel
            self.cancel_callback()
            
        elif action == '.':  # Decimal point
            # Only allow one decimal point
            if '.' not in text and len(text) > 0:
                self.labels['entry'].set_text(text + action)
            elif '.' not in text and len(text) == 0:
                # If empty, add "0." when decimal pressed
                self.labels['entry'].set_text("0.")
                
        else:  # Regular digits
            # Limit total length to prevent extremely long numbers
            if len(text) < 8:
                self.labels['entry'].set_text(text + action)

    @staticmethod
    def validate_weight(weight_str):
        """
        Validate and convert weight string to float.
        Returns the weight as float if valid, None if invalid.
        """
        if not weight_str or weight_str.strip() == "":
            return 0
            
        try:
            weight = float(weight_str)
            if weight < 0:
                return None
            return weight  
        except ValueError:
            return None