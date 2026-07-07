"""ssh_panel_auto_connect.py — automatic SSH-Panel phone connection.

The user's phone can be on two WiFi networks (Xfinity 10.0.0.x or
CenturyLink/Phil's5G 192.168.0.x).  SSH-Panel has one settings entry per
network.  This command probes each 'Phone-*' entry and opens SSH-Panel
for the first one that answers.
"""

import os
import sys
import threading

import sublime
import sublime_plugin


class SshPanelAutoConnectCommand(sublime_plugin.WindowCommand):
    """Probe each configured Phone-* SSH-Panel server in the background and connect to the first that answers.

    Command palette: "SSH-Panel: Auto Connect Phone"
    """

    GROUP_PREFIX = "Phone-"
    PROBE_TIMEOUT = 3  # seconds per candidate

    def run(self):
        settings = sublime.load_settings("ssh-panel.sublime-settings")
        server_settings = settings.get("server_settings", {})
        candidates = [
            (name, cfg)
            for name, cfg in server_settings.items()
            if name.startswith(self.GROUP_PREFIX)
        ]
        if not candidates:
            sublime.status_message(
                "SSH-Panel: no %s* servers configured" % self.GROUP_PREFIX
            )
            return

        # If a Phone connection is already alive, just bring it forward.
        sp_main = sys.modules.get("SSH-Panel.main")
        if sp_main is not None:
            for client in sp_main.client_map.values():
                if not (client and client.transport and client.user_settings):
                    continue
                name = getattr(client.user_settings, "server_name", None)
                if not name or not name.startswith(self.GROUP_PREFIX):
                    continue
                ref = client.command_ref()
                cmd = ref() if ref else None
                if cmd and cmd.window:
                    sublime.set_timeout(cmd.window.bring_to_front, 0)
                    sublime.status_message(
                        "SSH-Panel: already connected to %s" % name
                    )
                    return

        sublime.status_message("SSH-Panel: probing Phone servers...")
        threading.Thread(target=self._probe, args=(candidates,), daemon=True).start()

    def _probe(self, candidates):
        try:
            import paramiko
        except Exception as e:
            sublime.set_timeout(
                lambda: sublime.message_dialog(
                    "SSH-Panel auto-connect: cannot import paramiko (%s)" % e
                ),
                0,
            )
            return

        for name, cfg in candidates:
            hostname = cfg.get("hostname")
            port = cfg.get("port", 22)
            username = cfg.get("username")
            private_key = cfg.get("private_key")
            if not (
                hostname
                and username
                and isinstance(private_key, list)
                and len(private_key) == 2
            ):
                print(
                    "SSH-Panel auto: skipping %s (missing hostname/username/key)"
                    % name
                )
                continue
            key_path = os.path.expanduser(os.path.expandvars(private_key[1]))
            if not os.path.exists(key_path):
                print("SSH-Panel auto: %s key not found: %s" % (name, key_path))
                continue

            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect(
                    hostname,
                    port=port,
                    username=username,
                    key_filename=[key_path],
                    timeout=self.PROBE_TIMEOUT,
                    banner_timeout=self.PROBE_TIMEOUT,
                    auth_timeout=self.PROBE_TIMEOUT,
                )
                client.close()
                print("SSH-Panel auto: %s is reachable" % name)
                sublime.set_timeout(lambda n=name: self._connect(n), 0)
                return
            except Exception as e:
                print("SSH-Panel auto: %s probe failed: %s" % (name, e))
                continue

        sublime.set_timeout(
            lambda: sublime.message_dialog(
                "SSH-Panel: no Phone server is reachable.\n"
                "Check that Termux sshd is running on the phone."
            ),
            0,
        )

    def _connect(self, server_name):
        sublime.status_message("SSH-Panel: connecting to %s" % server_name)
        self.window.run_command(
            "ssh_panel_connect",
            {
                "server_name": server_name,
                "connect_now": True,
                "reload_from_view": False,
            },
        )
