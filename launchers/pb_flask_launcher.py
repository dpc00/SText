import sublime
import sublime_plugin

from User.launchers._pb_port import kill_existing, PORT


class PbFlaskLauncherCommand(sublime_plugin.WindowCommand):
    """Open a Terminus tab that starts the PyBackup Flask app and launches its browser UI.

    Kills any prior process still holding port %d first, so a stale instance
    with cached engine state can't serve the freshly launched UI.

    Menu: Main.sublime-menu → Tools — "PyBackup Flask App"
    Command palette: "PyBackup: Flask App"
    """ % PORT

    def run(self):
        kill_existing(PORT)
        self.window.run_command(
            "terminus_open",
            {
                "title": "Pybackup Flask App",
                "tag": "pb_flask",
                "post_view_hooks": [
                    [
                        "terminus_paste_text",
                        {
                            "text": "start http://127.0.0.1:%d\n" % PORT,
                            "bracketed": False,
                        },
                    ],
                    [
                        "terminus_paste_text",
                        {
                            "text": "start /B python C:/Users/donal/projects/pybackup/ui/app.py\n",
                            "bracketed": False,
                        },
                    ],
                ],
            },
        )