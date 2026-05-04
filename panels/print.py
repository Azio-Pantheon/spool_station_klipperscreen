import logging
import os
import gi
import yaml
import re
import requests
import socket
from io import StringIO


import subprocess
import threading

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Pango, GdkPixbuf
from datetime import datetime
from ks_includes.screen_panel import ScreenPanel
from ks_includes.KlippyGtk import find_widget
from ks_includes.widgets.flowboxchild_extended import PrintListItem


def format_label(widget):
    label = find_widget(widget, Gtk.Label)
    if label is not None:
        label.set_line_wrap_mode(Pango.WrapMode.CHAR)
        label.set_line_wrap(True)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_lines(2)


class Panel(ScreenPanel):
    def __init__(self, screen, title, shared_printer_config):
        super().__init__(screen, title)
        sortdir = self._config.get_main_config().get("print_sort_dir", "name_asc")
        sortdir = sortdir.split('_')
        self.sort_items = {
            "name": _("Name"),
            "date": _("Date"),
            "size": _("Size"),
        }
        if sortdir[0] not in self.sort_items or sortdir[1] not in ["asc", "desc"]:
            sortdir = ["name", "asc"]
        self.sort_current = [sortdir[0], 0 if sortdir[1] == "asc" else 1]  # 0 for asc, 1 for desc
        self.sort_icon = ["arrow-up", "arrow-down"]
        self.source = ""
        self.time_24 = self._config.get_main_config().getboolean("24htime", True)
        self.showing_rename = False
        self.loading = False
        self.cur_directory = 'gcodes'
        self.list_button_size = self._gtk.img_scale * self.bts
        self.file_metadata = {}
        self.headerbox = Gtk.Box(hexpand=True, vexpand=False)

        self.shared_printer_config = shared_printer_config

        # Fleet integration
        ks_printer_cfg = self._config.get_printer_config(self._screen.connected_printer)
        self.fleet_daemon_url = (
            ks_printer_cfg.get("fleet_daemon_url", "").strip('" ')
            if ks_printer_cfg else ""
        )
        self.fleet_files = []  # cached fleet file list
        self._fleet_downloading = False
        self._fleet_download_filename = ""
        hostname = socket.gethostname().lower()
        if not hostname.endswith('.local'):
            hostname += '.local'
        self._fleet_hostname = hostname

        n = 0
        for name, val in self.sort_items.items():
            s = self._gtk.Button(None, val, f"color{n % 4 + 1}", .5, Gtk.PositionType.RIGHT, 1)
            s.get_style_context().add_class("buttons_slim")
            if name == self.sort_current[0]:
                s.set_image(self._gtk.Image(self.sort_icon[self.sort_current[1]], self._gtk.img_scale * self.bts))
            s.connect("clicked", self.change_sort, name)
            self.labels[f'sort_{name}'] = s
            self.headerbox.add(s)
            n += 1

        self.refresh = self._gtk.Button("refresh", style=f"color{n % 4 + 1}", scale=self.bts)
        self.refresh.get_style_context().add_class("buttons_slim")
        self.refresh.connect('clicked', self._refresh_files)
        n += 1
        self.headerbox.add(self.refresh)

        self.switch_mode = self._gtk.Button("fine-tune", style=f"color{n % 4 + 1}", scale=self.bts)
        self.switch_mode.get_style_context().add_class("buttons_slim")
        self.switch_mode.connect('clicked', self.switch_view_mode)
        n += 1
        self.headerbox.add(self.switch_mode)

        self.loading_msg = _('Loading...')
        self.labels['path'] = Gtk.Label(label=self.loading_msg, vexpand=True, no_show_all=True)
        self.labels['path'].show()
        self.thumbsize = self._gtk.img_scale * self._gtk.button_image_scale * 2.5
        logging.info(f"Thumbsize: {self.thumbsize}")

        self.flowbox = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                   column_spacing=0, row_spacing=0, homogeneous=True)
        list_mode = self._config.get_main_config().get("print_view", 'thumbs')
        logging.info(list_mode)
        self.list_mode = list_mode == 'list'
        if self.list_mode:
            self.flowbox.set_min_children_per_line(1)
            self.flowbox.set_max_children_per_line(1)
        else:
            columns = 3 if self._screen.vertical_mode else 4
            self.flowbox.set_min_children_per_line(columns)
            self.flowbox.set_max_children_per_line(columns)

        self.scroll = self._gtk.ScrolledWindow()
        self.scroll.add(self.flowbox)

        self.main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        self.main.add(self.headerbox)
        self.main.add(self.labels['path'])
        self.main.add(self.scroll)
        self.content.add(self.main)
        self.set_loading(True)
        self._screen._ws.klippy.get_dir_info(self.load_files, self.cur_directory)



    def switch_view_mode(self, widget):
        self.list_mode ^= True
        logging.info(f"lista {self.list_mode}")
        if self.list_mode:
            self.flowbox.set_min_children_per_line(1)
            self.flowbox.set_max_children_per_line(1)
        else:
            columns = 3 if self._screen.vertical_mode else 4
            self.flowbox.set_min_children_per_line(columns)
            self.flowbox.set_max_children_per_line(columns)
        self._config.set("main", "print_view", 'list' if self.list_mode else 'thumbs')
        self._config.save_user_config_options()
        self._refresh_files()

    def activate(self):
        if self.cur_directory != "gcodes":
            self.change_dir()
        self._screen.files.add_callback(self._callback)

    def deactivate(self):
        self._screen.files.remove_callback(self._callback)

    def create_item(self, item):
        fbchild = PrintListItem()
        fbchild.set_date(item['modified'])
        fbchild.set_size(item['size'])
        if 'dirname' in item:
            if item['dirname'].startswith("."):
                return
            name = item['dirname']
            path = f"{self.cur_directory}/{name}"
            fbchild.set_as_dir(True)
        elif 'filename' in item:
            if (item['filename'].startswith(".") or
                    os.path.splitext(item['filename'])[1] not in {'.gcode', '.gco', '.g'}):
                return
            name = item['filename']
            # Fixed path handling - more robust than simple replace
            if self.cur_directory == 'gcodes':
                path = name
            else:
                # Remove 'gcodes/' prefix from current directory if present
                if self.cur_directory.startswith('gcodes/'):
                    relative_dir = self.cur_directory[7:]  # Remove 'gcodes/' prefix
                    path = f"{relative_dir}/{name}"
                else:
                    path = f"{self.cur_directory}/{name}"
        else:
            logging.error(f"Unknown item {item}")
            return
        basename = os.path.splitext(name)[0]
        fbchild.set_path(path)
        fbchild.set_name(basename.casefold())
        if self.list_mode:
            label = Gtk.Label(label=basename, hexpand=True, vexpand=False)
            format_label(label)
            info = Gtk.Label(hexpand=True, halign=Gtk.Align.START, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
            info.get_style_context().add_class("print-info")
            info.set_markup(self.get_info_str(item, path))
            delete = Gtk.Button(hexpand=False, vexpand=False, can_focus=False, always_show_image=True)
            delete.get_style_context().add_class("color1")
            delete.set_image(self._gtk.Image("delete", self.list_button_size, self.list_button_size))
            rename = Gtk.Button(hexpand=False, vexpand=False, can_focus=False, always_show_image=True)
            rename.get_style_context().add_class("color2")
            rename.set_image(self._gtk.Image("files", self.list_button_size, self.list_button_size))
            itemname = Gtk.Label(hexpand=True, halign=Gtk.Align.START, ellipsize=Pango.EllipsizeMode.END)
            itemname.get_style_context().add_class("print-filename")
            itemname.set_markup(f"<big><b>{basename}</b></big>")
            icon = Gtk.Button()
            row = Gtk.Grid(hexpand=True, vexpand=False, valign=Gtk.Align.CENTER)
            row.get_style_context().add_class("frame-item")
            row.attach(icon, 0, 0, 1, 2)
            row.attach(itemname, 1, 0, 3, 1)
            row.attach(info, 1, 1, 1, 1)
            row.attach(rename, 2, 1, 1, 1)
            row.attach(delete, 3, 1, 1, 1)
            if 'filename' in item:
                if path.startswith('flash_drive'):
                    icon.connect("clicked", self.confirm_move_gcode, path)
                    action = self._gtk.Button("usb download", style="color3")
                    action.connect("clicked", self.confirm_move_gcode, path)
                else:
                    icon.connect("clicked", self.confirm_compatible_print, path)
                    action = self._gtk.Button("print", style="color3")
                    action.connect("clicked", self.confirm_compatible_print, path)

                image_args = (path, icon, self.thumbsize, False, "file")
                delete.connect("clicked", self.confirm_delete_file, f"gcodes/{path}")
                rename.connect("clicked", self.show_rename, f"gcodes/{path}")
                action.set_hexpand(False)
                action.set_vexpand(False)
                action.set_halign(Gtk.Align.END)
                row.attach(action, 4, 0, 1, 2)
            elif 'dirname' in item:
                icon.connect("clicked", self.change_dir, path)
                image_args = (None, icon, self.thumbsize, False, "folder")
                delete.connect("clicked", self.confirm_delete_directory, path)
                rename.connect("clicked", self.show_rename, path)
                action = self._gtk.Button("load", style="color3")
                action.connect("clicked", self.change_dir, path)
                action.set_hexpand(False)
                action.set_vexpand(False)
                action.set_halign(Gtk.Align.END)
                row.attach(action, 4, 0, 1, 2)
            else:
                return
            fbchild.add(row)
        else:  # Thumbnail view
            icon = self._gtk.Button(label=basename)
            if 'filename' in item:
                if path.startswith('flash_drive'):
                    icon.connect("clicked", self.confirm_move_gcode, path)
                else:
                    icon.connect("clicked", self.confirm_compatible_print, path)
                image_args = (path, icon, self.thumbsize, False, "file")

            elif 'dirname' in item:
                icon.connect("clicked", self.change_dir, path)
                image_args = (None, icon, self.thumbsize, False, "folder")
            else:
                return
            fbchild.add(icon)
        self.image_load(*image_args)
        return fbchild

    def show_path(self):
        self.labels['path'].set_vexpand(False)
        if self.cur_directory == 'gcodes':
            self.labels['path'].hide()
        else:
            self.labels['path'].set_text(self.cur_directory)
            self.labels['path'].show()

    def image_load(self, filepath, widget, size=-1, small=True, iconname=None):
        pixbuf = self.get_file_image(filepath, size, size, small)
        if pixbuf is not None:
            widget.set_image(Gtk.Image.new_from_pixbuf(pixbuf))
        elif iconname is not None:
            widget.set_image(self._gtk.Image(iconname, size, size))
        format_label(widget)

    def confirm_delete_file(self, widget, filepath):
        logging.debug(f"Sending delete_file {filepath}")
        params = {"path": f"{filepath}"}
        self._screen._confirm_send_action(
            None,
            _("Delete File?") + "\n\n" + filepath,
            "server.files.delete_file",
            params
        )

    def confirm_delete_directory(self, widget, dirpath):
        logging.debug(f"Sending delete_directory {dirpath}")
        params = {"path": f"{dirpath}", "force": True}
        self._screen._confirm_send_action(
            None,
            _("Delete Directory?") + "\n\n" + dirpath,
            "server.files.delete_directory",
            params
        )

    def back(self):
        if self.showing_rename:
            self.hide_rename()
            return True
        if self.cur_directory != 'gcodes':
            self.change_dir(None, os.path.dirname(self.cur_directory))
            return True
        return False

    def change_dir(self, widget=None, directory='gcodes'):
        if directory == '':
            directory = 'gcodes'
        if directory != self.cur_directory:
            logging.info(f'Changing directory to: {directory}')
            self.cur_directory = directory
        self.show_path()
        self._refresh_files()

    def change_sort(self, widget, key):
        if self.sort_current[0] == key:
            self.sort_current[1] = (self.sort_current[1] + 1) % 2
        else:
            oldkey = self.sort_current[0]
            logging.info(f"Changing from {oldkey} to {key}")
            self.labels[f'sort_{oldkey}'].set_image(None)
            self.labels[f'sort_{oldkey}'].show_all()
            self.sort_current = [key, 0]
        self.labels[f'sort_{key}'].set_image(self._gtk.Image(self.sort_icon[self.sort_current[1]],
                                                             self._gtk.img_scale * self.bts))
        self.labels[f'sort_{key}'].show()

        self.set_sort()

        self._config.set("main", "print_sort_dir", f'{key}_{"asc" if self.sort_current[1] == 0 else "desc"}')
        self._config.save_user_config_options()

    def set_sort(self):
        reverse = self.sort_current[1] != 0
        if self.sort_current[0] == "name":
            self.flowbox.set_sort_func(self.sort_names, reverse)
        elif self.sort_current[0] == "date":
            self.flowbox.set_sort_func(self.sort_dates, reverse)
        elif self.sort_current[0] == "size":
            self.flowbox.set_sort_func(self.sort_sizes, reverse)

    @staticmethod
    def sort_names(a: PrintListItem, b: PrintListItem, reverse):
        if a.get_is_dir() - b.get_is_dir() != 0:
            return a.get_is_dir() - b.get_is_dir()
        if a.get_name() < b.get_name():
            return 1 if reverse else -1
        if a.get_name() > b.get_name():
            return -1 if reverse else 1
        return 0

    @staticmethod
    def sort_sizes(a: PrintListItem, b: PrintListItem, reverse):
        if a.get_is_dir() - b.get_is_dir() != 0:
            return a.get_is_dir() - b.get_is_dir()
        return b.get_size() - a.get_size() if reverse else a.get_size() - b.get_size()

    @staticmethod
    def sort_dates(a: PrintListItem, b: PrintListItem, reverse):
        if a.get_is_dir() - b.get_is_dir() != 0:
            return a.get_is_dir() - b.get_is_dir()
        return b.get_date() - a.get_date() if reverse else a.get_date() - b.get_date()

    def parse_weight_from_filename(self, filename):
        """
        Parse weight from filename with pattern: *_XXXXg.gcode
        Returns weight as float or None if not found/invalid
        """
        # Pattern matches: underscore + number (int or float) + "g.gcode" at end of string
        pattern = r'_(\d+(?:\.\d+)?)g\.gcode$'
        match = re.search(pattern, filename)
        
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None
        return None

    def get_spool_tracker_remaining_weight(self):
            """
            Get remaining weight from spool_tracker
            Returns remaining weight as float or None if unavailable
            """
            try:
                # Check if apiclient is available
                if not hasattr(self._screen, 'apiclient') or self._screen.apiclient is None:
                    return None
                    
                # Get spool tracker status
                result = self._screen.apiclient.send_request("server/spool_tracker/status")
                if not result or "result" not in result:
                    return None
                
                tracker_data = result.get("result", {})
                
                # Check if tracking is enabled
                can_track = tracker_data.get("can_track", False)
                if not can_track:
                    return None
                
                # Get remaining weight from tracker
                weights = tracker_data.get("weights", {})
                remaining_weight = weights.get("remaining_weight", 0)
                
                return float(remaining_weight) if remaining_weight > 0 else None
                    
            except Exception as e:
                logging.error(f"Error getting spool_tracker remaining weight: {e}")
                # Show popup warning for spool_tracker API errors
                self._screen.show_popup_message(f"Spool tracker error: {str(e)}", level=3)
                
            return None

    def check_filament_weight(self, filename):
        """
        Check if filament weight is sufficient for the print
        Returns tuple: (weight_status, required_weight, remaining_weight)
        weight_status: 'sufficient', 'caution', 'warning', or 'skip'
        """
        # Parse weight from filename
        required_weight = self.parse_weight_from_filename(filename)
        if required_weight is None:
            return 'skip', None, None
            
        # Get remaining weight from spool_tracker
        remaining_weight = self.get_spool_tracker_remaining_weight()
        if remaining_weight is None:
            return 'skip', None, None
            
        # Check weight levels
        if remaining_weight < required_weight:
            return 'warning', required_weight, remaining_weight
        elif remaining_weight < required_weight + 150:  # 150g buffer
            return 'caution', required_weight, remaining_weight
        else:
            return 'sufficient', required_weight, remaining_weight

    def confirm_print(self, widget, filename):

        buttons = [
            {"name": _("Print"), "response": Gtk.ResponseType.OK},
            {"name": _("Cancel"), "response": Gtk.ResponseType.CANCEL, "style": 'dialog-error'}
        ]

        label = Gtk.Label(hexpand=True, vexpand=True, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        label.set_markup(f"<b>{filename}</b>\n")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add(label)

        height = (self._screen.height - self._gtk.dialog_buttons_height - self._gtk.font_size) * .75
        pixbuf = self.get_file_image(filename, self._screen.width * .9, height)
        if pixbuf is not None:
            image = Gtk.Image.new_from_pixbuf(pixbuf)
            box.add(image)

        self._gtk.Dialog(_("Print") + f' {filename}', buttons, box, self.confirm_print_response, filename)

    def confirm_print_response(self, dialog, response_id, filename):
        self._gtk.remove_dialog(dialog)
        if response_id == Gtk.ResponseType.OK:
            logging.info(f"Starting print: {filename}")
            self._screen._ws.klippy.print_start(filename)

    def confirm_compatible_print(self, widget, filename):
        # Check if its purging, if purging, notify the user.
        if self._screen.shared_printer_config.is_purging == 1:
            self._screen.show_popup_message(("Wet Filament Purge: Purging wet filament. A print has already been started, it will begin after the purge."), level=2)
            return
        # Check whether to show prime dialogue or not
        if (self._screen.shared_printer_config.enable_prime == 1) and (not self.is_primed):
            self.prime_print(widget)
            return

        cautionGenericText = 'Print Quality may be degraded'
        warningGenericText = 'Running this file may damage your machine'
        self.file_metadata = self._files.get_file_info(filename)
        
        # Check filament weight
        weight_status, required_weight, remaining_weight = self.check_filament_weight(filename)
        
        # if printer config doesnt exist, then skip all config checks
        if isinstance(self.file_metadata, dict) and self.file_metadata.get('enable_config_verifier', True):
            #Load the yml config from gcode
            label_text = ""
            label_class = ""
            # if the slicer is not PantheonSlicer then show a warning
            slicer = self.file_metadata.get('slicer')
            if slicer is None:
                buttons = [
                    {"name": _("Print"), "response": Gtk.ResponseType.OK},
                    {"name": _("Cancel"), "response": Gtk.ResponseType.CANCEL, "style": 'dialog-error'}
                ]

                label = Gtk.Label(hexpand=True, vexpand=True, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
                label.set_markup(f"<b>{filename}</b>\n")

                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                
                # Add weight warning banner if needed
                if weight_status in ['warning', 'caution']:
                    weight_banner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
                    weight_banner_box.set_margin_bottom(10)
                    
                    # Main banner
                    weight_main_label = Gtk.Label()
                    weight_main_label.set_markup('<b><span size="20480">Warning: Filament is Low</span></b>')
                    weight_main_label.get_style_context().add_class('compatibilityMessage-warning')
                    weight_main_label.set_use_markup(True)
                    weight_main_label.set_xalign(0.0)
                    
                    # Detail text
                    weight_detail_label = Gtk.Label()
                    if weight_status == 'warning':
                        weight_detail_label.set_markup(f'Warning! Filament required ({required_weight}g) is higher than the remaining weight ({remaining_weight:.1f}g)')
                    else:  # caution
                        weight_detail_label.set_markup(f'Caution, Filament required ({required_weight}g) is close to the remaining weight ({remaining_weight:.1f}g), filament may runout midprint')
                    weight_detail_label.set_use_markup(True)
                    weight_detail_label.set_xalign(0.0)
                    
                    weight_banner_box.add(weight_main_label)
                    weight_banner_box.add(weight_detail_label)
                    box.add(weight_banner_box)

                box.add(label)

                height = (self._screen.height - self._gtk.dialog_buttons_height - self._gtk.font_size) * .75
                pixbuf = self.get_file_image(filename, self._screen.width * .9, height)
                if pixbuf is not None:
                    image = Gtk.Image.new_from_pixbuf(pixbuf)
                    box.add(image)

                dialog = self._gtk.Dialog(_("Print") + f' {filename}', buttons, box, self.confirm_print_response, filename)
                dialog.get_style_context().add_class('confirmPrintDialog')
                return
            if slicer == 'PantheonSlicer':
                buttons = []
                if 'config_yml' not in self.file_metadata or not self.file_metadata['config_yml']:
                    # Scenario 1: config_yml doesn't exist for pantheonslicer
                    label_text = Gtk.Label(label=f"<b><span size='20480'>Caution: {cautionGenericText} </span></b>")  
                    label_text.get_style_context().add_class('compatibilityMessage-caution')
                    label_text.set_use_markup(True)
                    label_text.set_xalign(0.0)

                    caution_label = Gtk.Label(label="Out of date PantheonSlicer Detected. Please update PantheonSlicer and the profiles")
                    caution_label.set_use_markup(True)
                    caution_label.set_xalign(0.0)

                    buttons = [
                        {"name": _("Print"), "response": Gtk.ResponseType.OK},
                        {"name": _("Cancel"), "response": Gtk.ResponseType.CANCEL, "style": 'dialog-error'}
                    ]

                    grid = Gtk.Grid()
                    grid.set_column_homogeneous(True)
                    
                    # Add weight warning banner at the top
                    current_row = 0
                    if weight_status in ['warning', 'caution']:
                        weight_banner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
                        
                        # Main banner
                        weight_main_label = Gtk.Label()
                        weight_main_label.set_markup('<b><span size="20480">Warning: Filament is Low</span></b>')
                        weight_main_label.get_style_context().add_class('compatibilityMessage-warning')
                        weight_main_label.set_use_markup(True)
                        weight_main_label.set_xalign(0.0)
                        
                        # Detail text
                        weight_detail_label = Gtk.Label()
                        if weight_status == 'warning':
                            weight_detail_label.set_markup(f'Warning! Filament required ({required_weight}g) is higher than the remaining weight ({remaining_weight:.1f}g)')
                        else:  # caution
                            weight_detail_label.set_markup(f'Caution, Filament required ({required_weight}g) is close to the remaining weight ({remaining_weight:.1f}g), filament may runout midprint')
                        weight_detail_label.set_use_markup(True)
                        weight_detail_label.set_xalign(0.0)
                        
                        weight_banner_box.add(weight_main_label)
                        weight_banner_box.add(weight_detail_label)
                        weight_banner_box.set_margin_bottom(10)
                        grid.attach(weight_banner_box, 0, current_row, 1, 1)
                        current_row += 1
                    
                    label_text.set_margin_bottom(10)
                    grid.attach(label_text, 0, current_row, 1, 1)
                    grid.attach(caution_label, 0, current_row + 1, 1, 1)

                    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                    box.add(grid)


                    height = (self._screen.height - self._gtk.dialog_buttons_height - self._gtk.font_size) * .70
                    pixbuf = self.get_file_image(filename, self._screen.width * .9, height)
                    if pixbuf is not None:
                        image = Gtk.Image.new_from_pixbuf(pixbuf)
                        box.add(image)


                    dialog = self._gtk.Dialog(_("Print") + f' {filename}', buttons, box, self.confirm_compatible_print_response, filename)
                    dialog.get_style_context().add_class('confirmPrintDialog')
                    return
                else:
                    if ('config_verifier' not in self.file_metadata):
                        label_text = Gtk.Label(label=f"<b><span size='20480'>Caution: {cautionGenericText}</span></b>")  
                        label_text.get_style_context().add_class('compatibilityMessage-caution')
                        label_text.set_use_markup(True)
                        label_text.set_xalign(0.0)

                        warning_label = Gtk.Label(label=f"Gcode_yml format is invalid. Please try update PantheonSlicer profiles or check gcode content")
                        warning_label.set_use_markup(True)
                        warning_label.set_xalign(0.0)
                        
                        buttons = [
                            {"name": _("Print"), "response": Gtk.ResponseType.OK},
                            {"name": _("Cancel"), "response": Gtk.ResponseType.CANCEL, "style": 'dialog-error'}
                        ]

                        grid = Gtk.Grid()
                        grid.set_column_homogeneous(True)
                        
                        # Add weight warning banner at the top
                        current_row = 0
                        if weight_status in ['warning', 'caution']:
                            weight_banner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
                            
                            # Main banner
                            weight_main_label = Gtk.Label()
                            weight_main_label.set_markup('<b><span size="20480">Warning: Filament is Low</span></b>')
                            weight_main_label.get_style_context().add_class('compatibilityMessage-warning')
                            weight_main_label.set_use_markup(True)
                            weight_main_label.set_xalign(0.0)
                            
                            # Detail text
                            weight_detail_label = Gtk.Label()
                            if weight_status == 'warning':
                                weight_detail_label.set_markup(f'Warning! Filament required ({required_weight}g) is higher than the remaining weight ({remaining_weight:.1f}g)')
                            else:  # caution
                                weight_detail_label.set_markup(f'Caution, Filament required ({required_weight}g) is close to the remaining weight ({remaining_weight:.1f}g), filament may runout midprint')
                            weight_detail_label.set_use_markup(True)
                            weight_detail_label.set_xalign(0.0)
                            
                            weight_banner_box.add(weight_main_label)
                            weight_banner_box.add(weight_detail_label)
                            weight_banner_box.set_margin_bottom(10)
                            grid.attach(weight_banner_box, 0, current_row, 1, 1)
                            current_row += 1
                        
                        label_text.set_margin_bottom(10)
                        grid.attach(label_text, 0, current_row, 1, 1)
                        grid.attach(warning_label, 0, current_row + 1, 1, 1)

                        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                        box.add(grid)

                        height = (self._screen.height - self._gtk.dialog_buttons_height - self._gtk.font_size) * .70
                        pixbuf = self.get_file_image(filename, self._screen.width * .9, height)
                        if pixbuf is not None:
                            image = Gtk.Image.new_from_pixbuf(pixbuf)
                            box.add(image)


                        dialog = self._gtk.Dialog(_("Print") + f' {filename}', buttons, box, self.confirm_compatible_print_response, filename)
                        dialog.get_style_context().add_class('confirmPrintDialog')
                        return    
                    # Handle filament type and nozzle size check
                    config_verifier = self.file_metadata['config_verifier'].copy()
                    if self.shared_printer_config.filament is None:
                        config_verifier.append("Warning! Filament type is not set on this printer.")
                    elif self.file_metadata['filament_type'] != self.shared_printer_config.filament:
                        filament_warning = f"Warning! Filament type mismatch: expected {self.file_metadata['filament_type']},\n\t but the printer filament is set to {self.shared_printer_config.filament}"
                        config_verifier.append(filament_warning)

                    try:
                        nozzle_diameter = float(self.shared_printer_config.nozzle)
                        if self.file_metadata['nozzle_diameter'] != nozzle_diameter:
                            nozzle_warning = f"Warning! Nozzle diameter mismatch: expected {self.file_metadata['nozzle_diameter']} mm,\n\t but but the printer nozzle size is set to {self.shared_printer_config.nozzle} mm"
                            config_verifier.append(nozzle_warning)
                    except (ValueError, TypeError):
                        nozzle_val = self.shared_printer_config.nozzle
                        if nozzle_val:
                            config_verifier.append(f"Warning! Nozzle size is invalid: '{nozzle_val}'")
                        else:
                            config_verifier.append("Warning! Nozzle size is not set on this printer.")
                    #Senario 2: Config check passed
                    if (config_verifier == []):
                        label_text = f"{filename}\n"
                        buttons = [
                            {"name": _("Print"), "response": Gtk.ResponseType.OK},
                            {"name": _("Cancel"), "response": Gtk.ResponseType.CANCEL, "style": 'dialog-error'}
                        ]
                    else:
                        # Scenario 4: config_yml exists, but Config check failed
                        # Find differences between the two YAML files
                        warningStrings = []
                        cautionStrings = []
                        dangerStrings = []

                        label_text = filename
                        label_class = ''

                        for entry in config_verifier:

                            if entry.startswith("Warning!"):
                                warningStrings.append(entry)
                            elif entry.startswith("Danger!"):
                                dangerStrings.append(entry)
                            else:
                                cautionStrings.append(entry)
                        buttons = [
                            {"name": _("Print"), "response": Gtk.ResponseType.OK},
                            {"name": _("Cancel"), "response": Gtk.ResponseType.CANCEL, "style": 'dialog-error'}
                        ]

                        
                        # Create a grid to display differences side by side
                        grid = Gtk.Grid()
                        grid.set_column_homogeneous(True)
                        
                        # Row 0: Filename
                        label = Gtk.Label(label=filename)
                        if label_class:
                            label.get_style_context().add_class(label_class)
                        grid.attach(label, 0, 0, 2, 1)
                        
                        # Row 1: Add weight warning banner (if needed)
                        current_row = 1
                        if weight_status in ['warning', 'caution']:
                            weight_banner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
                            
                            # Main banner
                            weight_main_label = Gtk.Label()
                            weight_main_label.set_markup('<b><span size="20480">Warning: Filament is Low</span></b>')
                            weight_main_label.get_style_context().add_class('compatibilityMessage-warning')
                            weight_main_label.set_use_markup(True)
                            weight_main_label.set_xalign(0.0)
                            
                            # Detail text
                            weight_detail_label = Gtk.Label()
                            if weight_status == 'warning':
                                weight_detail_label.set_markup(f'Warning! Filament required ({required_weight}g) is higher than the remaining weight ({remaining_weight:.1f}g)')
                            else:  # caution
                                weight_detail_label.set_markup(f'Caution, Filament required ({required_weight}g) is close to the remaining weight ({remaining_weight:.1f}g), filament may runout midprint')
                            weight_detail_label.set_use_markup(True)
                            weight_detail_label.set_xalign(0.0)
                            
                            weight_banner_box.add(weight_main_label)
                            weight_banner_box.add(weight_detail_label)
                            weight_banner_box.set_margin_bottom(10)
                            grid.attach(weight_banner_box, 0, current_row, 2, 1)
                            current_row += 1

                        # Display warning strings
                        for i in range(len(warningStrings)):
                            if i == 0:
                                warning_header = Gtk.Label(label=f'<b><span size="20480">Warning: {warningGenericText}</span></b>')
                                warning_header.get_style_context().add_class('compatibilityMessage-warning')
                                warning_header.set_use_markup(True)
                                warning_header.set_xalign(0.0)
                                warning_header.set_margin_bottom(5)
                                grid.attach(warning_header, 0, current_row, 2, 1)
                                current_row += 1
                            detail_label = Gtk.Label(label=warningStrings[i])
                            detail_label.set_xalign(0.0)
                            detail_label.set_line_wrap(True)
                            detail_label.set_margin_bottom(5)
                            grid.attach(detail_label, 0, current_row, 2, 1)
                            current_row += 1

                        # Display caution strings
                        for i in range(len(cautionStrings)):
                            if i == 0:
                                caution_header = Gtk.Label(label=f'<b><span size="20480">Caution: {cautionGenericText}</span></b>')
                                caution_header.get_style_context().add_class('compatibilityMessage-caution')
                                caution_header.set_use_markup(True)
                                caution_header.set_xalign(0.0)
                                caution_header.set_margin_bottom(5)
                                grid.attach(caution_header, 0, current_row, 2, 1)
                                current_row += 1
                            detail_label = Gtk.Label(label=cautionStrings[i])
                            detail_label.set_xalign(0.0)
                            detail_label.set_line_wrap(True)
                            detail_label.set_margin_bottom(5)
                            grid.attach(detail_label, 0, current_row, 2, 1)
                            current_row += 1

                        # Create TextView widgets to display the YAML content with appropriate classes
                        # DangerStrings is not implemented

                    # Create label for simple case (no differences found)
                    if config_verifier == []:
                        simple_label = Gtk.Label(label=f"{filename}\n")
                
                # Create a ScrolledWindow and set maximum height
                scrolled_window = Gtk.ScrolledWindow()
                scrolled_window.set_hexpand(True)
                scrolled_window.set_vexpand(True)
                scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
                scrolled_window.set_max_content_height(300)  # Set the maximum height as needed

                if 'grid' in locals():
                    scrolled_window.add(grid)
                else:
                    # Handle simple case with no grid (no differences found)
                    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                    box.add(simple_label)
                    # Add weight warning banner after filename
                    if weight_status in ['warning', 'caution']:
                        weight_banner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
                        weight_banner_box.set_margin_bottom(10)
                        
                        # Main banner
                        weight_main_label = Gtk.Label()
                        weight_main_label.set_markup('<b><span size="20480">Warning: Filament is Low</span></b>')
                        weight_main_label.get_style_context().add_class('compatibilityMessage-warning')
                        weight_main_label.set_use_markup(True)
                        weight_main_label.set_xalign(0.0)
                        
                        # Detail text
                        weight_detail_label = Gtk.Label()
                        if weight_status == 'warning':
                            weight_detail_label.set_markup(f'Warning! Filament required ({required_weight}g) is higher than the remaining weight ({remaining_weight:.1f}g)')
                        else:  # caution
                            weight_detail_label.set_markup(f'Caution, Filament required ({required_weight}g) is close to the remaining weight ({remaining_weight:.1f}g), filament may runout midprint')
                        weight_detail_label.set_use_markup(True)
                        weight_detail_label.set_xalign(0.0)
                        
                        weight_banner_box.add(weight_main_label)
                        weight_banner_box.add(weight_detail_label)
                        box.add(weight_banner_box)
                    scrolled_window.add(box)
                    
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                box.add(scrolled_window)
                #box.get_style_context().add_class('confirmPrintDialog')

                height = (self._screen.height - self._gtk.dialog_buttons_height - self._gtk.font_size) * .35
                pixbuf = self.get_file_image(filename, self._screen.width * .9, height)
                if pixbuf is not None:
                    image = Gtk.Image.new_from_pixbuf(pixbuf)
                    box.add(image)


                dialog = self._gtk.Dialog(_("Print") + f' {filename}', buttons, box, self.confirm_compatible_print_response, filename)
                dialog.get_style_context().add_class('confirmPrintDialog')
                # Adding background color
                #dialog.get_style_context().add_class('dialog-compatibilityMessage-warning')
            else:
                # Scenario 5: not PantheonSlicer
                label_text = Gtk.Label(label=f"<b><span size='20480'>Warning: {warningGenericText}</span></b>")  
                label_text.get_style_context().add_class('compatibilityMessage-warning')
                label_text.set_use_markup(True)
                label_text.set_xalign(0.0)

                warning_label = Gtk.Label(label=f"Third-party slicer detected: {slicer} ")
                warning_label.set_use_markup(True)
                warning_label.set_xalign(0.0)
                
                buttons = [
                    {"name": _("Print"), "response": Gtk.ResponseType.OK},
                    {"name": _("Cancel"), "response": Gtk.ResponseType.CANCEL, "style": 'dialog-error'}
                ]

                grid = Gtk.Grid()
                grid.set_column_homogeneous(True)
                
                # Add weight warning banner at the top
                current_row = 0
                if weight_status in ['warning', 'caution']:
                    weight_banner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
                    
                    # Main banner
                    weight_main_label = Gtk.Label()
                    weight_main_label.set_markup('<b><span size="20480">Warning: Filament is Low</span></b>')
                    weight_main_label.get_style_context().add_class('compatibilityMessage-warning')
                    weight_main_label.set_use_markup(True)
                    weight_main_label.set_xalign(0.0)
                    
                    # Detail text
                    weight_detail_label = Gtk.Label()
                    if weight_status == 'warning':
                        weight_detail_label.set_markup(f'Warning! Filament required ({required_weight}g) is higher than the remaining weight ({remaining_weight:.1f}g)')
                    else:  # caution
                        weight_detail_label.set_markup(f'Caution, Filament required ({required_weight}g) is close to the remaining weight ({required_weight:.1f}g), filament may runout midprint')
                    weight_detail_label.set_use_markup(True)
                    weight_detail_label.set_xalign(0.0)
                    
                    weight_banner_box.add(weight_main_label)
                    weight_banner_box.add(weight_detail_label)
                    weight_banner_box.set_margin_bottom(10)
                    grid.attach(weight_banner_box, 0, current_row, 1, 1)
                    current_row += 1
                
                label_text.set_margin_bottom(10)
                grid.attach(label_text, 0, current_row, 1, 1)
                grid.attach(warning_label, 0, current_row + 1, 1, 1)

                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                box.add(grid)

                height = (self._screen.height - self._gtk.dialog_buttons_height - self._gtk.font_size) * .70
                pixbuf = self.get_file_image(filename, self._screen.width * .9, height)
                if pixbuf is not None:
                    image = Gtk.Image.new_from_pixbuf(pixbuf)
                    box.add(image)


                dialog = self._gtk.Dialog(_("Print") + f' {filename}', buttons, box, self.confirm_compatible_print_response, filename)
                dialog.get_style_context().add_class('confirmPrintDialog')
        else:
            buttons = [
                {"name": _("Print"), "response": Gtk.ResponseType.OK},
                {"name": _("Cancel"), "response": Gtk.ResponseType.CANCEL, "style": 'dialog-error'}
            ]

            label = Gtk.Label(hexpand=True, vexpand=True, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
            label.set_markup(f"<b>{filename}</b>\n")

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            
            # Add weight warning banner if needed
            if weight_status in ['warning', 'caution']:
                weight_banner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
                weight_banner_box.set_margin_bottom(10)
                
                # Main banner
                weight_main_label = Gtk.Label()
                weight_main_label.set_markup('<b><span size="20480">Warning: Filament is Low</span></b>')
                weight_main_label.get_style_context().add_class('compatibilityMessage-warning')
                weight_main_label.set_use_markup(True)
                weight_main_label.set_xalign(0.0)
                
                # Detail text
                weight_detail_label = Gtk.Label()
                if weight_status == 'warning':
                    weight_detail_label.set_markup(f'Warning! Filament required ({required_weight}g) is higher than the remaining weight ({remaining_weight:.1f}g)')
                else:  # caution
                    weight_detail_label.set_markup(f'Caution, Filament required ({required_weight}g) is close to the remaining weight ({remaining_weight:.1f}g), filament may runout midprint')
                weight_detail_label.set_use_markup(True)
                weight_detail_label.set_xalign(0.0)
                
                weight_banner_box.add(weight_main_label)
                weight_banner_box.add(weight_detail_label)
                box.add(weight_banner_box)
            
            box.add(label)

            height = (self._screen.height - self._gtk.dialog_buttons_height - self._gtk.font_size) * .75
            pixbuf = self.get_file_image(filename, self._screen.width * .9, height)
            if pixbuf is not None:
                image = Gtk.Image.new_from_pixbuf(pixbuf)
                box.add(image)

            dialog = self._gtk.Dialog(_("Print") + f' {filename}', buttons, box, self.confirm_print_response, filename)
            dialog.get_style_context().add_class('confirmPrintDialog')


    def confirm_compatible_print_response(self, dialog, response_id, filename):
        self._gtk.remove_dialog(dialog)
        if response_id == Gtk.ResponseType.OK:
            logging.info(f"Starting print: {filename}")
            self._screen._ws.klippy.print_start(filename)

    def get_info_str(self, item, path):
        info = ""
        if "modified" in item:
            info += _("Modified") if 'dirname' in item else _("Uploaded")
            if self.time_24:
                info += f':<b> {datetime.fromtimestamp(item["modified"]):%Y/%m/%d %H:%M}</b>\n'
            else:
                info += f':<b> {datetime.fromtimestamp(item["modified"]):%Y/%m/%d %I:%M %p}</b>\n'
        if 'filename' in item:
            fileinfo = self._screen.files.get_file_info(path)
            filament_type = fileinfo.get("filament_type") or "-"
            nozzle = fileinfo.get("nozzle_diameter")
            try:
                nozzle_str = f"{float(nozzle):.2f} mm" if nozzle is not None else "-"
            except (TypeError, ValueError):
                nozzle_str = "-"
            info += (_("Filament") + f': <b>{filament_type}</b>   '
                     + _("Nozzle") + f': <b>{nozzle_str}</b>\n')
            time_str = self.format_time(fileinfo["estimated_time"]) if "estimated_time" in fileinfo else "-"
            weight = fileinfo.get("filament_weight_total")
            try:
                weight_str = f"{float(weight):.0f} g" if weight is not None else "-"
            except (TypeError, ValueError):
                weight_str = "-"
            info += (_("Print Time") + f': <b>{time_str}</b>   '
                     + _("Material") + f': <b>{weight_str}</b>')
        return info

    def load_files(self, result, method, params):
        start = datetime.now()
        self.set_loading(True)
        if not result.get("result") or not isinstance(result["result"], dict):
            logging.info(result)
            self.set_loading(False)
            return
        local_files = result["result"].get("files", [])
        local_filenames = {f.get("filename", "") for f in local_files}
        items = [self.create_item(item) for item in [*result["result"]["dirs"], *local_files]]
        for item in filter(None, items):
            self.flowbox.add(item)

        # If browsing fleet_gcodes, inject remote fleet files
        if self._is_fleet_dir() and self.fleet_daemon_url:
            self._inject_fleet_remote_files(local_filenames)

        self.set_sort()
        self.set_loading(False)
        logging.info(f"Loaded in {(datetime.now() - start).total_seconds():.3f} seconds")

    def delete_from_list(self, path):
        logging.info(f"deleting {path}")
        for item in self.flowbox.get_children():
            if item.get_path() in {path, f"gcodes/{path}"}:
                logging.info("found removing")
                self.flowbox.remove(item)
                return True

    def add_item_from_callback(self, action, data):
        item = data['item']
        if 'source_item' in data:
            self.delete_from_list(data['source_item']['path'])
        else:
            self.delete_from_list(item['path'])
        path = os.path.join("gcodes", item["path"])
        if self.cur_directory != os.path.dirname(path):
            return
        if action in {"create_dir", "move_dir"}:
            item.update({"path": path, "dirname": os.path.split(item["path"])[1]})
        else:
            item.update({"path": path, "filename": os.path.split(item["path"])[1]})
        fbchild = self.create_item(item)
        if fbchild:
            self.flowbox.add(fbchild)
            self.flowbox.invalidate_sort()
            self.flowbox.show_all()

    def _callback(self, action, data):
        logging.info(f"{action}: {data}")
        if action in {"create_dir", "create_file"}:
            self.add_item_from_callback(action, data)
        elif action == "delete_file":
            self.delete_from_list(data['item']["path"])
        elif action == "delete_dir":
            self.delete_from_list(os.path.join("gcodes", data['item']["path"]))
        elif action in {"modify_file", "move_file", "move_dir"}:
            if "path" in data['item'] and data['item']["path"].startswith("gcodes/"):
                data['item']["path"] = data['item']["path"][7:]
            self.add_item_from_callback(action, data)

    def _refresh_files(self, *args):
        logging.info("Refreshing")
        self.set_loading(True)
        for child in self.flowbox.get_children():
            self.flowbox.remove(child)
        # Refresh fleet file cache when entering fleet directory
        if self._is_fleet_dir() and self.fleet_daemon_url:
            threading.Thread(target=self._fetch_fleet_files_bg, daemon=True).start()
        self._screen._ws.klippy.get_dir_info(self.load_files, self.cur_directory)

    # ------------------------------------------------------------------
    # Fleet GCode Integration
    # ------------------------------------------------------------------

    def _is_fleet_dir(self):
        """Check if currently browsing a fleet_gcodes directory."""
        return (self.cur_directory == 'gcodes/fleet_gcodes'
                or self.cur_directory.startswith('gcodes/fleet_gcodes/'))

    def _fleet_subpath(self):
        """Get the relative subpath within fleet_gcodes."""
        if self.cur_directory == 'gcodes/fleet_gcodes':
            return ""
        return self.cur_directory.replace('gcodes/fleet_gcodes/', '', 1)

    def _fetch_fleet_files_bg(self):
        """Background thread: fetch fleet file list from fleet_daemon."""
        try:
            resp = requests.get(
                f"{self.fleet_daemon_url}/gcodes/fleet-files",
                timeout=10
            )
            if resp.status_code == 200:
                self.fleet_files = resp.json().get("files", [])
                logging.info(f"[Fleet] Fetched {len(self.fleet_files)} fleet files")
            else:
                logging.warning(f"[Fleet] Failed to fetch files: HTTP {resp.status_code}")
        except requests.exceptions.ConnectionError:
            logging.warning("[Fleet] Cannot connect to fleet_daemon")
            GLib.idle_add(
                self._screen.show_popup_message,
                "Cannot connect to fleet server", 2
            )
        except Exception as e:
            logging.error(f"[Fleet] Error fetching files: {e}")

    def _inject_fleet_remote_files(self, local_filenames):
        """Add fleet remote files (not yet downloaded) to the file list."""
        subpath = self._fleet_subpath()
        for f in self.fleet_files:
            fname = f.get("filename", "")
            # Filter to current subdirectory
            if subpath:
                if not fname.startswith(subpath + '/'):
                    continue
                remainder = fname[len(subpath) + 1:]
            else:
                remainder = fname
            # Only direct children (no nested subdirs)
            if '/' in remainder:
                continue
            # Skip if already available locally
            if remainder in local_filenames:
                continue
            # Create a fleet remote item
            fbchild = self._create_fleet_item(remainder, fname, f.get("size", 0))
            if fbchild:
                self.flowbox.add(fbchild)

    def _create_fleet_item(self, display_name, fleet_filename, size):
        """Create a FlowBox child for a fleet remote file with cloud icon."""
        basename = os.path.splitext(display_name)[0]
        fbchild = PrintListItem()
        fbchild.set_name(basename.casefold())
        fbchild.set_path(f"__fleet__:{fleet_filename}")
        fbchild.set_size(size)
        fbchild.set_date(0)

        if self.list_mode:
            itemname = Gtk.Label(hexpand=True, halign=Gtk.Align.START,
                                ellipsize=Pango.EllipsizeMode.END)
            itemname.get_style_context().add_class("print-filename")
            itemname.set_markup(f"<big><b>☁ {basename}</b></big>")
            info = Gtk.Label(hexpand=True, halign=Gtk.Align.START)
            info.get_style_context().add_class("print-info")
            size_str = self._human_size(size)
            info.set_markup(f"<small>Fleet · {size_str}</small>")

            icon = Gtk.Button()
            icon.connect("clicked", self._show_fleet_download_dialog, fleet_filename, display_name, size)
            image_args = (None, icon, self.thumbsize, False, "network")

            action = self._gtk.Button("network", style="color3")
            action.connect("clicked", self._show_fleet_download_dialog, fleet_filename, display_name, size)
            action.set_hexpand(False)
            action.set_vexpand(False)
            action.set_halign(Gtk.Align.END)

            row = Gtk.Grid(hexpand=True, vexpand=False, valign=Gtk.Align.CENTER)
            row.get_style_context().add_class("frame-item")
            row.attach(icon, 0, 0, 1, 2)
            row.attach(itemname, 1, 0, 3, 1)
            row.attach(info, 1, 1, 1, 1)
            row.attach(action, 4, 0, 1, 2)
            fbchild.add(row)
        else:
            icon = self._gtk.Button(label=f"☁ {basename}")
            icon.connect("clicked", self._show_fleet_download_dialog, fleet_filename, display_name, size)
            image_args = (None, icon, self.thumbsize, False, "network")
            fbchild.add(icon)

        self.image_load(*image_args)
        return fbchild

    @staticmethod
    def _human_size(size):
        for unit in ('B', 'KB', 'MB', 'GB'):
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    def _show_fleet_download_dialog(self, widget, fleet_filename, display_name, size):
        """Show dialog with Cancel / Download / Download & Print."""
        if self._fleet_downloading:
            self._screen.show_popup_message(
                f"Download already in progress:\n{self._fleet_download_filename}", 2
            )
            return

        size_str = self._human_size(size)
        buttons = [
            {"name": _("Cancel"), "response": Gtk.ResponseType.CANCEL, "style": "dialog-error"},
            {"name": "Download", "response": Gtk.ResponseType.APPLY, "style": "dialog-info"},
            {"name": "Download & Print", "response": Gtk.ResponseType.OK},
        ]
        label = Gtk.Label(hexpand=True, vexpand=True, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        label.set_markup(
            f'<b>{display_name}</b>\n\n'
            f'<span size="small">Size: {size_str}\n'
            f'Source: Fleet Server</span>\n\n'
            f'This file is on the fleet server.\n'
            f'Download it to start printing.'
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add(label)
        self._gtk.Dialog(
            f"☁ Fleet File", buttons, box,
            self._fleet_download_dialog_response, fleet_filename
        )

    def _fleet_download_dialog_response(self, dialog, response_id, fleet_filename):
        self._gtk.remove_dialog(dialog)
        if response_id == Gtk.ResponseType.OK:
            self._fleet_start_download(fleet_filename, start_print=True)
        elif response_id == Gtk.ResponseType.APPLY:
            self._fleet_start_download(fleet_filename, start_print=False)

    def _fleet_start_download(self, fleet_filename, start_print=False):
        """Kick off a fleet download in a background thread."""
        self._fleet_downloading = True
        self._fleet_download_filename = fleet_filename
        action = "Download & Print" if start_print else "Download"
        logging.info(f"[Fleet] {action}: {fleet_filename}")
        self._screen.show_popup_message(
            f"☁ Downloading from fleet...\n{fleet_filename.split('/')[-1]}", 1
        )
        threading.Thread(
            target=self._fleet_download_worker,
            args=(fleet_filename, start_print),
            daemon=True
        ).start()

    def _fleet_download_worker(self, fleet_filename, start_print):
        """Background thread: request download from fleet_daemon, poll for completion."""
        url = f"{self.fleet_daemon_url}/gcodes/download"
        body = {
            "filename": fleet_filename,
            "printer_hostname": self._fleet_hostname,
        }
        try:
            resp = requests.post(url, json=body, timeout=15)
            if resp.status_code in (200, 201):
                logging.info(f"[Fleet] Download queued: {fleet_filename}")
                GLib.idle_add(
                    self._screen.show_popup_message,
                    f"☁ Download queued\n{fleet_filename.split('/')[-1]}", 1
                )
                # Poll for completion
                self._fleet_poll_download(fleet_filename, start_print)
            elif resp.status_code == 409:
                logging.info(f"[Fleet] Download already queued: {fleet_filename}")
                GLib.idle_add(
                    self._screen.show_popup_message,
                    "Download already queued for this file", 2
                )
            else:
                error = resp.text[:200]
                logging.error(f"[Fleet] Download request failed: {error}")
                GLib.idle_add(
                    self._screen.show_popup_message,
                    f"Fleet download failed:\n{error}", 3
                )
        except requests.exceptions.ConnectionError:
            logging.error("[Fleet] Cannot connect to fleet_daemon for download")
            GLib.idle_add(
                self._screen.show_popup_message,
                "Cannot connect to fleet server", 3
            )
        except Exception as e:
            logging.error(f"[Fleet] Download error: {e}")
            GLib.idle_add(
                self._screen.show_popup_message,
                f"Fleet download error:\n{str(e)[:100]}", 3
            )
        finally:
            self._fleet_downloading = False
            self._fleet_download_filename = ""

    def _fleet_poll_download(self, fleet_filename, start_print, timeout=600):
        """Poll fleet_daemon download queue until file is delivered."""
        import time
        elapsed = 0
        poll_interval = 3
        while elapsed < timeout:
            time.sleep(poll_interval)
            elapsed += poll_interval
            try:
                resp = requests.get(
                    f"{self.fleet_daemon_url}/gcodes/download/queue?include_history=true",
                    timeout=10
                )
                if resp.status_code != 200:
                    continue
                queue = resp.json().get("queue", [])
                # Find our download
                job = None
                for entry in queue:
                    if (entry.get("filename") == fleet_filename
                            and entry.get("printer_hostname") == self._fleet_hostname):
                        job = entry
                        break
                if job is None:
                    continue
                status = job.get("status")
                if status == "completed":
                    logging.info(f"[Fleet] Download complete: {fleet_filename}")
                    if start_print:
                        print_path = f"fleet_gcodes/{fleet_filename}"
                        logging.info(f"[Fleet] Starting print: {print_path}")
                        GLib.idle_add(self._fleet_start_print_after_download, print_path)
                        GLib.idle_add(
                            self._screen.show_popup_message,
                            f"☁ Print starting\n{fleet_filename.split('/')[-1]}", 1
                        )
                    else:
                        GLib.idle_add(
                            self._screen.show_popup_message,
                            f"☁ Downloaded\n{fleet_filename.split('/')[-1]}", 1
                        )
                        GLib.idle_add(self._refresh_files)
                    return
                elif status == "failed":
                    error = job.get("error_message", "Unknown error")
                    logging.error(f"[Fleet] Download failed: {error}")
                    GLib.idle_add(
                        self._screen.show_popup_message,
                        f"Fleet download failed:\n{error[:100]}", 3
                    )
                    return
                elif status == "cancelled":
                    logging.info(f"[Fleet] Download cancelled: {fleet_filename}")
                    GLib.idle_add(
                        self._screen.show_popup_message,
                        "Download cancelled", 2
                    )
                    return
                # pending or downloading — keep waiting
            except Exception as e:
                logging.warning(f"[Fleet] Poll error: {e}")

        # Timeout
        logging.error(f"[Fleet] Download timed out after {timeout}s: {fleet_filename}")
        GLib.idle_add(
            self._screen.show_popup_message,
            f"Fleet download timed out\n({timeout}s)", 3
        )

    def _fleet_start_print_after_download(self, print_path):
        """Start print via Moonraker (called on main thread via GLib.idle_add)."""
        logging.info(f"[Fleet] Sending print_start: {print_path}")
        self._screen._ws.klippy.print_start(print_path)

    def set_loading(self, loading):
        self.loading = loading
        for child in self.headerbox.get_children():
            child.set_sensitive(not loading)
        self._gtk.Button_busy(self.refresh, loading)
        if loading:
            self.labels['path'].set_text(self.loading_msg)
            self.labels['path'].show()
            return
        self.show_path()
        self.content.show_all()

    def show_rename(self, widget, fullpath):
        self.source = fullpath
        logging.info(self.source)

        for child in self.content.get_children():
            self.content.remove(child)

        if "rename_file" not in self.labels:
            self._create_rename_box(fullpath)
        self.content.add(self.labels['rename_file'])
        self.labels['new_name'].set_text(fullpath[7:])
        self.labels['new_name'].grab_focus_without_selecting()
        self.showing_rename = True

    def _create_rename_box(self, fullpath):
        lbl = Gtk.Label(label=_("Rename/Move:"), halign=Gtk.Align.START, hexpand=False)
        self.labels['new_name'] = Gtk.Entry(text=fullpath, hexpand=True)
        self.labels['new_name'].connect("activate", self.rename)
        self.labels['new_name'].connect("focus-in-event", self._screen.show_keyboard)

        save = self._gtk.Button("complete", _("Save"), "color3")
        save.set_hexpand(False)
        save.connect("clicked", self.rename)

        box = Gtk.Box()
        box.pack_start(self.labels['new_name'], True, True, 5)
        box.pack_start(save, False, False, 5)

        self.labels['rename_file'] = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5,
                                             hexpand=True, vexpand=True, valign=Gtk.Align.CENTER)
        self.labels['rename_file'].pack_start(lbl, True, True, 5)
        self.labels['rename_file'].pack_start(box, True, True, 5)

    def hide_rename(self):
        self._screen.remove_keyboard()
        for child in self.content.get_children():
            self.content.remove(child)
        self.content.add(self.main)
        self.content.show()
        self.showing_rename = False

    def rename(self, widget):
        params = {"source": self.source, "dest": f"gcodes/{self.labels['new_name'].get_text()}"}
        self._screen._send_action(
            widget,
            "server.files.move",
            params
        )
        self.back()

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

    def confirm_move_gcode(self, widget, filename):
        self.file_metadata = self._files.get_file_info(filename)

        buttons = [
            {"name": _("Copy to Printer"), "response": Gtk.ResponseType.OK},
            {"name": _("Cancel"), "response": Gtk.ResponseType.CANCEL, "style": 'dialog-error'}
        ]

        label = Gtk.Label(hexpand=True, vexpand=True, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        label.set_markup(f"<b>{filename}</b>\n")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add(label)

        height = (self._screen.height - self._gtk.dialog_buttons_height - self._gtk.font_size) * .75
        pixbuf = self.get_file_image(filename, self._screen.width * .9, height)
        if pixbuf is not None:
            image = Gtk.Image.new_from_pixbuf(pixbuf)
            box.add(image)

        dialog = self._gtk.Dialog(_("Copy to Printer") + f' {filename}', buttons, box, self.confirm_move_gcode_response, self.cur_directory, filename, widget)
        dialog.get_style_context().add_class('confirm_move_gcode')


    def confirm_move_gcode_response(self, dialog, response_id, cur_directory, filename, widget):
        self._gtk.remove_dialog(dialog)
        if response_id == Gtk.ResponseType.OK:
            self.copy_to_gcodes(cur_directory, filename, widget)

    def copy_to_gcodes(self, cur_directory, filename, widget):
        basename = os.path.basename(filename)
        source = os.path.join(cur_directory, basename)
        destination = os.path.join('gcodes', basename)
        self._screen.show_popup_message(f'Copying {basename} to {source}', level=1)

        params = {"source": source, "dest": destination}
        self._screen._send_action(
            widget,
            "server.files.copy",
            params
        )