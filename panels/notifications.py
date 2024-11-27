import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib
from ks_includes.screen_panel import ScreenPanel


COLORS = {
    "time": "DarkGrey",
    "info": "Silver",
    "warning": "DarkOrange",
    "error": "FireBrick",
}

NOTIFICATION_DISPLAY_DURATION_MS = 15000  # 5000ms = 5 seconds


def remove_newlines(msg: str) -> str:
    return msg.replace('\n', ' ')


class Panel(ScreenPanel):
    def __init__(self, screen, title):
        super().__init__(screen, title)
        self.empty = _("Notification log empty")
        self.tb = Gtk.TextBuffer(text=self.empty)
        tv = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        tv.set_buffer(self.tb)
        tv.connect("size-allocate", self._autoscroll)

        scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroll.add(tv)
        self.content.add(scroll)

    def activate(self):
        self.clear()
        for log in self._screen.notification_log:
            self.add_notification(log)

    def add_notification(self, log):
        if log["level"] == 0:
            if "error" in log["message"].lower() or "cannot" in log["message"].lower():
                color = COLORS["error"]
            else:
                color = COLORS["info"]
        elif log["level"] == 1:
            color = COLORS["info"]
        elif log["level"] == 2:
            color = COLORS["warning"]
        else:
            color = COLORS["error"]
        self.tb.insert_markup(
            self.tb.get_end_iter(),
            f'\n<span color="{COLORS["time"]}">{log["time"]}</span> '
            f'<span color="{color}"><b>{remove_newlines(log["message"])}</b></span>',
            -1
        )

        # Reset the clear timeout if a new notification is added
        if self.clear_timeout_id:
            GLib.source_remove(self.clear_timeout_id)
        self.clear_timeout_id = GLib.timeout_add(NOTIFICATION_DISPLAY_DURATION_MS, self.clear)


    def clear(self):
        self.tb.set_text("")

    def process_update(self, action, data):
        if action != "notify_log":
            return
        self.add_notification(data)
