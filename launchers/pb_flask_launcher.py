import sublime
import sublime_plugin


class PbFlaskLauncherCommand(sublime_plugin.WindowCommand):
    """Open a Terminus tab that starts the PyBackup Flask app and launches its browser UI.

    Menu: Main.sublime-menu → Tools — "PyBackup Flask App"
    Command palette: "PyBackup: Flask App"
    """

    def run(self):
        self.window.run_command(
            "terminus_open",
            {
                "title": "Pybackup Flask App",
                "tag": "pb_flask",
                "post_view_hooks": [
                    [
                        "terminus_paste_text",
                        {
                            "text": "start http://127.0.0.1:5757\n",
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
