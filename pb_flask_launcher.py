import sublime
import sublime_plugin


class PbFlaskLauncherCommand(sublime_plugin.WindowCommand):
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
                            "text": "start chrome http://127.0.0.1:5757\n",
                            "bracketed": False,
                        },
                    ],
                    [
                        "terminus_paste_text",
                        {
                            "text": "python C:/Users/donal/projects/pybackup/ui/app.py\n",
                            "bracketed": False,
                        },
                    ],
                ],
            },
        )
