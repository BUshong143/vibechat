from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "apps.accounts"

    def ready(self):
        # Skip the UPDATE users SET last_login=... on every login: one less round trip to the remote database.
        from django.contrib.auth import user_logged_in
        user_logged_in.disconnect(dispatch_uid="update_last_login")
